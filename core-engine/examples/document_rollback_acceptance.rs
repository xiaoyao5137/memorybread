//! Cross-runtime rollback acceptance against a new isolated SQLite database.
use memory_bread_core::{storage::StorageManager, services::document_refresh::DOCUMENT_REFRESH_CONFIG_KEY};
use anyhow::{Context,ensure};
use serde_json::json;
use sha2::{Digest,Sha256};

fn main() -> anyhow::Result<()> {
    let path=std::env::args().nth(1).context("provide a new isolated database path")?;
    ensure!(!std::path::Path::new(&path).exists(),"refusing to overwrite fixture");
    let db=StorageManager::open(std::path::Path::new(&path))?;
    let old="缓存一致性方案：写入前检查来源版本，读取时拒绝过期索引。恢复失败必须撤销正文和索引变更，保留原来的引用。";
    let new="缓存更新方案：本次修订增加重试次数限制，明确错误状态，并保证读取引用与正文版本一致。";
    let url="https://example.com/isolated-rollback";
    db.with_conn(|c| {
        c.execute("INSERT INTO bake_documents(id,title,doc_type,full_content,source_url,status,created_at,updated_at)
            VALUES(1,'缓存方案','document',?1,?2,'enabled',1,100)",rusqlite::params![new,url])?;
        for (id,body) in [(7,old),(8,new)] {
            c.execute("INSERT INTO bake_document_source_snapshots(id,document_id,source_url,page_title,content_text,content_hash,completeness_status,identity_match,collected_at)
                VALUES(?1,1,?2,'缓存方案',?3,?4,'complete',1,1)",rusqlite::params![id,url,body,format!("{:x}",Sha256::digest(body.as_bytes()))])?;
        }
        c.execute("INSERT INTO bake_document_source_heads VALUES(1,8,100)",[])?;
        for (id,body,summary,binding) in [(1,old,Some("旧摘要：写入前检查版本。"),Some(7)),(2,"知识库 首页 目录 收藏 分享 编辑 全部暂停",None,None)] {
            let record=json!({"title":"缓存方案","full_content":body,"summary":summary,
                "summary_source_snapshot_id":binding,"summary_generation_version":"document-summary.v1"});
            c.execute("INSERT INTO bake_document_body_versions(id,document_id,replaced_by_snapshot_id,record_json,saved_at) VALUES(?1,1,?2,?3,1)",rusqlite::params![id,if id==1 {8} else {7},record.to_string()])?;
        }
        Ok(())
    })?;
    let now=chrono::Utc::now().timestamp_millis();
    let lease=db.claim_document_summary_job(now)?.context("expected active pre-rollback summary lease")?;
    db.upsert_preference(DOCUMENT_REFRESH_CONFIG_KEY,r#"{"source_writes_enabled":false,"automatic_document_writes_enabled":false}"#,"user",1.0)?;
    db.upsert_preference("runtime.capture_enabled","false","user",1.0)?;
    ensure!(db.claim_document_summary_job(now+1)?.is_none(),"paused scheduler claimed work");
    let root=std::path::Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap();
    let restore=|version:&str,snapshot:Option<&str>| -> anyhow::Result<()> {
        let mut command=std::process::Command::new(root.join("ai-sidecar/.venv/bin/python"));
        command.arg(root.join("ai-sidecar/scripts/restore_document_body_version.py"))
            .args(["--db",&path,"--version-id",version,"--apply"]);
        if let Some(id)=snapshot {command.args(["--source-snapshot-id",id]);}
        let output=command.output()?;
        ensure!(output.status.success(),"restore failed: {}",String::from_utf8_lossy(&output.stderr));
        Ok(())
    };
    restore("1",Some("7"))?;
    ensure!(db.publish_document_summary_job(&lease,"late newer summary").is_err(),"late summary was published");
    db.with_conn(|c| {
        let row:(String,Option<i64>,i64)=c.query_row("SELECT full_content,summary_source_snapshot_id,(SELECT snapshot_id FROM bake_document_source_heads WHERE document_id=1) FROM bake_documents WHERE id=1",[],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?)))?;
        assert_eq!(row,(old.to_string(),Some(7),7));Ok(())
    })?;
    restore("2",None)?;
    ensure!(db.claim_document_summary_job(now+2)?.is_none(),"shell rollback bypassed pause");
    db.with_conn(|c| {
        let head:i64=c.query_row("SELECT COUNT(*) FROM bake_document_source_heads WHERE document_id=1",[],|r|r.get(0))?;
        assert_eq!(head,0);
        let binding:Option<i64>=c.query_row("SELECT summary_source_snapshot_id FROM bake_documents WHERE id=1",[],|r|r.get(0))?;
        assert_eq!(binding,None);Ok(())
    })?;
    println!("{}",json!({"db":path,"paused_claim_rejected":true,"verified_head_restored":7,"late_summary_rejected":true,"shell_head_cleared":true}));
    Ok(())
}

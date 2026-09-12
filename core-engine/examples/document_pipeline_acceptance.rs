//! Runs the real bake pipeline against isolated synthetic data and the local Model API.
use memory_bread_core::{services::{bake_service::BakeService,document_refresh::DOCUMENT_REFRESH_CONFIG_KEY},storage::{StorageManager,NewBakeDocument}};
use serde_json::json;
use anyhow::Context;

#[tokio::main]
async fn main()->anyhow::Result<()> {
    let output=std::env::args().nth(1).context("provide a new isolated database path")?;
    let path=std::path::Path::new(&output);
    anyhow::ensure!(!path.exists(),"refusing to overwrite existing fixture");
    let db=StorageManager::open(&path)?;
    let body="缓存一致性方案：读取时校验来源版本，写入前检查文档修订，避免旧结果覆盖新编辑。失败后保留任务并有界重试，恢复后继续处理。当前收益为目标，尚未完成上线验收。".repeat(8);
    db.with_conn(|c| {
        c.execute("INSERT INTO captures(id,ts,app_name,win_title,event_type,ax_text,url,webpage_title) VALUES(1,100,'Google Chrome','缓存一致性方案','manual',?1,'https://example.com/doc/pipeline-fixture','缓存一致性方案')",[&body])?;
        c.execute("INSERT INTO timelines(id,capture_id,summary,overview,category,importance,occurrence_count,observed_at,history_view,content_origin,activity_type,evidence_strength,created_at_ms,updated_at_ms) VALUES(1,1,'缓存一致性方案','来源版本校验与恢复','document',5,1,100,1,'historical_content','reading','high',100,100)",[])?;
        c.execute("UPDATE timelines SET entities='[]',details='{}' WHERE id=1",[])?;
        Ok(())
    })?;
    let mut doc=NewBakeDocument::with_defaults("缓存一致性方案".into(),"document".into());
    doc.source_url=Some("https://example.com/doc/pipeline-fixture".into());
    doc.source_memory_ids="[\"1\"]".into();
    doc.full_content=Some("保留原有正文，后续观察必须先检查再应用。".into());
    let id=db.insert_bake_document(&doc)?;
    let service=BakeService::new(db.clone(),"http://127.0.0.1:7071");
    db.upsert_preference(DOCUMENT_REFRESH_CONFIG_KEY,r#"{"automatic_document_writes_enabled":false}"#,"test",1.0)?;
    let paused=service.run_bake_pipeline("acceptance",10).await.context("paused pipeline")?;
    let before=db.get_bake_document(id)?.unwrap();
    anyhow::ensure!(before.source_capture_ids=="[]" && before.full_content==doc.full_content,"paused write changed document");
    db.with_conn(|c| {
        let code:String=c.query_row("SELECT last_error_code FROM bake_retry_state WHERE timeline_id=1",[],|r|r.get(0))?;
        assert_eq!(code,"DOCUMENT_AUTOMATIC_WRITES_PAUSED");Ok(())
    })?;
    db.upsert_preference(DOCUMENT_REFRESH_CONFIG_KEY,r#"{"automatic_document_rollout_percent":0}"#,"test",1.0)?;
    let excluded=service.run_bake_pipeline("acceptance",10).await.context("excluded pipeline")?;
    anyhow::ensure!(db.get_bake_document(id)?.unwrap().source_capture_ids=="[]","gray exclusion wrote metadata");
    db.upsert_preference(DOCUMENT_REFRESH_CONFIG_KEY,"{}","test",1.0)?;
    let resumed=service.run_bake_pipeline("acceptance",10).await.context("resumed pipeline")?;
    let after=db.get_bake_document(id)?.unwrap();
    anyhow::ensure!(after.source_capture_ids.contains('1') && after.full_content==doc.full_content,"resume lost source or overwrote body");
    let observations=db.with_conn(|c|Ok(c.query_row("SELECT count(*) FROM bake_document_refresh_observations WHERE document_id=?1",[id],|r|r.get::<_,i64>(0))?))?;
    anyhow::ensure!(observations>0,"resume did not enqueue source inspection");
    db.with_conn(|c| {
        let status:String=c.query_row("SELECT persist_status FROM bake_candidate_audits WHERE run_id=?1",[resumed.id.parse::<i64>().unwrap()],|r|r.get(0))?;
        assert_eq!(status,"pending_source_refresh");
        let retries:i64=c.query_row("SELECT count(*) FROM bake_retry_state",[],|r|r.get(0))?;
        assert_eq!(retries,0);Ok(())
    })?;
    println!("{}",json!({"paused":paused,"excluded":excluded,"resumed":resumed,"observations":observations,"body_preserved":true,"db":output}));
    Ok(())
}

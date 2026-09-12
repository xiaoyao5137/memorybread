//! Isolated live-UI fixture: real handlers and summary worker, no user database.
//! Run with `cargo run --example document_summary_acceptance`. Expires in 20 min.
use memory_bread_core::{api::{server::create_router,state::AppState,handlers::bake::run_document_summary_worker},storage::StorageManager};

#[tokio::main]
async fn main()->anyhow::Result<()> {
    let fixture=tempfile::tempdir()?;
    let db_path=fixture.path().join("summary-acceptance.db");
    let db=StorageManager::open(&db_path)?;
    db.with_conn(|c| {
        for (id,title,summary) in [(1,"摘要验收：旧摘要重建",Some("旧摘要：系统已经完成上线验收。")),(2,"摘要验收：失败后重试",None)] {
            let body="这是一份隔离的摘要验收资料。缓存读取必须检查来源版本，旧版本索引应失效并重新构建。写入时校验文档版本，避免过期结果覆盖用户的新编辑。方案尚未完成上线验收，收益只是目标，不能声称已实现。";
            c.execute("INSERT INTO bake_documents(id,title,doc_type,summary,full_content,status,created_at,updated_at)
                VALUES(?1,?2,'document',?3,?4,'enabled',1,1)",rusqlite::params![id,title,summary,body])?;
            c.execute("INSERT INTO bake_document_source_snapshots(id,document_id,source_url,page_title,content_text,content_hash,completeness_status,identity_match,collected_at)
                VALUES(?1,?1,'https://example.com/isolated-summary-fixture',?2,?3,?4,'complete',1,1)",
                rusqlite::params![id,title,body,format!("isolated-fixture-{id}")])?;
            c.execute("INSERT INTO bake_document_source_heads(document_id,snapshot_id,applied_at) VALUES(?1,?1,1)",[id])?;
        }
        c.execute("INSERT INTO document_summary_jobs(document_id,source_snapshot_id,expected_updated_at,attempts,state,last_error)
            VALUES(2,2,1,3,'blocked','SUMMARY_GENERATION_FAILED')",[])?;
        Ok(())
    })?;
    db.upsert_preference("runtime.capture_enabled","false","user",1.0)?;
    db.upsert_preference("runtime.document_source_refresh",r#"{"poll_seconds":1}"#,"user",1.0)?;
    let state=AppState::with_service_urls(db,"http://127.0.0.1:7071".into(),String::new(),vec![]);
    let worker=tokio::spawn(run_document_summary_worker(state.clone()));
    let listener=tokio::net::TcpListener::bind("127.0.0.1:0").await?;
    println!("{}",serde_json::json!({"fixture_url":format!("http://{}",listener.local_addr()?),"db_path":db_path,"pid":std::process::id(),"expires_seconds":1200}));
    let result=axum::serve(listener,create_router(state)).with_graceful_shutdown(async {
        tokio::time::sleep(std::time::Duration::from_secs(1200)).await;
    }).await;
    worker.abort();
    result?;
    Ok(())
}

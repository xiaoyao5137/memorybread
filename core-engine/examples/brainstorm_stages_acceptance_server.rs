//! Isolated real-router host for the staged brainstorm acceptance script.
//! Uses a temporary database; never opens the user's desktop database.
use memory_bread_core::{api::{create_router, state::AppState}, storage::StorageManager};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<String> = std::env::args().collect();
    let port: u16 = args.get(1).map(String::as_str).unwrap_or("17070").parse()?;
    let creation_url = args.get(2).cloned().unwrap_or_else(|| "http://127.0.0.1:18001".into());
    if !creation_url.starts_with("http://127.0.0.1:") {
        return Err("Acceptance server requires a loopback creation service".into());
    }
    let temp = tempfile::tempdir()?;
    let database = args.get(3).map(std::path::PathBuf::from)
        .unwrap_or_else(|| temp.path().join("brainstorm-acceptance.db"));
    let storage = StorageManager::open(&database)?;
    let state = AppState::with_service_urls(
        storage,
        "http://127.0.0.1:7071".into(),
        creation_url,
        vec![],
    );
    let listener = tokio::net::TcpListener::bind(("127.0.0.1", port)).await?;
    println!("Isolated brainstorm acceptance API: http://127.0.0.1:{port}");
    axum::serve(listener, create_router(state))
        .with_graceful_shutdown(async { let _ = tokio::signal::ctrl_c().await; })
        .await?;
    drop(temp);
    Ok(())
}

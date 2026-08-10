//! axum Router 组装与服务启动

use std::sync::Arc;

use axum::{
    extract::DefaultBodyLimit,
    routing::{delete, get, post, put},
    Router,
};
use tower_http::cors::{Any, CorsLayer};
use tracing::info;

use super::{
    handlers::{
        action::execute_action,
        bake::{
            create_bake_document, delete_bake_capture, delete_bake_document, delete_bake_knowledge,
            delete_bake_memory, delete_bake_sop, get_bake_capture, get_bake_capture_screenshot,
            get_bake_document, get_bake_knowledge, get_bake_memory_preview, get_bake_overview,
            get_bake_queue_status, get_bake_sop, get_bake_style_config, ignore_bake_memory,
            initialize_bake_memories, list_bake_captures, list_bake_documents, list_bake_knowledge,
            list_bake_memories, list_bake_sops, promote_bake_memory_to_document,
            promote_bake_memory_to_sop, run_bake_pipeline, toggle_bake_document_status,
            update_bake_document, update_bake_style_config,
        },
        capture_health::monitor_capture_health,
        captures::list_captures,
        config_checks::{
            delete_config_check, install_config_check, list_config_checks, run_config_check,
        },
        creation::{
            generate_document, list_history, preview_references, run_creation_agent, save_history,
        },
        creation_skill::{
            analyze_creation_skill, create_creation_skill_analysis_job, delete_creation_skill,
            get_creation_skill, get_creation_skill_analysis_job, list_creation_skills,
            save_creation_skill, update_creation_skill,
        },
        data::{
            delete_data_source, extract_data_sources, get_browser_preview_image,
            get_creation_evidence_image, get_data_source, list_data_sources, refresh_data_source,
            register_discovered_source, search_data, validate_creation_evidence,
        },
        debug::{
            clear_extraction_queue, debug_log_content, debug_log_files, system_stats, vector_status,
        },
        diary::{get_diary, get_latest_diary, list_diaries, update_diary},
        health::health_handler,
        integration_skill::{
            download_integration_skill_bundle, download_integration_skill_file,
            get_integration_skill, get_integration_skill_run, list_integration_memory_options,
            list_integration_skill_runs, list_integration_skills, start_integration_skill_run,
        },
        knowledge::{
            delete_knowledge, extract_knowledge, get_knowledge, list_knowledge, verify_knowledge,
        },
        monitor::{
            monitor_extraction_live, monitor_overview, monitor_pipeline_dag, monitor_system,
        },
        notification_channels::{
            create_notification_channel, delete_notification_channel, list_notification_channels,
            update_notification_channel,
        },
        pii::pii_scrub,
        preferences::{
            list_preferences, run_capture_cleanup_now, run_screenshot_cleanup_now,
            update_preference,
        },
        privacy::{
            add_blacklist, delete_blacklist, list_blacklist, list_filters,
            update_blacklist_enabled, update_filter_config, update_filter_enabled,
        },
        profile::{get_latest_profile, get_profile, list_profiles, update_profile},
        query::{
            create_rag_job, get_rag_job, rag_history, rag_query, rag_references, rag_stream,
            save_rag_history,
        },
        runtime::{get_runtime_status, update_runtime_status},
        snapshot::{
            backup_asset_snapshot_to_cloud, export_asset_snapshot, import_asset_snapshot,
            restore_asset_snapshot_from_cloud,
        },
        tasks::{
            create_task, delete_task, get_task, list_executions, list_tasks, trigger_task,
            update_task,
        },
        work_profile::get_work_profile,
    },
    state::AppState,
};

/// 构造 axum Router（不启动监听）。
///
/// 测试中直接使用此函数构造 router，无需真实 TCP 端口。
pub fn create_router(state: Arc<AppState>) -> Router {
    let cors = CorsLayer::new()
        .allow_origin(Any)
        .allow_methods(Any)
        .allow_headers(Any);

    Router::new()
        .route("/health", get(health_handler))
        .route(
            "/api/runtime/status",
            get(get_runtime_status).put(update_runtime_status),
        )
        .route("/api/snapshots/assets/export", post(export_asset_snapshot))
        .route("/api/snapshots/assets/import", post(import_asset_snapshot))
        .route(
            "/api/snapshots/cloud/backup",
            post(backup_asset_snapshot_to_cloud),
        )
        .route(
            "/api/snapshots/cloud/restore",
            post(restore_asset_snapshot_from_cloud),
        )
        .route("/api/captures", get(list_captures))
        .route("/captures", get(list_captures))
        .route("/api/work-profile", get(get_work_profile))
        .route("/query", post(rag_query))
        .route("/api/rag/jobs", post(create_rag_job))
        .route("/api/rag/jobs/:job_id", get(get_rag_job))
        .route("/api/rag/stream", post(rag_stream))
        .route("/api/rag/references", post(rag_references))
        .route("/api/rag/history", get(rag_history).post(save_rag_history))
        .route("/action/execute", post(execute_action))
        .route("/preferences", get(list_preferences))
        .route(
            "/preferences/screenshot-cleanup/run",
            post(run_screenshot_cleanup_now),
        )
        .route(
            "/preferences/capture-cleanup/run",
            post(run_capture_cleanup_now),
        )
        .route("/preferences/:key", put(update_preference))
        .route("/api/config-checks", get(list_config_checks))
        .route("/api/config-checks/:id/verify", post(run_config_check))
        .route("/api/config-checks/:id/install", post(install_config_check))
        .route("/api/config-checks/:id", delete(delete_config_check))
        .route("/pii/scrub", post(pii_scrub))
        .route("/api/creation/generate", post(generate_document))
        .route("/api/creation/agent/run", post(run_creation_agent))
        .route("/api/creation/references", post(preview_references))
        .route("/api/creation/history", post(save_history))
        .route("/api/creation/history", get(list_history))
        .route("/api/data/sources", get(list_data_sources))
        .route("/api/data/sources/extract", post(extract_data_sources))
        .route(
            "/api/data/sources/discovered",
            post(register_discovered_source),
        )
        .route(
            "/api/data/sources/:id",
            get(get_data_source).delete(delete_data_source),
        )
        .route("/api/data/sources/:id/refresh", post(refresh_data_source))
        .route("/api/tools/data-search", post(search_data))
        .route(
            "/api/creation/evidence/:id/image",
            get(get_creation_evidence_image),
        )
        .route(
            "/api/creation/browser-previews/:id/image",
            get(get_browser_preview_image),
        )
        .route(
            "/api/creation/evidence/:id/validate",
            post(validate_creation_evidence),
        )
        .route("/api/creation/skills/analyze", post(analyze_creation_skill))
        .route(
            "/api/creation/skills/analyze/jobs",
            post(create_creation_skill_analysis_job),
        )
        .route(
            "/api/creation/skills/analyze/jobs/:job_id",
            get(get_creation_skill_analysis_job),
        )
        .route(
            "/api/creation/skills",
            get(list_creation_skills)
                .post(save_creation_skill)
                .layer(DefaultBodyLimit::max(16 * 1024 * 1024)),
        )
        .route(
            "/api/creation/skills/:id",
            get(get_creation_skill)
                .put(update_creation_skill)
                .delete(delete_creation_skill)
                .layer(DefaultBodyLimit::max(16 * 1024 * 1024)),
        )
        .route("/api/integration-skills", get(list_integration_skills))
        .route(
            "/api/integration-skills/memory-options",
            get(list_integration_memory_options),
        )
        .route(
            "/api/integration-skills/runs",
            get(list_integration_skill_runs),
        )
        .route(
            "/api/integration-skills/runs/:run_id",
            get(get_integration_skill_run),
        )
        .route(
            "/api/integration-skills/:skill_id/file",
            get(download_integration_skill_file),
        )
        .route(
            "/api/integration-skills/:skill_id/bundle",
            get(download_integration_skill_bundle),
        )
        .route(
            "/api/integration-skills/:skill_id/runs",
            post(start_integration_skill_run).layer(DefaultBodyLimit::max(32 * 1024 * 1024)),
        )
        .route(
            "/api/integration-skills/:skill_id",
            get(get_integration_skill),
        )
        .route("/api/vector/status", get(vector_status))
        .route("/api/stats", get(system_stats))
        .route("/api/debug/log-files", get(debug_log_files))
        .route("/api/debug/log-files/:key", get(debug_log_content))
        .route(
            "/api/debug/clear-extraction-queue",
            post(clear_extraction_queue),
        )
        .route("/api/knowledge", get(list_knowledge))
        .route("/api/knowledge/extract", post(extract_knowledge))
        .route("/api/knowledge/:id/verify", post(verify_knowledge))
        .route(
            "/api/knowledge/:id",
            get(get_knowledge).delete(delete_knowledge),
        )
        // 定时任务
        .route("/api/tasks", get(list_tasks).post(create_task))
        .route(
            "/api/tasks/:id",
            get(get_task).put(update_task).delete(delete_task),
        )
        .route("/api/tasks/:id/executions", get(list_executions))
        .route("/api/tasks/:id/trigger", post(trigger_task))
        .route(
            "/api/notification-channels",
            get(list_notification_channels).post(create_notification_channel),
        )
        .route(
            "/api/notification-channels/:id",
            put(update_notification_channel).delete(delete_notification_channel),
        )
        // 监控
        .route("/api/monitor/overview", get(monitor_overview))
        .route("/api/monitor/capture-health", get(monitor_capture_health))
        .route("/api/monitor/extraction_live", get(monitor_extraction_live))
        .route("/api/monitor/pipeline_dag", get(monitor_pipeline_dag))
        .route("/api/monitor/system", get(monitor_system))
        // 日记
        .route("/api/diaries", get(list_diaries))
        .route("/api/diaries/latest", get(get_latest_diary))
        .route("/api/diaries/:id", get(get_diary).put(update_diary))
        // 旧用户画像 API：兼容旧客户端，返回同一批日记快照
        .route("/api/profiles", get(list_profiles))
        .route("/api/profiles/latest", get(get_latest_profile))
        .route("/api/profiles/:id", get(get_profile).put(update_profile))
        // 隐私设置
        .route(
            "/api/privacy/blacklist",
            get(list_blacklist).post(add_blacklist),
        )
        .route(
            "/api/privacy/blacklist/:id/enabled",
            axum::routing::patch(update_blacklist_enabled),
        )
        .route(
            "/api/privacy/blacklist/:id",
            axum::routing::delete(delete_blacklist),
        )
        .route("/api/privacy/filters", get(list_filters))
        .route(
            "/api/privacy/filters/:filter_type/enabled",
            axum::routing::patch(update_filter_enabled),
        )
        .route(
            "/api/privacy/filters/:filter_type/config",
            axum::routing::patch(update_filter_config),
        )
        // 烤面包
        .route("/api/bake/overview", get(get_bake_overview))
        .route("/api/bake/queue-status", get(get_bake_queue_status))
        .route("/api/bake/run", post(run_bake_pipeline))
        .route(
            "/api/bake/style-config",
            get(get_bake_style_config).put(update_bake_style_config),
        )
        .route("/api/bake/sops", get(list_bake_sops))
        .route(
            "/api/bake/sops/:id",
            get(get_bake_sop).delete(delete_bake_sop),
        )
        .route(
            "/api/bake/documents",
            get(list_bake_documents).post(create_bake_document),
        )
        .route(
            "/api/bake/documents/:id",
            get(get_bake_document)
                .put(update_bake_document)
                .delete(delete_bake_document),
        )
        .route(
            "/api/bake/documents/:id/toggle-status",
            post(toggle_bake_document_status),
        )
        .route("/api/bake/articles", get(list_bake_memories))
        .route("/api/bake/memories", get(list_bake_memories))
        .route("/api/bake/memories/:id", delete(delete_bake_memory))
        .route("/api/bake/knowledge", get(list_bake_knowledge))
        .route(
            "/api/bake/knowledge/:id",
            get(get_bake_knowledge).delete(delete_bake_knowledge),
        )
        .route("/api/bake/captures", get(list_bake_captures))
        .route(
            "/api/bake/captures/:id",
            get(get_bake_capture).delete(delete_bake_capture),
        )
        .route(
            "/api/bake/captures/:id/screenshot",
            get(get_bake_capture_screenshot),
        )
        .route("/api/bake/articles/init", post(initialize_bake_memories))
        .route("/api/bake/memories/init", post(initialize_bake_memories))
        .route("/api/bake/articles/:id/ignore", post(ignore_bake_memory))
        .route("/api/bake/memories/:id/ignore", post(ignore_bake_memory))
        .route(
            "/api/bake/articles/:id/promote-document",
            post(promote_bake_memory_to_document),
        )
        .route(
            "/api/bake/memories/:id/promote-document",
            post(promote_bake_memory_to_document),
        )
        .route(
            "/api/bake/articles/:id/promote-sop",
            post(promote_bake_memory_to_sop),
        )
        .route(
            "/api/bake/memories/:id/promote-sop",
            post(promote_bake_memory_to_sop),
        )
        .route(
            "/api/bake/articles/:id/preview",
            get(get_bake_memory_preview),
        )
        .route(
            "/api/bake/memories/:id/preview",
            get(get_bake_memory_preview),
        )
        .layer(cors)
        .with_state(state)
}

/// 启动 HTTP 服务器（绑定到 addr，阻塞直到关闭）。
///
/// `addr` 默认为 `"127.0.0.1:7070"`。
pub async fn start_server(state: Arc<AppState>, addr: &str) -> anyhow::Result<()> {
    let app = create_router(state);
    let listener = tokio::net::TcpListener::bind(addr).await?;
    info!("记忆面包 API 服务已启动，监听地址: http://{addr}");
    axum::serve(listener, app).await?;
    Ok(())
}

//! Local asset snapshot endpoints.

use std::{collections::BTreeMap, path::PathBuf, sync::Arc};

use aes_gcm::{
    aead::Aead,
    Aes256Gcm, KeyInit, Nonce,
};
#[cfg(test)]
use aes_gcm::aead::{rand_core::RngCore, OsRng};
use axum::{extract::{Path, State}, Json};
use base64::{engine::general_purpose::STANDARD as BASE64, Engine as _};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use crate::{
    api::{error::ApiError, state::{AppState, CloudBackupJobRecord}},
    storage::{
        db::current_ts_ms,
        snapshot::{AssetSnapshotExportResult, AssetSnapshotImportReport},
    },
};

#[derive(Debug, Deserialize)]
pub struct ExportAssetSnapshotRequest {
    pub output_path: Option<String>,
    #[serde(default)]
    pub client_state: BTreeMap<String, String>,
}

#[derive(Debug, Deserialize)]
pub struct ImportAssetSnapshotRequest {
    pub path: Option<String>,
    pub content: Option<String>,
    /// Defaults to true. Set to false explicitly to write into the active DB.
    pub dry_run: Option<bool>,
}

#[derive(Debug, Deserialize)]
pub struct CloudBackupRequest {
    pub admin_base_url: String,
    pub service_environment: String,
    pub access_token: String,
    pub device_id: String,
    pub output_path: Option<String>,
    #[serde(default)]
    pub client_state: BTreeMap<String, String>,
}

#[derive(Debug, Serialize)]
pub struct CloudBackupResponse {
    /// Historical wire name: for version 0 this is the ordinary memory package.
    pub local_encrypted_path: String,
    pub oss_object_key: String,
    pub checksum_sha256: String,
    pub encrypted_size: i64,
    pub snapshot: Value,
}

#[derive(Debug, Serialize)]
pub struct CloudBackupJobCreated {
    pub job_id: String,
}

pub async fn start_cloud_backup_job(
    State(state): State<Arc<AppState>>,
    Json(body): Json<CloudBackupRequest>,
) -> Result<Json<CloudBackupJobCreated>, ApiError> {
    // Validate before acknowledging the job so malformed requests retain the normal API error.
    normalize_admin_base_url(&body.admin_base_url)?;
    normalize_service_environment(&body.service_environment)?;
    clean_required(&body.access_token, "access_token")?;
    clean_required(&body.device_id, "device_id")?;
    let job_id = uuid::Uuid::new_v4().to_string();
    let now = current_ts_ms();
    state.cloud_backup_jobs.lock().unwrap().insert(job_id.clone(), CloudBackupJobRecord {
        id: job_id.clone(),
        status: "running".to_string(),
        result: None,
        error: None,
        created_at_ms: now,
        updated_at_ms: now,
    });
    let task_state = state.clone();
    let task_id = job_id.clone();
    tokio::spawn(async move {
        let outcome = run_cloud_backup(task_state.clone(), body).await;
        let mut jobs = task_state.cloud_backup_jobs.lock().unwrap();
        if let Some(job) = jobs.get_mut(&task_id) {
            job.updated_at_ms = current_ts_ms();
            match outcome {
                Ok(response) => {
                    job.status = "succeeded".to_string();
                    job.result = serde_json::to_value(response).ok();
                }
                Err(error) => {
                    tracing::error!(job_id = %task_id, error_kind = %cloud_backup_error_message(&error), "cloud backup job failed");
                    job.status = "failed".to_string();
                    job.error = Some(cloud_backup_error_message(&error));
                }
            }
        }
    });
    Ok(Json(CloudBackupJobCreated { job_id }))
}

pub async fn get_cloud_backup_job(
    State(state): State<Arc<AppState>>,
    Path(job_id): Path<String>,
) -> Result<Json<CloudBackupJobRecord>, ApiError> {
    state.cloud_backup_jobs.lock().unwrap().get(&job_id).cloned()
        .map(Json)
        .ok_or_else(|| ApiError::NotFound("云端备份任务不存在或本地服务已重启，请刷新云端列表".to_string()))
}

fn cloud_backup_error_message(error: &ApiError) -> String {
    match error {
        ApiError::Upstream { code, .. } => match *code {
            "CLOUD_PREPARE_UPLOAD_FAILED" => "云端备份准备失败，请检查账户和存储空间".to_string(),
            "OSS_UPLOAD_FAILED" => "记忆包上传失败，请检查网络后重试".to_string(),
            "CLOUD_COMPLETE_SNAPSHOT_FAILED" => "云端备份登记失败，请刷新列表后重试".to_string(),
            _ => "云端服务暂时不可用，请稍后重试".to_string(),
        },
        ApiError::Storage(_) => "本地快照导出失败".to_string(),
        ApiError::BadRequest(message) => message.clone(),
        ApiError::NotFound(_) => "备份资源不存在".to_string(),
        ApiError::Internal(_) => "云端备份请求失败，请检查网络后重试".to_string(),
    }
}

#[derive(Debug, Deserialize)]
pub struct CloudRestoreRequest {
    pub admin_base_url: String,
    pub service_environment: String,
    pub access_token: String,
    pub snapshot_id: String,
    pub encrypted_output_path: Option<String>,
    pub decrypted_output_path: Option<String>,
    pub import_to_local: Option<bool>,
    /// Defaults to true when import_to_local is true.
    pub dry_run: Option<bool>,
}

#[derive(Debug, Serialize)]
pub struct CloudRestoreResponse {
    /// Historical wire name: for version 0 this is the ordinary memory package.
    pub local_encrypted_path: String,
    pub local_decrypted_path: String,
    pub checksum_sha256: String,
    pub encrypted_size: i64,
    pub oss_object_key: String,
    pub import_report: Option<AssetSnapshotImportReport>,
}

#[derive(Debug, Serialize, Deserialize)]
struct EncryptedAssetSnapshotEnvelope {
    magic: String,
    format_version: i32,
    encryption_version: i32,
    algorithm: String,
    nonce_base64: String,
    ciphertext_base64: String,
    plaintext_sha256: String,
    plaintext_size: u64,
}

#[derive(Debug, Deserialize)]
struct CloudEnvelope<T> {
    data: T,
}

#[derive(Debug, Deserialize)]
struct PreparedUploadData {
    oss_object_key: String,
    upload_url: String,
    required_headers: BTreeMap<String, String>,
}

#[derive(Debug, Deserialize)]
struct DownloadUrlData {
    oss_object_key: String,
    encrypted_size: i64,
    checksum_sha256: Option<String>,
    #[serde(default = "legacy_encryption_version")]
    encryption_version: i32,
    #[serde(default)]
    encryption_key_base64: Option<String>,
    download_url: String,
}

pub async fn export_asset_snapshot(
    State(state): State<Arc<AppState>>,
    Json(body): Json<ExportAssetSnapshotRequest>,
) -> Result<Json<AssetSnapshotExportResult>, ApiError> {
    let storage = state.storage.clone();
    let client_state = body.client_state;
    let path = body
        .output_path
        .map(PathBuf::from)
        .unwrap_or_else(default_asset_snapshot_path);

    let result = tokio::task::spawn_blocking(move || {
        storage.export_asset_snapshot_to_path_with_client_state(&path, client_state)
    })
    .await
    .map_err(|error| ApiError::Internal(error.to_string()))??;

    Ok(Json(result))
}

pub async fn import_asset_snapshot(
    State(state): State<Arc<AppState>>,
    Json(body): Json<ImportAssetSnapshotRequest>,
) -> Result<Json<AssetSnapshotImportReport>, ApiError> {
    let dry_run = body.dry_run.unwrap_or(true);
    let storage = state.storage.clone();
    let result = if let Some(content) = body.content.filter(|value| !value.trim().is_empty()) {
        tokio::task::spawn_blocking(move || {
            storage.import_asset_snapshot_from_bytes(content.as_bytes(), dry_run)
        })
        .await
        .map_err(|error| ApiError::Internal(error.to_string()))??
    } else if let Some(path) = body.path.filter(|value| !value.trim().is_empty()) {
        let path = PathBuf::from(path);
        tokio::task::spawn_blocking(move || storage.import_asset_snapshot_from_path(&path, dry_run))
            .await
            .map_err(|error| ApiError::Internal(error.to_string()))??
    } else {
        return Err(ApiError::BadRequest("path 或 content 不能为空".to_string()));
    };

    Ok(Json(result))
}

pub async fn backup_asset_snapshot_to_cloud(
    State(state): State<Arc<AppState>>,
    Json(body): Json<CloudBackupRequest>,
) -> Result<Json<CloudBackupResponse>, ApiError> {
    run_cloud_backup(state, body).await.map(Json)
}

async fn run_cloud_backup(
    state: Arc<AppState>,
    body: CloudBackupRequest,
) -> Result<CloudBackupResponse, ApiError> {
    let admin_base_url = normalize_admin_base_url(&body.admin_base_url)?;
    let service_environment = normalize_service_environment(&body.service_environment)?;
    let access_token = clean_required(&body.access_token, "access_token")?;
    let device_id = clean_required(&body.device_id, "device_id")?;
    let output_path = body
        .output_path
        .map(PathBuf::from)
        .unwrap_or_else(default_cloud_package_path);

    let storage = state.storage.clone();
    let client_state = body.client_state;
    let package_bytes = tokio::task::spawn_blocking(move || {
        let snapshot = storage.export_asset_snapshot_with_client_state(client_state)?;
        serde_json::to_vec_pretty(&snapshot).map_err(crate::storage::StorageError::from)
    })
    .await
    .map_err(|error| ApiError::Internal(error.to_string()))??;
    let client = reqwest::Client::new();
    // Keep historical wire field names for compatibility; version 0 is an unencrypted package.
    let encrypted_size = i64::try_from(package_bytes.len())
        .map_err(|_| ApiError::BadRequest("备份包过大".to_string()))?;
    let checksum_sha256 = sha256_hex(&package_bytes);
    let prepare: CloudEnvelope<PreparedUploadData> = send_json(
        client
            .post(format!("{admin_base_url}/v1/snapshots/upload-url"))
            .header("x-memorybread-environment", &service_environment)
            .bearer_auth(&access_token)
            .json(&json!({
                "device_id": device_id,
                "encrypted_size": encrypted_size,
                "checksum_sha256": checksum_sha256,
                "format_version": crate::storage::snapshot::ASSET_SNAPSHOT_FORMAT_VERSION,
                "schema_version": crate::storage::snapshot::ASSET_SNAPSHOT_SCHEMA_VERSION,
                "encryption_version": 0,
                "content_type": "application/octet-stream"
            })),
        "CLOUD_PREPARE_UPLOAD_FAILED",
    )
    .await?;

    if let Some(parent) = output_path.parent() {
        tokio::fs::create_dir_all(parent)
            .await
            .map_err(|error| ApiError::Internal(error.to_string()))?;
    }
    tokio::fs::write(&output_path, &package_bytes)
        .await
        .map_err(|error| ApiError::Internal(error.to_string()))?;

    let mut put = client.put(&prepare.data.upload_url);
    for (name, value) in &prepare.data.required_headers {
        put = put.header(name, value);
    }
    let put_response = put
        .body(package_bytes)
        .send()
        .await
        .map_err(|error| ApiError::Internal(error.to_string()))?;
    ensure_success(put_response, "OSS_UPLOAD_FAILED").await?;

    let completed: CloudEnvelope<Value> = send_json_retry_transient(
        client
            .post(format!("{admin_base_url}/v1/snapshots"))
            .header("x-memorybread-environment", &service_environment)
            .bearer_auth(&access_token)
            .json(&json!({
                "device_id": device_id,
                "encrypted_size": encrypted_size,
                "oss_object_key": prepare.data.oss_object_key,
                "checksum_sha256": checksum_sha256,
                "format_version": crate::storage::snapshot::ASSET_SNAPSHOT_FORMAT_VERSION,
                "schema_version": crate::storage::snapshot::ASSET_SNAPSHOT_SCHEMA_VERSION,
                "encryption_version": 0
            })),
        "CLOUD_COMPLETE_SNAPSHOT_FAILED",
    )
    .await?;

    Ok(CloudBackupResponse {
        local_encrypted_path: output_path.display().to_string(),
        oss_object_key: prepare.data.oss_object_key,
        checksum_sha256,
        encrypted_size,
        snapshot: completed.data,
    })
}

pub async fn restore_asset_snapshot_from_cloud(
    State(state): State<Arc<AppState>>,
    Json(body): Json<CloudRestoreRequest>,
) -> Result<Json<CloudRestoreResponse>, ApiError> {
    let admin_base_url = normalize_admin_base_url(&body.admin_base_url)?;
    let service_environment = normalize_service_environment(&body.service_environment)?;
    let access_token = clean_required(&body.access_token, "access_token")?;
    let snapshot_id = clean_required(&body.snapshot_id, "snapshot_id")?;
    let encrypted_output_path = body
        .encrypted_output_path
        .map(PathBuf::from)
        .unwrap_or_else(default_downloaded_package_path);
    let decrypted_output_path = body
        .decrypted_output_path
        .map(PathBuf::from)
        .unwrap_or_else(default_restored_package_path);

    let client = reqwest::Client::new();
    let download: CloudEnvelope<DownloadUrlData> = send_json(
        client
            .get(format!(
                "{admin_base_url}/v1/snapshots/{snapshot_id}/download-url"
            ))
            .header("x-memorybread-environment", &service_environment)
            .bearer_auth(&access_token),
        "CLOUD_PREPARE_DOWNLOAD_FAILED",
    )
    .await?;

    let response = client
        .get(&download.data.download_url)
        .send()
        .await
        .map_err(|error| ApiError::Internal(error.to_string()))?;
    let response = ensure_success(response, "OSS_DOWNLOAD_FAILED").await?;
    let encrypted = response
        .bytes()
        .await
        .map_err(|error| ApiError::Internal(error.to_string()))?
        .to_vec();
    let checksum_sha256 = sha256_hex(&encrypted);
    if encrypted.len() as i64 != download.data.encrypted_size {
        return Err(ApiError::BadRequest(
            "下载对象大小与数据库登记不一致".to_string(),
        ));
    }
    if download.data.encryption_version == 0 && download.data.checksum_sha256.is_none() {
        return Err(ApiError::BadRequest("云端记忆包缺少校验值".to_string()));
    }
    if let Some(expected_checksum) = download.data.checksum_sha256.as_deref() {
        if expected_checksum != checksum_sha256 {
            return Err(ApiError::BadRequest(
                "下载对象 checksum 与数据库登记不一致".to_string(),
            ));
        }
    }

    if let Some(parent) = encrypted_output_path.parent() {
        tokio::fs::create_dir_all(parent)
            .await
            .map_err(|error| ApiError::Internal(error.to_string()))?;
    }
    tokio::fs::write(&encrypted_output_path, &encrypted)
        .await
        .map_err(|error| ApiError::Internal(error.to_string()))?;

    let plaintext = match download.data.encryption_version {
        0 => encrypted.clone(),
        2 => {
            let key = parse_encryption_key(download.data.encryption_key_base64.as_deref()
                .ok_or_else(|| ApiError::BadRequest("旧版备份缺少解密材料".to_string()))?)?;
            decrypt_snapshot_bytes(&encrypted, &key)?
        }
        _ => return Err(ApiError::BadRequest("不支持的云端备份版本".to_string())),
    };
    if let Some(parent) = decrypted_output_path.parent() {
        tokio::fs::create_dir_all(parent)
            .await
            .map_err(|error| ApiError::Internal(error.to_string()))?;
    }
    tokio::fs::write(&decrypted_output_path, &plaintext)
        .await
        .map_err(|error| ApiError::Internal(error.to_string()))?;

    let import_report = if body.import_to_local.unwrap_or(false) {
        let storage = state.storage.clone();
        let path = decrypted_output_path.clone();
        let dry_run = body.dry_run.unwrap_or(true);
        Some(
            tokio::task::spawn_blocking(move || {
                storage.import_asset_snapshot_from_path(&path, dry_run)
            })
            .await
            .map_err(|error| ApiError::Internal(error.to_string()))??,
        )
    } else {
        None
    };

    Ok(Json(CloudRestoreResponse {
        local_encrypted_path: encrypted_output_path.display().to_string(),
        local_decrypted_path: decrypted_output_path.display().to_string(),
        checksum_sha256,
        encrypted_size: encrypted.len() as i64,
        oss_object_key: download.data.oss_object_key,
        import_report,
    }))
}

fn default_asset_snapshot_path() -> PathBuf {
    let home = std::env::var("HOME").unwrap_or_else(|_| ".".to_string());
    PathBuf::from(home)
        .join(".memory-bread")
        .join("backups")
        .join(format!("memory-package-{}.mbmemory.json", current_ts_ms()))
}

fn default_cloud_package_path() -> PathBuf {
    let home = std::env::var("HOME").unwrap_or_else(|_| ".".to_string());
    PathBuf::from(home)
        .join(".memory-bread")
        .join("backups")
        .join(format!(
            "cloud-memory-package-{}.mbmemory.json",
            current_ts_ms()
        ))
}

fn default_downloaded_package_path() -> PathBuf {
    let home = std::env::var("HOME").unwrap_or_else(|_| ".".to_string());
    PathBuf::from(home)
        .join(".memory-bread")
        .join("backups")
        .join(format!(
            "downloaded-memory-package-{}.mbmemory.json",
            current_ts_ms()
        ))
}

fn default_restored_package_path() -> PathBuf {
    let home = std::env::var("HOME").unwrap_or_else(|_| ".".to_string());
    PathBuf::from(home)
        .join(".memory-bread")
        .join("backups")
        .join(format!(
            "restored-memory-package-{}.mbmemory.json",
            current_ts_ms()
        ))
}

fn normalize_admin_base_url(value: &str) -> Result<String, ApiError> {
    let value = clean_required(value, "admin_base_url")?;
    if !(value.starts_with("http://") || value.starts_with("https://")) {
        return Err(ApiError::BadRequest(
            "admin_base_url 必须以 http:// 或 https:// 开头".to_string(),
        ));
    }
    Ok(value.trim_end_matches('/').to_string())
}

fn normalize_service_environment(value: &str) -> Result<String, ApiError> {
    match value.trim() {
        "production" => Ok("production".to_string()),
        "staging" => Ok("staging".to_string()),
        _ => Err(ApiError::BadRequest(
            "service_environment 必须是 production 或 staging".to_string(),
        )),
    }
}

fn clean_required(value: &str, field: &str) -> Result<String, ApiError> {
    let trimmed = value.trim();
    if trimmed.is_empty() {
        return Err(ApiError::BadRequest(format!("{field} 不能为空")));
    }
    Ok(trimmed.to_string())
}

// Older servers omitted the version and always returned service-managed encryption.
fn legacy_encryption_version() -> i32 { 2 }

fn parse_encryption_key(value: &str) -> Result<[u8; 32], ApiError> {
    let decoded = BASE64
        .decode(value.trim())
        .map_err(|_| ApiError::BadRequest("云端备份加密材料不合法".to_string()))?;
    if decoded.len() != 32 {
        return Err(ApiError::BadRequest(
            "云端备份加密材料长度不合法".to_string(),
        ));
    }
    let mut key = [0u8; 32];
    key.copy_from_slice(&decoded);
    Ok(key)
}

#[cfg(test)]
fn encrypt_snapshot_bytes(plaintext: &[u8], key: &[u8; 32]) -> Result<Vec<u8>, ApiError> {
    let cipher = Aes256Gcm::new_from_slice(key)
        .map_err(|_| ApiError::Internal("初始化快照加密器失败".to_string()))?;
    let mut nonce = [0u8; 12];
    OsRng.fill_bytes(&mut nonce);
    let ciphertext = cipher
        .encrypt(Nonce::from_slice(&nonce), plaintext)
        .map_err(|_| ApiError::Internal("快照加密失败".to_string()))?;
    let envelope = EncryptedAssetSnapshotEnvelope {
        magic: "MemoryBreadAssetSnapshotEncrypted".to_string(),
        format_version: 1,
        encryption_version: 2,
        algorithm: "AES-256-GCM".to_string(),
        nonce_base64: BASE64.encode(nonce),
        ciphertext_base64: BASE64.encode(ciphertext),
        plaintext_sha256: sha256_hex(plaintext),
        plaintext_size: plaintext.len() as u64,
    };
    serde_json::to_vec_pretty(&envelope)
        .map_err(|error| ApiError::Internal(format!("序列化加密快照失败: {error}")))
}

fn decrypt_snapshot_bytes(encrypted: &[u8], key: &[u8; 32]) -> Result<Vec<u8>, ApiError> {
    let envelope: EncryptedAssetSnapshotEnvelope = serde_json::from_slice(encrypted)
        .map_err(|_| ApiError::BadRequest("加密快照格式不合法".to_string()))?;
    if envelope.magic != "MemoryBreadAssetSnapshotEncrypted"
        || !matches!(envelope.encryption_version, 1 | 2)
        || envelope.algorithm != "AES-256-GCM"
    {
        return Err(ApiError::BadRequest("不支持的加密快照格式".to_string()));
    }
    let nonce = BASE64
        .decode(envelope.nonce_base64)
        .map_err(|_| ApiError::BadRequest("加密快照 nonce 不合法".to_string()))?;
    let ciphertext = BASE64
        .decode(envelope.ciphertext_base64)
        .map_err(|_| ApiError::BadRequest("加密快照密文不合法".to_string()))?;
    if nonce.len() != 12 {
        return Err(ApiError::BadRequest(
            "加密快照 nonce 长度不合法".to_string(),
        ));
    }
    let cipher = Aes256Gcm::new_from_slice(key)
        .map_err(|_| ApiError::Internal("初始化快照解密器失败".to_string()))?;
    let plaintext = cipher
        .decrypt(Nonce::from_slice(&nonce), ciphertext.as_ref())
        .map_err(|_| ApiError::BadRequest("云端备份解密失败或快照已损坏".to_string()))?;
    if plaintext.len() as u64 != envelope.plaintext_size
        || sha256_hex(&plaintext) != envelope.plaintext_sha256
    {
        return Err(ApiError::BadRequest("解密后快照校验失败".to_string()));
    }
    Ok(plaintext)
}

async fn send_json<T>(request: reqwest::RequestBuilder, code: &'static str) -> Result<T, ApiError>
where
    T: for<'de> Deserialize<'de>,
{
    let response = request
        .send()
        .await
        .map_err(|error| ApiError::Internal(error.to_string()))?;
    let response = ensure_success(response, code).await?;
    response
        .json::<T>()
        .await
        .map_err(|error| ApiError::Internal(error.to_string()))
}

async fn send_json_retry_transient<T>(
    request: reqwest::RequestBuilder,
    code: &'static str,
) -> Result<T, ApiError>
where
    T: for<'de> Deserialize<'de>,
{
    for attempt in 0..5 {
        let current = request.try_clone().ok_or_else(|| {
            ApiError::Internal("无法重试云端登记请求".to_string())
        })?;
        match send_json(current, code).await {
            Ok(value) => return Ok(value),
            Err(error) if attempt < 4 && transient_snapshot_error(&error) => {
                tokio::time::sleep(std::time::Duration::from_secs(1 << attempt)).await;
            }
            Err(error) => return Err(error),
        }
    }
    unreachable!("five attempts always return")
}

fn transient_snapshot_error(error: &ApiError) -> bool {
    match error {
        ApiError::Upstream { status, .. } => status.is_server_error(),
        ApiError::Internal(_) => true,
        _ => false,
    }
}

async fn ensure_success(
    response: reqwest::Response,
    code: &'static str,
) -> Result<reqwest::Response, ApiError> {
    let status = response.status();
    if status.is_success() {
        return Ok(response);
    }

    let response_body = response
        .text()
        .await
        .unwrap_or_else(|_| "上游服务请求失败".to_string());
    let message = serde_json::from_str::<Value>(&response_body)
        .ok()
        .and_then(|payload| {
            payload
                .pointer("/error/message")
                .and_then(Value::as_str)
                .map(str::to_string)
        })
        .unwrap_or(response_body);
    let status = axum::http::StatusCode::from_u16(status.as_u16())
        .unwrap_or(axum::http::StatusCode::BAD_GATEWAY);
    Err(ApiError::Upstream {
        status,
        code,
        message,
    })
}

fn sha256_hex(bytes: &[u8]) -> String {
    let digest = Sha256::digest(bytes);
    digest.iter().map(|byte| format!("{byte:02x}")).collect()
}

#[cfg(test)]
mod tests {
    use super::{
        decrypt_snapshot_bytes, encrypt_snapshot_bytes,
        normalize_service_environment, send_json_retry_transient, transient_snapshot_error,
    };
    use crate::api::error::ApiError;
    use axum::http::StatusCode;
    use std::sync::{atomic::{AtomicUsize, Ordering}, Arc};

    /// Real HTTP upload/download plus real SQLite export/import, with isolated machine directories.
    #[tokio::test]
    async fn cloud_round_trip_restores_on_new_machine_without_keys() {
        use super::*;
        use crate::storage::StorageManager;
        use axum::{body::Bytes, http::HeaderMap, routing::{get, post, put}, Router};
        use std::sync::Mutex;

        #[derive(Default)]
        struct Cloud { bytes: Vec<u8>, metadata: Value, corrupt: bool, wrong_size: bool, missing_checksum: bool, version: i32 }
        let cloud = Arc::new(Mutex::new(Cloud::default()));
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let base = format!("http://{}", listener.local_addr().unwrap());
        let upload_url = format!("{base}/object");
        let download_url = upload_url.clone();
        let upload_cloud = cloud.clone();
        let complete_cloud = cloud.clone();
        let download_cloud = cloud.clone();
        let object_cloud = cloud.clone();
        let app = Router::new()
            .route("/v1/snapshots/upload-url", post(move |headers: HeaderMap, Json(body): Json<Value>| {
                let url = upload_url.clone();
                async move {
                    assert_eq!(headers["authorization"], "Bearer same-account");
                    assert_eq!(headers["x-memorybread-environment"], "staging");
                    assert_eq!(body["encryption_version"], 0);
                    assert_eq!(body["checksum_sha256"].as_str().unwrap().len(), 64);
                    Json(json!({"data": {"oss_object_key": "snapshots/account/old-device/package", "upload_url": url,
                        "required_headers": {"content-type": "application/octet-stream"}}}))
                }
            }))
            .route("/object", put(move |headers: HeaderMap, bytes: Bytes| {
                let cloud = upload_cloud.clone();
                async move {
                    assert_eq!(headers["content-type"], "application/octet-stream");
                    let package: Value = serde_json::from_slice(&bytes).unwrap();
                    assert!(package.get("manifest").is_some());
                    assert!(package.get("ciphertext_base64").is_none());
                    cloud.lock().unwrap().bytes = bytes.to_vec();
                    StatusCode::OK
                }
            }).get(move || {
                let cloud = object_cloud.clone();
                async move {
                    let cloud = cloud.lock().unwrap();
                    let mut bytes = cloud.bytes.clone();
                    if cloud.corrupt { bytes[0] ^= 1; }
                    bytes
                }
            }))
            .route("/v1/snapshots", post(move |headers: HeaderMap, Json(body): Json<Value>| {
                let cloud = complete_cloud.clone();
                async move {
                    assert_eq!(headers["authorization"], "Bearer same-account");
                    let mut cloud = cloud.lock().unwrap();
                    assert_eq!(body["encrypted_size"], cloud.bytes.len() as i64);
                    assert_eq!(body["checksum_sha256"], sha256_hex(&cloud.bytes));
                    assert_eq!(body["encryption_version"], 0);
                    cloud.metadata = body;
                    Json(json!({"data": {"id": "snapshot-1", "status": "committed"}}))
                }
            }))
            .route("/v1/snapshots/snapshot-1/download-url", get(move |headers: HeaderMap| {
                let cloud = download_cloud.clone();
                let url = download_url.clone();
                async move {
                    assert_eq!(headers["authorization"], "Bearer same-account");
                    let cloud = cloud.lock().unwrap();
                    Json(json!({"data": {"oss_object_key": cloud.metadata["oss_object_key"],
                        "encryption_version": cloud.version,
                        "encryption_key_base64": if cloud.version == 2 { Some(BASE64.encode([7u8; 32])) } else { None },
                        "encrypted_size": cloud.bytes.len() + usize::from(cloud.wrong_size),
                        "checksum_sha256": if cloud.missing_checksum { Value::Null } else { cloud.metadata["checksum_sha256"].clone() },
                        "download_url": url}}))
                }
            }));
        let server = tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        let temp = tempfile::tempdir().unwrap();
        let old_root = temp.path().join("old-machine");
        let new_root = temp.path().join("new-machine");
        let old = StorageManager::open(&old_root.join("memory-bread.db")).unwrap();
        old.upsert_preference("cloud.fixture", "跨机器恢复", "user", 1.0).unwrap();
        std::fs::create_dir_all(old_root.join("skills/example")).unwrap();
        std::fs::write(old_root.join("skills/example/SKILL.md"), "fixture skill").unwrap();
        let old_state = AppState::with_service_urls(old, String::new(), String::new(), vec![]);
        let result = run_cloud_backup(old_state.clone(), CloudBackupRequest {
            admin_base_url: base.clone(), service_environment: "staging".into(), access_token: "same-account".into(),
            device_id: "old-device".into(), output_path: Some(temp.path().join("upload.json").display().to_string()),
            client_state: BTreeMap::from([("memory-bread_theme".into(), "dark".into())]),
        }).await.unwrap();
        assert_eq!(result.snapshot["id"], "snapshot-1");
        drop(old_state);
        std::fs::remove_dir_all(&old_root).unwrap();
        let new = StorageManager::open(&new_root.join("memory-bread.db")).unwrap();
        new.upsert_preference("local.only", "preserve", "user", 1.0).unwrap();
        let state = AppState::with_service_urls(new, String::new(), String::new(), vec![]);
        let request = |dry_run| CloudRestoreRequest {
            admin_base_url: base.clone(), service_environment: "staging".into(), access_token: "same-account".into(),
            snapshot_id: "snapshot-1".into(),
            encrypted_output_path: Some(temp.path().join("download.json").display().to_string()),
            decrypted_output_path: Some(temp.path().join("restore.json").display().to_string()),
            import_to_local: Some(true), dry_run,
        };
        let preview = restore_asset_snapshot_from_cloud(State(state.clone()), Json(request(None))).await.unwrap().0;
        assert!(preview.import_report.unwrap().dry_run);
        assert!(state.storage.get_preference_value("cloud.fixture").unwrap().is_none());
        assert!(!new_root.join("skills/example/SKILL.md").exists());
        let restored = restore_asset_snapshot_from_cloud(State(state.clone()), Json(request(Some(false)))).await.unwrap().0;
        assert_eq!(restored.checksum_sha256, result.checksum_sha256);
        assert_eq!(restored.import_report.unwrap().client_state["memory-bread_theme"], "dark");
        assert_eq!(state.storage.get_preference_value("cloud.fixture").unwrap().as_deref(), Some("跨机器恢复"));
        assert_eq!(state.storage.get_preference_value("local.only").unwrap().as_deref(), Some("preserve"));
        assert_eq!(std::fs::read_to_string(new_root.join("skills/example/SKILL.md")).unwrap(), "fixture skill");
        let repeated = restore_asset_snapshot_from_cloud(State(state.clone()), Json(request(Some(false)))).await.unwrap().0;
        assert!(repeated.import_report.unwrap().tables.iter().all(|t| t.inserted == 0));
        // Fail before touching the destination database for every integrity failure.
        for mode in 0..3 {
            {
                let mut cloud = cloud.lock().unwrap();
                cloud.corrupt = mode == 0;
                cloud.wrong_size = mode == 1;
                cloud.missing_checksum = mode == 2;
            }
            let error = restore_asset_snapshot_from_cloud(State(state.clone()), Json(request(Some(false)))).await.unwrap_err();
            assert!(matches!(error, ApiError::BadRequest(_)));
            assert_eq!(state.storage.get_preference_value("local.only").unwrap().as_deref(), Some("preserve"));
        }
        // Existing service-managed encrypted snapshots still restore through the same handler.
        {
            let mut cloud = cloud.lock().unwrap();
            cloud.missing_checksum = false;
            cloud.bytes = encrypt_snapshot_bytes(&cloud.bytes, &[7u8; 32]).unwrap();
            cloud.metadata["checksum_sha256"] = json!(sha256_hex(&cloud.bytes));
            cloud.version = 2;
        }
        let legacy = restore_asset_snapshot_from_cloud(State(state.clone()), Json(request(Some(false)))).await.unwrap().0;
        assert!(legacy.import_report.unwrap().tables.iter().all(|t| t.inserted == 0));
        server.abort();
    }

    #[tokio::test]
    async fn completion_retry_uses_the_same_request_after_a_server_error() {
        let attempts = Arc::new(AtomicUsize::new(0));
        let count = attempts.clone();
        let app = axum::Router::new().route("/", axum::routing::post(move |axum::Json(body): axum::Json<serde_json::Value>| {
            let count = count.clone();
            async move {
                assert_eq!(body["oss_object_key"], "same-object");
                if count.fetch_add(1, Ordering::SeqCst) == 0 {
                    (StatusCode::INTERNAL_SERVER_ERROR, axum::Json(serde_json::json!({"error": {"message": "temporary"}})))
                } else {
                    (StatusCode::OK, axum::Json(serde_json::json!({"data": {"id": "committed"}})))
                }
            }
        }));
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        let request = reqwest::Client::new()
            .post(format!("http://{address}/"))
            .json(&serde_json::json!({"oss_object_key": "same-object"}));
        let result: super::CloudEnvelope<serde_json::Value> =
            send_json_retry_transient(request, "CLOUD_COMPLETE_SNAPSHOT_FAILED").await.unwrap();
        server.abort();
        assert_eq!(result.data["id"], "committed");
        assert_eq!(attempts.load(Ordering::SeqCst), 2);
    }

    #[test]
    fn retries_only_transient_snapshot_completion_errors() {
        let error = |status| ApiError::Upstream {
            status,
            code: "CLOUD_COMPLETE_SNAPSHOT_FAILED",
            message: "unavailable".to_string(),
        };
        assert!(transient_snapshot_error(&error(StatusCode::INTERNAL_SERVER_ERROR)));
        assert!(transient_snapshot_error(&error(StatusCode::SERVICE_UNAVAILABLE)));
        assert!(!transient_snapshot_error(&error(StatusCode::CONFLICT)));
    }

    #[test]
    fn encrypts_and_decrypts_snapshot_bytes() {
        let key = [7u8; 32];

        let plaintext = br#"{"hello":"bread"}"#;
        let encrypted = encrypt_snapshot_bytes(plaintext, &key).unwrap();
        assert!(!encrypted.windows(5).any(|window| window == b"hello"));

        let decrypted = decrypt_snapshot_bytes(&encrypted, &key).unwrap();
        assert_eq!(decrypted, plaintext);
    }

    #[test]
    fn cloud_snapshot_environment_is_explicit() {
        assert_eq!(
            normalize_service_environment("production").unwrap(),
            "production"
        );
        assert_eq!(normalize_service_environment("staging").unwrap(), "staging");
        assert!(normalize_service_environment("test").is_err());
    }
}

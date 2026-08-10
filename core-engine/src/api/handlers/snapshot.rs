//! Local asset snapshot endpoints.

use std::{collections::BTreeMap, path::PathBuf, sync::Arc};

use aes_gcm::{
    aead::{rand_core::RngCore, Aead, OsRng},
    Aes256Gcm, KeyInit, Nonce,
};
use axum::{extract::State, Json};
use base64::{engine::general_purpose::STANDARD as BASE64, Engine as _};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use crate::{
    api::{error::ApiError, state::AppState},
    storage::{
        db::current_ts_ms,
        snapshot::{AssetSnapshotExportResult, AssetSnapshotImportReport},
    },
};

#[derive(Debug, Deserialize)]
pub struct ExportAssetSnapshotRequest {
    pub output_path: Option<String>,
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
}

#[derive(Debug, Serialize)]
pub struct CloudBackupResponse {
    pub local_encrypted_path: String,
    pub oss_object_key: String,
    pub checksum_sha256: String,
    pub encrypted_size: i64,
    pub snapshot: Value,
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
    encryption_key_base64: String,
    upload_url: String,
    required_headers: BTreeMap<String, String>,
}

#[derive(Debug, Deserialize)]
struct DownloadUrlData {
    oss_object_key: String,
    encrypted_size: i64,
    checksum_sha256: Option<String>,
    encryption_key_base64: String,
    download_url: String,
}

pub async fn export_asset_snapshot(
    State(state): State<Arc<AppState>>,
    Json(body): Json<ExportAssetSnapshotRequest>,
) -> Result<Json<AssetSnapshotExportResult>, ApiError> {
    let storage = state.storage.clone();
    let path = body
        .output_path
        .map(PathBuf::from)
        .unwrap_or_else(default_asset_snapshot_path);

    let result = tokio::task::spawn_blocking(move || storage.export_asset_snapshot_to_path(&path))
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
    let admin_base_url = normalize_admin_base_url(&body.admin_base_url)?;
    let service_environment = normalize_service_environment(&body.service_environment)?;
    let access_token = clean_required(&body.access_token, "access_token")?;
    let device_id = clean_required(&body.device_id, "device_id")?;
    let output_path = body
        .output_path
        .map(PathBuf::from)
        .unwrap_or_else(default_cloud_encrypted_path);

    let storage = state.storage.clone();
    let plaintext = tokio::task::spawn_blocking(move || {
        let snapshot = storage.export_asset_snapshot()?;
        serde_json::to_vec_pretty(&snapshot).map_err(crate::storage::StorageError::from)
    })
    .await
    .map_err(|error| ApiError::Internal(error.to_string()))??;
    let client = reqwest::Client::new();
    // AES-GCM changes bytes but not envelope length, so this gives an exact quota preflight
    // without generating or persisting user-managed recovery material.
    let encrypted_size = encrypt_snapshot_bytes(&plaintext, &[0u8; 32])?.len() as i64;
    let prepare: CloudEnvelope<PreparedUploadData> = send_json(
        client
            .post(format!("{admin_base_url}/v1/snapshots/upload-url"))
            .header("x-memorybread-environment", &service_environment)
            .bearer_auth(&access_token)
            .json(&json!({
                "device_id": device_id,
                "encrypted_size": encrypted_size,
                "checksum_sha256": null,
                "format_version": crate::storage::snapshot::ASSET_SNAPSHOT_FORMAT_VERSION,
                "schema_version": crate::storage::snapshot::ASSET_SNAPSHOT_SCHEMA_VERSION,
                "encryption_version": 2,
                "content_type": "application/octet-stream"
            })),
        "CLOUD_PREPARE_UPLOAD_FAILED",
    )
    .await?;
    let key = parse_encryption_key(&prepare.data.encryption_key_base64)?;
    let encrypted = encrypt_snapshot_bytes(&plaintext, &key)?;
    let checksum_sha256 = sha256_hex(&encrypted);

    if let Some(parent) = output_path.parent() {
        tokio::fs::create_dir_all(parent)
            .await
            .map_err(|error| ApiError::Internal(error.to_string()))?;
    }
    tokio::fs::write(&output_path, &encrypted)
        .await
        .map_err(|error| ApiError::Internal(error.to_string()))?;

    let mut put = client.put(&prepare.data.upload_url);
    for (name, value) in &prepare.data.required_headers {
        put = put.header(name, value);
    }
    let put_response = put
        .body(encrypted)
        .send()
        .await
        .map_err(|error| ApiError::Internal(error.to_string()))?;
    ensure_success(put_response, "OSS_UPLOAD_FAILED").await?;

    let completed: CloudEnvelope<Value> = send_json(
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
                "encryption_version": 2
            })),
        "CLOUD_COMPLETE_SNAPSHOT_FAILED",
    )
    .await?;

    Ok(Json(CloudBackupResponse {
        local_encrypted_path: output_path.display().to_string(),
        oss_object_key: prepare.data.oss_object_key,
        checksum_sha256,
        encrypted_size,
        snapshot: completed.data,
    }))
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
        .unwrap_or_else(default_restored_encrypted_path);
    let decrypted_output_path = body
        .decrypted_output_path
        .map(PathBuf::from)
        .unwrap_or_else(default_restored_decrypted_path);

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
    let key = parse_encryption_key(&download.data.encryption_key_base64)?;

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

    let plaintext = decrypt_snapshot_bytes(&encrypted, &key)?;
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

fn default_cloud_encrypted_path() -> PathBuf {
    let home = std::env::var("HOME").unwrap_or_else(|_| ".".to_string());
    PathBuf::from(home)
        .join(".memory-bread")
        .join("backups")
        .join(format!(
            "cloud-memory-package-{}.mbmemory.enc.json",
            current_ts_ms()
        ))
}

fn default_restored_encrypted_path() -> PathBuf {
    let home = std::env::var("HOME").unwrap_or_else(|_| ".".to_string());
    PathBuf::from(home)
        .join(".memory-bread")
        .join("backups")
        .join(format!(
            "restored-memory-package-{}.mbmemory.enc.json",
            current_ts_ms()
        ))
}

fn default_restored_decrypted_path() -> PathBuf {
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
    use super::{decrypt_snapshot_bytes, encrypt_snapshot_bytes, normalize_service_environment};

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

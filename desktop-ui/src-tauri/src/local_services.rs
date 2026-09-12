use serde::{Deserialize, Serialize};
use std::{
    collections::BTreeMap,
    fs,
    io::Write,
    net::TcpListener,
    path::{Path, PathBuf},
};

#[cfg(unix)]
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};

pub const REGISTRY_SCHEMA_VERSION: &str = "local-services.v1";
pub const REGISTRY_FILE_NAME: &str = "local-services.v1.json";
pub const TOKEN_FILE_NAME: &str = "local-services.token";

pub const SERVICE_NAMES: [&str; 5] = ["core", "model_api", "creation", "vector_search", "ollama"];

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct LocalServiceEndpoint {
    pub scheme: String,
    pub host: String,
    pub port: u16,
    pub health_path: String,
    pub status: String,
}

impl LocalServiceEndpoint {
    pub fn base_url(&self) -> String {
        format!("{}://{}:{}", self.scheme, self.host, self.port)
    }

    pub fn bind_address(&self) -> String {
        format!("{}:{}", self.host, self.port)
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct LocalServiceRegistry {
    pub schema_version: String,
    pub instance_id: String,
    pub generation: u64,
    pub services: BTreeMap<String, LocalServiceEndpoint>,
}

impl LocalServiceRegistry {
    pub fn service(&self, name: &str) -> Result<&LocalServiceEndpoint, String> {
        self.services
            .get(name)
            .ok_or_else(|| format!("本机服务注册表缺少逻辑服务: {name}"))
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct LocalServiceEndpointsResponse {
    #[serde(flatten)]
    pub registry: LocalServiceRegistry,
    pub auth_token: String,
}

pub fn state_dir(runtime_home: &Path) -> PathBuf {
    runtime_home.join(".memory-bread").join("state")
}

pub fn registry_path(runtime_home: &Path) -> PathBuf {
    state_dir(runtime_home).join(REGISTRY_FILE_NAME)
}

pub fn token_path(runtime_home: &Path) -> PathBuf {
    state_dir(runtime_home).join(TOKEN_FILE_NAME)
}

fn reserve_ports() -> Result<Vec<(String, TcpListener, u16)>, String> {
    let mut reservations = Vec::with_capacity(SERVICE_NAMES.len());
    for name in SERVICE_NAMES {
        let listener = TcpListener::bind("127.0.0.1:0")
            .map_err(|error| format!("无法为本机服务 {name} 分配回环端点: {error}"))?;
        let address = listener
            .local_addr()
            .map_err(|error| format!("无法读取本机服务 {name} 的回环端点: {error}"))?;
        reservations.push((name.to_string(), listener, address.port()));
    }
    Ok(reservations)
}

pub fn allocate_registry(generation: u64) -> Result<LocalServiceRegistry, String> {
    // Hold every listener until all ports have been selected. This guarantees
    // uniqueness inside one generation. The packaged supervisor starts children
    // immediately after this function returns and regenerates the complete
    // generation if a third-party process wins the short release/spawn window.
    let reservations = reserve_ports()?;
    let mut services = BTreeMap::new();
    for (name, _listener, port) in reservations {
        services.insert(
            name,
            LocalServiceEndpoint {
                scheme: "http".to_string(),
                host: "127.0.0.1".to_string(),
                port,
                health_path: "/health".to_string(),
                status: "allocated".to_string(),
            },
        );
    }
    Ok(LocalServiceRegistry {
        schema_version: REGISTRY_SCHEMA_VERSION.to_string(),
        instance_id: uuid::Uuid::new_v4().to_string(),
        generation,
        services,
    })
}

pub fn legacy_development_registry() -> LocalServiceRegistry {
    let ports = [
        ("core", 7070),
        ("model_api", 7071),
        ("creation", 8001),
        ("vector_search", 7072),
        ("ollama", 11434),
    ];
    let services = ports
        .into_iter()
        .map(|(name, port)| {
            (
                name.to_string(),
                LocalServiceEndpoint {
                    scheme: "http".to_string(),
                    host: "127.0.0.1".to_string(),
                    port,
                    health_path: "/health".to_string(),
                    status: "legacy_development".to_string(),
                },
            )
        })
        .collect();
    LocalServiceRegistry {
        schema_version: REGISTRY_SCHEMA_VERSION.to_string(),
        instance_id: "legacy-development".to_string(),
        generation: 0,
        services,
    }
}

fn write_private_atomic(path: &Path, bytes: &[u8]) -> Result<(), String> {
    let parent = path
        .parent()
        .ok_or_else(|| "无法定位本机服务状态目录".to_string())?;
    fs::create_dir_all(parent).map_err(|error| error.to_string())?;
    let temporary = path.with_extension(format!("tmp-{}", uuid::Uuid::new_v4()));
    let mut options = fs::OpenOptions::new();
    options.create_new(true).write(true);
    #[cfg(unix)]
    options.mode(0o600);
    let mut file = options
        .open(&temporary)
        .map_err(|error| format!("无法创建本机服务状态临时文件: {error}"))?;
    file.write_all(bytes)
        .and_then(|_| file.sync_all())
        .map_err(|error| format!("无法写入本机服务状态: {error}"))?;
    fs::rename(&temporary, path).map_err(|error| {
        let _ = fs::remove_file(&temporary);
        format!("无法提交本机服务状态: {error}")
    })?;
    #[cfg(unix)]
    fs::set_permissions(path, fs::Permissions::from_mode(0o600))
        .map_err(|error| format!("无法限制本机服务状态权限: {error}"))?;
    Ok(())
}

pub fn persist_registry(
    runtime_home: &Path,
    registry: &LocalServiceRegistry,
    auth_token: &str,
) -> Result<(), String> {
    let registry_bytes = serde_json::to_vec_pretty(registry)
        .map_err(|error| format!("无法编码本机服务注册表: {error}"))?;
    write_private_atomic(&registry_path(runtime_home), &registry_bytes)?;
    write_private_atomic(&token_path(runtime_home), auth_token.as_bytes())
}

pub fn load_registry(runtime_home: &Path) -> Result<LocalServiceRegistry, String> {
    let bytes = fs::read(registry_path(runtime_home))
        .map_err(|error| format!("无法读取本机服务注册表: {error}"))?;
    let registry: LocalServiceRegistry = serde_json::from_slice(&bytes)
        .map_err(|error| format!("本机服务注册表格式无效: {error}"))?;
    if registry.schema_version != REGISTRY_SCHEMA_VERSION {
        return Err("本机服务注册表版本不受支持".to_string());
    }
    for name in SERVICE_NAMES {
        let endpoint = registry.service(name)?;
        if endpoint.host != "127.0.0.1" || endpoint.scheme != "http" || endpoint.port == 0 {
            return Err(format!("本机服务 {name} 端点不安全"));
        }
    }
    Ok(registry)
}

pub fn load_auth_token(runtime_home: &Path) -> Result<String, String> {
    let token = fs::read_to_string(token_path(runtime_home))
        .map_err(|error| format!("无法读取本机服务会话凭据: {error}"))?;
    let token = token.trim().to_string();
    if token.len() < 64 || !token.chars().all(|character| character.is_ascii_hexdigit()) {
        return Err("本机服务会话凭据格式无效".to_string());
    }
    Ok(token)
}

pub fn new_auth_token() -> String {
    format!(
        "{}{}",
        uuid::Uuid::new_v4().simple(),
        uuid::Uuid::new_v4().simple()
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeSet;

    #[test]
    fn allocated_registry_uses_unique_nonzero_loopback_ports() {
        let registry = allocate_registry(7).expect("registry");
        assert_eq!(registry.schema_version, REGISTRY_SCHEMA_VERSION);
        assert_eq!(registry.generation, 7);
        assert_eq!(registry.services.len(), SERVICE_NAMES.len());
        let ports = registry
            .services
            .values()
            .map(|endpoint| endpoint.port)
            .collect::<BTreeSet<_>>();
        assert_eq!(ports.len(), SERVICE_NAMES.len());
        assert!(registry.services.values().all(|endpoint| {
            endpoint.host == "127.0.0.1" && endpoint.scheme == "http" && endpoint.port > 0
        }));
    }

    #[test]
    fn persisted_registry_and_token_round_trip() {
        let root = std::env::temp_dir().join(format!(
            "memorybread-local-services-test-{}",
            uuid::Uuid::new_v4()
        ));
        let registry = allocate_registry(3).expect("registry");
        let token = new_auth_token();
        persist_registry(&root, &registry, &token).expect("persist");
        assert_eq!(load_registry(&root).expect("load registry"), registry);
        assert_eq!(load_auth_token(&root).expect("load token"), token);
        #[cfg(unix)]
        {
            assert_eq!(
                fs::metadata(registry_path(&root))
                    .expect("registry metadata")
                    .permissions()
                    .mode()
                    & 0o777,
                0o600
            );
            assert_eq!(
                fs::metadata(token_path(&root))
                    .expect("token metadata")
                    .permissions()
                    .mode()
                    & 0o777,
                0o600
            );
        }
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn loader_rejects_non_loopback_endpoint() {
        let root = std::env::temp_dir().join(format!(
            "memorybread-local-services-test-{}",
            uuid::Uuid::new_v4()
        ));
        let mut registry = allocate_registry(1).expect("registry");
        registry.services.get_mut("core").expect("core").host = "0.0.0.0".to_string();
        let token = new_auth_token();
        persist_registry(&root, &registry, &token).expect("persist");
        assert!(load_registry(&root).is_err());
        let _ = fs::remove_dir_all(root);
    }
}

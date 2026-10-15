//! Live loopback health is separate from sidecar capability/heartbeat snapshots.
use serde::{Deserialize, Serialize};
use std::{path::Path, time::Duration};

#[derive(Debug, Clone, Serialize)]
pub struct InferenceRuntimeHealth {
    pub available: bool,
    pub checked_at_ms: i64,
    pub recovery_status: String,
    pub recovery_stale: bool,
    pub attempts: u32,
    pub max_attempts: u32,
    pub next_retry_at_ms: Option<i64>,
    pub error_code: Option<String>,
}

#[derive(Default, Deserialize)]
pub(super) struct RecoveryState {
    #[serde(default)]
    status: String,
    #[serde(default)]
    updated_at_ms: i64,
    #[serde(default)]
    attempts: u32,
    #[serde(default)]
    max_attempts: u32,
    #[serde(default)]
    next_attempt: f64,
    #[serde(default)]
    startup_until: f64,
}

pub(super) fn loopback_base_url(raw: &str) -> Option<String> {
    let raw = if raw.contains("://") {
        raw.to_string()
    } else {
        format!("http://{raw}")
    };
    let url = reqwest::Url::parse(&raw).ok()?;
    if url.scheme() != "http"
        || !matches!(url.host_str(), Some("127.0.0.1" | "localhost"))
        || !url.username().is_empty()
        || url.password().is_some()
        || url.query().is_some()
        || url.fragment().is_some()
        || url.path() != "/"
        || url.port().is_none()
    {
        return None;
    }
    Some(url.as_str().trim_end_matches('/').to_string())
}

pub(super) fn configured_base_url() -> Option<String> {
    for key in ["MEMORY_BREAD_OLLAMA_URL", "OLLAMA_HOST"] {
        if let Ok(raw) = std::env::var(key) {
            if !raw.trim().is_empty() {
                return loopback_base_url(raw.trim());
            }
        }
    }
    if let Ok(path) = std::env::var("MEMORY_BREAD_ENDPOINT_REGISTRY") {
        let body: serde_json::Value = serde_json::from_slice(&std::fs::read(path).ok()?).ok()?;
        let endpoint = &body["services"]["ollama"];
        return loopback_base_url(&format!(
            "{}://{}:{}",
            endpoint["scheme"].as_str()?,
            endpoint["host"].as_str()?,
            endpoint["port"].as_u64()?
        ));
    }
    loopback_base_url("127.0.0.1:11434")
}

pub(super) async fn probe(base_url: Option<&str>) -> bool {
    let Some(base_url) = base_url.and_then(loopback_base_url) else {
        return false;
    };
    let Ok(client) = reqwest::Client::builder()
        .no_proxy()
        .redirect(reqwest::redirect::Policy::none())
        .timeout(Duration::from_millis(800))
        .build()
    else {
        return false;
    };
    let Ok(response) = client.get(format!("{base_url}/api/tags")).send().await else {
        return false;
    };
    if !response.status().is_success() {
        return false;
    }
    let Ok(body) = response.json::<serde_json::Value>().await else {
        return false;
    };
    body.get("models").is_some_and(|models| models.is_array())
}

pub(super) fn classify(
    available: bool,
    now_ms: i64,
    recovery: Option<RecoveryState>,
) -> InferenceRuntimeHealth {
    let recovery = recovery.unwrap_or_default();
    let fresh = recovery.updated_at_ms > 0
        && recovery.updated_at_ms <= now_ms + 5_000
        && now_ms.saturating_sub(recovery.updated_at_ms) <= 60_000;
    let startup_until = (recovery.startup_until * 1000.0) as i64;
    let next_retry = ((recovery.next_attempt * 1000.0) as i64).max(startup_until);
    let max_attempts = recovery.max_attempts.max(1);
    let status = if available {
        "healthy"
    } else if !fresh {
        "unavailable"
    } else if matches!(
        recovery.status.as_str(),
        "component_missing" | "port_conflict"
    ) {
        &recovery.status
    } else if recovery.status == "restart_failed" {
        if recovery.attempts >= max_attempts && next_retry > now_ms {
            "cooldown"
        } else {
            "retry_wait"
        }
    } else if recovery.status == "warming_up" {
        "starting"
    } else if recovery.attempts > 0 && startup_until > now_ms {
        "restarting"
    } else if recovery.attempts >= max_attempts && next_retry > now_ms {
        "cooldown"
    } else if next_retry > now_ms {
        "retry_wait"
    } else {
        "unavailable"
    };
    InferenceRuntimeHealth {
        available,
        checked_at_ms: now_ms,
        recovery_status: status.into(),
        recovery_stale: !fresh,
        attempts: if fresh { recovery.attempts } else { 0 },
        max_attempts: if fresh { max_attempts } else { 0 },
        next_retry_at_ms: if !available && fresh && next_retry > now_ms {
            Some(next_retry)
        } else {
            None
        },
        error_code: if available {
            None
        } else {
            Some("LOCAL_INFERENCE_UNAVAILABLE".into())
        },
    }
}

pub(super) async fn sample(
    base_url: Option<&str>,
    recovery_path: Option<&Path>,
    now_ms: i64,
) -> InferenceRuntimeHealth {
    let recovery = if let Some(path) = recovery_path {
        tokio::fs::read(path)
            .await
            .ok()
            .and_then(|bytes| serde_json::from_slice(&bytes).ok())
    } else {
        None
    };
    classify(probe(base_url).await, now_ms, recovery)
}

#[cfg(test)]
mod tests {
    use super::*;
    fn recovery(status: &str, updated_at_ms: i64, attempts: u32) -> RecoveryState {
        RecoveryState {
            status: status.into(),
            updated_at_ms,
            attempts,
            max_attempts: 3,
            next_attempt: 110.0,
            startup_until: 105.0,
        }
    }
    #[test]
    fn recovery_is_not_health_and_stale_recovery_is_not_current_progress() {
        let failed = classify(false, 100_000, Some(recovery("healthy", 100_000, 0)));
        assert!(!failed.available);
        assert_eq!(failed.recovery_status, "retry_wait");
        let healthy = classify(true, 100_000, Some(recovery("restarting", 100_000, 1)));
        assert_eq!(healthy.recovery_status, "healthy");
        assert_eq!(healthy.next_retry_at_ms, None);
        let stale = classify(false, 100_000, Some(recovery("restarting", 1, 1)));
        assert_eq!(stale.recovery_status, "unavailable");
        assert!(stale.recovery_stale);
    }
    #[test]
    fn exposes_restart_cooldown_and_actionable_blockers() {
        for (status, attempts, expected) in [
            ("restarting", 1, "restarting"),
            ("waiting", 3, "cooldown"),
            ("restart_failed", 1, "retry_wait"),
            ("component_missing", 0, "component_missing"),
            ("port_conflict", 0, "port_conflict"),
            ("warming_up", 0, "starting"),
        ] {
            let mut state = recovery(status, 100_000, attempts);
            if expected == "cooldown" {
                state.startup_until = 95.0;
            }
            assert_eq!(
                classify(false, 100_000, Some(state)).recovery_status,
                expected
            );
        }
    }
    #[test]
    fn final_attempt_is_still_starting_until_grace_expires() {
        let state = recovery("waiting", 100_000, 3);
        let result = classify(false, 100_000, Some(state));
        assert_eq!(result.recovery_status, "restarting");
        let mut state = recovery("waiting", 100_000, 1);
        state.next_attempt = 101.0;
        state.startup_until = 190.0;
        assert_eq!(
            classify(false, 100_000, Some(state)).next_retry_at_ms,
            Some(190_000)
        );
    }
    #[test]
    fn only_configured_loopback_origins_are_probed() {
        assert_eq!(
            loopback_base_url("127.0.0.1:58875"),
            Some("http://127.0.0.1:58875".into())
        );
        for url in [
            "https://127.0.0.1:11434",
            "http://example.com:11434",
            "http://127.0.0.1:11434/other",
            "http://user:pass@127.0.0.1:11434",
            "http://127.0.0.1:11434?secret=test",
        ] {
            assert_eq!(loopback_base_url(url), None);
        }
    }
    #[tokio::test]
    async fn actual_listener_then_connection_refused_overrides_prior_healthy_state() {
        use std::io::{Read, Write};
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let worker = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = [0; 1024];
            stream.read(&mut request).unwrap();
            let body = r#"{"models":[]}"#;
            write!(
                stream,
                "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                body.len(),
                body
            )
            .unwrap();
        });
        assert!(probe(Some(&url)).await);
        worker.join().unwrap();
        let failed = sample(Some(&url), None, 100_000).await;
        assert!(!failed.available);
        assert_eq!(
            failed.error_code.as_deref(),
            Some("LOCAL_INFERENCE_UNAVAILABLE")
        );
    }
}

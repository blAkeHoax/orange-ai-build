use std::sync::Arc;
use serde_json::{json, Value};
use tauri::State;
use crate::python_runtime::{HandshakeStatus, RuntimeSupervisor};

#[tauri::command]
pub async fn get_handshake_status(
    supervisor: State<'_, Arc<RuntimeSupervisor>>,
) -> Result<HandshakeStatus, String> {
    Ok(supervisor.get_status().await)
}

#[tauri::command]
pub async fn retry_handshake(
    supervisor: State<'_, Arc<RuntimeSupervisor>>,
) -> Result<HandshakeStatus, String> {
    supervisor.restart_and_handshake().await
}

#[tauri::command]
pub async fn open_data_folder() -> Result<(), String> {
    let local_app_data = std::env::var("LOCALAPPDATA").map_err(|e| e.to_string())?;
    let path = std::path::PathBuf::from(local_app_data).join("Orange AI");
    if !path.exists() {
        let _ = std::fs::create_dir_all(&path);
    }
    std::process::Command::new("explorer")
        .arg(&path)
        .spawn()
        .map_err(|e| format!("Failed to open data folder: {}", e))?;
    Ok(())
}

#[tauri::command]
pub async fn open_official_releases() -> Result<(), String> {
    let url = "https://github.com/blAkeHoax/orange-ai/releases/latest";
    std::process::Command::new("cmd")
        .args(["/C", "start", "", url])
        .spawn()
        .map_err(|e| format!("Failed to open release URL: {}", e))?;
    Ok(())
}

#[tauri::command]
pub async fn ping(supervisor: State<'_, Arc<RuntimeSupervisor>>) -> Result<Value, String> {
    let runtime = supervisor.get_runtime().await?;
    runtime.call("system.ping", json!({})).await
}

#[tauri::command]
pub async fn get_health(supervisor: State<'_, Arc<RuntimeSupervisor>>) -> Result<Value, String> {
    let runtime = supervisor.get_runtime().await?;
    runtime.call("system.health", json!({})).await
}

#[tauri::command]
pub async fn get_memory(supervisor: State<'_, Arc<RuntimeSupervisor>>) -> Result<Value, String> {
    let runtime = supervisor.get_runtime().await?;
    runtime.call("memory.stats", json!({})).await
}

#[tauri::command]
pub async fn get_jobs(supervisor: State<'_, Arc<RuntimeSupervisor>>) -> Result<Value, String> {
    let runtime = supervisor.get_runtime().await?;
    runtime.call("jobs.list", json!({})).await
}

#[tauri::command]
pub async fn send_message(
    supervisor: State<'_, Arc<RuntimeSupervisor>>,
    text: String,
) -> Result<Value, String> {
    let runtime = supervisor.get_runtime().await?;
    runtime.call("chat.send", json!({ "text": text })).await
}

#[tauri::command]
pub async fn confirm_action(
    supervisor: State<'_, Arc<RuntimeSupervisor>>,
    confirmation_id: String,
    decision: String,
) -> Result<Value, String> {
    let runtime = supervisor.get_runtime().await?;
    runtime
        .call(
            "chat.confirm",
            json!({
                "confirmation_id": confirmation_id,
                "decision": decision
            }),
        )
        .await
}

#[tauri::command]
pub async fn call_runtime(
    supervisor: State<'_, Arc<RuntimeSupervisor>>,
    method: String,
    params: Option<Value>,
) -> Result<Value, String> {
    let runtime = supervisor.get_runtime().await?;
    runtime.call(&method, params.unwrap_or(json!({}))).await
}

#[tauri::command]
pub async fn shutdown(supervisor: State<'_, Arc<RuntimeSupervisor>>) -> Result<Value, String> {
    let runtime = supervisor.get_runtime().await?;
    runtime.call("system.shutdown", json!({})).await
}

#[tauri::command]
pub async fn prepare_for_update(
    supervisor: State<'_, Arc<RuntimeSupervisor>>,
    custom_db_path: Option<String>,
) -> Result<Value, String> {
    // Security safeguard: custom_db_path is strictly forbidden in production
    if let Some(ref _custom_path) = custom_db_path {
        let test_mode = std::env::var("ORANGE_AI_TEST_MODE").unwrap_or_default().to_lowercase();
        if !matches!(test_mode.as_str(), "1" | "true" | "yes" | "test" | "dev") {
            return Err("custom_db_path is strictly forbidden in production mode".to_string());
        }
    }

    let runtime = supervisor.get_runtime().await?;

    // 1. Dispatch RPC to Python adapter
    let params = match &custom_db_path {
        Some(path) => json!({ "db_path": path }),
        None => json!({}),
    };

    let response = runtime.call("system.shutdown_for_update", params).await?;

    verify_pre_update_response(&response, custom_db_path.as_deref())
}

/// Standalone pure verification function for pre-update shutdown response and sidecars.
/// Encapsulates all safety assertions to enable direct unit testing and failure-path validation.
pub fn verify_pre_update_response(
    response: &Value,
    custom_db_path: Option<&str>,
) -> Result<Value, String> {
    // 2. Validate response status
    let status = response
        .get("status")
        .and_then(|s| s.as_str())
        .unwrap_or("");
    if status != "ready_for_update" {
        return Err(format!(
            "Pre-update shutdown returned unexpected status: '{}'",
            status
        ));
    }

    // 3. Validate checkpoint busy flag
    let busy = response
        .get("checkpoint")
        .and_then(|c| c.get("busy"))
        .and_then(|b| b.as_i64())
        .unwrap_or(-1);
    if busy != 0 {
        return Err(format!(
            "Database WAL checkpoint failed with busy status: {}",
            busy
        ));
    }

    // 4. Validate exact database path returned by Python
    let python_target_db = response
        .get("target_db")
        .and_then(|s| s.as_str())
        .ok_or_else(|| "Missing 'target_db' in Python pre-update response".to_string())?;

    let canonical_python_path = std::fs::canonicalize(python_target_db)
        .map_err(|e| format!("Failed to canonicalize Python target_db '{}': {}", python_target_db, e))?;

    // Validate path consistency between Python and expected path
    if let Some(expected_custom) = custom_db_path {
        let canonical_expected = std::fs::canonicalize(expected_custom)
            .map_err(|e| format!("Failed to canonicalize custom_db_path '{}': {}", expected_custom, e))?;
        if canonical_python_path != canonical_expected {
            return Err(format!(
                "Database path mismatch: Python checkpointed '{:?}', but expected '{:?}'",
                canonical_python_path, canonical_expected
            ));
        }
    } else {
        let local_app_data = std::env::var("LOCALAPPDATA")
            .map_err(|e| format!("LOCALAPPDATA environment variable not set: {}", e))?;
        let expected_buf = std::path::PathBuf::from(local_app_data)
            .join("Orange AI")
            .join("memory.db");
        if expected_buf.exists() {
            let canonical_expected = std::fs::canonicalize(&expected_buf)
                .map_err(|e| format!("Failed to canonicalize expected DB path: {}", e))?;
            if canonical_python_path != canonical_expected {
                return Err(format!(
                    "Database path mismatch: Python checkpointed '{:?}', but expected '{:?}'",
                    canonical_python_path, canonical_expected
                ));
            }
        }
    }

    let db_path = canonical_python_path.as_path();
    if !db_path.exists() {
        return Err(format!("Database file does not exist: {:?}", db_path));
    }

    // 5. Inspect WAL sidecar on filesystem
    let wal_path_buf = std::path::PathBuf::from(format!("{}-wal", python_target_db));
    if wal_path_buf.exists() {
        let metadata = std::fs::metadata(&wal_path_buf)
            .map_err(|e| format!("Failed to read WAL metadata: {}", e))?;
        if metadata.len() > 0 {
            return Err(format!(
                "Update aborted: database WAL sidecar is not truncated ({} bytes)",
                metadata.len()
            ));
        }
    }

    // 6. Test file access with read/write permissions
    match std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open(&db_path)
    {
        Ok(file) => drop(file),
        Err(e) => {
            return Err(format!(
                "Update aborted: cannot open database file with read/write access: {}",
                e
            ));
        }
    }

    Ok(json!({
        "status": "verified",
        "db_path": python_target_db,
        "wal_bytes": 0,
        "python_response": response
    }))
}

#[tauri::command]
pub async fn minimize_window(window: tauri::Window) -> Result<(), String> {
    window.minimize().map_err(|e| e.to_string())
}

#[tauri::command]
pub async fn toggle_maximize_window(window: tauri::Window) -> Result<(), String> {
    if window.is_maximized().unwrap_or(false) {
        window.unmaximize().map_err(|e| e.to_string())
    } else {
        window.maximize().map_err(|e| e.to_string())
    }
}

#[tauri::command]
pub async fn close_window(window: tauri::Window) -> Result<(), String> {
    window.close().map_err(|e| e.to_string())
}

#[tauri::command]
pub async fn set_window_size(window: tauri::Window, width: f64, height: f64) -> Result<(), String> {
    window
        .set_size(tauri::Size::Logical(tauri::LogicalSize { width, height }))
        .map_err(|e| e.to_string())
}

#[tauri::command]
pub async fn set_theme(
    supervisor: State<'_, Arc<RuntimeSupervisor>>,
    theme: String,
) -> Result<Value, String> {
    let runtime = supervisor.get_runtime().await?;
    runtime.call("ui.set_theme", json!({ "theme": theme })).await
}

#[tauri::command]
pub async fn toggle_drawer(
    supervisor: State<'_, Arc<RuntimeSupervisor>>,
    open: Option<bool>,
) -> Result<Value, String> {
    let runtime = supervisor.get_runtime().await?;
    runtime.call("ui.toggle_drawer", json!({ "open": open })).await
}

#[tauri::command]
pub async fn select_view(
    supervisor: State<'_, Arc<RuntimeSupervisor>>,
    view: String,
) -> Result<Value, String> {
    let runtime = supervisor.get_runtime().await?;
    runtime.call("ui.select_view", json!({ "view": view })).await
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs::{self, File};
    use std::io::Write;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn make_test_temp_dir() -> std::path::PathBuf {
        let ts = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos();
        let dir = std::env::temp_dir().join(format!("orange_ai_rust_test_{}", ts));
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn test_rust_rejection_of_unexpected_status() {
        let resp = json!({
            "status": "unexpected_error",
            "checkpoint": { "busy": 0 }
        });
        let res = verify_pre_update_response(&resp, None);
        assert!(res.is_err(), "Expected rejection of unexpected status");
        let err = res.unwrap_err();
        assert!(err.contains("unexpected status: 'unexpected_error'"));
    }

    #[test]
    fn test_rust_rejection_of_nonzero_checkpoint_busy() {
        let resp = json!({
            "status": "ready_for_update",
            "checkpoint": { "busy": 2 }
        });
        let res = verify_pre_update_response(&resp, None);
        assert!(res.is_err(), "Expected rejection of non-zero busy status");
        let err = res.unwrap_err();
        assert!(err.contains("Database WAL checkpoint failed with busy status: 2"));
    }

    #[test]
    fn test_rust_rejection_of_database_path_mismatch() {
        let dir = make_test_temp_dir();
        let db_a = dir.join("db_a.db");
        let db_b = dir.join("db_b.db");
        File::create(&db_a).unwrap();
        File::create(&db_b).unwrap();

        let resp = json!({
            "status": "ready_for_update",
            "checkpoint": { "busy": 0 },
            "target_db": db_a.to_str().unwrap()
        });

        let res = verify_pre_update_response(&resp, Some(db_b.to_str().unwrap()));
        assert!(res.is_err(), "Expected rejection of database path mismatch");
        let err = res.unwrap_err();
        assert!(err.contains("Database path mismatch"));

        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_rust_rejection_of_invalid_wal_sidecar() {
        let dir = make_test_temp_dir();
        let db = dir.join("test.db");
        let wal = dir.join("test.db-wal");
        File::create(&db).unwrap();
        let mut f_wal = File::create(&wal).unwrap();
        f_wal.write_all(&[1u8; 1024]).unwrap();

        let resp = json!({
            "status": "ready_for_update",
            "checkpoint": { "busy": 0 },
            "target_db": db.to_str().unwrap()
        });

        let res = verify_pre_update_response(&resp, Some(db.to_str().unwrap()));
        assert!(res.is_err(), "Expected rejection of non-zero WAL sidecar");
        let err = res.unwrap_err();
        assert!(err.contains("database WAL sidecar is not truncated (1024 bytes)"));

        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_rust_acceptance_of_valid_pre_update() {
        let dir = make_test_temp_dir();
        let db = dir.join("valid.db");
        File::create(&db).unwrap();

        let resp = json!({
            "status": "ready_for_update",
            "checkpoint": { "busy": 0 },
            "target_db": db.to_str().unwrap()
        });

        let res = verify_pre_update_response(&resp, Some(db.to_str().unwrap()));
        assert!(res.is_ok(), "Expected valid response to verify successfully: {:?}", res);
        let val = res.unwrap();
        assert_eq!(val.get("status").and_then(|s| s.as_str()), Some("verified"));
        assert_eq!(val.get("wal_bytes").and_then(|b| b.as_i64()), Some(0));

        let _ = fs::remove_dir_all(&dir);
    }
}

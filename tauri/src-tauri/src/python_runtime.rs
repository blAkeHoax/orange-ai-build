use std::collections::HashMap;
use std::path::PathBuf;
use std::process::Stdio;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tauri::{AppHandle, Emitter, Manager};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::{ChildStdin, Command};
use tokio::sync::{oneshot, Mutex};
use tokio::time::timeout;

use crate::job_object::SafeJobObject;

const CREATE_NO_WINDOW: u32 = 0x08000000;

pub const DESKTOP_VERSION: &str = "2.0.0";
pub const SUPPORTED_PROTOCOL_VERSION: &str = "1.0";

#[derive(Debug, Clone)]
pub enum RuntimeTarget {
    Standalone(PathBuf),
    Script { python: PathBuf, adapter: PathBuf },
}

#[derive(Debug, Serialize, Deserialize)]
pub struct JsonRpcRequest {
    pub jsonrpc: String,
    pub id: String,
    pub method: String,
    pub params: Value,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct JsonRpcResponse {
    pub jsonrpc: String,
    pub id: Option<String>,
    pub result: Option<Value>,
    pub error: Option<Value>,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct JsonRpcNotification {
    pub jsonrpc: String,
    pub method: String,
    pub params: Value,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct HandshakeSuccess {
    pub desktop_version: String,
    pub runtime_version: String,
    pub protocol_version: String,
    pub pid: u32,
    pub capabilities: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub enum HandshakeError {
    InvalidResponse(String),
    MissingVersion(String),
    IncompatibleRuntimeVersion {
        desktop_version: String,
        runtime_version: String,
    },
    UnsupportedProtocolVersion {
        expected_protocol: String,
        received_protocol: String,
    },
    NotReady(String),
}

/// Pure verification function for handshake response against version contract.
pub fn verify_handshake_response(
    response: &Value,
    expected_desktop_version: &str,
    expected_protocol_version: &str,
) -> Result<HandshakeSuccess, HandshakeError> {
    let obj = response.as_object().ok_or_else(|| {
        HandshakeError::InvalidResponse("Response is not a JSON object".to_string())
    })?;

    let status = obj.get("status").and_then(|v| v.as_str()).unwrap_or("");
    if status != "ready" && status != "ok" {
        return Err(HandshakeError::NotReady(format!(
            "Runtime reported non-ready status: '{}'",
            status
        )));
    }

    let runtime_version = obj
        .get("version")
        .and_then(|v| v.as_str())
        .ok_or_else(|| {
            HandshakeError::MissingVersion("Missing 'version' field in handshake response".to_string())
        })?;

    let protocol_version = obj
        .get("protocol_version")
        .and_then(|v| v.as_str())
        .ok_or_else(|| {
            HandshakeError::InvalidResponse(
                "Missing 'protocol_version' field in handshake response".to_string(),
            )
        })?;

    if runtime_version != expected_desktop_version {
        return Err(HandshakeError::IncompatibleRuntimeVersion {
            desktop_version: expected_desktop_version.to_string(),
            runtime_version: runtime_version.to_string(),
        });
    }

    if protocol_version != expected_protocol_version {
        return Err(HandshakeError::UnsupportedProtocolVersion {
            expected_protocol: expected_protocol_version.to_string(),
            received_protocol: protocol_version.to_string(),
        });
    }

    let pid = obj.get("pid").and_then(|v| v.as_u64()).unwrap_or(0) as u32;

    let capabilities = obj
        .get("capabilities")
        .and_then(|v| v.as_array())
        .map(|arr| {
            arr.iter()
                .filter_map(|v| v.as_str().map(|s| s.to_string()))
                .collect()
        })
        .unwrap_or_default();

    Ok(HandshakeSuccess {
        desktop_version: expected_desktop_version.to_string(),
        runtime_version: runtime_version.to_string(),
        protocol_version: protocol_version.to_string(),
        pid,
        capabilities,
    })
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "status", content = "details")]
pub enum HandshakeStatus {
    Ready {
        desktop_version: String,
        runtime_version: String,
        protocol_version: String,
        pid: u32,
        capabilities: Vec<String>,
    },
    StartupFailed {
        error: String,
    },
    Timeout {
        elapsed_ms: u64,
    },
    InvalidResponse {
        error: String,
    },
    MissingVersion {
        error: String,
    },
    IncompatibleRuntimeVersion {
        desktop_version: String,
        runtime_version: String,
    },
    UnsupportedProtocolVersion {
        expected_protocol: String,
        received_protocol: String,
    },
    ProcessTerminated {
        exit_code: Option<i32>,
        error: String,
    },
}

pub struct PythonRuntime {
    stdin: Arc<Mutex<ChildStdin>>,
    pending_requests: Arc<Mutex<HashMap<String, oneshot::Sender<Result<Value, String>>>>>,
    request_counter: AtomicU64,
    _job_object: SafeJobObject,
    pid: u32,
    child: Arc<Mutex<tokio::process::Child>>,
}

impl PythonRuntime {
    pub fn locate_runtime(app_handle: &AppHandle) -> Result<RuntimeTarget, String> {
        // 1. Check relative to current executable (installed layout: <app_dir>/resources/runtime/runtime_adapter.exe)
        if let Ok(current_exe) = std::env::current_exe() {
            if let Some(exe_dir) = current_exe.parent() {
                let candidates = [
                    exe_dir.join("resources").join("runtime").join("runtime_adapter.exe"),
                    exe_dir.join("runtime").join("runtime_adapter.exe"),
                    exe_dir.join("runtime_adapter.exe"),
                ];
                for cand in &candidates {
                    if cand.exists() {
                        eprintln!("[runtime-locator] Found standalone runtime at {:?}", cand);
                        return Ok(RuntimeTarget::Standalone(cand.clone()));
                    }
                }
            }
        }

        // 2. Check Tauri resource_dir
        if let Ok(res_dir) = app_handle.path().resource_dir() {
            let cand = res_dir.join("runtime").join("runtime_adapter.exe");
            if cand.exists() {
                eprintln!("[runtime-locator] Found standalone runtime in resource_dir at {:?}", cand);
                return Ok(RuntimeTarget::Standalone(cand));
            }
        }

        // 3. Check workspace build output (during dev release testing)
        let ws_cand = PathBuf::from(r"D:\Orange AI\Orange AI Tauri\src-tauri\resources\runtime\runtime_adapter.exe");
        if ws_cand.exists() {
            eprintln!("[runtime-locator] Found workspace bundled runtime at {:?}", ws_cand);
            return Ok(RuntimeTarget::Standalone(ws_cand));
        }

        // 4. Fallback for developer environment (source mode)
        let dev_python = PathBuf::from(r"D:\Orange AI\Orange AI Desktop\venv\Scripts\python.exe");
        let dev_adapter = PathBuf::from(r"D:\Orange AI\Orange AI Tauri\runtime\runtime_adapter.py");
        if dev_python.exists() && dev_adapter.exists() {
            eprintln!("[runtime-locator] Using development Python environment at {:?}", dev_python);
            return Ok(RuntimeTarget::Script {
                python: dev_python,
                adapter: dev_adapter,
            });
        }

        Err("Unable to locate either bundled standalone runtime_adapter.exe or development Python environment.".to_string())
    }

    pub async fn start(app_handle: AppHandle) -> Result<Arc<Self>, String> {
        let runtime_target = Self::locate_runtime(&app_handle)?;
        Self::start_with_target(app_handle, runtime_target).await
    }

    pub async fn start_with_target(app_handle: AppHandle, runtime_target: RuntimeTarget) -> Result<Arc<Self>, String> {
        // Initialize Windows Job Object for automatic orphan cleanup on termination
        let job_object = SafeJobObject::new()?;

        let mut cmd = match runtime_target {
            RuntimeTarget::Standalone(exe_path) => {
                let mut c = Command::new(&exe_path);
                c.env("PYTHONUNBUFFERED", "1")
                    .env("PYTHONIOENCODING", "utf-8")
                    .env("PYTHONUTF8", "1");
                c
            }
            RuntimeTarget::Script { python, adapter } => {
                let mut c = Command::new(&python);
                c.arg("-u")
                    .arg(&adapter)
                    .env("PYTHONUNBUFFERED", "1")
                    .env("PYTHONIOENCODING", "utf-8")
                    .env("PYTHONUTF8", "1");
                c
            }
        };

        cmd.stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .creation_flags(CREATE_NO_WINDOW);

        let mut child = cmd.spawn().map_err(|e| format!("Failed to spawn Python runtime adapter: {}", e))?;

        let pid = child.id().unwrap_or(0);

        // Assign child process handle to Job Object
        if let Some(raw_handle) = child.raw_handle() {
            let _ = job_object.assign_process(raw_handle as *mut _);
        }

        let stdin = child.stdin.take().ok_or("Failed to open child stdin pipe")?;
        let stdout = child.stdout.take().ok_or("Failed to open child stdout pipe")?;
        let stderr = child.stderr.take().ok_or("Failed to open child stderr pipe")?;

        let pending_requests: Arc<Mutex<HashMap<String, oneshot::Sender<Result<Value, String>>>>> =
            Arc::new(Mutex::new(HashMap::new()));

        let stdin_arc = Arc::new(Mutex::new(stdin));

        let runtime = Arc::new(Self {
            stdin: stdin_arc,
            pending_requests: pending_requests.clone(),
            request_counter: AtomicU64::new(1),
            _job_object: job_object,
            pid,
            child: Arc::new(Mutex::new(child)),
        });

        // Background reader for child stdout (JSON-RPC line framing)
        let pending_clone = pending_requests.clone();
        let app_handle_clone = app_handle.clone();
        tokio::spawn(async move {
            let mut reader = BufReader::new(stdout);
            let mut buf = Vec::new();
            loop {
                buf.clear();
                match reader.read_until(b'\n', &mut buf).await {
                    Ok(0) => break, // EOF reached
                    Ok(_) => {
                        let line = String::from_utf8_lossy(&buf);
                        let trimmed = line.trim();
                        if trimmed.is_empty() {
                            continue;
                        }

                        if let Ok(v) = serde_json::from_str::<Value>(trimmed) {
                            if let Some(id_val) = v.get("id").and_then(|i| i.as_str()) {
                                // Response to a request
                                let mut map = pending_clone.lock().await;
                                if let Some(tx) = map.remove(id_val) {
                                    if let Some(err) = v.get("error") {
                                        let err_msg = err.get("message").and_then(|m| m.as_str()).unwrap_or("Unknown JSON-RPC error");
                                        let _ = tx.send(Err(err_msg.to_string()));
                                    } else {
                                        let res = v.get("result").cloned().unwrap_or(Value::Null);
                                        let _ = tx.send(Ok(res));
                                    }
                                }
                            } else if let Some(method_val) = v.get("method").and_then(|m| m.as_str()) {
                                // Asynchronous notification / event from Python adapter
                                let params = v.get("params").cloned().unwrap_or(Value::Null);
                                let sanitized = method_val.replace('.', "_");
                                let _ = app_handle_clone.emit(&sanitized, params);
                            }
                        } else {
                            eprintln!("[python-stdout-unparsed] {}", trimmed);
                        }
                    }
                    Err(e) => {
                        eprintln!("[python-stdout-error] {}", e);
                        break;
                    }
                }
            }
            eprintln!("[python-stdout] EOF reached, child stdout closed.");
        });

        // Background reader for child stderr (diagnostics and desktop logs)
        tokio::spawn(async move {
            let mut reader = BufReader::new(stderr);
            let mut buf = Vec::new();
            loop {
                buf.clear();
                match reader.read_until(b'\n', &mut buf).await {
                    Ok(0) => break, // EOF
                    Ok(_) => {
                        let line = String::from_utf8_lossy(&buf);
                        let trimmed = line.trim();
                        if !trimmed.is_empty() {
                            eprintln!("[python-adapter-log] {}", trimmed);
                        }
                    }
                    Err(_) => break,
                }
            }
        });

        Ok(runtime)
    }

    pub fn pid(&self) -> u32 {
        self.pid
    }

    pub async fn terminate(&self) -> Result<(), String> {
        let mut child_guard = self.child.lock().await;
        child_guard
            .kill()
            .await
            .map_err(|e| format!("Failed to terminate child process: {}", e))
    }

    pub async fn call(&self, method: &str, params: Value) -> Result<Value, String> {
        self.call_with_timeout(method, params, Duration::from_secs(120)).await
    }

    pub async fn call_with_timeout(
        &self,
        method: &str,
        params: Value,
        timeout_duration: Duration,
    ) -> Result<Value, String> {
        let req_id = format!("req-{}", self.request_counter.fetch_add(1, Ordering::SeqCst));
        let request = JsonRpcRequest {
            jsonrpc: "2.0".to_string(),
            id: req_id.clone(),
            method: method.to_string(),
            params,
        };

        let mut payload = serde_json::to_string(&request).map_err(|e| e.to_string())?;
        payload.push('\n');

        let (tx, rx) = oneshot::channel();
        {
            let mut map = self.pending_requests.lock().await;
            map.insert(req_id.clone(), tx);
        }

        // Send line to child stdin
        {
            let mut sin = self.stdin.lock().await;
            sin.write_all(payload.as_bytes())
                .await
                .map_err(|e| format!("Failed to write to Python adapter: {}", e))?;
            sin.flush()
                .await
                .map_err(|e| format!("Failed to flush to Python adapter: {}", e))?;
        }

        // Await response with timeout
        match timeout(timeout_duration, rx).await {
            Ok(Ok(result)) => result,
            Ok(Err(_)) => {
                let mut map = self.pending_requests.lock().await;
                map.remove(&req_id);
                Err("Request dropped before response received".to_string())
            }
            Err(_) => {
                let mut map = self.pending_requests.lock().await;
                map.remove(&req_id);
                Err(format!(
                    "Request '{}' timed out after {} seconds",
                    method,
                    timeout_duration.as_secs()
                ))
            }
        }
    }
}

pub struct RuntimeSupervisor {
    runtime: Arc<Mutex<Option<Arc<PythonRuntime>>>>,
    status: Arc<Mutex<HandshakeStatus>>,
    app_handle: AppHandle,
}

impl RuntimeSupervisor {
    pub fn new(app_handle: AppHandle) -> Arc<Self> {
        Arc::new(Self {
            runtime: Arc::new(Mutex::new(None)),
            status: Arc::new(Mutex::new(HandshakeStatus::StartupFailed {
                error: "Uninitialized supervisor".to_string(),
            })),
            app_handle,
        })
    }

    pub async fn initialize(&self) -> HandshakeStatus {
        let mut rt_lock = self.runtime.lock().await;
        let mut status_lock = self.status.lock().await;

        // Clean up previous child if any
        if let Some(prev_rt) = rt_lock.take() {
            let _ = prev_rt.terminate().await;
        }

        let runtime = match PythonRuntime::start(self.app_handle.clone()).await {
            Ok(rt) => rt,
            Err(e) => {
                let s = HandshakeStatus::StartupFailed { error: e };
                *status_lock = s.clone();
                return s;
            }
        };

        // Bounded handshake with 5.0-second timeout
        let handshake_res = runtime
            .call_with_timeout("system.handshake", json!({}), Duration::from_secs(5))
            .await;

        let new_status = match handshake_res {
            Ok(val) => {
                match verify_handshake_response(&val, DESKTOP_VERSION, SUPPORTED_PROTOCOL_VERSION) {
                    Ok(success) => {
                        *rt_lock = Some(runtime);
                        HandshakeStatus::Ready {
                            desktop_version: success.desktop_version,
                            runtime_version: success.runtime_version,
                            protocol_version: success.protocol_version,
                            pid: success.pid,
                            capabilities: success.capabilities,
                        }
                    }
                    Err(HandshakeError::MissingVersion(e)) => {
                        HandshakeStatus::MissingVersion { error: e }
                    }
                    Err(HandshakeError::IncompatibleRuntimeVersion {
                        desktop_version,
                        runtime_version,
                    }) => HandshakeStatus::IncompatibleRuntimeVersion {
                        desktop_version,
                        runtime_version,
                    },
                    Err(HandshakeError::UnsupportedProtocolVersion {
                        expected_protocol,
                        received_protocol,
                    }) => HandshakeStatus::UnsupportedProtocolVersion {
                        expected_protocol,
                        received_protocol,
                    },
                    Err(HandshakeError::InvalidResponse(e))
                    | Err(HandshakeError::NotReady(e)) => {
                        HandshakeStatus::InvalidResponse { error: e }
                    }
                }
            }
            Err(e) => {
                if e.contains("timed out") {
                    HandshakeStatus::Timeout { elapsed_ms: 5000 }
                } else if e.contains("Request dropped") || e.contains("closed") {
                    HandshakeStatus::ProcessTerminated {
                        exit_code: None,
                        error: e,
                    }
                } else {
                    HandshakeStatus::InvalidResponse { error: e }
                }
            }
        };

        *status_lock = new_status.clone();
        new_status
    }

    pub async fn get_status(&self) -> HandshakeStatus {
        self.status.lock().await.clone()
    }

    pub async fn get_runtime(&self) -> Result<Arc<PythonRuntime>, String> {
        let status = self.status.lock().await.clone();
        match status {
            HandshakeStatus::Ready { .. } => {
                let rt_lock = self.runtime.lock().await;
                rt_lock
                    .clone()
                    .ok_or_else(|| "Runtime process reference missing despite Ready status".to_string())
            }
            _ => Err(format!(
                "Operation blocked: Python runtime is not ready ({:?}).",
                status
            )),
        }
    }

    pub async fn restart_and_handshake(&self) -> Result<HandshakeStatus, String> {
        Ok(self.initialize().await)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn test_handshake_success() {
        let resp = json!({
            "status": "ready",
            "runtime": "Orange AI Python Runtime",
            "version": "2.0.0",
            "protocol_version": "1.0",
            "pid": 4321,
            "capabilities": ["chat", "memory", "pre_update_quiescence"]
        });

        let res = verify_handshake_response(&resp, "2.0.0", "1.0");
        assert!(res.is_ok(), "Expected valid handshake to succeed");
        let success = res.unwrap();
        assert_eq!(success.desktop_version, "2.0.0");
        assert_eq!(success.runtime_version, "2.0.0");
        assert_eq!(success.protocol_version, "1.0");
        assert_eq!(success.pid, 4321);
        assert!(success.capabilities.contains(&"chat".to_string()));
    }

    #[test]
    fn test_handshake_invalid_response_non_object() {
        let resp = json!("just a string");
        let res = verify_handshake_response(&resp, "2.0.0", "1.0");
        assert_eq!(
            res,
            Err(HandshakeError::InvalidResponse(
                "Response is not a JSON object".to_string()
            ))
        );
    }

    #[test]
    fn test_handshake_not_ready_status() {
        let resp = json!({
            "status": "initializing",
            "version": "2.0.0",
            "protocol_version": "1.0"
        });
        let res = verify_handshake_response(&resp, "2.0.0", "1.0");
        assert_eq!(
            res,
            Err(HandshakeError::NotReady(
                "Runtime reported non-ready status: 'initializing'".to_string()
            ))
        );
    }

    #[test]
    fn test_handshake_missing_version() {
        let resp = json!({
            "status": "ready",
            "protocol_version": "1.0"
        });
        let res = verify_handshake_response(&resp, "2.0.0", "1.0");
        assert_eq!(
            res,
            Err(HandshakeError::MissingVersion(
                "Missing 'version' field in handshake response".to_string()
            ))
        );
    }

    #[test]
    fn test_handshake_missing_protocol_version() {
        let resp = json!({
            "status": "ready",
            "version": "2.0.0"
        });
        let res = verify_handshake_response(&resp, "2.0.0", "1.0");
        assert_eq!(
            res,
            Err(HandshakeError::InvalidResponse(
                "Missing 'protocol_version' field in handshake response".to_string()
            ))
        );
    }

    #[test]
    fn test_handshake_incompatible_runtime_version() {
        let resp = json!({
            "status": "ready",
            "version": "2.0.1",
            "protocol_version": "1.0"
        });
        let res = verify_handshake_response(&resp, "2.0.0", "1.0");
        assert_eq!(
            res,
            Err(HandshakeError::IncompatibleRuntimeVersion {
                desktop_version: "2.0.0".to_string(),
                runtime_version: "2.0.1".to_string(),
            })
        );
    }

    #[test]
    fn test_handshake_unsupported_protocol_version() {
        let resp = json!({
            "status": "ready",
            "version": "2.0.0",
            "protocol_version": "99.0"
        });
        let res = verify_handshake_response(&resp, "2.0.0", "1.0");
        assert_eq!(
            res,
            Err(HandshakeError::UnsupportedProtocolVersion {
                expected_protocol: "1.0".to_string(),
                received_protocol: "99.0".to_string(),
            })
        );
    }
}

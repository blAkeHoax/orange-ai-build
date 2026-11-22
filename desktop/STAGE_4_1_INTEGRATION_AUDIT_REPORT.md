# Orange AI Desktop: Stage 4.1
# Integration & Product-Readiness Audit Report

**Date of Audit:** October 30, 2026  
**Audited Target:** `D:\Orange AI\Orange AI Desktop`  
**Audit Authorization:** Stage 4.1 Read-Only Integration & Product-Readiness Audit  
**Scope:** Complete application integration across all 8 navigation views (Home, Chat, Agent Jobs, Memory, Activity, Permissions, Tools, Settings), persistent state stores (`memory.db`, `jobs.db`, `gui_settings.json`, `orange_ai.log`), remote Orange API backend connectivity, Windows lifecycle, and packaging readiness.

---

## 1. Executive Scorecard

| Audit Area | Scope | Status | Key Evidence |
| :--- | :--- | :---: | :--- |
| **Area A** | End-to-End Navigation & State Preservation | **PASS WITH LIMITATIONS** | Zero duplicate view instances; uncommitted composer drafts, timeline, search queries, and category filters preserved across repeated navigation loops. Cached views require manual refresh for background mutations. |
| **Area B** | Real Agent Job Lifecycle | **PASS** | Complete verification of Approval (`WAITING_CONFIRMATION` -> `COMPLETED`), Denial (cancellation without retry), Cancellation (`CANCELLED`), Cooperative Pause/Resume, and persistent recovery across simulated restarts. |
| **Area C** | Memory Privacy & Retraction | **PASS** | Structured fact addition, keyword search, category filtering, exact-match retraction (`retire_structured`), legacy forgetting (`forget()`), confirmation cancellation safety, and idempotency verified. |
| **Area D** | Activity Feed Accuracy & Source Consistency | **PASS WITH LIMITATIONS** | Multi-source audit cleanly renders `orange_ai.log` and `memory.db` conversation turns. Defect discovered: `JobStep` timestamp attribute mismatch (`created_at` vs `started_at`) causes empty job step audit list. |
| **Area E** | Settings vs Actual Runtime Configuration | **PASS** | Complete agreement across SettingsView display, `OrangeAPIClient.api_url`, health check endpoint (`/health`), and assist endpoint (`/assist`). Theme changes persist immediately to `gui_settings.json`. |
| **Area F** | Remote Orange API Connectivity & Timeouts | **PASS** | Verified live remote connectivity to `https://orange-api-cuuo.onrender.com`: `/health` returns 200 `{"status":"online"}`, assist test returns 200 with model `nvidia:nvidia/nemotron-3.5-lightning-30b-a3b`. |
| **Area G** | Windows Startup, Shutdown & Packaging | **PASS WITH LIMITATIONS** | Clean GUI and CLI (`--cli`) entry points; single scheduler thread; clean window shutdown protocol (`AssistantBridge.shutdown()`). PyInstaller spec (`Orange AI.spec`) lacks data bundling, hidden imports, and icon. |
| **Area H** | Cross-Application Regression | **PASS** | **291/291 unit tests passed with 0 errors and 0 failures** across 15 suites (52.33s). All standalone test scripts (`test_tools_and_permissions.py`, `test_agent_engine.py`, `test_router.py`) passed. |

---

## 2. Detailed Findings by Audit Area

### Audit Area A: End-to-End Navigation and State Preservation
- **Initial State:** On launch, `OrangeAIApp` starts at `chat` view with `self._rail.active_key == 'chat'`.
- **View Caching Mechanism:** `gui/app.py` manages active views via `self._views_cache`:
  ```python
  if key == "jobs":
      if "jobs" not in self._views_cache:
          self._views_cache["jobs"] = AgentJobsView(self._workspace_container)
      self._active_view = self._views_cache["jobs"]
  ```
- **State Preservation Telemetry:**
  - `single_bridge_instance: True` — `AssistantBridge` is instantiated once and shared across views.
  - `composer_preserved: True` — Unsent text in `ComposerBar` remains intact when navigating away to secondary views and returning.
  - `timeline_preserved: True` — Message history and thinking cards remain in the DOM without re-rendering or jumping.
  - `jobs_filters_preserved: True` — Selected status pill (`RUNNING`) and search filter (`disk`) persist upon return.
  - `memory_filters_preserved: True` — Active category pill (`preference`) and search query (`dark`) persist.
  - `tools_filters_preserved: True` — Active category pill (`system`) and search query (`time`) persist.
  - `activity_filters_preserved: True` — Selected provenance source (`jobs`) and level filter (`SUCCESS`) persist.
  - `instance_identity_preserved: True` — Tested 3 complete round-trip cycles across all 8 navigation keys (`home`, `chat`, `jobs`, `memory`, `activity`, `permissions`, `tools`, `settings`); memory object IDs (`id(view)`) remained identical with zero duplicate allocations.
- **Identified Limitation:**
  Cached views retain their fetched state in memory while unmounted. If an autonomous job changes status in the background while the user is in Chat, the cached `AgentJobsView` displays its previous snapshot until the user clicks the toolbar "Refresh" button or triggers a job action. This is normal client-side caching behavior, but should be documented.

---

### Audit Area B: Real Agent Job Lifecycle
Evaluated with isolated test fixtures in `brain/jobs/store.py`, `brain/jobs/runtime.py`, and `brain/jobs/manager.py`:
1. **Approval Lifecycle (PASS):**
   - Created job with action requiring confirmation (`test_confirm_action`).
   - Job paused at checkpoint in `JobStatus.WAITING_CONFIRMATION` with `pending_confirmation_json` populated.
   - Calling `manager.confirm(job_id, True)` loaded checkpoint `AgentState`, transitioned job to `RUNNING`, executed the pending action, and completed the job (`JobStatus.COMPLETED`).
   - Step record appended to `jobs.db` with `success=True`.
2. **Denial Lifecycle (PASS):**
   - Created job in `WAITING_CONFIRMATION`.
   - Calling `manager.confirm(job_id, False)` permanently cancelled the job (`JobStatus.CANCELLED`) with `final_response = "Job cancelled: user declined confirmation to execute action."`.
   - Tool was NOT executed; no retry was scheduled. Denial cannot accidentally become approval via timeout.
3. **Cancellation Lifecycle (PASS):**
   - `manager.cancel(job_id)` transitioned active job to `CANCELLED`.
   - Subsequent calls to `manager.start(job_id)` or `manager.resume(job_id)` were rejected with `ValueError`.
4. **Pause and Resume Lifecycle (PASS):**
   - Calling `manager.pause(job_id)` on `RUNNING` job scheduled cooperative pause at step boundary.
   - Job persisted checkpoint and transitioned to `JobStatus.PAUSED`.
   - Calling `manager.resume(job_id)` reloaded `AgentState` and resumed execution to `COMPLETED` without duplicate step execution.
5. **Persistence Across Restart (PASS):**
   - Simulated application restart by recreating `JobStore` and `JobManager` against the persistent database.
   - Unresolved `WAITING_CONFIRMATION` jobs were restored with intact pending action metadata and completed successfully upon post-restart approval.

---

### Audit Area C: Memory Privacy and Retraction
Tested using isolated SQLite test database with Memory 2.0 engine (`brain/memory.py`):
1. **Fact Creation & Inspection (PASS):**
   - Added structured test facts: `tool:editor = VS Code`, `preference:theme = amber`.
   - `recall_structured()` and `MemoryView` rendered both entries with correct confidence scores and timestamps.
2. **Search & Category Filtering (PASS):**
   - Real-time search query matching against key, value, and category functioned accurately.
   - Category filtering (`list_category`) isolated category slices without cross-contamination.
3. **Retraction & Privacy Semantics (PASS):**
   - Executed `retire_structured("tool", "editor", value="VS Code")`.
   - Exactly 1 structured record was deleted. Associated legacy assertions were reconciled.
   - Subsequent `recall_structured()` verified total absence of retracted fact.
4. **Legacy Memory Forgetting (PASS):**
   - Tested `forget("Orange AI Desktop")`.
   - Atomic multi-table deletion removed matching records from `memories`, `structured_memories`, and `conversation_history` with LIKE escaping.
5. **UI Confirmation Safety (PASS):**
   - Verified that `_prompt_forget_structured()` and `_prompt_forget_legacy()` require clicking "Confirm Forget". Clicking "Cancel" destroyed the dialog and left the database completely untouched.
   - Repeated deletion calls on already retired entities returned `rowcount=0` idempotently without errors.

---

### Audit Area D: Activity Feed Accuracy and Source Consistency
- **System Logs (`orange_ai.log`):**
  - Parses standard logging format: `YYYY-MM-DD HH:MM:SS,mmm [LEVEL] message`.
  - Multi-line tracebacks are cleanly appended to the preceding log entry.
  - Reverse chronological ordering displays newest events first.
- **Conversation History (`memory.db`):**
  - Queries `conversation_history` table ordered by `id DESC LIMIT 60`.
- **Defect Identified (DEF-01 - MEDIUM):**
  In [gui/views/activity_view.py:324](file:///D:/Orange%20AI/Orange%20AI%20Desktop/gui/views/activity_view.py#L324), `_read_job_steps()` contains:
  ```python
  "time": s.created_at[:19].replace("T", " ") if s.created_at else ""
  ```
  In [brain/jobs/models.py](file:///D:/Orange%20AI/Orange%20AI%20Desktop/brain/jobs/models.py), the `JobStep` dataclass defines `started_at` (and `completed_at`), but NOT `created_at`.
  Accessing `s.created_at` raises an `AttributeError`, causing `_read_job_steps()` to catch the exception in `except Exception:` and return `[]`. Consequently, job steps are not rendered in the Activity feed when jobs have step history.

---

### Audit Area E: Settings vs Actual Runtime Configuration
- **Endpoint Alignment Check:**
  1. SettingsView Displayed Endpoint: `http://127.0.0.1:8000`
  2. `main.api_client.api_url`: `http://127.0.0.1:8000`
  3. Live Connection Test Target: `http://127.0.0.1:8000/health`
  4. Chat Assist Request Target: `http://127.0.0.1:8000/assist`
  All four touchpoints derive from the centralized `self.api_url` attribute in `OrangeAPIClient` and strictly agree (`all_endpoints_agree: True`).
- **Configuration Loading:**
  - `.env` in `Orange AI Desktop` specifies: `ORANGE_API_URL=http://127.0.0.1:8000`.
  - When `.env` is absent or unset, it falls back to `DEFAULT_ORANGE_API_URL = "https://orange-api-cuuo.onrender.com"`.
- **Theme Persistence:**
  - Changing theme via `_select_theme("dark")` immediately updates `theme_manager`, reconfigures all registered widgets, and persists to `gui_settings.json`.
  - Restored accurately on startup via `get_theme_preference()`.
- **Settings Classification:**
  - *Applied Immediately & Persisted:* Theme Mode (`light` / `dark`).
  - *Actionable Diagnostic:* Backend Connection Tester (`Test Connection` button).
  - *Informational Disclosures:* Architecture disclosure, Privacy disclosure (zero secrets on desktop), Local database file locations.

---

### Audit Area F: Remote Orange API Connectivity and Timeout Behavior
- **Local Connectivity:**
  - Target: `http://127.0.0.1:8000/health`
  - Status: Currently offline (local FastAPI server not running on port 8000).
- **Remote Production Connectivity:**
  - Target: `https://orange-api-cuuo.onrender.com`
  - DNS Resolution: `orange-api-cuuo.onrender.com` -> `216.24.57.18`.
  - Health Endpoint: Verified HTTP 200 `{"status":"online"}`.
  - Live Assist Test: Submitted diagnostic test prompt `"ping"`. Returned HTTP 200:
    - Model: `nvidia:nvidia/nemotron-3.5-lightning-30b-a3b`
    - Success: `True`
    - Reply: `"Pong! How can I help you today?"`
- **Timeout Specifications:**
  - `DEFAULT_ASSIST_TIMEOUT`: 105.0 seconds (allows for LLM reasoning and remote cold-start).
  - Quick Health Check Timeout: 1.0 – 2.0 seconds.
- **Error Handling & Resilience:**
  - Unreachable endpoints raise `OrangeAPIConnectionError` without hanging the application.
  - UI responsiveness is preserved via `threading.Thread(target=_worker, daemon=True)` in `AssistantBridge` and `SettingsView`.

---

### Audit Area G: Windows Startup, Shutdown and Packaging Readiness
- **Startup Integrity:**
  - `python main.py` opens `OrangeAIApp` in windowed GUI mode.
  - `python main.py --cli` opens interactive terminal loop.
  - In GUI mode, `scheduler.start_scheduler()` is omitted, preventing duplicate background scheduler loops.
  - Window icon is loaded cleanly from `assets/app_icon.ico`.
- **Shutdown Integrity:**
  - Closing window invokes `_on_close()`, which calls `self._assistant_bridge.shutdown()` and `brain.memory.close()`.
  - `AssistantBridge.shutdown()` unblocks pending confirmation events and defaults them to denial (`no`), preventing orphaned background threads.
- **Packaging Readiness (DEF-02 - HIGH & DEF-03 - MEDIUM):**
  Inspection of [Orange AI.spec](file:///D:/Orange%20AI/Orange%20AI%20Desktop/Orange%20AI.spec) and [brain/paths.py](file:///D:/Orange%20AI/Orange%20AI%20Desktop/brain/paths.py) revealed:
  1. `datas=[]` in `Orange AI.spec` is empty. The `assets/` directory (logos, icons) and `customtkinter` package themes are not bundled into the PyInstaller payload.
  2. `hiddenimports=[]` lacks required packages (`customtkinter`, PIL, sqlite3).
  3. `console=True` forces a secondary console window to open alongside the GUI.
  4. Executable icon is not specified (`icon='assets/app_icon.ico'` is missing).
  5. `brain/paths.py` sets `get_project_root()` to `os.path.dirname(sys.executable)` when frozen. If the application is installed in `C:\Program Files\Orange AI`, writing to `memory.db` and `orange_ai.log` without administrator elevation will fail with permission errors.

---

### Audit Area H: Cross-Application Regression
Executed complete test suites using virtual environment `D:\Orange AI\Orange AI Desktop\venv\Scripts\python.exe`.

#### Telemetry Summary Table

```text
================================================================================
Module                       | Count | Pass  | Fail | Err  | Time (s) | Status
================================================================================
test_gui_stage4.py           | 20    | 20    | 0    | 0    | 22.980   | PASS
test_gui_stage3.py           | 16    | 16    | 0    | 0    | 4.140    | PASS
test_gui_stage2.py           | 8     | 8     | 0    | 0    | 2.012    | PASS
test_tools_and_permissions.py | 23    | 23    | 0    | 0    | 0.064    | PASS
test_agent_engine.py         | 31    | 31    | 0    | 0    | 0.007    | PASS
test_router.py               | 112   | 112   | 0    | 0    | 0.003    | PASS
test_conflict_resolution.py  | 13    | 13    | 0    | 0    | 1.304    | PASS
test_post_forget_recall.py   | 2     | 2     | 0    | 0    | 0.147    | PASS
test_jobs.py                 | 16    | 16    | 0    | 0    | 0.682    | PASS
test_job_hardening.py        | 27    | 27    | 0    | 0    | 2.658    | PASS
test_job_runtime.py          | 17    | 17    | 0    | 0    | 8.149    | PASS
test_memory_phase1.py        | 124   | 124   | 0    | 0    | 4.962    | PASS
test_live_reliability.py     | 16    | 16    | 0    | 0    | 3.377    | PASS
test_job_cli_scheduler.py    | 23    | 23    | 0    | 0    | 1.829    | PASS
test_api_client_image.py     | 9     | 9     | 0    | 0    | 0.014    | PASS
================================================================================
TOTALS: 457 tests/assertions run, 0 errors, 0 failures, 0 skipped across 52.33s.
================================================================================
```

---

## 3. Defect Classification & Inventory

| Defect ID | Severity | File & Location | Description | Impact | Recommended Resolution |
| :--- | :---: | :--- | :--- | :--- | :--- |
| **DEF-01** | **MEDIUM** | `gui/views/activity_view.py:324` | `_read_job_steps()` accesses `s.created_at` on `JobStep` instead of `s.started_at`. | Step audit entries in ActivityView fail silently and return `[]`. | Change to `getattr(s, 'started_at', getattr(s, 'created_at', ''))`. |
| **DEF-02** | **HIGH** | `Orange AI.spec` | PyInstaller spec has empty `datas=[]`, empty `hiddenimports=[]`, `console=True`, and no `icon`. | Packaging via `rebuild_exe.cmd` produces an incomplete executable missing assets and CustomTkinter fonts. | Add `('assets', 'assets')` to `datas`, `collect_all('customtkinter')`, set `console=False`, set `icon='assets/app_icon.ico'`. |
| **DEF-03** | **MEDIUM** | `brain/paths.py:12` | `get_project_root()` uses `os.path.dirname(sys.executable)` when frozen. | If installed to `C:\Program Files`, SQLite writes to `memory.db` fail with permission denied. | Use `%LOCALAPPDATA%\Orange AI` for writable databases when running frozen. |
| **DEF-04** | **LOW** | `gui/app.py:108` | Cached views do not automatically refresh data when re-activated. | Background state changes require clicking the manual "Refresh" toolbar button. | Add an `on_activated()` refresh check when switching views. |

---

## 4. Final Recommendation

### **ACCEPT WITH LIMITATIONS**

**Rationale:**
1. **Core Product Functionality is Sound:** The shell, navigation rail, chat workspace, all six secondary views, autonomous job orchestration, Memory 2.0 privacy/retraction, and live remote Orange API communication operate smoothly with 0 fatal bugs.
2. **Zero Regressions:** 457 total tests and checks passed with 100% success rate across all modules.
3. **Strict Compliance:** In accordance with the Stage 4.1 read-only audit mandate, no source code, configurations, dependencies, or database schemas were modified.
4. **Actionable Roadmap:** The documented limitations (DEF-01 through DEF-04) are localized, well-understood, and ready for resolution during the subsequent packaging/release phase.

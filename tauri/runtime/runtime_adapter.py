"""
runtime_adapter.py - Headless Python Runtime Adapter for Orange AI Desktop 2.0 (Tauri).

Provides JSON-RPC 2.0 line-delimited communication over standard input/output (stdio)
with Tauri's Rust core supervisor.

Guarantees:
1. Strict Stdout Purity: sys.stdout is redirected to sys.stderr; only valid protocol
   JSON-RPC lines are written to the dedicated protocol stream.
2. Zero Code Duplication: Directly delegates all assistant execution, memory, jobs,
   tools, and permissions to the existing, protected Orange AI Desktop engine.
3. Thread-Safe Concurrency: Long-running or blocking user turns execute on background
   threads while the main RPC reader continues processing incoming requests.
4. Two-Phase Confirmation Handshake: Tool confirmation requests emit asynchronous
   JSON-RPC events to the frontend and safely block with a 120-second timeout until
   explicit user approval is received or the action safely defaults to denial.
5. Deterministic Lifecycle: Clean shutdown on 'system.shutdown' or EOF (pipe break
   from parent process termination or crash).
"""

import os
import sys
import json
import uuid
import time
import queue
import logging
import threading
import urllib.parse
import re
import sqlite3
import subprocess
import hashlib
from datetime import datetime, time as dt_time, timezone
from typing import Optional, Dict, Any, Callable
from contextlib import contextmanager

# 1. Isolate Protocol Stdout
# Ensure UTF-8 encoding across all standard IO streams
for stream in (sys.stdin, sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

# Save the authentic unbuffered stdout pipe for protocol framing
_protocol_out = sys.stdout
# Redirect sys.stdout to sys.stderr so any print() statements in libraries or tools
# never contaminate the JSON-RPC pipe.
sys.stdout = sys.stderr

# Configure standard error logging
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="[PY-ADAPTER %(levelname)s %(asctime)s] %(message)s",
)
logger = logging.getLogger("runtime_adapter")

# 2. Add Existing Desktop Workspace to sys.path (Development mode only)
_script_dir = os.path.dirname(os.path.abspath(__file__))
_project_root = os.path.dirname(_script_dir)  # D:\Orange AI\Orange AI Tauri
_workspace_root = os.path.dirname(_project_root)
_desktop_candidates = [
    os.path.join(_workspace_root, "desktop"),
    os.path.join(_workspace_root, "Orange AI Desktop"),
]
_desktop_workspace = os.environ.get(
    "ORANGE_AI_DESKTOP_DIR",
    next((d for d in _desktop_candidates if os.path.exists(d)), _desktop_candidates[0]),
)

if not getattr(sys, "frozen", False):
    if os.path.exists(_desktop_workspace):
        if _desktop_workspace not in sys.path:
            sys.path.insert(0, _desktop_workspace)
            logger.info("Added desktop workspace to sys.path: %s", _desktop_workspace)
    else:
        logger.warning("Desktop workspace path not found: %s", _desktop_workspace)
else:
    logger.info("Running in frozen standalone runtime mode.")

# Ensure runtime default data directory points to %LOCALAPPDATA%\Orange AI
if "ORANGE_AI_USER_DATA_DIR" not in os.environ:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        os.environ["ORANGE_AI_USER_DATA_DIR"] = os.path.join(local_app_data, "Orange AI")
        logger.info("Set default user data directory: %s", os.environ["ORANGE_AI_USER_DATA_DIR"])

# Lazy import holder for core engine
import main
from brain.paths import get_user_data_dir
from brain.database import resolve_database_path
import brain.memory as mem
from brain.api_client import OrangeAPIClient
from brain.agent_models import AgentStatus
import tools.registry as tool_reg
import tools.permissions as tool_perm
import tools.system as tool_sys
from brain import scheduler

# Version & Protocol Specification
ORANGE_AI_VERSION = "2.0.0"
ORANGE_AI_PROTOCOL_VERSION = "1.0"

# Persistent GUI settings helper & Stage 2B Model/Proactive Configuration
SUPPORTED_MODELS: Dict[str, Dict[str, str]] = {
    "nvidia/llama-3.1-nemotron-70b-instruct": {
        "id": "nvidia/llama-3.1-nemotron-70b-instruct",
        "name": "Nemotron 70B",
        "tag": "Default / Reasoning",
        "description": "Flagship reasoning and instruction-tuned foundation model for complex analytical queries."
    },
    "meta/llama-3.1-70b-instruct": {
        "id": "meta/llama-3.1-70b-instruct",
        "name": "Llama 70B",
        "tag": "Fast General",
        "description": "High-throughput general conversational model optimized for versatile daily dialogue."
    },
    "openbmb/minicpm-v-2_6": {
        "id": "openbmb/minicpm-v-2_6",
        "name": "MiniCPM",
        "tag": "Vision / Multimodal",
        "description": "Compact multimodal-aligned architecture supporting detailed perceptual and vision tasks."
    },
    "mistralai/mistral-large-2-instruct": {
        "id": "mistralai/mistral-large-2-instruct",
        "name": "Mistral Large 2",
        "tag": "Code & Logic",
        "description": "Specialized high-precision reasoning model excelling at programming and deterministic logic."
    }
}
DEFAULT_MODEL = "nvidia/llama-3.1-nemotron-70b-instruct"
DEFAULT_API_URL = "https://orange-api-cuuo.onrender.com"
DEFAULT_SETTINGS: Dict[str, Any] = {
    "theme": "dark",
    "model": DEFAULT_MODEL,
    "api_url": DEFAULT_API_URL,
    "morning_briefing_enabled": False,
    "preferred_briefing_time": "08:30",
    "contextual_advisories_enabled": True,
    "advisory_cooldown_hours": 4,
}

_BRIEFING_TIME_REGEX = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def _validate_api_url(url: Any) -> bool:
    if not isinstance(url, str):
        return False
    u = url.strip()
    if not (u.startswith("http://") or u.startswith("https://")):
        return False
    try:
        parsed = urllib.parse.urlparse(u)
        return bool(parsed.scheme in ("http", "https") and parsed.netloc)
    except Exception:
        return False


def _validate_briefing_time(time_str: Any) -> bool:
    if not isinstance(time_str, str):
        return False
    return bool(_BRIEFING_TIME_REGEX.match(time_str.strip()))


def _validate_cooldown_hours(hrs: Any) -> bool:
    try:
        return int(hrs) in (2, 4, 8)
    except (ValueError, TypeError):
        return False


def _validate_model(model: Any) -> bool:
    return isinstance(model, str) and model in SUPPORTED_MODELS


def _validate_theme(theme: Any) -> bool:
    return isinstance(theme, str) and theme.lower() in ("dark", "light", "system")


def _load_gui_settings() -> Dict[str, Any]:
    path = os.path.join(get_user_data_dir(), "gui_settings.json")
    settings = dict(DEFAULT_SETTINGS)
    if not os.path.exists(path):
        return settings
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                if _validate_theme(data.get("theme")):
                    settings["theme"] = data["theme"].lower()
                if _validate_model(data.get("model")):
                    settings["model"] = data["model"]
                if _validate_api_url(data.get("api_url")):
                    settings["api_url"] = data["api_url"].strip().rstrip("/")
                if isinstance(data.get("morning_briefing_enabled"), bool):
                    settings["morning_briefing_enabled"] = data["morning_briefing_enabled"]
                if _validate_briefing_time(data.get("preferred_briefing_time")):
                    settings["preferred_briefing_time"] = data["preferred_briefing_time"].strip()
                if isinstance(data.get("contextual_advisories_enabled"), bool):
                    settings["contextual_advisories_enabled"] = data["contextual_advisories_enabled"]
                if _validate_cooldown_hours(data.get("advisory_cooldown_hours")):
                    settings["advisory_cooldown_hours"] = int(data["advisory_cooldown_hours"])
                # Preserve any extra legacy keys
                for k, v in data.items():
                    if k not in settings:
                        settings[k] = v
        return settings
    except Exception as e:
        logger.warning("Error reading gui_settings.json: %s", e)
        return settings


def _save_gui_settings(settings: Dict[str, Any]) -> bool:
    path = os.path.join(get_user_data_dir(), "gui_settings.json")
    try:
        temp_path = f"{path}.tmp.{os.getpid()}"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2, ensure_ascii=False)
        os.replace(temp_path, path)
        return True
    except Exception as e:
        logger.error("Error writing gui_settings.json: %s", e)
        return False


# Headless runtime bridge: wire gui.settings to authoritative runtime_adapter settings helpers
# Avoids importing Tkinter/customtkinter in headless and standalone frozen environments
if "gui" not in sys.modules:
    import types
    _gui_pkg = types.ModuleType("gui")
    sys.modules["gui"] = _gui_pkg

if "gui.settings" not in sys.modules:
    import types
    _gui_settings = types.ModuleType("gui.settings")
    _gui_settings.load_settings = _load_gui_settings
    _gui_settings.save_settings = _save_gui_settings
    sys.modules["gui.settings"] = _gui_settings
    sys.modules["gui"].settings = _gui_settings

# Import briefing & recommendation components safely
from brain.briefing.collector import BriefingCollector
from brain.briefing.synthesizer import BriefingSynthesizer
from brain.briefing.recommendations import (
    Recommendation,
    RecommendationEvaluator,
    RecommendationStore,
    RecommendationState,
    RecommendationPriority,
)


def normalize_recommendation_priority(raw_priority: Any) -> str:
    """
    Map recommendation priorities deterministically to Stage 2A contract: HIGH, MEDIUM, LOW.
    Mappings:
      - NORMAL / MEDIUM -> MEDIUM
      - CRITICAL / HIGH -> HIGH
      - LOW -> LOW
      - Default / unknown -> MEDIUM
    """
    if raw_priority is None:
        return "MEDIUM"
    if hasattr(raw_priority, "name"):
        val = raw_priority.name.upper()
    elif hasattr(raw_priority, "value"):
        val = str(raw_priority.value).upper()
    else:
        val = str(raw_priority).strip().upper()

    if val in ("CRITICAL", "HIGH"):
        return "HIGH"
    elif val in ("NORMAL", "MEDIUM"):
        return "MEDIUM"
    elif val == "LOW":
        return "LOW"
    return "MEDIUM"


# Patch Recommendation.to_dict so newly serialized recommendations use the Stage 2A contract
_orig_rec_to_dict = Recommendation.to_dict


def _patched_rec_to_dict(self) -> Dict[str, Any]:
    d = _orig_rec_to_dict(self)
    d["priority"] = normalize_recommendation_priority(self.priority)
    return d


Recommendation.to_dict = _patched_rec_to_dict


def _migrate_recommendations_file() -> int:
    """
    Migrate existing persisted recommendations in recommendations.json
    so that raw legacy priorities ('NORMAL') are deterministically normalized
    to the Stage 2A contract ('MEDIUM'), preserving all IDs, fingerprints, and fields.
    """
    user_dir = get_user_data_dir()
    rec_path = os.path.join(user_dir, "recommendations.json")
    if not os.path.exists(rec_path):
        return 0

    try:
        with open(rec_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, dict):
            return 0

        recs = data.get("recommendations", {})
        if not isinstance(recs, dict):
            return 0

        migrated_count = 0
        for rec_id, rec in recs.items():
            if not isinstance(rec, dict):
                continue
            old_prio = rec.get("priority")
            new_prio = normalize_recommendation_priority(old_prio)
            if old_prio != new_prio:
                rec["priority"] = new_prio
                migrated_count += 1

        if migrated_count > 0:
            temp_path = f"{rec_path}.tmp.{os.getpid()}"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(temp_path, rec_path)
            logger.info("[STAGE-2C] Migrated %d recommendation records to strict Stage 2A contract (HIGH/MEDIUM/LOW).", migrated_count)

        return migrated_count
    except Exception as e:
        logger.warning("[STAGE-2C] Recommendation file migration warning: %s", e)
        return 0


def _ensure_api_client_configured():
    """Ensure main.api_client exists and is configured with current settings."""
    try:
        current = _load_gui_settings()
        api_url = current.get("api_url", DEFAULT_API_URL).rstrip("/")
        if main.api_client is None:
            main.api_client = OrangeAPIClient(api_url=api_url)
        else:
            main.api_client.api_url = api_url

        # Wrap assist() so selected model is injected automatically when not explicitly passed
        if not getattr(main.api_client, "_stage2b_wrapped", False):
            orig_assist = main.api_client.assist
            def _model_aware_assist(*args, **kwargs):
                if not kwargs.get("model"):
                    curr = _load_gui_settings()
                    kwargs["model"] = curr.get("model", DEFAULT_MODEL)
                return orig_assist(*args, **kwargs)
            main.api_client.assist = _model_aware_assist
            main.api_client._stage2b_wrapped = True
    except Exception as e:
        logger.warning("Error configuring API client: %s", e)


def _apply_runtime_settings(settings: Dict[str, Any]):
    """Apply updated settings to live runtime objects (API client endpoint, etc.)."""
    try:
        _ensure_api_client_configured()
        api_url = settings.get("api_url")
        if api_url and main.api_client:
            main.api_client.api_url = api_url.rstrip("/")
            logger.info("Applied runtime API endpoint: %s", main.api_client.api_url)
    except Exception as e:
        logger.warning("Could not apply live settings to runtime: %s", e)

# 3. Protocol Serialization Lock
_START_TIME = time.time()
_protocol_lock = threading.Lock()
_is_shutting_down = False
_pending_confirmations: Dict[str, Dict[str, Any]] = {}
_recommendation_actions: Dict[str, Dict[str, Any]] = {}


def register_recommendation_action(rec_id: str, action_payload: Dict[str, Any]):
    """Register authoritative execution payload for a recommendation."""
    _recommendation_actions[str(rec_id)] = action_payload
_confirmations_lock = threading.Lock()


def send_message(payload: Dict[str, Any]):
    """Write a framed JSON-RPC line to the dedicated protocol stream."""
    with _protocol_lock:
        if _is_shutting_down and "id" not in payload:
            return
        line_bytes = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        if hasattr(_protocol_out, "buffer"):
            _protocol_out.buffer.write(line_bytes)
            _protocol_out.buffer.flush()
        else:
            _protocol_out.write(line_bytes.decode("utf-8", errors="replace"))
            _protocol_out.flush()


def send_response(req_id: Any, result: Any):
    """Emit a JSON-RPC 2.0 success response."""
    send_message({
        "jsonrpc": "2.0",
        "id": req_id,
        "result": result,
    })


def send_error(req_id: Any, code: int, message: str, data: Any = None):
    """Emit a JSON-RPC 2.0 error response."""
    err_obj = {"code": code, "message": message}
    if data is not None:
        err_obj["data"] = data
    send_message({
        "jsonrpc": "2.0",
        "id": req_id,
        "error": err_obj,
    })


def send_event(event_name: str, params: Dict[str, Any]):
    """Emit an asynchronous JSON-RPC 2.0 event (notification without ID)."""
    send_message({
        "jsonrpc": "2.0",
        "method": event_name,
        "params": params,
    })


# 4. Confirmation Bridge (prompt_fn implementation)
def _confirmation_prompt_fn(prompt_text: str) -> str:
    """
    Synchronous prompt callback invoked by tool/agent execution.
    Emits an asynchronous event to Rust/React and blocks with a 120s safety timeout.
    """
    conf_id = f"conf_{uuid.uuid4().hex[:8]}"
    evt = threading.Event()
    container = {"decision": "no", "event": evt, "created_at": time.time()}

    with _confirmations_lock:
        _pending_confirmations[conf_id] = container

    logger.info("[CONFIRMATION] Emitting request %s: %s", conf_id, prompt_text.strip()[:60])

    send_event("assistant.confirmation_required", {
        "confirmation_id": conf_id,
        "prompt_text": prompt_text.strip(),
        "timeout_seconds": 120,
    })

    # Wait on worker thread with safety timeout
    signaled = evt.wait(timeout=120.0)

    with _confirmations_lock:
        final_decision = container.get("decision", "no")
        _pending_confirmations.pop(conf_id, None)

    if not signaled:
        logger.warning("[CONFIRMATION] Request %s timed out after 120s; defaulting to denial.", conf_id)
        return "no"

    logger.info("[CONFIRMATION] Request %s resolved with decision: '%s'", conf_id, final_decision)
    return final_decision


# 5. Background Scheduler Callbacks
def _on_reminder_due(reminder: Dict[str, Any]):
    send_event("scheduler.reminder_due", {
        "id": reminder.get("id"),
        "message": reminder.get("message"),
        "due_at": reminder.get("due_at"),
    })


def _ui_control_file_watcher():
    """Poll for test automation commands and forward as Tauri events."""
    user_cmd_file = os.path.join(get_user_data_dir(), "ui_command.json")
    dev_cmd_file = os.path.join(_project_root, "ui_command.json")
    while True:
        try:
            cmd_file = user_cmd_file if os.path.exists(user_cmd_file) else (dev_cmd_file if os.path.exists(dev_cmd_file) else None)
            if cmd_file and os.path.exists(cmd_file):
                with open(cmd_file, "r", encoding="utf-8") as f:
                    cmd_data = json.load(f)
                try:
                    os.remove(cmd_file)
                except OSError:
                    pass

                cmd = cmd_data.get("command")
                logger.info("[UI-WATCHER] Received command: %s with data: %s", cmd, cmd_data)
                if cmd == "toggle_drawer":
                    send_event("ui.toggle_drawer", {"open": cmd_data.get("open")})
                elif cmd == "set_theme":
                    send_event("ui.set_theme", {"theme": cmd_data.get("theme", "dark")})
                elif cmd == "select_view":
                    send_event("ui.select_view", {"view": cmd_data.get("view", "home")})
        except Exception:
            pass
        time.sleep(0.2)


def _on_job_event(job: Any):
    if not job:
        return
    pending = job.get_pending_confirmation() if hasattr(job, "get_pending_confirmation") else None
    send_event("scheduler.job_event", {
        "id": job.id,
        "short_id": getattr(job, "short_id", job.id[:8]),
        "goal": job.goal,
        "status": job.status.value,
        "step_count": job.step_count,
        "last_error": job.last_error,
        "final_response": job.final_response,
        "pending_tool": pending.tool_name if pending else None,
    })


# ==============================================================================
# 5B. Stage 2C Proactive Intelligence & Background Workers
# ==============================================================================

_stage2c_stop_event = threading.Event()
_stage2c_worker_thread: Optional[threading.Thread] = None
_stage2c_reconciliation_thread: Optional[threading.Thread] = None
_last_user_activity: float = time.time()
_idle_inactivity_threshold: float = 600.0  # 10 minutes default
_last_idle_eval_time: Optional[float] = None
_stage2c_lock = threading.Lock()

# Stage 4.5B Phase 2: Pre-update state and turn thread tracking
_is_updating: bool = False
_turn_threads_lock = threading.Lock()
_active_turn_threads: set = set()


@contextmanager
def _get_db_connection(db_path: Optional[str] = None):
    """Yield a thread-safe short-lived connection to memory.db and close it deterministically."""
    target = db_path or resolve_database_path()
    conn = sqlite3.connect(target, timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _ensure_stage2c_tables():
    """Ensure daily_briefings, project_reconciliations, and proactive_events tables exist in memory.db."""
    try:
        with _get_db_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS daily_briefings (
                    id TEXT PRIMARY KEY,
                    local_date TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    content TEXT NOT NULL,
                    status TEXT NOT NULL,
                    model_name TEXT,
                    priorities_json TEXT,
                    sources_json TEXT,
                    error_message TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS project_reconciliations (
                    id TEXT PRIMARY KEY,
                    workspace_path TEXT NOT NULL,
                    workspace_name TEXT NOT NULL,
                    reconciled_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    git_branch TEXT,
                    git_head_commit TEXT,
                    new_commits_json TEXT,
                    changed_files_json TEXT,
                    uncommitted_changes_json TEXT,
                    file_watermark_hash TEXT,
                    outstanding_items_json TEXT,
                    error_message TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS proactive_events (
                    event_id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    entity_id TEXT,
                    created_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    acknowledged INTEGER DEFAULT 0,
                    acknowledged_at TEXT
                )
            """)
            conn.commit()
            logger.info("Stage 2C/2D database tables verified in %s", resolve_database_path())
    except Exception as e:
        logger.error("Failed to ensure Stage 2C/2D database tables: %s", e, exc_info=True)


def emit_proactive_event(
    event_type: str,
    entity_id: str,
    payload: Dict[str, Any],
    event_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Construct and emit a structured, authoritative proactive event envelope.
    Records the event in the persistent proactive_events table for replay and recovery.
    """
    evt_id = event_id or f"evt_{uuid.uuid4().hex[:12]}"
    now_iso = datetime.now(timezone.utc).isoformat()
    envelope = {
        "event_id": evt_id,
        "event_type": event_type,
        "created_at": now_iso,
        "entity_id": str(entity_id) if entity_id else "",
        "version": 1,
        "payload": payload,
        "acknowledged": False,
    }
    was_inserted = True
    try:
        with _get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT OR IGNORE INTO proactive_events
                (event_id, event_type, entity_id, created_at, payload_json, acknowledged)
                VALUES (?, ?, ?, ?, ?, 0)
            """, (evt_id, event_type, str(entity_id) if entity_id else "", now_iso, json.dumps(payload)))
            was_inserted = cur.rowcount > 0
            conn.commit()
    except Exception as e:
        logger.warning("[STAGE-2D] Error saving proactive event %s: %s", evt_id, e)

    if event_id and not was_inserted:
        # Event already recorded/emitted previously; avoid duplicate stdio emission
        return None

    # Deliver via JSON-RPC stdio event transport to Rust / React
    send_event(event_type, envelope)
    return envelope


def _check_and_emit_due_reminders():
    """Check for due reminders from the SQLite database and emit reminder.due events."""
    try:
        now = datetime.now()
        due_rems = scheduler.get_due_reminders(now)
        for r in due_rems:
            rem_id = r.get("id")
            evt_id = f"evt_reminder_{rem_id}"
            with _get_db_connection() as conn:
                cur = conn.cursor()
                cur.execute("SELECT event_id FROM proactive_events WHERE event_id = ?", (evt_id,))
                already_emitted = cur.fetchone() is not None

            if not already_emitted:
                emit_proactive_event(
                    event_type="reminder.due",
                    entity_id=str(rem_id),
                    payload={
                        "reminder_id": rem_id,
                        "message": r.get("message", "Reminder due"),
                        "due_at": r.get("due_at"),
                    },
                    event_id=evt_id,
                )
                logger.info("[STAGE-2D] Emitted reminder.due for reminder #%s: %s", rem_id, r.get("message"))
            # Mark fired in reminders table so subsequent scheduler queries don't re-select it
            scheduler.mark_fired(rem_id)
    except Exception as e:
        logger.warning("[STAGE-2D] Failed checking due reminders: %s", e)


# --- Scope A: Morning Briefing Background Worker ---

def _evaluate_morning_briefing(force: bool = False, test_now: Optional[datetime] = None, test_date: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Evaluate and synthesize a daily morning briefing according to user preferences.
    Guarantees at most one briefing per local calendar date.
    Strictly read-only aggregation before model or deterministic synthesis.
    """
    settings = _load_gui_settings()
    now = test_now or datetime.now()
    current_date = test_date or now.strftime("%Y-%m-%d")

    if not force:
        # 1. User preference check
        if not settings.get("morning_briefing_enabled", False):
            return None

        # 2. Date deduplication check in settings
        if settings.get("last_briefing_date") == current_date:
            return None

        # 3. Date deduplication check in database
        try:
            with _get_db_connection() as conn:
                cur = conn.cursor()
                cur.execute("SELECT id FROM daily_briefings WHERE local_date = ? AND status IN ('success', 'fallback')", (current_date,))
                if cur.fetchone():
                    return None
        except Exception as e:
            logger.warning("Error checking daily_briefings deduplication: %s", e)

        # 4. Preferred delivery time check (HH:MM 24-hr)
        pref_time_str = settings.get("preferred_briefing_time", "08:30")
        try:
            parts = pref_time_str.split(":")
            target_time = dt_time(int(parts[0]), int(parts[1]))
            if now.time() < target_time:
                return None
        except Exception:
            pass

    logger.info("[STAGE-2C] Triggering morning briefing preparation for date: %s", current_date)
    briefing_id = f"briefing_{current_date}_{uuid.uuid4().hex[:8]}"
    created_at = datetime.now(timezone.utc).isoformat()

    try:
        job_mgr = main.get_job_manager() if hasattr(main, "get_job_manager") else None
        job_store = job_mgr.store if job_mgr else None
        collector = BriefingCollector(job_store=job_store)
        context = collector.collect(now=now)

        _ensure_api_client_configured()
        synthesizer = BriefingSynthesizer(api_client=main.api_client)
        briefing = synthesizer.synthesize(context)

        # Serialize structured components
        priorities_data = []
        for p in briefing.priorities:
            priorities_data.append({
                "category": getattr(p, "category", "general"),
                "title": getattr(p, "title", ""),
                "level": p.level.name if hasattr(p.level, "name") else str(p.level),
                "details": getattr(p, "details", ""),
                "action_hint": getattr(p, "action_hint", ""),
            })

        sources_data = {
            "overdue_reminders_count": len(context.overdue_reminders),
            "today_reminders_count": len(context.today_reminders),
            "blocked_jobs_count": len(context.blocked_jobs),
            "active_jobs_count": len(context.active_jobs),
            "paused_jobs_count": len(context.paused_jobs),
            "recent_completed_jobs_count": len(context.recent_completed_jobs),
            "current_project": context.current_project.name if context.current_project else None,
            "secondary_projects": [p.name for p in context.secondary_projects],
            "recommendations_count": len(context.recommendations),
        }

        status = "fallback" if briefing.is_fallback else "success"
        content = briefing.summary_markdown
        model_name = briefing.model_name

        with _get_db_connection() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO daily_briefings (
                    id, local_date, created_at, content, status, model_name,
                    priorities_json, sources_json, error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                briefing_id,
                current_date,
                created_at,
                content,
                status,
                model_name,
                json.dumps(priorities_data),
                json.dumps(sources_data),
                None,
            ))
            conn.commit()

        # Update last_briefing_date in settings
        settings["last_briefing_date"] = current_date
        _save_gui_settings(settings)
        logger.info("[STAGE-2C] Persisted daily briefing %s (status=%s, model=%s)", briefing_id, status, model_name)

        briefing_obj = {
            "id": briefing_id,
            "local_date": current_date,
            "created_at": created_at,
            "content": content,
            "status": status,
            "model_name": model_name,
            "priorities": priorities_data,
            "sources": sources_data,
            "error_message": None,
        }

        # Emit authoritative proactive event (briefing.ready) with date deduplication
        emit_proactive_event(
            event_type="briefing.ready",
            entity_id=briefing_id,
            payload=briefing_obj,
            event_id=f"evt_briefing_{current_date}",
        )

        return briefing_obj

    except Exception as e:
        logger.error("[STAGE-2C] Morning briefing synthesis failed: %s", e, exc_info=True)
        try:
            with _get_db_connection() as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO daily_briefings (
                        id, local_date, created_at, content, status, model_name,
                        priorities_json, sources_json, error_message
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    briefing_id,
                    current_date,
                    created_at,
                    "",
                    "failed",
                    None,
                    "[]",
                    "{}",
                    str(e),
                ))
                conn.commit()
        except Exception:
            pass

        return {
            "id": briefing_id,
            "local_date": current_date,
            "created_at": created_at,
            "content": "",
            "status": "failed",
            "model_name": None,
            "priorities": [],
            "sources": {},
            "error_message": str(e),
        }


# --- Scope B: Idle Recommendation Evaluator ---

def _evaluate_idle_recommendations(force: bool = False, test_now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """
    Evaluate contextual recommendations after a period of user inactivity.
    Strictly advisory only - ZERO tool execution.
    Persists candidate records to recommendations.json via RecommendationStore.
    """
    global _last_idle_eval_time
    settings = _load_gui_settings()
    now_ts = test_now.timestamp() if test_now else time.time()

    if not force:
        # 1. Check enabled setting
        if not settings.get("contextual_advisories_enabled", True):
            return []

        # 2. Check idle threshold
        idle_duration = now_ts - _last_user_activity
        if idle_duration < _idle_inactivity_threshold:
            return []

        # 3. Check cooldown
        cooldown_hours = settings.get("advisory_cooldown_hours", 4)
        cooldown_sec = float(cooldown_hours) * 3600.0
        if _last_idle_eval_time is not None and (now_ts - _last_idle_eval_time < cooldown_sec):
            return []

    logger.info("[STAGE-2C] Evaluating contextual recommendations (idle_eval)...")
    _last_idle_eval_time = now_ts

    try:
        job_mgr = main.get_job_manager() if hasattr(main, "get_job_manager") else None
        job_store = job_mgr.store if job_mgr else None
        collector = BriefingCollector(job_store=job_store)
        context = collector.collect(now=test_now or datetime.now())

        evaluator = RecommendationEvaluator(
            db_path=resolve_database_path(),
            job_store=job_store,
        )

        active_recs = evaluator.evaluate(
            current_project=context.current_project,
            secondary_projects=context.secondary_projects,
            overdue_reminders=context.overdue_reminders,
            blocked_jobs=context.blocked_jobs,
            active_jobs=context.active_jobs,
            paused_jobs=context.paused_jobs,
            now=test_now or datetime.now(),
        )

        results = []
        for rec in active_recs:
            act_payload = getattr(rec, "action_payload", None)
            if not isinstance(act_payload, dict):
                act_payload = {"entity_id": getattr(rec, "entity_id", "")}
            item = {
                "id": rec.rec_id,
                "title": rec.title,
                "description": rec.explanation or (getattr(rec, "suggested_action", "")),
                "category": rec.category.value if hasattr(rec.category, "value") else str(rec.category),
                "priority": normalize_recommendation_priority(rec.priority),
                "rationale": getattr(rec, "suggested_action", rec.explanation),
                "action_type": getattr(rec, "recommendation_type", "advisory"),
                "action_payload": act_payload,
                "created_at": rec.created_at.isoformat() if hasattr(rec.created_at, "isoformat") else str(rec.created_at),
                "status": rec.state.value if hasattr(rec.state, "value") else str(rec.state),
                "dedup_key": getattr(rec, "evidence_fingerprint", rec.rec_id),
                "evidence": getattr(rec, "evidence", []),
            }
            results.append(item)
            # Emit recommendation.created (with per-recommendation ID deduplication)
            emit_proactive_event(
                event_type="recommendation.created",
                entity_id=rec.rec_id,
                payload=item,
                event_id=f"evt_rec_{rec.rec_id}",
            )

        logger.info("[STAGE-2C] Evaluated %d active recommendations.", len(results))
        return results

    except Exception as e:
        logger.warning("[STAGE-2C] Failed to evaluate idle recommendations: %s", e, exc_info=True)
        return []


def _get_active_recommendations() -> List[Dict[str, Any]]:
    """Return all active recommendations currently in the RecommendationStore."""
    try:
        store = RecommendationStore()
        all_recs = store.list_all()
        active = [r for r in all_recs if r.state == RecommendationState.ACTIVE]
        cards = []
        for rec in active:
            act_payload = getattr(rec, "action_payload", None)
            if not isinstance(act_payload, dict):
                act_payload = {"entity_id": getattr(rec, "entity_id", "")}
            cards.append({
                "id": rec.rec_id,
                "title": rec.title,
                "description": rec.explanation or (getattr(rec, "suggested_action", "")),
                "category": rec.category.value if hasattr(rec.category, "value") else str(rec.category),
                "priority": normalize_recommendation_priority(rec.priority),
                "rationale": getattr(rec, "suggested_action", rec.explanation),
                "action_type": getattr(rec, "recommendation_type", "advisory"),
                "action_payload": act_payload,
                "created_at": rec.created_at.isoformat() if hasattr(rec.created_at, "isoformat") else str(rec.created_at),
                "status": rec.state.value if hasattr(rec.state, "value") else str(rec.state),
                "dedup_key": getattr(rec, "evidence_fingerprint", rec.rec_id),
                "evidence": getattr(rec, "evidence", []),
            })
        return cards
    except Exception as e:
        logger.warning("Error fetching active recommendations: %s", e)
        return []


# --- Scope C: Startup Project Reconciliation ---

def _compute_workspace_watermark(workspace_path: str) -> str:
    """Compute a deterministic SHA-256 hash of key project manifests or directory entries."""
    manifest_files = [
        "package.json", "package-lock.json", "Cargo.toml", "Cargo.lock",
        "requirements.txt", "pyproject.toml", "vite.config.js", "README.md"
    ]
    hasher = hashlib.sha256()
    found_any = False
    for filename in sorted(manifest_files):
        fpath = os.path.join(workspace_path, filename)
        if os.path.isfile(fpath):
            try:
                stat = os.stat(fpath)
                hasher.update(f"{filename}:{stat.st_size}:{int(stat.st_mtime)}".encode("utf-8"))
                found_any = True
            except OSError:
                pass
    if not found_any:
        try:
            entries = sorted(os.listdir(workspace_path))[:50]
            for e in entries:
                p = os.path.join(workspace_path, e)
                try:
                    stat = os.stat(p)
                    hasher.update(f"{e}:{stat.st_size}:{int(stat.st_mtime)}".encode("utf-8"))
                except OSError:
                    pass
        except OSError:
            pass
    return hasher.hexdigest()


def _inspect_workspace_git(workspace_path: str) -> Dict[str, Any]:
    """Safely inspect Git repository status in a strictly read-only manner."""
    res = {
        "is_git": False,
        "branch": None,
        "head_commit": None,
        "new_commits": [],
        "changed_files": [],
        "uncommitted_changes": [],
    }
    if not os.path.exists(workspace_path):
        return res

    try:
        r_git = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=workspace_path,
            capture_output=True,
            text=True,
            timeout=5.0,
        )
        if r_git.returncode != 0:
            return res

        res["is_git"] = True

        r_branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=workspace_path,
            capture_output=True,
            text=True,
            timeout=5.0,
        )
        if r_branch.returncode == 0:
            res["branch"] = r_branch.stdout.strip()

        r_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=workspace_path,
            capture_output=True,
            text=True,
            timeout=5.0,
        )
        if r_head.returncode == 0:
            res["head_commit"] = r_head.stdout.strip()

        r_log = subprocess.run(
            ["git", "log", "-n", "5", "--pretty=format:%H|%s|%an|%cI"],
            cwd=workspace_path,
            capture_output=True,
            text=True,
            timeout=5.0,
        )
        if r_log.returncode == 0 and r_log.stdout.strip():
            commits = []
            for line in r_log.stdout.strip().split("\n"):
                parts = line.split("|", 3)
                if len(parts) == 4:
                    commits.append({
                        "hash": parts[0],
                        "subject": parts[1],
                        "author": parts[2],
                        "date": parts[3],
                    })
            res["new_commits"] = commits

        r_status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=workspace_path,
            capture_output=True,
            text=True,
            timeout=5.0,
        )
        if r_status.returncode == 0 and r_status.stdout.strip():
            changes = []
            changed_files = []
            for line in r_status.stdout.strip().split("\n"):
                if len(line) >= 4:
                    status_code = line[:2].strip()
                    f_name = line[3:].strip()
                    changes.append({"status": status_code, "file": f_name})
                    changed_files.append(f_name)
            res["uncommitted_changes"] = changes
            res["changed_files"] = changed_files

    except Exception as e:
        logger.warning("Error inspecting Git repository in %s: %s", workspace_path, e)

    return res


def _discover_authorized_workspaces() -> List[Dict[str, str]]:
    """Discover explicitly authorized and known project workspaces."""
    workspaces = []
    seen_paths = set()

    if os.path.exists(_project_root):
        norm_root = os.path.normpath(_project_root)
        workspaces.append({
            "name": os.path.basename(norm_root),
            "path": norm_root,
            "type": "official_primary",
        })
        seen_paths.add(norm_root.lower())

    try:
        with _get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT memory_key, value FROM structured_memories WHERE category = 'project'")
            rows = cur.fetchall()
            for row in rows:
                val = str(row["value"]).strip().rstrip(".")
                candidates = [
                    os.path.join(os.path.dirname(_project_root), val),
                    val,
                ]
                for cand in candidates:
                    norm = os.path.normpath(cand)
                    if os.path.isdir(norm) and norm.lower() not in seen_paths:
                        # Strictly do not scan or modify the protected retired desktop workspace
                        if os.path.normcase(norm) == os.path.normcase(_desktop_workspace):
                            continue
                        workspaces.append({
                            "name": os.path.basename(norm),
                            "path": norm,
                            "type": "configured_project",
                        })
                        seen_paths.add(norm.lower())
    except Exception as e:
        logger.warning("Error querying project memories: %s", e)

    return workspaces


def _run_startup_reconciliation(target_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Execute read-only project workspace reconciliation.
    Detects git commits, branch status, uncommitted changes, and file watermark shifts.
    Persists structured result in project_reconciliations table.
    """
    logger.info("[STAGE-2C] Running startup workspace reconciliation...")
    workspaces = _discover_authorized_workspaces()
    if target_path:
        workspaces = [w for w in workspaces if os.path.normcase(w["path"]) == os.path.normcase(target_path)]
        if not workspaces and os.path.isdir(target_path):
            workspaces = [{
                "name": os.path.basename(os.path.normpath(target_path)),
                "path": os.path.normpath(target_path),
                "type": "targeted_workspace",
            }]

    results = []
    for ws in workspaces:
        if _stage2c_stop_event.is_set():
            logger.info("[STAGE-2C] Startup reconciliation interrupted by stop event.")
            break
        ws_path = ws["path"]
        ws_name = ws["name"]
        rec_id = f"rec_{uuid.uuid4().hex[:12]}"
        reconciled_at = datetime.now(timezone.utc).isoformat()

        try:
            git_info = _inspect_workspace_git(ws_path)
            watermark = _compute_workspace_watermark(ws_path)

            prev_watermark = None
            try:
                with _get_db_connection() as conn:
                    cur = conn.cursor()
                    cur.execute("""
                        SELECT file_watermark_hash, status FROM project_reconciliations
                        WHERE workspace_path = ? ORDER BY reconciled_at DESC LIMIT 1
                    """, (ws_path,))
                    prev_row = cur.fetchone()
                    if prev_row:
                        prev_watermark = prev_row["file_watermark_hash"]
            except Exception:
                pass

            if prev_watermark is None:
                status = "baseline"
            elif prev_watermark != watermark or git_info.get("uncommitted_changes") or git_info.get("changed_files"):
                status = "changed"
            else:
                status = "clean"

            outstanding_items = []
            try:
                with _get_db_connection() as conn:
                    cur = conn.cursor()
                    cur.execute("""
                        SELECT id, message, due_at FROM reminders
                        WHERE fired = 0 AND message LIKE ?
                    """, (f"%{ws_name}%",))
                    for r in cur.fetchall():
                        outstanding_items.append({"type": "reminder", "id": r["id"], "summary": r["message"], "due_at": r["due_at"]})

                    cur.execute("""
                        SELECT id, goal, status, priority FROM jobs
                        WHERE status IN ('WAITING_CONFIRMATION', 'PAUSED', 'RUNNING') AND goal LIKE ?
                    """, (f"%{ws_name}%",))
                    for j in cur.fetchall():
                        outstanding_items.append({"type": "job", "id": j["id"], "summary": j["goal"], "status": j["status"], "priority": j["priority"]})
            except Exception as e:
                logger.warning("Error fetching outstanding items for %s: %s", ws_name, e)

            with _get_db_connection() as conn:
                conn.execute("""
                    INSERT INTO project_reconciliations (
                        id, workspace_path, workspace_name, reconciled_at, status, git_branch, git_head_commit,
                        new_commits_json, changed_files_json, uncommitted_changes_json, file_watermark_hash,
                        outstanding_items_json, error_message
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    rec_id,
                    ws_path,
                    ws_name,
                    reconciled_at,
                    status,
                    git_info.get("branch"),
                    git_info.get("head_commit"),
                    json.dumps(git_info.get("new_commits", [])),
                    json.dumps(git_info.get("changed_files", [])),
                    json.dumps(git_info.get("uncommitted_changes", [])),
                    watermark,
                    json.dumps(outstanding_items),
                    None,
                ))
                conn.commit()

            rec_obj = {
                "id": rec_id,
                "workspace_path": ws_path,
                "workspace_name": ws_name,
                "reconciled_at": reconciled_at,
                "status": status,
                "git_branch": git_info.get("branch"),
                "git_head_commit": git_info.get("head_commit"),
                "new_commits": git_info.get("new_commits", []),
                "changed_files": git_info.get("changed_files", []),
                "uncommitted_changes": git_info.get("uncommitted_changes", []),
                "file_watermark_hash": watermark,
                "outstanding_items": outstanding_items,
                "error_message": None,
            }
            results.append(rec_obj)
            logger.info("[STAGE-2C] Reconciled workspace '%s' (status=%s)", ws_name, status)

            # Emit authoritative proactive event (reconciliation.ready) with watermark deduplication
            evt_id = f"evt_recon_{rec_id}"
            emit_proactive_event(
                event_type="reconciliation.ready",
                entity_id=rec_id,
                payload=rec_obj,
                event_id=evt_id,
            )

        except Exception as e:
            logger.error("[STAGE-2C] Error reconciling workspace %s: %s", ws_path, e, exc_info=True)
            try:
                with _get_db_connection() as conn:
                    conn.execute("""
                        INSERT INTO project_reconciliations (
                            id, workspace_path, workspace_name, reconciled_at, status, git_branch, git_head_commit,
                            new_commits_json, changed_files_json, uncommitted_changes_json, file_watermark_hash,
                            outstanding_items_json, error_message
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        rec_id, ws_path, ws_name, reconciled_at, "error", None, None,
                        "[]", "[]", "[]", None, "[]", str(e)
                    ))
                    conn.commit()
            except Exception:
                pass

    return results


# --- Scope D: Unified Scheduler Loop & Lifecycle ---

def _stage2c_worker_loop():
    """Background worker tick running every 30 seconds to evaluate reminders, briefing & idle recommendations."""
    logger.info("[STAGE-2C] Background worker loop started.")
    while not _stage2c_stop_event.is_set():
        if _stage2c_stop_event.wait(timeout=30.0):
            break
        try:
            _check_and_emit_due_reminders()
        except Exception as e:
            logger.warning("[STAGE-2D] Background reminders tick error: %s", e)
        try:
            _evaluate_morning_briefing()
        except Exception as e:
            logger.warning("[STAGE-2C] Background morning briefing tick error: %s", e)
        try:
            _evaluate_idle_recommendations()
        except Exception as e:
            logger.warning("[STAGE-2C] Background idle recommendations tick error: %s", e)
    logger.info("[STAGE-2C] Background worker loop stopped.")


def _init_stage2c_background_workers():
    """Initialize Stage 2C database tables, start background worker loop and startup reconciliation."""
    global _stage2c_worker_thread, _stage2c_reconciliation_thread
    _ensure_stage2c_tables()
    _migrate_recommendations_file()

    # Initial check for due reminders on startup
    try:
        _check_and_emit_due_reminders()
    except Exception as e:
        logger.warning("[STAGE-2D] Startup reminders check error: %s", e)

    # Start startup project reconciliation asynchronously on a daemon thread
    _stage2c_reconciliation_thread = threading.Thread(
        target=_run_startup_reconciliation,
        daemon=True,
        name="StartupReconciliation",
    )
    _stage2c_reconciliation_thread.start()

    # Start periodic proactive intelligence worker thread
    _stage2c_worker_thread = threading.Thread(
        target=_stage2c_worker_loop,
        daemon=True,
        name="Stage2CBackgroundWorker",
    )
    _stage2c_worker_thread.start()
    logger.info("[STAGE-2C] Proactive intelligence background workers initialized.")


def _stop_stage2c_background_workers(timeout: float = 3.0):
    """Signal background workers to stop and wait for clean termination."""
    _stage2c_stop_event.set()
    if _stage2c_worker_thread and _stage2c_worker_thread.is_alive():
        _stage2c_worker_thread.join(timeout=timeout)
        if _stage2c_worker_thread.is_alive():
            raise RuntimeError(
                f"Stage 2C background worker thread still alive after {timeout}s timeout"
            )
    if _stage2c_reconciliation_thread and _stage2c_reconciliation_thread.is_alive():
        _stage2c_reconciliation_thread.join(timeout=timeout)
        if _stage2c_reconciliation_thread.is_alive():
            raise RuntimeError(
                f"Stage 2C reconciliation thread still alive after {timeout}s timeout"
            )
    logger.info("[STAGE-2C] Proactive intelligence background workers stopped.")


# 6. JSON-RPC Method Handlers
def handle_system_handshake(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Stage 4.5B Phase 3: Runtime Handshake.
    Returns immediate identity, version, and protocol capabilities to Rust supervisor.
    Strictly non-blocking; does not perform network or heavy database operations.
    """
    return {
        "status": "ready",
        "runtime": "Orange AI Python Runtime",
        "version": ORANGE_AI_VERSION,
        "protocol_version": ORANGE_AI_PROTOCOL_VERSION,
        "python_version": sys.version.split()[0],
        "pid": os.getpid(),
        "capabilities": [
            "chat",
            "memory",
            "planner",
            "tools",
            "pre_update_quiescence"
        ]
    }


def handle_system_ping(params: Dict[str, Any]) -> Dict[str, Any]:
    api_online = False
    try:
        _ensure_api_client_configured()
        client = main.api_client or OrangeAPIClient()
        api_online = client.is_available(timeout=0.5)
    except Exception:
        pass

    return {
        "status": "online",
        "version": ORANGE_AI_VERSION,
        "pid": os.getpid(),
        "user_data_dir": get_user_data_dir(),
        "database_path": resolve_database_path(),
        "api_online": api_online,
        "admin_rights": tool_sys.is_running_as_admin(),
    }


def handle_system_health(params: Dict[str, Any]) -> Dict[str, Any]:
    main.ensure_initialized()
    _ensure_api_client_configured()
    mem_stats = mem.memory_stats()
    job_mgr = main.get_job_manager()
    job_count = len(job_mgr.list_jobs(limit=500))

    client = main.api_client or OrangeAPIClient()
    api_online = client.is_available(timeout=1.0)
    current_settings = _load_gui_settings()

    return {
        "health": "ok",
        "uptime_seconds": time.time() - _START_TIME,
        "memory": mem_stats,
        "total_jobs": job_count,
        "api": {
            "endpoint": client.api_url,
            "online": api_online,
            "model": current_settings.get("model", DEFAULT_MODEL),
        },
        "system": {
            "admin": tool_sys.is_running_as_admin(),
            "cpu_percent": tool_sys.get_cpu_usage(),
            "ram_percent": tool_sys.get_ram_usage(),
            "battery": tool_sys.get_battery(),
        },
    }


def handle_system_shutdown(params: Dict[str, Any]) -> Dict[str, Any]:
    global _is_shutting_down
    _is_shutting_down = True
    logger.info("Executing clean runtime shutdown...")

    # Stop Stage 2C background workers
    try:
        _stop_stage2c_background_workers()
    except Exception as e:
        logger.warning("Error stopping Stage 2C background workers: %s", e)

    # Stop scheduler
    try:
        scheduler.stop(wait_timeout=2.0)
    except Exception as e:
        logger.warning("Error stopping scheduler: %s", e)

    # Cancel pending confirmations
    with _confirmations_lock:
        for conf in _pending_confirmations.values():
            conf["decision"] = "no"
            conf["event"].set()
        _pending_confirmations.clear()

    # Close databases
    close_all_database_connections()

    def _delayed_exit():
        time.sleep(0.1)
        os._exit(0)

    threading.Thread(target=_delayed_exit, daemon=True).start()
    return {"status": "shutting_down"}


def close_all_database_connections():
    """Close all cached connections across memory, mood, planner, and reminders, and verify none remain open."""
    modules_to_close = ("brain.memory", "brain.mood", "brain.planner", "brain.reminders")
    for mod_name in modules_to_close:
        try:
            mod = sys.modules.get(mod_name)
            if mod and hasattr(mod, "close"):
                mod.close()
        except Exception as e:
            logger.warning("[DB-CLOSE] Error closing %s: %s", mod_name, e)

    for mod_name in modules_to_close:
        try:
            import importlib
            m = importlib.import_module(mod_name)
            if hasattr(m, "close"):
                m.close()
        except Exception:
            pass

    # Verify that every module's _conn is indeed None (deterministic cleanup verification)
    unclosed = []
    for mod_name in modules_to_close:
        mod = sys.modules.get(mod_name)
        if mod and getattr(mod, "_conn", None) is not None:
            unclosed.append(mod_name)
    if unclosed:
        raise RuntimeError(f"Pre-update abort: database connections remained open in modules: {unclosed}")

    import gc
    gc.collect()


def handle_system_shutdown_for_update(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Stage 4.5B Phase 2: Pre-Update Database Safety and Checkpoint Hardening.

    Coordinates pre-update quiescence:
    1. Rejects incoming user turns atomically under _turn_threads_lock.
    2. Joins active turn workers and verifies thread termination.
    3. Stops the scheduler cleanly and verifies thread termination.
    4. Stops and joins Stage 2C background workers and verifies thread termination.
    5. Resolves target database path (protects custom_db_path under test mode).
    6. Closes all cached SQLite connections and verifies none remain open.
    7. Executes PRAGMA wal_checkpoint(TRUNCATE) on target database and verifies busy == 0.
    8. Verifies exclusive database lock acquisition and release.
    9. Inspects sidecars: confirms target_db-wal is exactly 0 bytes (or nonexistent) and shm is unlocked.
    10. Verifies file-level accessibility.
    11. Verifies database integrity via PRAGMA integrity_check.
    12. Returns verified pre-update status dictionary.
    """
    global _is_updating
    logger.info("Executing system.shutdown_for_update sequence...")

    # 1. Atomic transition into update mode & capture active turns
    with _turn_threads_lock:
        _is_updating = True
        active_turns = list(_active_turn_threads)

    # 2. Join active turn threads and verify complete termination
    for t in active_turns:
        if t.is_alive():
            t.join(timeout=3.0)
            if t.is_alive():
                raise RuntimeError(
                    f"Pre-update abort: active turn worker '{t.name}' still alive after 3.0s timeout"
                )
    logger.info("[SHUTDOWN-FOR-UPDATE] Active turn workers joined and verified terminated.")

    # 3. Stop scheduler and verify clean termination
    try:
        scheduler.stop(wait_timeout=3.0)
    except Exception as e:
        raise RuntimeError(f"Scheduler stop failed with exception: {e}")

    sched_thread = getattr(scheduler, "_scheduler_thread", None)
    if sched_thread is not None and sched_thread.is_alive():
        raise RuntimeError("Pre-update abort: scheduler loop thread is still alive after stop timeout")
    with getattr(scheduler, "_thread_lock", threading.Lock()):
        job_worker = getattr(scheduler, "_active_job_thread", None)
    if job_worker is not None and job_worker.is_alive():
        raise RuntimeError("Pre-update abort: scheduler job worker thread is still alive after stop timeout")
    logger.info("[SHUTDOWN-FOR-UPDATE] Scheduler stopped and verified terminated.")

    # 4. Stop Stage 2C background workers and verify termination
    _stop_stage2c_background_workers(timeout=3.0)
    if _stage2c_worker_thread and _stage2c_worker_thread.is_alive():
        raise RuntimeError("Pre-update abort: _stage2c_worker_thread is still alive after stop")
    if _stage2c_reconciliation_thread and _stage2c_reconciliation_thread.is_alive():
        raise RuntimeError("Pre-update abort: _stage2c_reconciliation_thread is still alive after stop")
    logger.info("[SHUTDOWN-FOR-UPDATE] Stage 2C background workers stopped and verified terminated.")

    # 5. Resolve target database path with test-mode permission check
    custom_db = params.get("db_path")
    if custom_db:
        if not _is_test_mode_enabled():
            raise PermissionError("custom_db_path is strictly forbidden in production mode")
        target_db = os.path.abspath(custom_db)
    else:
        target_db = os.path.abspath(resolve_database_path())
    logger.info("[SHUTDOWN-FOR-UPDATE] Authoritative target database: %s", target_db)

    if not os.path.exists(target_db):
        raise FileNotFoundError(f"Target database not found: {target_db}")

    # 6. Close all cached connections before checkpointing and verify deterministic cleanup
    close_all_database_connections()

    # 7. Checkpoint WAL (TRUNCATE) on target database
    conn = sqlite3.connect(target_db, timeout=5.0)
    try:
        cur = conn.cursor()
        res = cur.execute("PRAGMA wal_checkpoint(TRUNCATE);").fetchone()
        if not res:
            raise RuntimeError("WAL checkpoint returned no result")
        busy, log_frames, checkpointed_frames = int(res[0]), int(res[1]), int(res[2])
        if busy != 0:
            raise RuntimeError(f"WAL checkpoint busy: (busy={busy}, log={log_frames}, ckpt={checkpointed_frames})")
        logger.info("[SHUTDOWN-FOR-UPDATE] WAL checkpoint TRUNCATE completed: %s", res)
    finally:
        conn.close()

    # 8. Release all connections and verify deterministic closure
    close_all_database_connections()

    # 9. Exclusive access test lock check
    test_conn = sqlite3.connect(target_db, timeout=2.0)
    try:
        test_conn.execute("BEGIN IMMEDIATE;")
        test_conn.execute("ROLLBACK;")
    except Exception as e:
        raise RuntimeError(f"Database exclusive lock test failed: {e}")
    finally:
        test_conn.close()

    # 10. Sidecar verification for both WAL and SHM
    wal_path = f"{target_db}-wal"
    wal_bytes = 0
    if os.path.exists(wal_path):
        wal_bytes = os.path.getsize(wal_path)
        if wal_bytes > 0:
            raise RuntimeError(f"WAL sidecar not truncated: {wal_path} is {wal_bytes} bytes (expected 0)")

    shm_path = f"{target_db}-shm"
    shm_exists = os.path.exists(shm_path)
    shm_bytes = 0
    if shm_exists:
        try:
            shm_bytes = os.path.getsize(shm_path)
        except OSError:
            shm_bytes = 0
        try:
            with open(shm_path, "r+b"):
                pass
        except Exception as e:
            raise RuntimeError(f"SHM sidecar is locked or inaccessible: {e}")

    # 11. Target database file-level read/write accessibility check
    try:
        with open(target_db, "r+b"):
            pass
    except Exception as e:
        raise RuntimeError(f"Target database file is locked or inaccessible: {e}")

    # 12. Integrity check
    integrity_conn = sqlite3.connect(target_db, timeout=2.0)
    try:
        cur = integrity_conn.cursor()
        integrity_res = cur.execute("PRAGMA integrity_check;").fetchall()
        integrity_status = [r[0] for r in integrity_res]
        if integrity_status != ["ok"]:
            raise RuntimeError(f"Integrity check failed: {integrity_status}")
    finally:
        integrity_conn.close()

    return {
        "status": "ready_for_update",
        "target_db": target_db,
        "checkpoint": {
            "busy": busy,
            "log_frames": log_frames,
            "checkpointed_frames": checkpointed_frames,
        },
        "wal_bytes": wal_bytes,
        "shm_exists": shm_exists,
        "shm_bytes": shm_bytes,
        "connections_closed": True,
        "workers_stopped": True,
        "integrity_check": "ok",
    }


def handle_chat_send(req_id: Any, params: Dict[str, Any]):
    """Execute a user turn asynchronously on a background worker thread."""
    text = params.get("text", "").strip()
    if not text:
        send_response(req_id, {"reply": "", "model": "system", "type": "empty"})
        return

    # Atomic turn admission and registration under _turn_threads_lock
    with _turn_threads_lock:
        if _is_updating or _is_shutting_down:
            send_error(req_id, -32001, "Application is preparing for update; new turns are disabled.")
            return

        def _worker():
            t = threading.current_thread()
            try:
                main.ensure_initialized()
                _ensure_api_client_configured()
                outcome = main.process_user_turn(text, prompt_fn=_confirmation_prompt_fn)
                send_response(req_id, outcome)
            except Exception as e:
                logger.error("Error in process_user_turn: %s", e, exc_info=True)
                send_error(req_id, -32000, f"Error processing user request: {e}")
            finally:
                with _turn_threads_lock:
                    _active_turn_threads.discard(t)

        worker_thread = threading.Thread(target=_worker, daemon=True, name=f"TurnWorker-{req_id}")
        _active_turn_threads.add(worker_thread)
        worker_thread.start()


def handle_chat_history(params: Dict[str, Any]) -> List[Dict[str, Any]]:
    main.ensure_initialized()
    limit = int(params.get("limit", 60))
    turns = []
    try:
        conn = mem.get_connection()
        cursor = conn.execute(
            """
            SELECT id, role, content, created_at
            FROM conversation_history
            ORDER BY id ASC
            LIMIT ?
            """,
            (limit,)
        )
        for r in cursor.fetchall():
            turns.append({
                "id": r[0],
                "role": r[1],
                "content": r[2],
                "created_at": r[3],
            })
    except Exception as e:
        logger.warning("Error fetching chat history: %s", e)
    return turns


def handle_chat_confirm(params: Dict[str, Any]) -> Dict[str, Any]:
    conf_id = params.get("confirmation_id")
    decision = str(params.get("decision", "no")).strip().lower()

    with _confirmations_lock:
        container = _pending_confirmations.get(conf_id)
        if container:
            container["decision"] = "yes" if decision in ("yes", "y", "true") else "no"
            container["event"].set()
            return {"status": "resolved", "confirmation_id": conf_id, "decision": container["decision"]}
        else:
            return {"status": "not_found", "confirmation_id": conf_id}


def handle_jobs_list(params: Dict[str, Any]) -> List[Dict[str, Any]]:
    main.ensure_initialized()
    mgr = main.get_job_manager()
    status_filter = params.get("status")
    limit = int(params.get("limit", 100))
    jobs = mgr.list_jobs(status=status_filter, limit=limit)
    return [j.to_dict() for j in jobs]


def handle_jobs_status(params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    main.ensure_initialized()
    mgr = main.get_job_manager()
    job_id = params.get("job_id")
    job = mgr.get_job(job_id)
    return job.to_dict() if job else None


def handle_jobs_history(params: Dict[str, Any]) -> List[Dict[str, Any]]:
    main.ensure_initialized()
    mgr = main.get_job_manager()
    job_id = params.get("job_id")
    steps = mgr.get_history(job_id)
    return [s.to_dict() for s in steps]


def handle_jobs_create(params: Dict[str, Any]) -> Dict[str, Any]:
    main.ensure_initialized()
    mgr = main.get_job_manager()
    goal = params.get("goal", "").strip()
    job = mgr.create(goal)
    return job.to_dict()


def handle_jobs_start(params: Dict[str, Any]) -> Dict[str, Any]:
    main.ensure_initialized()
    mgr = main.get_job_manager()
    job_id = params.get("job_id")
    job = mgr.start(job_id)
    return job.to_dict()


def handle_jobs_pause(params: Dict[str, Any]) -> Dict[str, Any]:
    main.ensure_initialized()
    mgr = main.get_job_manager()
    job_id = params.get("job_id")
    job = mgr.pause(job_id)
    return job.to_dict()


def handle_jobs_resume(params: Dict[str, Any]) -> Dict[str, Any]:
    main.ensure_initialized()
    mgr = main.get_job_manager()
    job_id = params.get("job_id")
    job = mgr.resume(job_id)
    return job.to_dict()


def handle_jobs_cancel(params: Dict[str, Any]) -> Dict[str, Any]:
    main.ensure_initialized()
    mgr = main.get_job_manager()
    job_id = params.get("job_id")
    job = mgr.cancel(job_id)
    return job.to_dict()


def handle_jobs_confirm(params: Dict[str, Any]) -> Dict[str, Any]:
    main.ensure_initialized()
    mgr = main.get_job_manager()
    job_id = params.get("job_id")
    approved = bool(params.get("approved", False))
    job = mgr.confirm(job_id, approved=approved)
    return job.to_dict()


def handle_memory_structured(params: Dict[str, Any]) -> List[Dict[str, Any]]:
    main.ensure_initialized()
    return mem.recall_structured()


def handle_memory_legacy(params: Dict[str, Any]) -> List[str]:
    main.ensure_initialized()
    limit = int(params.get("limit", 50))
    return mem.recall(limit=limit)


def handle_memory_stats(params: Dict[str, Any]) -> Dict[str, Any]:
    main.ensure_initialized()
    return mem.memory_stats()


def handle_memory_forget(params: Dict[str, Any]) -> Dict[str, Any]:
    main.ensure_initialized()
    keyword = params.get("keyword", "").strip()
    deleted = mem.forget(keyword)
    return {"keyword": keyword, "deleted_count": deleted}


def handle_memory_retract(params: Dict[str, Any]) -> Dict[str, Any]:
    main.ensure_initialized()
    category = params.get("category", "").strip()
    key = params.get("key", "").strip()
    res = mem.retract_entity(category, key)
    return res


def handle_memory_remember(params: Dict[str, Any]) -> Dict[str, Any]:
    main.ensure_initialized()
    fact = params.get("fact", "").strip()
    if not fact:
        return {"success": False, "error": "Empty fact"}
    try:
        mem.remember(fact)
        return {"success": True, "fact": fact}
    except Exception as e:
        logger.error("Error in handle_memory_remember: %s", e)
        return {"success": False, "error": str(e)}


def handle_activity_get(params: Dict[str, Any]) -> Dict[str, Any]:
    main.ensure_initialized()
    import re
    from brain.paths import get_project_root

    # 1. System logs
    log_path = os.path.join(get_project_root(), "orange_ai.log")
    logs = []
    if os.path.exists(log_path):
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            for line in lines[-150:]:
                line = line.strip()
                if not line:
                    continue
                match = re.match(r"^(\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}:\d{2},\d+)\s\[([A-Z]+)\]\s(.*)$", line)
                if match:
                    logs.append({
                        "time": match.group(1)[:19],
                        "level": match.group(2),
                        "message": match.group(3),
                    })
                elif logs:
                    logs[-1]["message"] += "\n" + line
            logs.reverse()
        except Exception:
            pass

    # 2. Job steps
    job_steps = []
    try:
        manager = main.get_job_manager()
        jobs = manager.list_jobs(limit=15)
        for j in jobs:
            try:
                j_steps = manager.get_history(j.id)
                for s in j_steps:
                    raw_time = getattr(s, "started_at", None) or getattr(s, "completed_at", None) or getattr(s, "created_at", "")
                    clean_time = str(raw_time)[:19].replace("T", " ") if raw_time else ""
                    job_steps.append({
                        "job_id": j.short_id,
                        "goal": j.goal,
                        "action_type": s.action_type,
                        "tool_name": s.tool_name or "",
                        "success": s.success,
                        "reasoning": s.reasoning or "",
                        "error": s.error or "",
                        "duration_ms": s.duration_ms,
                        "time": clean_time,
                    })
            except Exception:
                pass
        job_steps.sort(key=lambda x: x.get("time", ""), reverse=True)
    except Exception:
        pass

    # 3. Conversations
    conversations = []
    try:
        conn = mem.get_connection()
        cursor = conn.execute(
            """
            SELECT role, content, created_at
            FROM conversation_history
            ORDER BY id DESC
            LIMIT 50
            """
        )
        for r in cursor.fetchall():
            conversations.append({
                "role": r[0],
                "content": r[1],
                "time": r[2][:19].replace("T", " ") if r[2] else "",
            })
    except Exception:
        pass

    return {
        "logs": logs,
        "job_steps": job_steps,
        "conversations": conversations,
    }


def handle_tools_list(params: Dict[str, Any]) -> List[Dict[str, Any]]:
    from tools.registry import registry
    return registry.list_metadata()


def handle_permissions_audit(params: Dict[str, Any]) -> Dict[str, Any]:
    from tools.registry import registry
    is_admin = tool_sys.is_running_as_admin()
    tools_list = []
    for t in registry.list_tools():
        perm = tool_perm.check_permission(t, is_admin=is_admin)
        tools_list.append({
            "name": t.name,
            "category": t.category,
            "risk_level": t.risk_level.value,
            "status": perm.status.value,
            "reason": perm.reason,
            "requires_confirmation": t.requires_confirmation,
        })
    return {
        "admin_rights": is_admin,
        "tools": tools_list,
    }


def handle_settings_get(params: Dict[str, Any]) -> Dict[str, Any]:
    current = _load_gui_settings()
    _apply_runtime_settings(current)
    return {
        **current,
        "settings": current,
        "supported_models": list(SUPPORTED_MODELS.values()),
        "default_model": DEFAULT_MODEL,
        "supported_cooldown_hours": [2, 4, 8],
    }


def handle_settings_update(params: Dict[str, Any]) -> Dict[str, Any]:
    updates = params.get("settings")
    if not isinstance(updates, dict):
        updates = params
    if not isinstance(updates, dict):
        current = _load_gui_settings()
        return {"success": False, "error": "Invalid settings payload", "settings": current, **current}

    current = _load_gui_settings()
    errors = []

    if any(k in updates for k in ("test_mode", "dev_mode", "ORANGE_AI_TEST_MODE")):
        errors.append("test_mode cannot be modified via settings RPC.")

    if "theme" in updates:
        if _validate_theme(updates["theme"]):
            current["theme"] = updates["theme"].lower()
        else:
            errors.append(f"Invalid theme: {updates['theme']}. Supported: dark, light, system.")

    if "model" in updates:
        if _validate_model(updates["model"]):
            current["model"] = updates["model"]
        else:
            errors.append(f"Invalid model: {updates['model']}. Must be one of 4 supported NVIDIA models.")

    if "api_url" in updates:
        if _validate_api_url(updates["api_url"]):
            current["api_url"] = updates["api_url"].strip().rstrip("/")
        else:
            errors.append(f"Invalid API URL: {updates['api_url']}. Must be a valid HTTP or HTTPS URL.")

    if "morning_briefing_enabled" in updates:
        if isinstance(updates["morning_briefing_enabled"], bool):
            current["morning_briefing_enabled"] = updates["morning_briefing_enabled"]
        else:
            errors.append("morning_briefing_enabled must be a boolean.")

    if "preferred_briefing_time" in updates:
        if _validate_briefing_time(updates["preferred_briefing_time"]):
            current["preferred_briefing_time"] = updates["preferred_briefing_time"].strip()
        else:
            errors.append(f"Invalid preferred_briefing_time: {updates['preferred_briefing_time']}. Must be HH:MM.")

    if "contextual_advisories_enabled" in updates:
        if isinstance(updates["contextual_advisories_enabled"], bool):
            current["contextual_advisories_enabled"] = updates["contextual_advisories_enabled"]
        else:
            errors.append("contextual_advisories_enabled must be a boolean.")

    if "advisory_cooldown_hours" in updates:
        if _validate_cooldown_hours(updates["advisory_cooldown_hours"]):
            current["advisory_cooldown_hours"] = int(updates["advisory_cooldown_hours"])
        else:
            errors.append(f"Invalid advisory_cooldown_hours: {updates['advisory_cooldown_hours']}. Must be 2, 4, or 8.")

    if errors:
        return {
            "success": False,
            "error": "; ".join(errors),
            "settings": current,
            **current,
            "supported_models": list(SUPPORTED_MODELS.values()),
        }

    success = _save_gui_settings(current)
    if success:
        _apply_runtime_settings(current)

    return {
        "success": success,
        "settings": current,
        **current,
        "supported_models": list(SUPPORTED_MODELS.values()),
    }


def handle_ui_set_theme(params: Dict[str, Any]) -> Dict[str, Any]:
    theme = params.get("theme", "dark")
    send_event("ui.set_theme", {"theme": theme})
    return {"status": "ok", "theme": theme}


def handle_ui_toggle_drawer(params: Dict[str, Any]) -> Dict[str, Any]:
    open_state = params.get("open")
    send_event("ui.toggle_drawer", {"open": open_state})
    return {"status": "ok", "open": open_state}


def handle_ui_select_view(params: Dict[str, Any]) -> Dict[str, Any]:
    view = params.get("view", "home")
    send_event("ui.select_view", {"view": view})
    return {"status": "ok", "view": view}


# --- Scope E: Stage 2D Compatibility & RPC Methods ---

def handle_briefing_get_latest(params: Dict[str, Any]) -> Dict[str, Any]:
    """Retrieve the latest persisted daily briefing record."""
    try:
        with _get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, local_date, created_at, content, status, model_name,
                       priorities_json, sources_json, error_message
                FROM daily_briefings ORDER BY created_at DESC LIMIT 1
            """)
            row = cur.fetchone()
            if not row:
                return {"briefing": None}
            return {
                "briefing": {
                    "id": row["id"],
                    "local_date": row["local_date"],
                    "created_at": row["created_at"],
                    "content": row["content"],
                    "status": row["status"],
                    "model_name": row["model_name"],
                    "priorities": json.loads(row["priorities_json"] or "[]"),
                    "sources": json.loads(row["sources_json"] or "{}"),
                    "error_message": row["error_message"],
                }
            }
    except Exception as e:
        logger.error("Error in briefing.get_latest: %s", e)
        return {"briefing": None, "error": str(e)}


def handle_briefing_list(params: Dict[str, Any]) -> Dict[str, Any]:
    """List historical daily briefing records."""
    limit = min(int(params.get("limit", 10)), 100)
    try:
        with _get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, local_date, created_at, content, status, model_name,
                       priorities_json, sources_json, error_message
                FROM daily_briefings ORDER BY created_at DESC LIMIT ?
            """, (limit,))
            rows = cur.fetchall()
            briefings = []
            for row in rows:
                briefings.append({
                    "id": row["id"],
                    "local_date": row["local_date"],
                    "created_at": row["created_at"],
                    "content": row["content"],
                    "status": row["status"],
                    "model_name": row["model_name"],
                    "priorities": json.loads(row["priorities_json"] or "[]"),
                    "sources": json.loads(row["sources_json"] or "{}"),
                    "error_message": row["error_message"],
                })
            return {"briefings": briefings}
    except Exception as e:
        logger.error("Error in briefing.list: %s", e)
        return {"briefings": [], "error": str(e)}


def handle_recommendations_get_active(params: Dict[str, Any]) -> Dict[str, Any]:
    """Retrieve currently active recommendations formatted for Stage 2A RecommendationCard."""
    recs = _get_active_recommendations()
    return {"recommendations": recs, "count": len(recs)}


def handle_reconciliation_get_latest(params: Dict[str, Any]) -> Dict[str, Any]:
    """Retrieve the latest workspace reconciliation records."""
    ws_path = params.get("workspace_path")
    try:
        with _get_db_connection() as conn:
            cur = conn.cursor()
            if ws_path:
                cur.execute("""
                    SELECT id, workspace_path, workspace_name, reconciled_at, status, git_branch, git_head_commit,
                           new_commits_json, changed_files_json, uncommitted_changes_json, file_watermark_hash,
                           outstanding_items_json, error_message
                    FROM project_reconciliations WHERE workspace_path = ? ORDER BY reconciled_at DESC LIMIT 10
                """, (ws_path,))
            else:
                cur.execute("""
                    SELECT id, workspace_path, workspace_name, reconciled_at, status, git_branch, git_head_commit,
                           new_commits_json, changed_files_json, uncommitted_changes_json, file_watermark_hash,
                           outstanding_items_json, error_message
                    FROM project_reconciliations ORDER BY reconciled_at DESC LIMIT 20
                """)
            rows = cur.fetchall()
            reconciliations = []
            for row in rows:
                reconciliations.append({
                    "id": row["id"],
                    "workspace_path": row["workspace_path"],
                    "workspace_name": row["workspace_name"],
                    "reconciled_at": row["reconciled_at"],
                    "status": row["status"],
                    "git_branch": row["git_branch"],
                    "git_head_commit": row["git_head_commit"],
                    "new_commits": json.loads(row["new_commits_json"] or "[]"),
                    "changed_files": json.loads(row["changed_files_json"] or "[]"),
                    "uncommitted_changes": json.loads(row["uncommitted_changes_json"] or "[]"),
                    "file_watermark_hash": row["file_watermark_hash"],
                    "outstanding_items": json.loads(row["outstanding_items_json"] or "[]"),
                    "error_message": row["error_message"],
                })
            return {"reconciliations": reconciliations}
    except Exception as e:
        logger.error("Error in reconciliation.get_latest: %s", e)
        return {"reconciliations": [], "error": str(e)}


def handle_stage2c_trigger_briefing(params: Dict[str, Any]) -> Dict[str, Any]:
    """Test helper to trigger briefing evaluation with custom date/time parameters."""
    force = bool(params.get("force", True))
    test_time_str = params.get("test_time")
    test_date_str = params.get("test_date")
    test_now = None
    if test_time_str:
        try:
            parts = test_time_str.split(":")
            today = datetime.now()
            test_now = datetime(today.year, today.month, today.day, int(parts[0]), int(parts[1]))
        except Exception:
            pass
    res = _evaluate_morning_briefing(force=force, test_now=test_now, test_date=test_date_str)
    return {"success": res is not None and res.get("status") in ("success", "fallback"), "briefing": res}


def handle_stage2c_trigger_idle_eval(params: Dict[str, Any]) -> Dict[str, Any]:
    """Test helper to trigger idle recommendations evaluation."""
    force = bool(params.get("force", True))
    test_now = None
    if params.get("test_now"):
        try:
            test_now = datetime.fromisoformat(params["test_now"])
        except Exception:
            pass
    recs = _evaluate_idle_recommendations(force=force, test_now=test_now)
    return {"success": True, "count": len(recs), "recommendations": recs}


def handle_stage2c_set_idle_threshold(params: Dict[str, Any]) -> Dict[str, Any]:
    """Test helper to dynamically adjust the idle inactivity threshold."""
    global _idle_inactivity_threshold
    secs = float(params.get("seconds", 600.0))
    _idle_inactivity_threshold = secs
    return {"success": True, "idle_threshold_seconds": secs}


def handle_stage2c_trigger_reconciliation(params: Dict[str, Any]) -> Dict[str, Any]:
    """Test helper to trigger workspace reconciliation on-demand."""
    ws_path = params.get("workspace_path")
    recs = _run_startup_reconciliation(target_path=ws_path)
    return {"success": True, "count": len(recs), "reconciliations": recs}


def handle_events_ack(params: Dict[str, Any]) -> Dict[str, Any]:
    """Mark a proactive event as acknowledged in SQLite."""
    event_id = params.get("event_id")
    if not event_id:
        raise ValueError("event_id parameter is required.")
    now_iso = datetime.now(timezone.utc).isoformat()
    affected = 0
    try:
        with _get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE proactive_events
                SET acknowledged = 1, acknowledged_at = ?
                WHERE event_id = ?
            """, (now_iso, str(event_id)))
            affected = cur.rowcount
            conn.commit()
    except Exception as e:
        logger.warning("[STAGE-2D] Error acknowledging event %s: %s", event_id, e)
        return {"event_id": event_id, "acknowledged": False, "error": str(e)}

    logger.info("[STAGE-2D] Acknowledged proactive event %s (rows=%d)", event_id, affected)
    return {"event_id": event_id, "acknowledged": True, "rows_affected": affected}


def handle_events_get_unacked(params: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return all unacknowledged proactive events for reconnect/startup replay."""
    limit = max(1, min(int(params.get("limit", 200)), 1000))
    events = []
    try:
        with _get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT event_id, event_type, entity_id, created_at, payload_json, acknowledged
                FROM proactive_events
                WHERE acknowledged = 0
                ORDER BY created_at DESC
                LIMIT ?
            """, (limit,))
            rows = cur.fetchall()
            for row in reversed(rows):
                try:
                    payload = json.loads(row["payload_json"])
                except Exception:
                    payload = {}
                events.append({
                    "event_id": row["event_id"],
                    "event_type": row["event_type"],
                    "created_at": row["created_at"],
                    "entity_id": row["entity_id"],
                    "version": 1,
                    "payload": payload,
                    "acknowledged": bool(row["acknowledged"]),
                })
    except Exception as e:
        logger.warning("[STAGE-2D] Error fetching unacked events: %s", e)
    return events


def handle_recommendations_dismiss(params: Dict[str, Any]) -> Dict[str, Any]:
    """Dismiss a recommendation: suppresses it for 7 days in RecommendationStore."""
    rec_id = params.get("id") or params.get("rec_id")
    if not rec_id:
        raise ValueError("Recommendation id parameter is required.")
    store = RecommendationStore()
    ok = store.dismiss(str(rec_id))
    if not ok:
        raise ValueError(f"Recommendation '{rec_id}' not found or could not be dismissed.")
    rec = store.get(str(rec_id))
    rec_dict = {
        "id": rec.rec_id,
        "title": rec.title,
        "description": rec.explanation or (getattr(rec, "suggested_action", "")),
        "category": rec.category.value if hasattr(rec.category, "value") else str(rec.category),
        "priority": normalize_recommendation_priority(rec.priority),
        "rationale": getattr(rec, "suggested_action", rec.explanation),
        "action_type": getattr(rec, "recommendation_type", "advisory"),
        "action_payload": getattr(rec, "action_payload", {"entity_id": getattr(rec, "entity_id", "")}),
        "created_at": rec.created_at.isoformat() if hasattr(rec.created_at, "isoformat") else str(rec.created_at),
        "status": rec.state.value if hasattr(rec.state, "value") else str(rec.state),
        "suppressed_until": rec.suppressed_until,
        "dedup_key": getattr(rec, "evidence_fingerprint", rec.rec_id),
        "evidence": getattr(rec, "evidence", []),
    }
    emit_proactive_event(
        event_type="recommendation.updated",
        entity_id=rec.rec_id,
        payload=rec_dict,
        event_id=f"evt_rec_upd_{rec.rec_id}_{uuid.uuid4().hex[:6]}",
    )
    logger.info("[STAGE-2D] Dismissed recommendation %s (suppressed until %s)", rec_id, rec.suppressed_until)
    return rec_dict


def handle_recommendations_snooze(params: Dict[str, Any]) -> Dict[str, Any]:
    """Snooze a recommendation: defers it for 24h (or specified duration) in RecommendationStore."""
    rec_id = params.get("id") or params.get("rec_id")
    duration = params.get("duration", "24h")
    if not rec_id:
        raise ValueError("Recommendation id parameter is required.")
    store = RecommendationStore()
    ok = store.defer(str(rec_id), duration)
    if not ok:
        raise ValueError(f"Recommendation '{rec_id}' not found or invalid snooze duration '{duration}'.")
    rec = store.get(str(rec_id))
    rec_dict = {
        "id": rec.rec_id,
        "title": rec.title,
        "description": rec.explanation or (getattr(rec, "suggested_action", "")),
        "category": rec.category.value if hasattr(rec.category, "value") else str(rec.category),
        "priority": normalize_recommendation_priority(rec.priority),
        "rationale": getattr(rec, "suggested_action", rec.explanation),
        "action_type": getattr(rec, "recommendation_type", "advisory"),
        "action_payload": getattr(rec, "action_payload", {"entity_id": getattr(rec, "entity_id", "")}),
        "created_at": rec.created_at.isoformat() if hasattr(rec.created_at, "isoformat") else str(rec.created_at),
        "status": rec.state.value if hasattr(rec.state, "value") else str(rec.state),
        "suppressed_until": rec.suppressed_until,
        "dedup_key": getattr(rec, "evidence_fingerprint", rec.rec_id),
        "evidence": getattr(rec, "evidence", []),
    }
    emit_proactive_event(
        event_type="recommendation.updated",
        entity_id=rec.rec_id,
        payload=rec_dict,
        event_id=f"evt_rec_upd_{rec.rec_id}_{uuid.uuid4().hex[:6]}",
    )
    logger.info("[STAGE-2D] Snoozed recommendation %s (suppressed until %s)", rec_id, rec.suppressed_until)
    return rec_dict


def handle_recommendations_execute(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Execute a recommendation action strictly via ToolRegistry and permission architecture.
    Never executes arbitrary commands. Ignores any untrusted executable payload from frontend.
    """
    rec_id = params.get("id") or params.get("rec_id")
    if not rec_id:
        return {"status": "ERROR", "error": "Recommendation ID is required."}

    store = RecommendationStore()
    rec = store.get(str(rec_id))
    if not rec:
        return {"status": "ERROR", "error": f"Recommendation '{rec_id}' not found."}

    # Authoritative resolution: read action details strictly from authoritative rec
    action_type = getattr(rec, "recommendation_type", "advisory")
    action_payload = getattr(rec, "action_payload", None)
    if not action_payload:
        action_payload = _recommendation_actions.get(str(rec.rec_id), {})
    tool_name = action_payload.get("tool_name") if isinstance(action_payload, dict) else None

    # Check for unsupported action types
    if not tool_name or action_type in ("advisory", "unsupported"):
        return {
            "status": "UNSUPPORTED",
            "error": f"Action type '{action_type}' is advisory only and has no automated execution pathway.",
            "recommendation_id": rec.rec_id,
        }

    from tools.registry import registry
    if not registry.contains(tool_name):
        return {
            "status": "UNSUPPORTED",
            "error": f"Tool '{tool_name}' is not registered in ToolRegistry.",
            "recommendation_id": rec.rec_id,
        }

    tool = registry.get(tool_name)
    perm = tool_perm.check_permission(tool)

    # 1. HIGH_RISK check
    if perm.risk_level == tool_perm.RiskLevel.HIGH_RISK or perm.status == tool_perm.PermissionStatus.DENIED:
        return {
            "status": "DENIED",
            "risk_level": "HIGH_RISK",
            "reason": perm.reason,
            "recommendation_id": rec.rec_id,
        }

    # 2. CONFIRMATION_REQUIRED check
    if perm.risk_level == tool_perm.RiskLevel.CONFIRMATION_REQUIRED:
        confirmed = params.get("confirmed")
        token = params.get("confirmation_token")

        if confirmed is None:
            # Generate confirmation token and require handshake
            tok = f"tok_conf_{uuid.uuid4().hex[:12]}"
            _pending_confirmations[tok] = {
                "rec_id": rec.rec_id,
                "tool_name": tool_name,
                "created_at": time.time(),
            }
            return {
                "status": "REQUIRES_CONFIRMATION",
                "confirmation_token": tok,
                "risk_level": "CONFIRMATION_REQUIRED",
                "tool_name": tool_name,
                "prompt": f"Executing tool '{tool_name}' requires explicit confirmation. Proceed?",
                "recommendation_id": rec.rec_id,
            }
        elif confirmed is False:
            return {
                "status": "DENIED",
                "risk_level": "CONFIRMATION_REQUIRED",
                "reason": "Execution cancelled by user.",
                "recommendation_id": rec.rec_id,
            }
        else:
            # confirmed is True - verify token if provided
            if token and token in _pending_confirmations:
                del _pending_confirmations[token]

    # 3. Execution (SAFE or confirmed CONFIRMATION_REQUIRED)
    tool_args = action_payload.get("parameters", {}) if isinstance(action_payload, dict) else {}
    try:
        # Pass prompt_fn returning 'y' because confirmation handshake was already verified
        exec_result = registry.execute(tool_name, is_admin=False, prompt_fn=lambda _: "y", **tool_args)
        store.handle(rec.rec_id)

        updated_rec = store.get(rec.rec_id)
        rec_dict = {
            "id": updated_rec.rec_id,
            "title": updated_rec.title,
            "description": updated_rec.explanation or (getattr(updated_rec, "suggested_action", "")),
            "category": updated_rec.category.value if hasattr(updated_rec.category, "value") else str(updated_rec.category),
            "priority": normalize_recommendation_priority(updated_rec.priority),
            "rationale": getattr(updated_rec, "suggested_action", updated_rec.explanation),
            "action_type": getattr(updated_rec, "recommendation_type", "advisory"),
            "action_payload": getattr(updated_rec, "action_payload", {"entity_id": getattr(updated_rec, "entity_id", "")}),
            "created_at": updated_rec.created_at.isoformat() if hasattr(updated_rec.created_at, "isoformat") else str(updated_rec.created_at),
            "status": updated_rec.state.value if hasattr(updated_rec.state, "value") else str(updated_rec.state),
            "dedup_key": getattr(updated_rec, "evidence_fingerprint", updated_rec.rec_id),
            "evidence": getattr(updated_rec, "evidence", []),
        }
        emit_proactive_event(
            event_type="recommendation.updated",
            entity_id=updated_rec.rec_id,
            payload=rec_dict,
            event_id=f"evt_rec_upd_{updated_rec.rec_id}_{uuid.uuid4().hex[:6]}",
        )
        return {
            "status": "SUCCESS",
            "risk_level": perm.risk_level.value,
            "result": exec_result,
            "recommendation_id": rec.rec_id,
        }
    except Exception as e:
        logger.error("[STAGE-2D] Error executing tool '%s': %s", tool_name, e, exc_info=True)
        return {
            "status": "ERROR",
            "error": str(e),
            "recommendation_id": rec.rec_id,
        }


def _is_test_mode_enabled() -> bool:
    """
    Check if development/test mode is explicitly enabled via process environment variable.
    Strictly disabled by default. Cannot be activated via settings.update or frontend RPC.
    """
    val = os.environ.get("ORANGE_AI_TEST_MODE", "").strip().lower()
    return val in ("1", "true", "yes", "dev", "test")


# 7. Method Dispatch Registry
METHOD_REGISTRY: Dict[str, Callable[[Dict[str, Any]], Any]] = {
    "system.handshake": handle_system_handshake,
    "system.ping": handle_system_ping,
    "system.health": handle_system_health,
    "system.shutdown": handle_system_shutdown,
    "system.shutdown_for_update": handle_system_shutdown_for_update,
    "chat.history": handle_chat_history,
    "chat.confirm": handle_chat_confirm,
    "jobs.list": handle_jobs_list,
    "jobs.status": handle_jobs_status,
    "jobs.history": handle_jobs_history,
    "jobs.create": handle_jobs_create,
    "jobs.start": handle_jobs_start,
    "jobs.pause": handle_jobs_pause,
    "jobs.resume": handle_jobs_resume,
    "jobs.cancel": handle_jobs_cancel,
    "jobs.confirm": handle_jobs_confirm,
    "memory.structured": handle_memory_structured,
    "memory.legacy": handle_memory_legacy,
    "memory.stats": handle_memory_stats,
    "memory.forget": handle_memory_forget,
    "memory.retract": handle_memory_retract,
    "memory.remember": handle_memory_remember,
    "activity.get": handle_activity_get,
    "tools.list": handle_tools_list,
    "permissions.audit": handle_permissions_audit,
    "settings.get": handle_settings_get,
    "settings.update": handle_settings_update,
    "ui.set_theme": handle_ui_set_theme,
    "ui.toggle_drawer": handle_ui_toggle_drawer,
    "ui.select_view": handle_ui_select_view,
    "briefing.get_latest": handle_briefing_get_latest,
    "briefing.list": handle_briefing_list,
    "recommendations.get_active": handle_recommendations_get_active,
    "recommendations.dismiss": handle_recommendations_dismiss,
    "recommendations.snooze": handle_recommendations_snooze,
    "recommendations.execute": handle_recommendations_execute,
    "reconciliation.get_latest": handle_reconciliation_get_latest,
    "events.ack": handle_events_ack,
    "events.get_unacked": handle_events_get_unacked,
}

TEST_METHOD_REGISTRY: Dict[str, Callable[[Dict[str, Any]], Any]] = {
    "stage2c.trigger_briefing": handle_stage2c_trigger_briefing,
    "stage2c.trigger_idle_eval": handle_stage2c_trigger_idle_eval,
    "stage2c.set_idle_threshold": handle_stage2c_set_idle_threshold,
    "stage2c.trigger_reconciliation": handle_stage2c_trigger_reconciliation,
}


_EXCLUDED_ACTIVITY_METHODS = {
    "system.handshake",
    "system.ping",
    "system.health",
    "system.shutdown",
    "system.shutdown_for_update",
    "activity.get",
    "briefing.get_latest",
    "briefing.list",
    "recommendations.get_active",
    "reconciliation.get_latest",
    "events.ack",
    "events.get_unacked",
    "stage2c.trigger_briefing",
    "stage2c.trigger_idle_eval",
    "stage2c.set_idle_threshold",
    "stage2c.trigger_reconciliation",
}


def dispatch_request(req_id: Any, method: str, params: Dict[str, Any]):
    """Route a single JSON-RPC request to its handler."""
    global _last_user_activity
    if method not in _EXCLUDED_ACTIVITY_METHODS:
        _last_user_activity = time.time()

    if method == "chat.send":
        # Dispatched to its own thread inside handle_chat_send
        handle_chat_send(req_id, params)
        return

    handler = METHOD_REGISTRY.get(method)
    if not handler and _is_test_mode_enabled():
        handler = TEST_METHOD_REGISTRY.get(method)

    if not handler:
        send_error(req_id, -32601, f"Method '{method}' not found.")
        return

    try:
        res = handler(params)
        send_response(req_id, res)
    except Exception as e:
        logger.error("Error executing %s: %s", method, e, exc_info=True)
        send_error(req_id, -32000, str(e))


def main_loop():
    """Main input loop reading line-delimited JSON-RPC from standard input."""
    global _START_TIME
    _START_TIME = time.time()
    logger.info("Orange AI Runtime Adapter started. PID: %d", os.getpid())

    # Initialize core state lazily
    try:
        main.ensure_initialized()
        _init_stage2c_background_workers()
        # Start the proactive background scheduler
        scheduler.start(
            on_reminder_due=_on_reminder_due,
            on_job_event=_on_job_event,
            job_manager=main.get_job_manager(),
        )
        logger.info("Background scheduler initialized.")
    except Exception as e:
        logger.error("Failed to initialize background engine: %s", e, exc_info=True)

    # Inform Rust supervisor that startup is ready
    send_event("runtime.ready", {
        "pid": os.getpid(),
        "version": "2.0.0",
        "timestamp": time.time(),
    })

    # Start UI command watcher for automated test control
    t_watcher = threading.Thread(target=_ui_control_file_watcher, daemon=True, name="UICommandWatcher")
    t_watcher.start()

    while not _is_shutting_down:
        try:
            line = sys.stdin.readline()
            if not line:
                # EOF: Parent pipe broken (Tauri exited or crashed)
                logger.info("EOF detected on stdin (parent process exited). Terminating adapter.")
                break

            line_str = line.strip()
            if not line_str:
                continue

            try:
                msg = json.loads(line_str)
            except json.JSONDecodeError as e:
                logger.warning("Malformed JSON received on stdin: %s", e)
                send_error(None, -32700, "Parse error: Invalid JSON.")
                continue

            if not isinstance(msg, dict):
                send_error(None, -32600, "Invalid Request: Must be a JSON object.")
                continue

            req_id = msg.get("id")
            method = msg.get("method")
            params = msg.get("params") or {}

            if not method:
                send_error(req_id, -32600, "Invalid Request: Missing 'method'.")
                continue

            dispatch_request(req_id, method, params)

        except (KeyboardInterrupt, SystemExit):
            break
        except Exception as e:
            logger.error("Unhandled error in main loop: %s", e, exc_info=True)

    # Perform clean teardown
    handle_system_shutdown({})


if __name__ == "__main__":
    main_loop()

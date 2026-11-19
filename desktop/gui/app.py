"""
Orange AI Desktop - Main Application Window & Core Shell.

Integrates the navigation rail, central workspace container, theme engine,
and clean window lifecycle management using CustomTkinter and Pillow.
"""

import os
import sys
import threading
import time
from typing import Optional, Dict, Any
import customtkinter as ctk

from gui.theme import ThemeColors, theme_manager
from gui.settings import get_theme_preference
from gui.components.navigation_rail import NavigationRail, NAV_ITEMS
from gui.views.home_view import HomeView, ComingSoonView
from gui.views.chat_view import ChatView
from gui.views.agent_jobs_view import AgentJobsView
from gui.views.memory_view import MemoryView
from gui.views.activity_view import ActivityView
from gui.views.permissions_view import PermissionsView
from gui.views.tools_view import ToolsView
from gui.views.settings_view import SettingsView
from gui.services.assistant_bridge import AssistantBridge


class OrangeAIApp(ctk.CTk):
    """Main desktop application window for Orange AI."""

    def __init__(self, **kwargs):
        import main
        main.ensure_initialized()

        super().__init__(**kwargs)

        # 1. Window Initialization
        self.title("Orange AI")
        self.geometry("1120x750")
        self.minsize(960, 600)

        # 2. Window Icon
        self._set_app_icon()

        # 3. Initialize Theme Engine with Persisted Preference
        initial_theme = get_theme_preference()
        ctk.set_appearance_mode(initial_theme)
        theme_manager.register_root(self)

        # 4. Root Container Background
        self.configure(fg_color=ThemeColors.WINDOW_BG)

        # 5. Build Shell Layout (Grid: Left Rail + Central Workspace)
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=0, minsize=220)  # Rail (fixed width)
        self.grid_columnconfigure(1, weight=1)  # Workspace (expandable)

        # Left Navigation Rail
        self._rail = NavigationRail(
            self,
            on_navigate=self._on_navigate,
        )
        self._rail.grid(row=0, column=0, sticky="nsew")

        # Central Workspace Container
        self._workspace_container = ctk.CTkFrame(
            self,
            fg_color=ThemeColors.WORKSPACE_BG,
            corner_radius=0,
        )
        self._workspace_container.grid(row=0, column=1, sticky="nsew")
        self._workspace_container.grid_rowconfigure(0, weight=1)
        self._workspace_container.grid_columnconfigure(0, weight=1)

        # Active View Reference
        self._active_view = None
        self._views_cache = {}

        # Shared Assistant Bridge
        self._assistant_bridge = AssistantBridge(
            self,
            on_result=self._on_assistant_result,
            on_status=self._on_assistant_status,
            on_confirmation=self._on_assistant_confirmation,
        )

        # Show initial Chat view
        self._show_view("chat")

        # 6. Background Scheduler Lifecycle (Stage 3.7E.1)
        self._scheduler_started = False
        self._start_scheduler()

        # 7. Scheduled Morning Briefing Check (Non-blocking, Stage 3.7E.1 & Stage 4.3.2)
        self._briefing_check_interval_ms = 60000
        self._briefing_worker_lock = threading.Lock()
        self._briefing_worker_thread: Optional[threading.Thread] = None
        self._briefing_worker_token: int = 0
        self._briefing_attempt_states: Dict[int, str] = {}
        self._briefing_attempt_events: Dict[int, threading.Event] = {}
        self._morning_briefing_after_id = self.after(200, self._check_morning_briefing)

        # 8. Proactive Event Awareness Lifecycle (Stage 3.7E.3C.3)
        self._is_shutting_down = False
        from gui.services.event_coordinator import EventCoordinator
        self._event_coordinator = EventCoordinator(
            app=self,
            bridge=self._assistant_bridge,
        )
        self._startup_recon_after_id = self.after(300, self._run_startup_reconciliation)

        # 8b. Idle Recommendation Evaluation Lifecycle (Stage 4.3.4)
        self._last_user_activity_time = time.monotonic()
        self._idle_cycle_delivered = False
        self._idle_inactivity_threshold_sec = 600.0  # 10 minutes locked threshold
        self._idle_check_interval_ms = 30000        # 30 seconds periodic check
        self._idle_worker_lock = threading.Lock()
        self._idle_worker_thread: Optional[threading.Thread] = None
        self._idle_worker_token: int = 0
        self._idle_attempt_states: Dict[int, str] = {}
        self._idle_attempt_events: Dict[int, threading.Event] = {}

        # Register event listeners for keyboard, mouse clicks, and scrolling
        for seq in ("<Key>", "<ButtonPress>", "<MouseWheel>", "<Button-4>", "<Button-5>"):
            try:
                self.bind_all(seq, self._on_user_interaction, add="+")
            except Exception:
                pass

        self._idle_check_after_id = self.after(1000, self._check_idle_recommendations)

        # 9. Clean Shutdown Protocol
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _set_app_icon(self):
        """Configure native window icon from assets/app_icon.ico."""
        from brain.paths import get_asset_path
        ico_path = get_asset_path("app_icon.ico")
        if os.path.exists(ico_path):
            try:
                self.iconbitmap(ico_path)
            except Exception:
                pass

    def _on_navigate(self, key: str):
        """Handle navigation selection from the left rail."""
        self._show_view(key)

    def _show_view(self, key: str):
        """Switch active view within the central workspace container."""
        if hasattr(self, "_rail"):
            self._rail.set_active(key)

        if self._active_view:
            self._active_view.grid_forget()

        if key in ("home", "chat"):
            if "chat" not in self._views_cache:
                self._views_cache["chat"] = ChatView(
                    self._workspace_container,
                    assistant_bridge=self._assistant_bridge,
                )
            self._active_view = self._views_cache["chat"]
        elif key == "jobs":
            if "jobs" not in self._views_cache:
                self._views_cache["jobs"] = AgentJobsView(self._workspace_container)
            self._active_view = self._views_cache["jobs"]
        elif key == "memory":
            if "memory" not in self._views_cache:
                self._views_cache["memory"] = MemoryView(self._workspace_container)
            self._active_view = self._views_cache["memory"]
        elif key == "activity":
            if "activity" not in self._views_cache:
                self._views_cache["activity"] = ActivityView(self._workspace_container)
            self._active_view = self._views_cache["activity"]
        elif key == "permissions":
            if "permissions" not in self._views_cache:
                self._views_cache["permissions"] = PermissionsView(self._workspace_container)
            self._active_view = self._views_cache["permissions"]
        elif key == "tools":
            if "tools" not in self._views_cache:
                self._views_cache["tools"] = ToolsView(
                    self._workspace_container,
                    on_run_in_chat=self._run_query_in_chat,
                )
            self._active_view = self._views_cache["tools"]
        elif key == "settings":
            if "settings" not in self._views_cache:
                self._views_cache["settings"] = SettingsView(self._workspace_container)
            self._active_view = self._views_cache["settings"]
        else:
            # Map key to readable display name
            item_name = dict((k, label) for k, label, _ in NAV_ITEMS).get(key, key.title())
            if key not in self._views_cache:
                self._views_cache[key] = ComingSoonView(
                    self._workspace_container,
                    feature_name=item_name,
                )
            self._active_view = self._views_cache[key]

        self._active_view.grid(row=0, column=0, sticky="nsew")

        # Invoke activation hook for fresh state on cached views
        if hasattr(self._active_view, "on_activated") and callable(self._active_view.on_activated):
            try:
                self._active_view.on_activated()
            except Exception:
                pass

    def _run_query_in_chat(self, query: str):
        """Navigate to Chat view and populate composer with query."""
        self._show_view("chat")
        if "chat" in self._views_cache:
            self._views_cache["chat"]._composer.set_text(query)

    def _on_assistant_result(self, request_id: str, outcome: dict):
        if "chat" in self._views_cache:
            self._views_cache["chat"].on_assistant_result(request_id, outcome)

    def _on_assistant_status(self, request_id: str, status_msg: str):
        if "chat" in self._views_cache:
            self._views_cache["chat"].on_assistant_status(request_id, status_msg)

    def _on_assistant_confirmation(self, request_id: str, prompt_text: str, decision_cb):
        if "chat" in self._views_cache:
            self._views_cache["chat"].on_assistant_confirmation(request_id, prompt_text, decision_cb)

    def _start_scheduler(self):
        """Start the background scheduler with thread-safe GUI callbacks (Stage 3.7E.1)."""
        if getattr(self, "_scheduler_started", False):
            return
        try:
            import main
            from brain import scheduler
            from brain.logger import logger

            if main.job_manager is not None:
                try:
                    main.job_manager.recover_interrupted()
                except Exception as e:
                    logger.warning("[GUI] Job crash recovery warning: %s", e)

            scheduler.start(
                on_reminder_due=self._on_scheduler_reminder_due,
                on_job_event=self._on_scheduler_job_event,
                job_manager=main.job_manager,
                auto_mark_fired=False,
            )
            self._scheduler_started = True
        except Exception as e:
            from brain.logger import logger
            logger.error("[GUI] Failed to start scheduler: %s", e)
            self._scheduler_started = False

    def _run_startup_reconciliation(self):
        """Schedule proactive startup reconciliation in background worker (Stage 3.7E.3C.3)."""
        self._startup_recon_after_id = None
        if getattr(self, "_is_shutting_down", False):
            return
        try:
            if hasattr(self, "_event_coordinator") and self._event_coordinator:
                self._event_coordinator.trigger_reconciliation(reason="startup")
        except Exception as e:
            from brain.logger import logger
            logger.warning("[GUI] Failed to launch startup reconciliation: %s", e)

    def _on_scheduler_reminder_due(self, reminder: dict):
        """Scheduler callback invoked from background thread when a reminder is due."""
        if not hasattr(self, "_assistant_bridge") or self._assistant_bridge._is_closed:
            return False

        # Correction B: EventCoordinator is the single authoritative delivery path
        if hasattr(self, "_event_coordinator") and self._event_coordinator:
            self._event_coordinator.trigger_reconciliation(reason="reminder_due")
            return False

        # Fallback only when EventCoordinator is absent
        rem_id = reminder.get("id")
        msg = reminder.get("message", "")
        notification_text = f"⏰ Reminder #{rem_id}: {msg}" if rem_id else f"⏰ Reminder: {msg}"
        self._assistant_bridge._dispatch_to_ui(self._deliver_notification_to_chat, notification_text, "reminder")
        return True

    def _on_scheduler_job_event(self, job):
        """Scheduler callback invoked from background thread on job state changes."""
        if not job or not hasattr(self, "_assistant_bridge") or self._assistant_bridge._is_closed:
            return

        # Correction B: EventCoordinator is the single authoritative delivery path
        if hasattr(self, "_event_coordinator") and self._event_coordinator:
            self._event_coordinator.trigger_reconciliation(reason="job_event")
            return

        # Fallback only when EventCoordinator is absent
        from brain.jobs.models import JobStatus

        text = None
        if job.status == JobStatus.WAITING_CONFIRMATION:
            pending = job.get_pending_confirmation()
            tool_name = pending.tool_name if pending else "action"
            args_str = f" ({pending.arguments})" if pending and pending.arguments else ""
            text = (
                f"⚠️ Orange AI [Job {job.short_id}]: Action '{tool_name}'{args_str} requires confirmation.\n"
                f"Type 'job confirm {job.short_id} yes' to approve or 'job confirm {job.short_id} no' to decline."
            )
        elif job.status == JobStatus.COMPLETED:
            text = f"✅ Orange AI [Job {job.short_id}]: Completed: {job.final_response or 'Completed successfully.'}"
        elif job.status == JobStatus.FAILED:
            text = f"❌ Orange AI [Job {job.short_id}]: Failed: {job.last_error or 'Execution failed.'}"

        if text:
            self._assistant_bridge._dispatch_to_ui(self._deliver_notification_to_chat, text, "job")

    def _deliver_notification_to_chat(self, text: str, msg_type: str = "assistant"):
        """Executed on Tkinter main thread to append a notification into the chat timeline."""
        if hasattr(self, "_views_cache") and "chat" in self._views_cache:
            chat_view = self._views_cache["chat"]
            outcome = {"reply": text, "model": "system", "type": msg_type}
            chat_view.on_assistant_result(f"notif_{id(text)}", outcome)

    def _check_morning_briefing(self):
        """
        Periodically check proactive morning briefing eligibility and dispatch background worker.
        Runs approximately every 60 seconds without blocking the GUI or accumulating duplicate callbacks.
        """
        if getattr(self, "_is_shutting_down", False):
            return

        # Cancel any previous scheduled after-id and schedule the next recurring check
        if hasattr(self, "_morning_briefing_after_id") and self._morning_briefing_after_id:
            try:
                self.after_cancel(self._morning_briefing_after_id)
            except Exception:
                pass
            self._morning_briefing_after_id = None

        interval = getattr(self, "_briefing_check_interval_ms", 60000)
        self._morning_briefing_after_id = self.after(interval, self._check_morning_briefing)

        try:
            # 1. Concurrency guard: Ensure at most one briefing worker is active
            if hasattr(self, "_briefing_worker_lock"):
                with self._briefing_worker_lock:
                    if self._briefing_worker_thread is not None and self._briefing_worker_thread.is_alive():
                        return

            # 2. Eligibility check: Respects enabled, preferred time, and same-day deduplication
            from brain.briefing.settings import should_deliver_morning_briefing
            if not should_deliver_morning_briefing():
                return

            # 3. Dedicated background briefing worker
            with self._briefing_worker_lock:
                # Double-check under lock
                if self._briefing_worker_thread is not None and self._briefing_worker_thread.is_alive():
                    return
                if getattr(self, "_is_shutting_down", False):
                    return

                self._briefing_worker_token += 1
                token = self._briefing_worker_token
                from datetime import datetime
                captured_date = datetime.now().strftime("%Y-%m-%d")
                self._briefing_attempt_states[token] = "GENERATING"
                if hasattr(self, "_briefing_attempt_events"):
                    self._briefing_attempt_events[token] = threading.Event()

                def _worker():
                    try:
                        import main
                        main.deliver_morning_briefing_if_needed(
                            client=main.api_client,
                            manager=main.job_manager,
                            output_fn=lambda text: self._on_morning_briefing_raw_output(
                                text, token=token, captured_date=captured_date
                            ),
                            auto_mark_delivered=False,
                        )
                    except Exception as ex:
                        from brain.logger import logger
                        logger.warning("[GUI] Morning briefing worker error: %s", ex)
                        with self._briefing_worker_lock:
                            if self._briefing_attempt_states.get(token) not in ("ACKNOWLEDGED", "CANCELLED"):
                                self._briefing_attempt_states[token] = "FAILED"
                    finally:
                        with self._briefing_worker_lock:
                            if self._briefing_worker_token == token:
                                self._briefing_worker_thread = None

                worker_thread = threading.Thread(
                    target=_worker,
                    daemon=True,
                    name=f"MorningBriefingWorker-GUI-{token}",
                )
                self._briefing_worker_thread = worker_thread
                worker_thread.start()

        except Exception as e:
            from brain.logger import logger
            logger.warning("[GUI] Failed to launch morning briefing check: %s", e)

    def _on_morning_briefing_raw_output(
        self,
        text: str,
        token: Optional[int] = None,
        captured_date: Optional[str] = None,
    ) -> bool:
        """
        Callback invoked from morning briefing worker with formatted text.
        Synchronously marshals to UI thread via AssistantBridge.dispatch_with_ack().
        Enforces single date persistence owner: marks date ONLY upon confirmed UI presentation.
        """
        from brain.logger import logger
        from datetime import datetime

        if getattr(self, "_is_shutting_down", False):
            with self._briefing_worker_lock:
                if token is not None:
                    self._briefing_attempt_states[token] = "CANCELLED"
            return False

        if not hasattr(self, "_assistant_bridge") or self._assistant_bridge._is_closed:
            with self._briefing_worker_lock:
                if token is not None:
                    self._briefing_attempt_states[token] = "CANCELLED"
            return False

        # Token guard: Discard late output from stale or terminal workers
        if token is not None and hasattr(self, "_briefing_worker_lock"):
            with self._briefing_worker_lock:
                if token != self._briefing_worker_token or self._briefing_attempt_states.get(token) in ("CANCELLED", "FAILED", "ACKNOWLEDGED"):
                    logger.info("[GUI] Discarded late briefing output from stale/invalidated worker token %s", token)
                    return False
                self._briefing_attempt_states[token] = "READY_FOR_PRESENTATION"

        clean_text = text.strip()
        prefix = "🌅 Orange AI [Morning Briefing]:"
        if clean_text.startswith(prefix):
            clean_text = clean_text[len(prefix):].strip()
        if clean_text.endswith("--------------------------------------------------"):
            clean_text = clean_text[:-50].strip()

        msg_body = clean_text or text
        if not msg_body.strip():
            logger.warning("[GUI] Morning briefing content is empty; presentation rejected.")
            with self._briefing_worker_lock:
                if token is not None:
                    self._briefing_attempt_states[token] = "FAILED"
            return False

        # Narrowly scoped UI presentation callback executed on Tkinter thread
        def _ui_present() -> bool:
            # 1. Application is not shutting down
            if getattr(self, "_is_shutting_down", False):
                raise RuntimeError("Application shutting down during briefing presentation")

            # 2. AssistantBridge is still usable
            bridge = getattr(self, "_assistant_bridge", None)
            if not bridge or getattr(bridge, "_is_closed", False):
                raise RuntimeError("AssistantBridge closed during briefing presentation")

            # 3-5. Validate in-memory attempt state under lock
            if hasattr(self, "_briefing_worker_lock"):
                with self._briefing_worker_lock:
                    if token is not None:
                        # 3. Token matches current valid attempt
                        if token != self._briefing_worker_token:
                            raise RuntimeError(f"Stale attempt token {token} rejected (current: {self._briefing_worker_token})")
                        # 4. Attempt is still active and has not already failed or been cancelled
                        current_state = self._briefing_attempt_states.get(token)
                        if current_state in ("FAILED", "CANCELLED"):
                            raise RuntimeError(f"Attempt token {token} has already been invalidated ({current_state})")
                        # 5. Attempt has not already been acknowledged
                        if current_state == "ACKNOWLEDGED":
                            raise RuntimeError(f"Attempt token {token} already acknowledged")

            # 6. ChatView exists and is mounted/available
            if not hasattr(self, "_views_cache") or "chat" not in self._views_cache:
                raise RuntimeError("ChatView not available in views cache")

            chat_view = self._views_cache["chat"]
            outcome = {
                "reply": msg_body,
                "model": "system",
                "type": "briefing",
            }
            # Atomic check-and-commit immediately before timeline insertion
            if hasattr(self, "_briefing_worker_lock"):
                with self._briefing_worker_lock:
                    if token is not None:
                        if token != self._briefing_worker_token:
                            raise RuntimeError(f"Stale attempt token {token} rejected before insertion (current: {self._briefing_worker_token})")
                        current_state = self._briefing_attempt_states.get(token)
                        if current_state != "PRESENTING":
                            raise RuntimeError(f"Attempt token {token} cannot be presented: state is {current_state}")
                        self._briefing_attempt_states[token] = "COMMITTED"

            # Insert into timeline (outside lock to prevent UI deadlocks)
            try:
                chat_view.on_assistant_result(f"briefing_{token or 0}", outcome)
            except Exception:
                if hasattr(self, "_briefing_worker_lock"):
                    with self._briefing_worker_lock:
                        if token is not None:
                            self._briefing_attempt_states[token] = "FAILED"
                if hasattr(self, "_briefing_attempt_events") and token is not None:
                    ev = self._briefing_attempt_events.get(token)
                    if ev:
                        ev.set()
                raise

            if hasattr(self, "_briefing_worker_lock"):
                with self._briefing_worker_lock:
                    if token is not None:
                        self._briefing_attempt_states[token] = "PRESENTED"
            if hasattr(self, "_briefing_attempt_events") and token is not None:
                ev = self._briefing_attempt_events.get(token)
                if ev:
                    ev.set()
            return True

        with self._briefing_worker_lock:
            if token is not None:
                self._briefing_attempt_states[token] = "PRESENTING"
                if hasattr(self, "_briefing_attempt_events") and token not in self._briefing_attempt_events:
                    self._briefing_attempt_events[token] = threading.Event()

        # Dispatch with confirmed presentation acknowledgement
        success, res, err = self._assistant_bridge.dispatch_with_ack(_ui_present, timeout=5.0)

        # Coordinate presentation and timeout outcomes so that exactly one coherent outcome wins
        presentation_won = False
        commit_event = None
        with self._briefing_worker_lock:
            current_state = self._briefing_attempt_states.get(token) if token is not None else None
            if current_state == "PRESENTED" or (success and res is True):
                presentation_won = True
            elif current_state == "COMMITTED":
                if hasattr(self, "_briefing_attempt_events") and token is not None:
                    commit_event = self._briefing_attempt_events.get(token)

        # If UI committed to presentation and is currently in the middle of insertion, allow it to complete
        if not presentation_won and current_state == "COMMITTED" and commit_event is not None:
            commit_event.wait(timeout=2.0)
            with self._briefing_worker_lock:
                if self._briefing_attempt_states.get(token) == "PRESENTED":
                    presentation_won = True

        if not presentation_won:
            with self._briefing_worker_lock:
                if token is not None and self._briefing_attempt_states.get(token) != "ACKNOWLEDGED":
                    self._briefing_attempt_states[token] = "FAILED"
            logger.warning("[GUI] UI delivery unconfirmed for morning briefing (token %s): %s; date NOT marked", token, err)
            return False

        # Presentation confirmed! Now handle single-owner date persistence (Corrections A, C, D)
        delivery_date = captured_date or datetime.now().strftime("%Y-%m-%d")
        try:
            from brain.briefing.settings import set_last_briefing_date
            persisted = set_last_briefing_date(delivery_date)
        except Exception as ex:
            logger.error("[GUI] Error executing set_last_briefing_date for %s: %s", delivery_date, ex)
            persisted = False

        with self._briefing_worker_lock:
            if persisted:
                if token is not None:
                    self._briefing_attempt_states[token] = "ACKNOWLEDGED"
                logger.info("[GUI] Confirmed UI delivery for morning briefing; marked delivered for %s", delivery_date)
                return True
            else:
                # Correction D: Presentation succeeded, but persistence failed
                if token is not None:
                    self._briefing_attempt_states[token] = "FAILED"
                logger.error("[GUI] Morning briefing presented, but persistence failed for %s; date NOT marked in settings", delivery_date)
                return False

    def _on_user_interaction(self, event=None):
        """
        Record timestamp of user interaction and reset per-cycle idle delivery flag.
        Extended event tracking covers keyboard, mouse clicks, and scrolling.
        Does not consume or suppress events, allowing normal Tkinter event propagation.
        """
        self._last_user_activity_time = time.monotonic()
        if getattr(self, "_idle_cycle_delivered", False):
            self._idle_cycle_delivered = False

    def _check_idle_recommendations(self):
        """
        Periodic ticker checking user inactivity and triggering proactive recommendation evaluation.
        Respects contextual_advisories_enabled setting, 600s threshold, and mutual exclusion.
        """
        if getattr(self, "_is_shutting_down", False):
            return

        if hasattr(self, "_idle_check_after_id") and self._idle_check_after_id:
            try:
                self.after_cancel(self._idle_check_after_id)
            except Exception:
                pass
            self._idle_check_after_id = None

        interval = getattr(self, "_idle_check_interval_ms", 30000)
        self._idle_check_after_id = self.after(interval, self._check_idle_recommendations)

        try:
            # 1. Global setting gate: respect contextual advisories setting
            from brain.briefing.settings import is_contextual_advisories_enabled
            if not is_contextual_advisories_enabled():
                return

            # 2. Idle cycle guard: deliver at most one recommendation per uninterrupted idle period
            if getattr(self, "_idle_cycle_delivered", False):
                return

            # 3. Inactivity threshold check: locked at 600 seconds (10 minutes)
            now_mono = time.monotonic()
            elapsed = now_mono - getattr(self, "_last_user_activity_time", now_mono)
            threshold = getattr(self, "_idle_inactivity_threshold_sec", 600.0)
            if elapsed < threshold:
                return

            # 4. Mutual exclusion checks
            # A. AssistantBridge request in-flight
            if hasattr(self, "_assistant_bridge") and getattr(self._assistant_bridge, "_active_request_id", None) is not None:
                return

            # B. Morning briefing worker active
            if hasattr(self, "_briefing_worker_lock"):
                with self._briefing_worker_lock:
                    if self._briefing_worker_thread is not None and self._briefing_worker_thread.is_alive():
                        return

            # C. Idle recommendation worker active
            if hasattr(self, "_idle_worker_lock"):
                with self._idle_worker_lock:
                    if self._idle_worker_thread is not None and self._idle_worker_thread.is_alive():
                        return
                    if getattr(self, "_is_shutting_down", False):
                        return

                    self._idle_worker_token += 1
                    token = self._idle_worker_token
                    self._idle_attempt_states[token] = "EVALUATING"
                    if hasattr(self, "_idle_attempt_events"):
                        self._idle_attempt_events[token] = threading.Event()

                    def _worker():
                        try:
                            self._run_idle_recommendation_worker(token)
                        except Exception as ex:
                            from brain.logger import logger
                            logger.warning("[GUI] Idle recommendation worker error: %s", ex)
                            with self._idle_worker_lock:
                                if self._idle_attempt_states.get(token) not in ("ACKNOWLEDGED", "CANCELLED"):
                                    self._idle_attempt_states[token] = "FAILED"
                        finally:
                            with self._idle_worker_lock:
                                if self._idle_worker_token == token:
                                    self._idle_worker_thread = None

                    worker_thread = threading.Thread(
                        target=_worker,
                        daemon=True,
                        name=f"IdleRecommendationWorker-GUI-{token}",
                    )
                    self._idle_worker_thread = worker_thread
                    worker_thread.start()

        except Exception as e:
            from brain.logger import logger
            logger.warning("[GUI] Failed to launch idle recommendation check: %s", e)

    def _run_idle_recommendation_worker(self, token: int):
        """
        Dedicated background worker thread for idle recommendation evaluation.
        Strictly read-only evaluation; single-owner persistence upon confirmed delivery.
        """
        from brain.logger import logger
        from datetime import datetime, timedelta
        from brain.briefing.collector import BriefingCollector, is_default_db_path
        from brain.briefing.recommendations import (
            RecommendationEvaluator,
            RecommendationStore,
            RecommendationState,
        )
        from brain.briefing.settings import (
            is_contextual_advisories_enabled,
            get_advisory_cooldown_hours,
        )

        if getattr(self, "_is_shutting_down", False):
            with self._idle_worker_lock:
                self._idle_attempt_states[token] = "CANCELLED"
            return

        if not is_contextual_advisories_enabled():
            with self._idle_worker_lock:
                self._idle_attempt_states[token] = "SKIPPED_DISABLED"
            return

        local_now = datetime.now()
        start_of_today = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        end_of_today = local_now.replace(hour=23, minute=59, second=59, microsecond=999999)
        rolling_24h = local_now - timedelta(hours=24)

        # Resolve db_path, job_store, and store overrides safely
        db_path = self.__dict__.get("_db_path", None)
        job_store = self.__dict__.get("_job_store", None)
        store = self.__dict__.get("_recommendation_store", None)

        if store is None:
            if db_path is not None and not is_default_db_path(db_path):
                store = RecommendationStore(db_path=db_path)
            else:
                store = RecommendationStore()

        collector = BriefingCollector(
            db_path=db_path,
            job_store=job_store,
            recommendation_store=store,
        )

        _, current_project, secondary_projects = collector._collect_identity_and_projects()
        overdue_reminders, _ = collector._collect_reminders(start_of_today, end_of_today, local_now)
        blocked_jobs, active_jobs, paused_jobs, _ = collector._collect_jobs(rolling_24h)

        try:
            from brain.briefing.project_health import ProjectHealthEvaluator
            health_evaluator = ProjectHealthEvaluator(db_path=db_path, job_store=job_store)
            raw_health = health_evaluator.evaluate_all(now=local_now)
            health_map = {h.project_name.lower(): h for h in raw_health}
            if current_project and current_project.name.lower() in health_map:
                current_project.health = health_map[current_project.name.lower()]
            for sp in secondary_projects:
                if sp.name.lower() in health_map:
                    sp.health = health_map[sp.name.lower()]
        except Exception as e:
            logger.warning("[GUI] Project health evaluation failed gracefully during idle check: %s", e)

        rec_evaluator = RecommendationEvaluator(
            db_path=db_path,
            job_store=job_store,
            store=store,
        )

        candidates = rec_evaluator.evaluate(
            current_project=current_project,
            secondary_projects=secondary_projects,
            overdue_reminders=overdue_reminders,
            blocked_jobs=blocked_jobs,
            active_jobs=active_jobs,
            paused_jobs=paused_jobs,
            now=local_now,
        )

        if not candidates:
            with self._idle_worker_lock:
                self._idle_attempt_states[token] = "COMPLETED_EMPTY"
            return

        cooldown_hours = get_advisory_cooldown_hours()
        cooldown_seconds = cooldown_hours * 3600.0

        eligible_candidates = []
        for cand in candidates:
            if cand.state != RecommendationState.ACTIVE:
                continue
            if cand.last_surfaced_at:
                try:
                    last_dt = datetime.fromisoformat(cand.last_surfaced_at)
                    if last_dt.tzinfo is not None:
                        last_dt = last_dt.astimezone().replace(tzinfo=None)
                    if (local_now - last_dt).total_seconds() < cooldown_seconds:
                        continue
                except Exception:
                    pass
            eligible_candidates.append(cand)

        if not eligible_candidates:
            with self._idle_worker_lock:
                self._idle_attempt_states[token] = "COMPLETED_ALL_IN_COOLDOWN"
            return

        # Select exactly one highest-ranked recommendation
        selected_rec = eligible_candidates[0]

        self._dispatch_idle_recommendation(selected_rec, store=store, token=token, now=local_now)

    def _dispatch_idle_recommendation(
        self,
        rec,
        store,
        token: Optional[int] = None,
        now: Optional[Any] = None,
    ) -> bool:
        """
        Marshals selected recommendation to UI thread via AssistantBridge.dispatch_with_ack().
        Enforces single-owner persistence upon confirmed delivery.
        """
        from brain.logger import logger
        from brain.briefing.recommendations import RecommendationState

        if getattr(self, "_is_shutting_down", False):
            with self._idle_worker_lock:
                if token is not None:
                    self._idle_attempt_states[token] = "CANCELLED"
            return False

        if not hasattr(self, "_assistant_bridge") or self._assistant_bridge._is_closed:
            with self._idle_worker_lock:
                if token is not None:
                    self._idle_attempt_states[token] = "CANCELLED"
            return False

        if token is not None and hasattr(self, "_idle_worker_lock"):
            with self._idle_worker_lock:
                if token != self._idle_worker_token or self._idle_attempt_states.get(token) in ("CANCELLED", "FAILED", "ACKNOWLEDGED"):
                    logger.info("[GUI] Discarded late idle rec delivery from stale/invalidated worker token %s", token)
                    return False
                self._idle_attempt_states[token] = "READY_FOR_PRESENTATION"

        def _ui_present() -> bool:
            if getattr(self, "_is_shutting_down", False):
                raise RuntimeError("Application shutting down during idle recommendation presentation")

            bridge = getattr(self, "_assistant_bridge", None)
            if not bridge or getattr(bridge, "_is_closed", False):
                raise RuntimeError("AssistantBridge closed during idle recommendation presentation")

            if hasattr(self, "_idle_worker_lock"):
                with self._idle_worker_lock:
                    if token is not None:
                        if token != self._idle_worker_token:
                            raise RuntimeError(f"Stale idle attempt token {token} rejected (current: {self._idle_worker_token})")
                        current_state = self._idle_attempt_states.get(token)
                        if current_state in ("FAILED", "CANCELLED"):
                            raise RuntimeError(f"Idle attempt token {token} has already been invalidated ({current_state})")
                        if current_state == "ACKNOWLEDGED":
                            raise RuntimeError(f"Idle attempt token {token} already acknowledged")

            if not hasattr(self, "_views_cache") or "chat" not in self._views_cache:
                raise RuntimeError("ChatView not available in views cache")

            # Re-validate target recommendation is still ACTIVE in store
            rec_in_store = store.get(rec.rec_id)
            if not rec_in_store or rec_in_store.state != RecommendationState.ACTIVE:
                raise RuntimeError(f"Recommendation {rec.rec_id} is no longer active in store")

            chat_view = self._views_cache["chat"]
            outcome = {
                "reply": "",
                "model": "system",
                "type": "recommendation",
                "recommendation": rec_in_store.to_dict(),
            }

            # Atomic check-and-commit immediately before timeline insertion
            if hasattr(self, "_idle_worker_lock"):
                with self._idle_worker_lock:
                    if token is not None:
                        if token != self._idle_worker_token:
                            raise RuntimeError(f"Stale attempt token {token} rejected before insertion")
                        current_state = self._idle_attempt_states.get(token)
                        if current_state != "PRESENTING":
                            raise RuntimeError(f"Idle attempt token {token} cannot be presented: state is {current_state}")
                        self._idle_attempt_states[token] = "COMMITTED"

            try:
                chat_view.on_assistant_result(f"idle_rec_{rec_in_store.short_id}_{token or 0}", outcome)
            except Exception:
                if hasattr(self, "_idle_worker_lock"):
                    with self._idle_worker_lock:
                        if token is not None:
                            self._idle_attempt_states[token] = "FAILED"
                if hasattr(self, "_idle_attempt_events") and token is not None:
                    ev = self._idle_attempt_events.get(token)
                    if ev:
                        ev.set()
                raise

            if hasattr(self, "_idle_worker_lock"):
                with self._idle_worker_lock:
                    if token is not None:
                        self._idle_attempt_states[token] = "PRESENTED"
            if hasattr(self, "_idle_attempt_events") and token is not None:
                ev = self._idle_attempt_events.get(token)
                if ev:
                    ev.set()
            return True

        with self._idle_worker_lock:
            if token is not None:
                self._idle_attempt_states[token] = "PRESENTING"
                if hasattr(self, "_idle_attempt_events") and token not in self._idle_attempt_events:
                    self._idle_attempt_events[token] = threading.Event()

        success, res, err = self._assistant_bridge.dispatch_with_ack(_ui_present, timeout=5.0)

        presentation_won = False
        commit_event = None
        with self._idle_worker_lock:
            current_state = self._idle_attempt_states.get(token) if token is not None else None
            if current_state == "PRESENTED" or (success and res is True):
                presentation_won = True
            elif current_state == "COMMITTED":
                if hasattr(self, "_idle_attempt_events") and token is not None:
                    commit_event = self._idle_attempt_events.get(token)

        if not presentation_won and current_state == "COMMITTED" and commit_event is not None:
            commit_event.wait(timeout=2.0)
            with self._idle_worker_lock:
                if self._idle_attempt_states.get(token) == "PRESENTED":
                    presentation_won = True

        if not presentation_won:
            with self._idle_worker_lock:
                if token is not None:
                    self._idle_attempt_states[token] = "FAILED"
            logger.warning("[GUI] Idle recommendation delivery failed or timed out: %s", err)
            return False

        # Persist surfaced record only upon confirmed delivery
        try:
            persisted = store.record_surfaced(rec.rec_id, channel="proactive", now=now)
        except Exception as ex:
            logger.error("[GUI] Error executing record_surfaced for %s: %s", rec.rec_id, ex)
            persisted = False

        with self._idle_worker_lock:
            if persisted:
                if token is not None:
                    self._idle_attempt_states[token] = "ACKNOWLEDGED"
                self._idle_cycle_delivered = True
                logger.info("[GUI] Confirmed UI delivery for idle recommendation %s", rec.rec_id)
                return True
            else:
                if token is not None:
                    self._idle_attempt_states[token] = "FAILED"
                logger.error("[GUI] Idle recommendation presented, but persistence failed for %s", rec.rec_id)
                return False

    def _on_close(self):
        """Cleanly close the GUI without leaving orphan threads."""
        try:
            self._is_shutting_down = True

            if self.__dict__.get("_startup_recon_after_id"):
                try:
                    self.after_cancel(self._startup_recon_after_id)
                except Exception:
                    pass
                self._startup_recon_after_id = None

            if self.__dict__.get("_morning_briefing_after_id"):
                try:
                    self.after_cancel(self._morning_briefing_after_id)
                except Exception:
                    pass
                self._morning_briefing_after_id = None

            if self.__dict__.get("_idle_check_after_id"):
                try:
                    self.after_cancel(self._idle_check_after_id)
                except Exception:
                    pass
                self._idle_check_after_id = None

            # Safe bounded join of active briefing worker
            worker_to_join = None
            if "_briefing_worker_lock" in self.__dict__:
                with self._briefing_worker_lock:
                    worker_to_join = self.__dict__.get("_briefing_worker_thread")
                    if "_briefing_worker_token" in self.__dict__ and "_briefing_attempt_states" in self.__dict__:
                        self._briefing_attempt_states[self._briefing_worker_token] = "CANCELLED"
                    if "_briefing_worker_token" in self.__dict__ and "_briefing_attempt_events" in self.__dict__:
                        ev = self._briefing_attempt_events.get(self._briefing_worker_token)
                        if ev:
                            ev.set()

            if worker_to_join is not None and worker_to_join.is_alive() and worker_to_join is not threading.current_thread():
                try:
                    worker_to_join.join(timeout=1.5)
                except Exception:
                    pass

            # Safe bounded join of active idle recommendation worker
            idle_worker_to_join = None
            if "_idle_worker_lock" in self.__dict__:
                with self._idle_worker_lock:
                    idle_worker_to_join = self.__dict__.get("_idle_worker_thread")
                    if "_idle_worker_token" in self.__dict__ and "_idle_attempt_states" in self.__dict__:
                        self._idle_attempt_states[self._idle_worker_token] = "CANCELLED"
                    if "_idle_worker_token" in self.__dict__ and "_idle_attempt_events" in self.__dict__:
                        ev = self._idle_attempt_events.get(self._idle_worker_token)
                        if ev:
                            ev.set()

            if idle_worker_to_join is not None and idle_worker_to_join.is_alive() and idle_worker_to_join is not threading.current_thread():
                try:
                    idle_worker_to_join.join(timeout=1.5)
                except Exception:
                    pass

            if hasattr(self, "_event_coordinator") and self._event_coordinator:
                try:
                    self._event_coordinator.shutdown(timeout=2.0)
                except Exception:
                    pass

            if getattr(self, "_scheduler_started", False):
                from brain import scheduler
                scheduler.stop(wait_timeout=3.0)
                self._scheduler_started = False

            if hasattr(self, "_assistant_bridge") and self._assistant_bridge:
                self._assistant_bridge.shutdown()
            import brain.memory
            brain.memory.close()
            import brain.reminders
            brain.reminders.close()

            if hasattr(self, "_single_instance_guard") and self._single_instance_guard:
                try:
                    self._single_instance_guard.release()
                except Exception:
                    pass
                self._single_instance_guard = None

            self.destroy()
        except Exception:
            pass


def launch_gui(show_alert: bool = True):
    """Entry point for GUI mode with single-instance protection."""
    from gui.single_instance import check_single_instance
    can_start, guard = check_single_instance(show_dialog=show_alert)
    if not can_start:
        return None

    app = OrangeAIApp()
    app._single_instance_guard = guard
    try:
        app.mainloop()
    finally:
        if guard:
            guard.release()


if __name__ == "__main__":
    launch_gui()

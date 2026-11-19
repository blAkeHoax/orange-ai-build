"""
gui/services/assistant_bridge.py - Background Worker & UI Thread Bridge.

Safely coordinates execution of the Orange AI multi-tier assistant engine
outside the CustomTkinter UI thread. Ensures zero UI freezing, normalizes
exceptions, enforces single-flight requests, ignores stale responses,
and delivers results strictly via Tkinter's after() event queue.
"""

import sys
import threading
from typing import Optional, Callable, Dict, Any, Tuple, Set
from brain.logger import logger
import main


class AssistantBridge:
    """
    Bridge connecting the CustomTkinter GUI to the synchronous assistant pipeline.
    Executes tasks on background daemon threads and marshals callbacks safely back to the UI thread.
    """

    def __init__(
        self,
        ui_root: Any,
        on_result: Callable[[str, Dict[str, Any]], None],
        on_status: Optional[Callable[[str, str], None]] = None,
        on_confirmation: Optional[Callable[[str, str, Callable[[str], None]], None]] = None,
    ):
        self._ui_root = ui_root
        self._on_result = on_result
        self._on_status = on_status
        self._on_confirmation = on_confirmation

        self._lock = threading.Lock()
        self._active_request_id: Optional[str] = None
        self._generation_counter: int = 0
        self._is_closed: bool = False
        self._active_confirm_event: Optional[threading.Event] = None
        self._active_confirm_decision: Optional[str] = None
        self._active_ack_events: Set[threading.Event] = set()

    def is_busy(self) -> bool:
        """Check if an assistant request is currently executing."""
        with self._lock:
            return self._active_request_id is not None

    def get_active_request_id(self) -> Optional[str]:
        """Return the ID of the current in-flight request, if any."""
        with self._lock:
            return self._active_request_id

    def send_message(self, user_text: str) -> Optional[str]:
        """
        Dispatch a user message to the background worker thread.
        Returns the generated request_id, or None if the bridge is already busy.
        """
        clean_text = user_text.strip()
        if not clean_text:
            return None

        with self._lock:
            if self._is_closed:
                logger.warning("[AssistantBridge] Cannot send message: bridge is closed.")
                return None

            if self._active_request_id is not None:
                logger.warning("[AssistantBridge] Request rejected: previous request %s still active.", self._active_request_id)
                return None

            self._generation_counter += 1
            request_id = f"req_{self._generation_counter:04d}"
            self._active_request_id = request_id

        # Launch background daemon thread
        worker_thread = threading.Thread(
            target=self._worker_run,
            args=(clean_text, request_id),
            daemon=True,
            name=f"OrangeWorker-{request_id}",
        )
        worker_thread.start()
        return request_id

    def shutdown(self):
        """Cleanly cancel pending confirmations and prevent future callback dispatches."""
        with self._lock:
            self._is_closed = True
            if self._active_confirm_event is not None:
                self._active_confirm_decision = "no"
                self._active_confirm_event.set()
            for ev in list(self._active_ack_events):
                try:
                    ev.set()
                except Exception:
                    pass
            self._active_ack_events.clear()

    def _worker_run(self, text: str, request_id: str):
        """Worker thread entry point."""
        logger.info("[AssistantBridge] Worker started for %s: '%s'", request_id, text[:40])

        def _gui_prompt_fn(prompt_text: str) -> str:
            """Confirmation prompt callback invoked from worker when a tool/agent requires consent."""
            with self._lock:
                if self._is_closed or self._active_request_id != request_id:
                    return "no"

                event = threading.Event()
                self._active_confirm_event = event
                self._active_confirm_decision = "no"

            def _on_user_decision(decision: str):
                """Callback invoked from UI button (Approve / Deny)."""
                with self._lock:
                    self._active_confirm_decision = "yes" if decision.lower() in ("yes", "y") else "no"
                    if self._active_confirm_event is not None:
                        self._active_confirm_event.set()

            # Marshal confirmation request to UI thread
            if self._on_confirmation and not self._is_closed:
                self._dispatch_to_ui(self._on_confirmation, request_id, prompt_text, _on_user_decision)

            # Wait on worker thread with safety timeout
            event.wait(timeout=120.0)

            with self._lock:
                final_decision = self._active_confirm_decision or "no"
                self._active_confirm_event = None
                self._active_confirm_decision = None
                return final_decision

        outcome: Dict[str, Any]
        try:
            outcome = main.process_user_turn(text, prompt_fn=_gui_prompt_fn)
        except main.OrangeAPIConnectionError as e:
            logger.warning("[AssistantBridge %s] Connection error: %s", request_id, e)
            outcome = {
                "reply": "Orange API is currently unreachable. Make sure Orange API is running to enable conversational replies.",
                "model": "api",
                "type": "error",
                "is_connection_error": True,
            }
        except main.OrangeAPIResponseError as e:
            logger.warning("[AssistantBridge %s] API response error: %s", request_id, e)
            outcome = {
                "reply": f"Orange API returned an error: {e}",
                "model": "api",
                "type": "error",
            }
        except Exception as e:
            logger.error("[AssistantBridge %s] Unexpected error: %s", request_id, e)
            outcome = {
                "reply": f"An unexpected error occurred while processing your request: {type(e).__name__}",
                "model": "system",
                "type": "error",
            }

        # Safe delivery back to UI thread
        with self._lock:
            is_stale = (self._active_request_id != request_id)
            is_closed = self._is_closed
            self._active_request_id = None

        if not is_stale and not is_closed:
            self._dispatch_to_ui(self._on_result, request_id, outcome)
        else:
            logger.info("[AssistantBridge] Ignored response for stale/closed request %s", request_id)

    def _dispatch_to_ui(self, func: Callable, *args: Any):
        """Safely schedule a function call on the Tkinter main thread."""
        try:
            if hasattr(self._ui_root, "after") and not self._is_closed:
                self._ui_root.after(0, func, *args)
        except Exception as e:
            logger.debug("[AssistantBridge] Could not post to UI after(): %s", e)

    def dispatch_with_ack(
        self,
        func: Callable[..., Any],
        *args: Any,
        timeout: float = 5.0,
    ) -> Tuple[bool, Any, Optional[str]]:
        """
        Safely schedule a function call on the Tkinter main thread and wait for execution confirmation.
        
        Threading contract:
        - If bridge is closed or shutting down, returns (False, None, "Bridge is closed").
        - If current thread is the UI thread, executes directly to prevent deadlock.
        - If called from a worker thread, schedules via ui_root.after() and blocks on a threading.Event.
        - Returns (is_success, return_value, error_message).
        - If UI execution exceeds timeout, returns (False, None, "UI dispatch timed out").
        """
        with self._lock:
            if self._is_closed:
                return False, None, "Bridge is closed"

        if not hasattr(self._ui_root, "after"):
            return False, None, "UI root unavailable"

        # Check if called from Tkinter UI thread directly (prevents deadlock)
        if threading.current_thread() is threading.main_thread():
            try:
                res = func(*args)
                return True, res, None
            except Exception as e:
                return False, None, str(e)

        ack_event = threading.Event()
        holder: Dict[str, Any] = {"res": None, "err": None}

        with self._lock:
            if self._is_closed:
                return False, None, "Bridge is closed"
            self._active_ack_events.add(ack_event)

        def _ui_task():
            with self._lock:
                if self._is_closed:
                    holder["err"] = "Bridge closed before execution"
                    ack_event.set()
                    return
            try:
                holder["res"] = func(*args)
            except Exception as e:
                holder["err"] = str(e)
            finally:
                ack_event.set()

        try:
            self._ui_root.after(0, _ui_task)
        except Exception as e:
            with self._lock:
                self._active_ack_events.discard(ack_event)
            return False, None, f"Could not schedule UI after(): {e}"

        timeout_val = max(0.1, float(timeout))
        signaled = ack_event.wait(timeout=timeout_val)

        with self._lock:
            self._active_ack_events.discard(ack_event)

        if not signaled:
            return False, None, f"UI dispatch timed out after {timeout_val:.1f}s"

        with self._lock:
            if self._is_closed and holder["err"] is None and holder["res"] is None:
                return False, None, "Bridge shutdown during execution"

        if holder["err"] is not None:
            return False, None, holder["err"]

        return True, holder["res"], None

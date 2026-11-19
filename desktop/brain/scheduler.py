"""
brain/scheduler.py - the background thread that makes Orange AI proactive.

WHAT THIS ACTUALLY DOES:
Without this, Orange AI only does anything the instant you type a message --
it's purely reactive. This module runs a second thread alongside the main
chat loop that wakes up every CHECK_INTERVAL_SECONDS, checks the reminders
database for anything due, and prints an alert directly to the terminal --
even if you're not actively typing anything at that moment.

WHY A THREAD (not a separate process):
Threads share memory and are simple to start/stop from within the same
Python process as main.py, with no extra OS-level setup (no Task
Scheduler, no separate script to manage). The tradeoff -- alerts only
fire while Orange AI's terminal window is actually open and running --
was a deliberate, agreed choice given how this app gets used.

THREADING SAFETY NOTE:
This thread only ever talks to the database through brain/reminders.py's
functions, each of which opens/uses/commits a cursor and returns -- it
never holds a cursor open across the sleep() call. Combined with the
connection's check_same_thread=False, this keeps things simple and safe
without needing an explicit lock for this access pattern.
"""

import threading
import time
from datetime import datetime, timezone
from typing import Optional

from brain.reminders import get_due_reminders, mark_fired
from brain.logger import logger

CHECK_INTERVAL_SECONDS = 30
# Checking every 30 seconds means a reminder or job can fire up to 30 seconds
# late, which is a fine tradeoff for a personal desktop tool -- a tighter
# interval just means more idle wake-ups for no real benefit.

_stop_event = threading.Event()
_active_job_thread: Optional[threading.Thread] = None
_thread_lock = threading.Lock()
_scheduler_thread: Optional[threading.Thread] = None


def _dispatch_job(job_manager, target, on_job_event, async_job: bool):
    """Dispatch job execution either synchronously or asynchronously on a worker thread (DEF-05)."""
    global _active_job_thread
    if async_job:
        def _worker():
            try:
                res = job_manager.start(target.id)
                if on_job_event:
                    on_job_event(res)
            except Exception as e:
                logger.error("[SCHEDULER] Background job execution failed: %s", e)
            finally:
                global _active_job_thread
                with _thread_lock:
                    if _active_job_thread is threading.current_thread():
                        _active_job_thread = None

        with _thread_lock:
            thread = threading.Thread(
                target=_worker,
                daemon=True,
                name=f"JobWorker-{target.id[:8]}",
            )
            _active_job_thread = thread
            thread.start()
        return target
    else:
        res = job_manager.start(target.id)
        if on_job_event:
            on_job_event(res)
        return res


def check_once(on_reminder_due, on_job_event=None, job_manager=None, async_job: bool = False, auto_mark_fired: bool = True):
    """
    Run a single scheduler check pass synchronously.
    Handles reminders and at most one eligible job.
    Useful for deterministic testing and manual work ticks.

    Callback Return Contract for `on_reminder_due(reminder)`:
    - `True`: Callback explicitly confirms delivery; marks reminder fired immediately.
    - `False`: Callback rejects firing or handles asynchronously (e.g. GUI dispatch awaiting UI ack);
              scheduler will NOT mark the reminder fired.
    - `None` (or non-boolean): Legacy contract; marks reminder fired ONLY if `auto_mark_fired=True`.
    """
    # 1. Reminders check
    try:
        due = get_due_reminders(datetime.now())
        for reminder in due:
            res = on_reminder_due(reminder)
            if res is False:
                continue
            elif res is True:
                mark_fired(reminder["id"])
            elif res is None and auto_mark_fired:
                mark_fired(reminder["id"])
    except Exception as e:
        logger.error("Reminder check pass failed: %s", e)

    # 2. Jobs check (enforcing maximum 1 RUNNING job)
    if job_manager is not None:
        try:
            with _thread_lock:
                if _active_job_thread is not None and _active_job_thread.is_alive():
                    return None
            if not job_manager._is_any_job_running():
                now = datetime.now(timezone.utc)
                # Check due RETRYING jobs first
                due_retries = job_manager.store.get_due_retries(now=now)
                if due_retries:
                    target = due_retries[0]
                    logger.info("[SCHEDULER] Triggering due retry for job '%s'", target.id)
                    return _dispatch_job(job_manager, target, on_job_event, async_job)




                # Check eligible PENDING jobs (priority DESC, created_at ASC)
                pending = job_manager.store.get_pending_jobs(limit=1)
                if pending:
                    target = pending[0]
                    logger.info("[SCHEDULER] Triggering pending job '%s'", target.id)
                    return _dispatch_job(job_manager, target, on_job_event, async_job)



        except Exception as e:
            logger.error("Job scheduler check pass failed: %s", e)

    return None


def _check_loop(on_reminder_due, on_job_event=None, job_manager=None, auto_mark_fired: bool = True):
    while not _stop_event.is_set():
        check_once(on_reminder_due, on_job_event=on_job_event, job_manager=job_manager, async_job=True, auto_mark_fired=auto_mark_fired)
        # Wait in small increments rather than one long sleep, so stop()
        # can interrupt promptly instead of waiting out a full interval.
        _stop_event.wait(CHECK_INTERVAL_SECONDS)


def start(on_reminder_due, on_job_event=None, job_manager=None, auto_mark_fired: bool = True):
    """
    Start the background reminder-and-job-checking thread.

    on_reminder_due: callback for due reminders.
    on_job_event: optional callback invoked when a background job state changes.
    job_manager: optional JobManager instance to poll for retries and pending jobs.
    auto_mark_fired: whether to automatically mark reminders fired in the scheduler loop.

    The thread is a daemon thread, so it won't prevent the program from
    exiting when the main loop ends (e.g. on "exit").
    """
    global _scheduler_thread
    _stop_event.clear()
    thread = threading.Thread(
        target=_check_loop,
        args=(on_reminder_due, on_job_event, job_manager, auto_mark_fired),
        daemon=True,
        name="SchedulerLoop",
    )
    _scheduler_thread = thread
    thread.start()
    return thread


def stop(wait_timeout: float = 3.0):
    """
    Signal the background scheduler loop to stop and wait up to wait_timeout
    seconds for any active job worker thread to complete gracefully.
    Safe against repeated calls, self-joining, and deadlocks.
    """
    _stop_event.set()

    # Capture active worker under lock, then join outside lock to avoid lock contention
    with _thread_lock:
        worker = _active_job_thread

    current = threading.current_thread()
    timeout_val = max(0.0, float(wait_timeout))

    if worker is not None and worker.is_alive() and worker is not current:
        logger.info(
            "[SCHEDULER] Waiting up to %.1fs for active worker '%s' to finish...",
            timeout_val,
            worker.name,
        )
        worker.join(timeout=timeout_val)
        if worker.is_alive():
            logger.warning(
                "[SCHEDULER] Worker '%s' did not finish within %.1fs timeout; continuing shutdown.",
                worker.name,
                timeout_val,
            )

    # Join scheduler loop thread if active and not current thread
    global _scheduler_thread
    sched_thread = _scheduler_thread
    if sched_thread is not None and sched_thread.is_alive() and sched_thread is not current:
        sched_thread.join(timeout=min(1.0, timeout_val))

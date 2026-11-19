"""
gui/views/agent_jobs_view.py - Agent Jobs Management & Audit View (Stage 4).

Renders persistent autonomous jobs, execution state, and step audit logs using
the official JobManager backend:
- Real-time job listing with status filtering (All, Active, Paused, Completed, Failed)
- Clear lifecycle state representation (PENDING, RUNNING, PAUSED, WAITING_CONFIRMATION, COMPLETED, FAILED, CANCELLED)
- Step-by-step execution audit trail inspection
- Supported lifecycle controls (Pause, Resume, Cancel, Confirm)
- Thread-safe background data loading
"""

import threading
import time
import json
from typing import Optional, List, Dict, Any
import customtkinter as ctk

from gui.theme import ThemeColors, theme_manager
from brain.jobs.models import Job, JobStep, JobStatus
import main


STATUS_COLORS = {
    JobStatus.RUNNING.value: ("#FF6B00", "#FF8C33"),
    JobStatus.WAITING_CONFIRMATION.value: ("#D97706", "#F59E0B"),
    JobStatus.PENDING.value: ("#2563EB", "#60A5FA"),
    JobStatus.RETRYING.value: ("#7C3AED", "#A78BFA"),
    JobStatus.PAUSED.value: ("#D97706", "#FBBF24"),
    JobStatus.COMPLETED.value: ("#059669", "#10B981"),
    JobStatus.FAILED.value: ("#DC2626", "#EF4444"),
    JobStatus.CANCELLED.value: ("#6B7280", "#9CA3AF"),
}


class AgentJobsView(ctk.CTkFrame):
    """View presenting real persistent jobs and execution history."""

    def __init__(self, master, **kwargs):
        super().__init__(
            master,
            fg_color=ThemeColors.WORKSPACE_BG,
            corner_radius=0,
            **kwargs,
        )
        self._jobs: List[Job] = []
        self._selected_job: Optional[Job] = None
        self._filter_status: Optional[str] = None
        self._search_query: str = ""
        self._is_loading: bool = False
        self._last_activation: float = 0.0

        self._build_ui()
        theme_manager.add_listener(self._on_theme_changed)
        self.refresh_jobs()

    def on_activated(self):
        """Lifecycle hook invoked when view is brought to the foreground."""
        now = time.time()
        if now - self._last_activation > 1.0:
            self._last_activation = now
            self.refresh_jobs()

    def _build_ui(self):
        # 1. Header Area
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=28, pady=(20, 12))

        title_box = ctk.CTkFrame(header, fg_color="transparent")
        title_box.pack(side="left")

        self._title_lbl = ctk.CTkLabel(
            title_box,
            text="Agent Jobs",
            font=ctk.CTkFont(family="Segoe UI", size=22, weight="bold"),
            text_color=ThemeColors.TEXT_PRIMARY,
        )
        self._title_lbl.pack(anchor="w")

        self._sub_lbl = ctk.CTkLabel(
            title_box,
            text="Inspect persistent autonomous jobs, execution steps, and lifecycle states.",
            font=ctk.CTkFont(family="Segoe UI", size=13),
            text_color=ThemeColors.TEXT_SECONDARY,
        )
        self._sub_lbl.pack(anchor="w", pady=(2, 0))

        # Refresh button
        self._refresh_btn = ctk.CTkButton(
            header,
            text="🔄 Refresh",
            width=90,
            height=32,
            corner_radius=16,
            fg_color=ThemeColors.SURFACE_CARD_BG,
            border_width=1,
            border_color=ThemeColors.BORDER,
            hover_color=ThemeColors.SURFACE_CARD_HOVER,
            text_color=ThemeColors.TEXT_PRIMARY,
            font=ctk.CTkFont(family="Segoe UI", size=12),
            command=self.refresh_jobs,
        )
        self._refresh_btn.pack(side="right")

        # 2. Controls & Filter Bar
        controls = ctk.CTkFrame(
            self,
            fg_color=ThemeColors.SURFACE_CARD_BG,
            border_width=1,
            border_color=ThemeColors.BORDER,
            corner_radius=12,
            height=48,
        )
        controls.pack(fill="x", padx=28, pady=(0, 14))
        controls.pack_propagate(False)

        # Filter Pills
        filter_box = ctk.CTkFrame(controls, fg_color="transparent")
        filter_box.pack(side="left", padx=12, pady=8)

        self._filter_btns: Dict[str, ctk.CTkButton] = {}
        filters = [
            ("All", None),
            ("Active", "RUNNING"),
            ("Waiting", "WAITING_CONFIRMATION"),
            ("Paused", "PAUSED"),
            ("Completed", "COMPLETED"),
            ("Failed", "FAILED"),
        ]

        for label, val in filters:
            btn = ctk.CTkButton(
                filter_box,
                text=label,
                width=65,
                height=26,
                corner_radius=13,
                fg_color=ThemeColors.ORANGE_PRIMARY if val == self._filter_status else "transparent",
                hover_color=ThemeColors.SURFACE_CARD_HOVER,
                text_color="#FFFFFF" if val == self._filter_status else ThemeColors.TEXT_SECONDARY,
                font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold" if val == self._filter_status else "normal"),
                command=lambda v=val: self._set_filter(v),
            )
            btn.pack(side="left", padx=2)
            self._filter_btns[label] = btn

        # Search field
        self._search_entry = ctk.CTkEntry(
            controls,
            placeholder_text="Search jobs by goal or ID...",
            placeholder_text_color=ThemeColors.TEXT_MUTED,
            height=28,
            width=220,
            border_width=1,
            border_color=ThemeColors.BORDER,
            fg_color=ThemeColors.WORKSPACE_BG,
            font=ctk.CTkFont(family="Segoe UI", size=12),
        )
        self._search_entry.pack(side="right", padx=12, pady=10)
        self._search_entry.bind("<KeyRelease>", self._on_search_changed)

        # 3. Main Split Container (Left: Job Cards, Right: Audit Details)
        self._content_split = ctk.CTkFrame(self, fg_color="transparent")
        self._content_split.pack(fill="both", expand=True, padx=28, pady=(0, 20))
        self._content_split.grid_columnconfigure(0, weight=3)
        self._content_split.grid_columnconfigure(1, weight=4)
        self._content_split.grid_rowconfigure(0, weight=1)

        # Left Column: Job List
        self._job_list_scroll = ctk.CTkScrollableFrame(
            self._content_split,
            fg_color=ThemeColors.SURFACE_CARD_BG,
            border_width=1,
            border_color=ThemeColors.BORDER,
            corner_radius=12,
        )
        self._job_list_scroll.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self._job_list_scroll.grid_columnconfigure(0, weight=1)

        # Right Column: Detail / Audit Panel
        self._detail_frame = ctk.CTkFrame(
            self._content_split,
            fg_color=ThemeColors.SURFACE_CARD_BG,
            border_width=1,
            border_color=ThemeColors.BORDER,
            corner_radius=12,
        )
        self._detail_frame.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        self._detail_frame.grid_columnconfigure(0, weight=1)
        self._detail_frame.grid_rowconfigure(1, weight=1)

        # Status / Feedback label on detail pane
        self._render_empty_detail("Select a job from the list to view full execution details and step audit trail.")

    def _set_filter(self, status: Optional[str]):
        self._filter_status = status
        # Update button styling
        for label, btn in self._filter_btns.items():
            filter_val = None if label == "All" else ("RUNNING" if label == "Active" else ("WAITING_CONFIRMATION" if label == "Waiting" else label.upper()))
            is_active = (filter_val == self._filter_status)
            btn.configure(
                fg_color=ThemeColors.ORANGE_PRIMARY if is_active else "transparent",
                text_color="#FFFFFF" if is_active else ThemeColors.TEXT_SECONDARY,
                font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold" if is_active else "normal"),
            )
        self._render_job_list()

    def _on_search_changed(self, event=None):
        self._search_query = self._search_entry.get().strip().lower()
        self._render_job_list()

    def refresh_jobs(self):
        """Fetch real jobs from JobManager off the UI thread."""
        if self._is_loading:
            return
        self._is_loading = True
        self._refresh_btn.configure(text="⏳ Loading...", state="disabled")

        def _fetch_worker():
            try:
                manager = main.get_job_manager()
                jobs = manager.list_jobs(limit=100)
            except Exception as e:
                jobs = []
            try:
                if self.winfo_exists():
                    self.after(0, self._on_jobs_loaded, jobs)
            except Exception:
                pass

        threading.Thread(target=_fetch_worker, daemon=True, name="JobsLoader").start()

    def _on_jobs_loaded(self, jobs: List[Job]):
        self._is_loading = False
        self._refresh_btn.configure(text="🔄 Refresh", state="normal")
        self._jobs = jobs
        self._render_job_list()
        if self._selected_job:
            # Re-select matching job from fresh data
            matching = [j for j in self._jobs if j.id == self._selected_job.id]
            if matching:
                self._show_job_detail(matching[0])
            else:
                self._render_empty_detail("Selected job is no longer available.")

    def _render_job_list(self):
        # Clear existing job cards
        for child in self._job_list_scroll.winfo_children():
            child.destroy()

        # Apply status and search filters
        filtered = self._jobs
        if self._filter_status:
            filtered = [j for j in filtered if j.status.value == self._filter_status]
        if self._search_query:
            filtered = [
                j for j in filtered
                if self._search_query in j.goal.lower() or self._search_query in j.short_id.lower()
            ]

        if not filtered:
            empty_msg = "No jobs found matching the current filter." if self._jobs else "No persistent jobs recorded yet.\nStart a job in Chat with 'job <goal>'."
            empty_lbl = ctk.CTkLabel(
                self._job_list_scroll,
                text=empty_msg,
                font=ctk.CTkFont(family="Segoe UI", size=13),
                text_color=ThemeColors.TEXT_MUTED,
                justify="center",
            )
            empty_lbl.pack(pady=40)
            return

        for job in filtered:
            card = self._create_job_card(self._job_list_scroll, job)
            card.pack(fill="x", padx=6, pady=4)

    def _create_job_card(self, parent, job: Job) -> ctk.CTkFrame:
        is_selected = self._selected_job and self._selected_job.id == job.id
        card = ctk.CTkFrame(
            parent,
            fg_color=ThemeColors.SURFACE_CARD_HOVER if is_selected else ThemeColors.WORKSPACE_BG,
            border_width=1,
            border_color=ThemeColors.ORANGE_PRIMARY if is_selected else ThemeColors.BORDER,
            corner_radius=8,
            cursor="hand2",
        )

        top_row = ctk.CTkFrame(card, fg_color="transparent")
        top_row.pack(fill="x", padx=10, pady=(8, 2))

        # Short ID badge
        id_lbl = ctk.CTkLabel(
            top_row,
            text=f"[{job.short_id}]",
            font=ctk.CTkFont(family="Consolas", size=11, weight="bold"),
            text_color=ThemeColors.TEXT_PRIMARY,
        )
        id_lbl.pack(side="left")

        # Status Badge
        status_color = STATUS_COLORS.get(job.status.value, ThemeColors.TEXT_SECONDARY)
        status_badge = ctk.CTkLabel(
            top_row,
            text=job.status.value,
            font=ctk.CTkFont(family="Segoe UI", size=10, weight="bold"),
            text_color=status_color[0] if isinstance(status_color, tuple) else status_color,
        )
        status_badge.pack(side="right")

        # Goal text
        goal_lbl = ctk.CTkLabel(
            card,
            text=job.goal,
            font=ctk.CTkFont(family="Segoe UI", size=13),
            text_color=ThemeColors.TEXT_PRIMARY,
            anchor="w",
            wraplength=280,
            justify="left",
        )
        goal_lbl.pack(fill="x", padx=10, pady=2)

        # Footer info: attempts & created timestamp
        footer = ctk.CTkFrame(card, fg_color="transparent")
        footer.pack(fill="x", padx=10, pady=(2, 8))

        created_str = job.created_at[:16].replace("T", " ") if job.created_at else ""
        meta_lbl = ctk.CTkLabel(
            footer,
            text=f"Attempts: {job.attempt_count}/{job.max_attempts}  •  {created_str}",
            font=ctk.CTkFont(family="Segoe UI", size=10),
            text_color=ThemeColors.TEXT_MUTED,
        )
        meta_lbl.pack(side="left")

        # Bind click event on card and children
        for widget in (card, top_row, id_lbl, status_badge, goal_lbl, footer, meta_lbl):
            widget.bind("<Button-1>", lambda e, j=job: self._show_job_detail(j))

        return card

    def _show_job_detail(self, job: Job):
        self._selected_job = job
        self._render_job_list()

        for child in self._detail_frame.winfo_children():
            child.destroy()

        # 1. Detail Header
        header = ctk.CTkFrame(self._detail_frame, fg_color="transparent")
        header.pack(fill="x", padx=16, pady=(14, 8))

        id_text = ctk.CTkLabel(
            header,
            text=f"Job [{job.short_id}]",
            font=ctk.CTkFont(family="Segoe UI", size=16, weight="bold"),
            text_color=ThemeColors.TEXT_PRIMARY,
        )
        id_text.pack(side="left")

        status_color = STATUS_COLORS.get(job.status.value, ThemeColors.TEXT_SECONDARY)
        status_lbl = ctk.CTkLabel(
            header,
            text=f"STATUS: {job.status.value}",
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
            text_color=status_color[0] if isinstance(status_color, tuple) else status_color,
        )
        status_lbl.pack(side="right")

        # 2. Goal Section
        goal_box = ctk.CTkFrame(self._detail_frame, fg_color=ThemeColors.WORKSPACE_BG, corner_radius=8)
        goal_box.pack(fill="x", padx=16, pady=4)

        ctk.CTkLabel(
            goal_box,
            text="Goal:",
            font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            text_color=ThemeColors.TEXT_MUTED,
            anchor="w",
        ).pack(fill="x", padx=10, pady=(6, 0))

        ctk.CTkLabel(
            goal_box,
            text=job.goal,
            font=ctk.CTkFont(family="Segoe UI", size=13),
            text_color=ThemeColors.TEXT_PRIMARY,
            anchor="w",
            wraplength=380,
            justify="left",
        ).pack(fill="x", padx=10, pady=(2, 8))

        # 3. Action Buttons Bar (Conditional on Job Status)
        action_bar = ctk.CTkFrame(self._detail_frame, fg_color="transparent")
        action_bar.pack(fill="x", padx=16, pady=6)

        if job.status == JobStatus.PAUSED:
            resume_btn = ctk.CTkButton(
                action_bar,
                text="▶ Resume Job",
                width=110,
                height=28,
                corner_radius=14,
                fg_color=ThemeColors.ORANGE_PRIMARY,
                hover_color=ThemeColors.ORANGE_HOVER,
                font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
                command=lambda: self._execute_job_action("resume", job.id),
            )
            resume_btn.pack(side="left", padx=(0, 6))

        elif job.status == JobStatus.RUNNING:
            pause_btn = ctk.CTkButton(
                action_bar,
                text="⏸ Pause Job",
                width=100,
                height=28,
                corner_radius=14,
                fg_color=ThemeColors.SURFACE_CARD_BG,
                border_width=1,
                border_color=ThemeColors.BORDER,
                hover_color=ThemeColors.SURFACE_CARD_HOVER,
                text_color=ThemeColors.TEXT_PRIMARY,
                font=ctk.CTkFont(family="Segoe UI", size=11),
                command=lambda: self._execute_job_action("pause", job.id),
            )
            pause_btn.pack(side="left", padx=(0, 6))

        elif job.status == JobStatus.WAITING_CONFIRMATION:
            conf_btn = ctk.CTkButton(
                action_bar,
                text="✓ Approve Action",
                width=120,
                height=28,
                corner_radius=14,
                fg_color="#059669",
                hover_color="#10B981",
                font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
                command=lambda: self._execute_job_action("confirm_yes", job.id),
            )
            conf_btn.pack(side="left", padx=(0, 6))

            deny_btn = ctk.CTkButton(
                action_bar,
                text="✕ Deny Action",
                width=100,
                height=28,
                corner_radius=14,
                fg_color="#DC2626",
                hover_color="#EF4444",
                font=ctk.CTkFont(family="Segoe UI", size=11),
                command=lambda: self._execute_job_action("confirm_no", job.id),
            )
            deny_btn.pack(side="left", padx=(0, 6))

        # Cancel button for active / paused jobs
        if job.status in (JobStatus.PENDING, JobStatus.RUNNING, JobStatus.PAUSED, JobStatus.WAITING_CONFIRMATION, JobStatus.RETRYING):
            cancel_btn = ctk.CTkButton(
                action_bar,
                text="Cancel Job",
                width=90,
                height=28,
                corner_radius=14,
                fg_color="transparent",
                border_width=1,
                border_color=ThemeColors.BORDER,
                hover_color=ThemeColors.SURFACE_CARD_HOVER,
                text_color=ThemeColors.TEXT_MUTED,
                font=ctk.CTkFont(family="Segoe UI", size=11),
                command=lambda: self._execute_job_action("cancel", job.id),
            )
            cancel_btn.pack(side="left")

        # 4. Result / Error Preview
        if job.last_error:
            err_box = ctk.CTkFrame(self._detail_frame, fg_color="#FEE2E2", corner_radius=6)
            err_box.pack(fill="x", padx=16, pady=4)
            ctk.CTkLabel(
                err_box,
                text=f"⚠️ {job.last_error}",
                font=ctk.CTkFont(family="Segoe UI", size=11),
                text_color="#991B1B",
                wraplength=380,
                justify="left",
            ).pack(fill="x", padx=8, pady=6)

        if job.final_response:
            resp_box = ctk.CTkFrame(self._detail_frame, fg_color=ThemeColors.WORKSPACE_BG, corner_radius=6)
            resp_box.pack(fill="x", padx=16, pady=4)
            ctk.CTkLabel(
                resp_box,
                text=f"Outcome: {job.final_response}",
                font=ctk.CTkFont(family="Segoe UI", size=11),
                text_color=ThemeColors.TEXT_PRIMARY,
                wraplength=380,
                justify="left",
            ).pack(fill="x", padx=8, pady=6)

        # 4.5 Task Progress Checklist Card
        checklist_items = []
        if job.agent_state_json:
            try:
                state_dict = json.loads(job.agent_state_json)
                if isinstance(state_dict, dict):
                    checklist_items = state_dict.get("checklist") or []
            except Exception:
                checklist_items = []

        if checklist_items:
            chk_card = ctk.CTkFrame(self._detail_frame, fg_color=ThemeColors.WORKSPACE_BG, corner_radius=8)
            chk_card.pack(fill="x", padx=16, pady=6)

            # Header row: Title + Progress Badge
            chk_header = ctk.CTkFrame(chk_card, fg_color="transparent")
            chk_header.pack(fill="x", padx=10, pady=(8, 4))

            ctk.CTkLabel(
                chk_header,
                text="Task Progress Checklist",
                font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
                text_color=ThemeColors.TEXT_PRIMARY,
            ).pack(side="left")

            completed_cnt = sum(1 for it in checklist_items if it.get("status") == "completed")
            total_cnt = len(checklist_items)
            pct = int((completed_cnt / total_cnt) * 100) if total_cnt > 0 else 0

            badge_text = f"{completed_cnt}/{total_cnt} completed ({pct}%)"
            badge_color = "#10B981" if completed_cnt == total_cnt else ThemeColors.ORANGE_PRIMARY
            ctk.CTkLabel(
                chk_header,
                text=badge_text,
                font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
                text_color=badge_color,
            ).pack(side="right")

            # Checklist items list
            for idx, it in enumerate(checklist_items, start=1):
                it_status = it.get("status", "pending")
                it_content = it.get("content", "")
                it_active = it.get("active_form")

                row = ctk.CTkFrame(chk_card, fg_color="transparent")
                row.pack(fill="x", padx=10, pady=2)

                if it_status == "completed":
                    sym = "✓"
                    sym_color = "#10B981"
                    txt_color = ThemeColors.TEXT_SECONDARY
                    text_str = f"{idx}. {it_content}"
                elif it_status == "in_progress":
                    sym = "⏳"
                    sym_color = "#FF8C33"
                    txt_color = ThemeColors.TEXT_PRIMARY
                    act_desc = f" [{it_active}]" if it_active else " [In Progress]"
                    text_str = f"{idx}. {it_content}{act_desc}"
                else:
                    sym = "○"
                    sym_color = ThemeColors.TEXT_MUTED
                    txt_color = ThemeColors.TEXT_MUTED
                    text_str = f"{idx}. {it_content}"

                ctk.CTkLabel(
                    row,
                    text=sym,
                    width=20,
                    font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
                    text_color=sym_color,
                    anchor="w",
                ).pack(side="left")

                ctk.CTkLabel(
                    row,
                    text=text_str,
                    font=ctk.CTkFont(
                        family="Segoe UI",
                        size=11,
                        weight="bold" if it_status == "in_progress" else "normal",
                    ),
                    text_color=txt_color,
                    anchor="w",
                    wraplength=350,
                    justify="left",
                ).pack(side="left", fill="x", expand=True)

            ctk.CTkFrame(chk_card, fg_color="transparent", height=4).pack()

        # 5. Execution Steps Audit Log
        ctk.CTkLabel(
            self._detail_frame,
            text="Execution Step Audit Trail:",
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
            text_color=ThemeColors.TEXT_PRIMARY,
            anchor="w",
        ).pack(fill="x", padx=16, pady=(10, 4))

        steps_scroll = ctk.CTkScrollableFrame(
            self._detail_frame,
            fg_color=ThemeColors.WORKSPACE_BG,
            corner_radius=8,
        )
        steps_scroll.pack(fill="both", expand=True, padx=16, pady=(0, 14))

        # Fetch steps in background
        def _fetch_steps():
            try:
                manager = main.get_job_manager()
                steps = manager.get_history(job.id)
            except Exception:
                steps = []
            try:
                if self.winfo_exists():
                    self.after(0, self._render_steps, steps_scroll, steps)
            except Exception:
                pass

        threading.Thread(target=_fetch_steps, daemon=True, name=f"StepsLoader-{job.short_id}").start()

    def _render_steps(self, parent_scroll, steps: List[JobStep]):
        for child in parent_scroll.winfo_children():
            child.destroy()

        if not steps:
            ctk.CTkLabel(
                parent_scroll,
                text="No execution steps recorded for this job.",
                font=ctk.CTkFont(family="Segoe UI", size=11),
                text_color=ThemeColors.TEXT_MUTED,
            ).pack(pady=20)
            return

        for idx, s in enumerate(steps, start=1):
            s_frame = ctk.CTkFrame(parent_scroll, fg_color=ThemeColors.SURFACE_CARD_BG, corner_radius=6)
            s_frame.pack(fill="x", pady=2, padx=4)

            # Step title
            status_sym = "✓" if s.success is True else ("✕" if s.success is False else "⏳")
            status_color = "#059669" if s.success is True else ("#DC2626" if s.success is False else "#D97706")
            tool_part = f" (Tool: {s.tool_name})" if s.tool_name else ""
            dur_part = f" [{s.duration_ms}ms]" if s.duration_ms is not None else ""

            header_text = f"#{idx} {s.action_type}{tool_part} — {status_sym}{dur_part}"
            ctk.CTkLabel(
                s_frame,
                text=header_text,
                font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
                text_color=status_color,
                anchor="w",
            ).pack(fill="x", padx=8, pady=(4, 2))

            if s.reasoning:
                ctk.CTkLabel(
                    s_frame,
                    text=f"Reasoning: {s.reasoning}",
                    font=ctk.CTkFont(family="Segoe UI", size=10),
                    text_color=ThemeColors.TEXT_SECONDARY,
                    anchor="w",
                    wraplength=350,
                    justify="left",
                ).pack(fill="x", padx=8, pady=(0, 2))

            if s.error:
                ctk.CTkLabel(
                    s_frame,
                    text=f"Error: {s.error}",
                    font=ctk.CTkFont(family="Segoe UI", size=10),
                    text_color="#DC2626",
                    anchor="w",
                    wraplength=350,
                    justify="left",
                ).pack(fill="x", padx=8, pady=(0, 4))

    def _execute_job_action(self, action: str, job_id: str):
        """Execute pause, resume, cancel, or confirm off the UI thread."""
        def _action_worker():
            try:
                manager = main.get_job_manager()
                if action == "pause":
                    manager.pause(job_id)
                elif action == "resume":
                    manager.resume(job_id)
                elif action == "cancel":
                    manager.cancel(job_id)
                elif action == "confirm_yes":
                    manager.confirm(job_id, True)
                elif action == "confirm_no":
                    manager.confirm(job_id, False)
            except Exception as e:
                pass
            try:
                if self.winfo_exists():
                    self.after(0, self.refresh_jobs)
            except Exception:
                pass

        threading.Thread(target=_action_worker, daemon=True, name=f"JobAction-{action}").start()

    def _render_empty_detail(self, message: str):
        for child in self._detail_frame.winfo_children():
            child.destroy()
        ctk.CTkLabel(
            self._detail_frame,
            text=message,
            font=ctk.CTkFont(family="Segoe UI", size=13),
            text_color=ThemeColors.TEXT_MUTED,
            justify="center",
            wraplength=300,
        ).pack(expand=True, pady=60)

    def _on_theme_changed(self, theme: str):
        self.configure(fg_color=ThemeColors.WORKSPACE_BG)
        self._title_lbl.configure(text_color=ThemeColors.TEXT_PRIMARY)
        self._sub_lbl.configure(text_color=ThemeColors.TEXT_SECONDARY)

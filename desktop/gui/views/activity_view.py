"""
gui/views/activity_view.py - Activity & Audit Trail View (Stage 4).

Renders genuine historical and live system activity across verified sources:
1. System Logs (from orange_ai.log via brain.logger)
2. Autonomous Job Step Audits (from JobStore / JobManager)
3. Assistant Conversation Turns (from memory.db conversation_history)

Accurately distinguishes provenance without fabricating a synthetic unified stream.
"""

import os
import re
import threading
import time
from typing import Optional, List, Dict, Any
import customtkinter as ctk

from gui.theme import ThemeColors, theme_manager
from brain.paths import get_project_root
from brain.jobs.models import JobStep, Job
import brain.memory as mem
import main


class ActivityView(ctk.CTkFrame):
    """View displaying genuine activity records from persistent logs, job audits, and memory."""

    def __init__(self, master, **kwargs):
        super().__init__(
            master,
            fg_color=ThemeColors.WORKSPACE_BG,
            corner_radius=0,
            **kwargs,
        )
        self._active_source: str = "logs"  # "logs", "jobs", "conversations"
        self._level_filter: Optional[str] = None
        self._search_query: str = ""
        self._is_loading: bool = False

        self._log_entries: List[Dict[str, str]] = []
        self._job_steps: List[Dict[str, Any]] = []
        self._conversation_turns: List[Dict[str, str]] = []
        self._last_activation: float = 0.0

        self._build_ui()
        theme_manager.add_listener(self._on_theme_changed)
        self.refresh_activity()

    def on_activated(self):
        """Lifecycle hook invoked when view is brought to the foreground."""
        now = time.time()
        if now - self._last_activation > 1.0:
            self._last_activation = now
            self.refresh_activity()

    def _build_ui(self):
        # 1. Header
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=28, pady=(20, 10))

        title_box = ctk.CTkFrame(header, fg_color="transparent")
        title_box.pack(side="left")

        self._title_lbl = ctk.CTkLabel(
            title_box,
            text="Activity",
            font=ctk.CTkFont(family="Segoe UI", size=22, weight="bold"),
            text_color=ThemeColors.TEXT_PRIMARY,
        )
        self._title_lbl.pack(anchor="w")

        self._sub_lbl = ctk.CTkLabel(
            title_box,
            text="Inspect persistent system logs, autonomous job execution audits, and conversation turns.",
            font=ctk.CTkFont(family="Segoe UI", size=13),
            text_color=ThemeColors.TEXT_SECONDARY,
        )
        self._sub_lbl.pack(anchor="w", pady=(2, 0))

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
            command=self.refresh_activity,
        )
        self._refresh_btn.pack(side="right")

        # 2. Source Selector Bar (Tabs)
        source_bar = ctk.CTkFrame(
            self,
            fg_color=ThemeColors.SURFACE_CARD_BG,
            border_width=1,
            border_color=ThemeColors.BORDER,
            corner_radius=12,
            height=46,
        )
        source_bar.pack(fill="x", padx=28, pady=(0, 10))
        source_bar.pack_propagate(False)

        source_box = ctk.CTkFrame(source_bar, fg_color="transparent")
        source_box.pack(side="left", padx=12, pady=7)

        self._source_btns: Dict[str, ctk.CTkButton] = {}
        sources = [
            ("logs", "System Logs (orange_ai.log)"),
            ("jobs", "Job Execution Audits"),
            ("conversations", "Conversation Turns"),
        ]

        for key, label in sources:
            btn = ctk.CTkButton(
                source_box,
                text=label,
                height=28,
                corner_radius=14,
                fg_color=ThemeColors.ORANGE_PRIMARY if key == self._active_source else "transparent",
                hover_color=ThemeColors.SURFACE_CARD_HOVER,
                text_color="#FFFFFF" if key == self._active_source else ThemeColors.TEXT_SECONDARY,
                font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold" if key == self._active_source else "normal"),
                command=lambda k=key: self._set_source(k),
            )
            btn.pack(side="left", padx=3)
            self._source_btns[key] = btn

        # Search field
        self._search_entry = ctk.CTkEntry(
            source_bar,
            placeholder_text="Filter activity entries...",
            placeholder_text_color=ThemeColors.TEXT_MUTED,
            height=28,
            width=200,
            border_width=1,
            border_color=ThemeColors.BORDER,
            fg_color=ThemeColors.WORKSPACE_BG,
            font=ctk.CTkFont(family="Segoe UI", size=12),
        )
        self._search_entry.pack(side="right", padx=12, pady=9)
        self._search_entry.bind("<KeyRelease>", self._on_search_changed)

        # 3. Provenance & Sub-filter Information Banner
        self._info_banner = ctk.CTkFrame(
            self,
            fg_color=ThemeColors.SURFACE_CARD_BG,
            border_width=1,
            border_color=ThemeColors.BORDER_SUBTLE,
            corner_radius=8,
            height=34,
        )
        self._info_banner.pack(fill="x", padx=28, pady=(0, 12))
        self._info_banner.pack_propagate(False)

        self._info_text_lbl = ctk.CTkLabel(
            self._info_banner,
            text="",
            font=ctk.CTkFont(family="Segoe UI", size=11),
            text_color=ThemeColors.TEXT_SECONDARY,
        )
        self._info_text_lbl.pack(side="left", padx=14, pady=6)

        # Sub-filters (e.g. log levels)
        self._sub_filter_box = ctk.CTkFrame(self._info_banner, fg_color="transparent")
        self._sub_filter_box.pack(side="right", padx=10, pady=4)
        self._sub_filter_btns: Dict[str, ctk.CTkButton] = {}

        # 4. Scrollable Content Feed
        self._scroll_frame = ctk.CTkScrollableFrame(
            self,
            fg_color=ThemeColors.SURFACE_CARD_BG,
            border_width=1,
            border_color=ThemeColors.BORDER,
            corner_radius=12,
        )
        self._scroll_frame.pack(fill="both", expand=True, padx=28, pady=(0, 20))
        self._scroll_frame.grid_columnconfigure(0, weight=1)

    def _set_source(self, source: str):
        self._active_source = source
        self._level_filter = None
        for key, btn in self._source_btns.items():
            is_active = (key == self._active_source)
            btn.configure(
                fg_color=ThemeColors.ORANGE_PRIMARY if is_active else "transparent",
                text_color="#FFFFFF" if is_active else ThemeColors.TEXT_SECONDARY,
                font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold" if is_active else "normal"),
            )
        self._update_banner()
        self._render_feed()

    def _set_level_filter(self, level: Optional[str]):
        self._level_filter = level
        for lvl_name, btn in self._sub_filter_btns.items():
            val = None if lvl_name == "ALL" else lvl_name
            is_active = (val == self._level_filter)
            btn.configure(
                fg_color=ThemeColors.ORANGE_PRIMARY if is_active else "transparent",
                text_color="#FFFFFF" if is_active else ThemeColors.TEXT_SECONDARY,
                font=ctk.CTkFont(family="Segoe UI", size=10, weight="bold" if is_active else "normal"),
            )
        self._render_feed()

    def _on_search_changed(self, event=None):
        self._search_query = self._search_entry.get().strip().lower()
        self._render_feed()

    def _update_banner(self):
        for child in self._sub_filter_box.winfo_children():
            child.destroy()
        self._sub_filter_btns.clear()

        if self._active_source == "logs":
            self._info_text_lbl.configure(
                text="Source: orange_ai.log  •  Persistent system and component diagnostic messages."
            )
            for lvl in ["ALL", "INFO", "WARNING", "ERROR"]:
                val = None if lvl == "ALL" else lvl
                btn = ctk.CTkButton(
                    self._sub_filter_box,
                    text=lvl,
                    width=45,
                    height=20,
                    corner_radius=10,
                    fg_color=ThemeColors.ORANGE_PRIMARY if val == self._level_filter else "transparent",
                    text_color="#FFFFFF" if val == self._level_filter else ThemeColors.TEXT_SECONDARY,
                    font=ctk.CTkFont(family="Segoe UI", size=10, weight="bold" if val == self._level_filter else "normal"),
                    command=lambda v=val: self._set_level_filter(v),
                )
                btn.pack(side="left", padx=2)
                self._sub_filter_btns[lvl] = btn

        elif self._active_source == "jobs":
            self._info_text_lbl.configure(
                text="Source: JobStore  •  Audit trail steps recorded during autonomous job runs."
            )
            for lvl in ["ALL", "SUCCESS", "FAILED"]:
                val = None if lvl == "ALL" else lvl
                btn = ctk.CTkButton(
                    self._sub_filter_box,
                    text=lvl,
                    width=55,
                    height=20,
                    corner_radius=10,
                    fg_color=ThemeColors.ORANGE_PRIMARY if val == self._level_filter else "transparent",
                    text_color="#FFFFFF" if val == self._level_filter else ThemeColors.TEXT_SECONDARY,
                    font=ctk.CTkFont(family="Segoe UI", size=10, weight="bold" if val == self._level_filter else "normal"),
                    command=lambda v=val: self._set_level_filter(v),
                )
                btn.pack(side="left", padx=2)
                self._sub_filter_btns[lvl] = btn

        elif self._active_source == "conversations":
            self._info_text_lbl.configure(
                text="Source: memory.db  •  Recent user turns and assistant responses from conversation_history."
            )

    def refresh_activity(self):
        """Fetch records from all verified activity sources off the UI thread."""
        if self._is_loading:
            return
        self._is_loading = True
        self._refresh_btn.configure(text="⏳ Loading...", state="disabled")

        def _fetch_worker():
            logs = self._read_system_logs()
            steps = self._read_job_steps()
            turns = self._read_conversations()
            try:
                if self.winfo_exists():
                    self.after(0, self._on_activity_loaded, logs, steps, turns)
            except Exception:
                pass

        threading.Thread(target=_fetch_worker, daemon=True, name="ActivityLoader").start()

    def _read_system_logs(self) -> List[Dict[str, str]]:
        log_path = os.path.join(get_project_root(), "orange_ai.log")
        if not os.path.exists(log_path):
            return []
        entries = []
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            for line in lines[-200:]:
                line = line.strip()
                if not line:
                    continue
                match = re.match(r"^(\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}:\d{2},\d+)\s\[([A-Z]+)\]\s(.*)$", line)
                if match:
                    entries.append({
                        "time": match.group(1)[:19],
                        "level": match.group(2),
                        "message": match.group(3),
                    })
                else:
                    if entries:
                        entries[-1]["message"] += "\n" + line
            entries.reverse()
        except Exception:
            pass
        return entries

    def _read_job_steps(self) -> List[Dict[str, Any]]:
        steps_out = []
        try:
            manager = main.get_job_manager()
            jobs = manager.list_jobs(limit=20)
            for j in jobs:
                try:
                    j_steps = manager.get_history(j.id)
                    for s in j_steps:
                        # JobStep models define started_at (and completed_at)
                        raw_time = getattr(s, "started_at", None) or getattr(s, "completed_at", None) or getattr(s, "created_at", "")
                        clean_time = str(raw_time)[:19].replace("T", " ") if raw_time else ""
                        steps_out.append({
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
            steps_out.sort(key=lambda x: x.get("time", ""), reverse=True)
        except Exception:
            pass
        return steps_out

    def _read_conversations(self) -> List[Dict[str, str]]:
        turns_out = []
        try:
            conn = mem.get_connection()
            cursor = conn.execute(
                """
                SELECT role, content, created_at
                FROM conversation_history
                ORDER BY id DESC
                LIMIT 60
                """
            )
            for r in cursor.fetchall():
                turns_out.append({
                    "role": r[0],
                    "content": r[1],
                    "time": r[2][:19].replace("T", " ") if r[2] else "",
                })
        except Exception:
            pass
        return turns_out

    def _on_activity_loaded(self, logs, steps, turns):
        self._is_loading = False
        self._refresh_btn.configure(text="🔄 Refresh", state="normal")
        self._log_entries = logs
        self._job_steps = steps
        self._conversation_turns = turns
        self._update_banner()
        self._render_feed()

    def _render_feed(self):
        for child in self._scroll_frame.winfo_children():
            child.destroy()

        if self._active_source == "logs":
            self._render_logs()
        elif self._active_source == "jobs":
            self._render_job_steps()
        elif self._active_source == "conversations":
            self._render_conversations()

    def _render_logs(self):
        filtered = self._log_entries
        if self._level_filter:
            filtered = [e for e in filtered if e.get("level") == self._level_filter]
        if self._search_query:
            filtered = [e for e in filtered if self._search_query in e.get("message", "").lower()]

        if not filtered:
            self._show_empty_state("No system log entries recorded yet.")
            return

        level_colors = {
            "INFO": ("#2563EB", "#60A5FA"),
            "WARNING": ("#D97706", "#F59E0B"),
            "ERROR": ("#DC2626", "#EF4444"),
        }

        for item in filtered[:100]:
            card = ctk.CTkFrame(self._scroll_frame, fg_color=ThemeColors.WORKSPACE_BG, corner_radius=6)
            card.pack(fill="x", padx=8, pady=3)

            top = ctk.CTkFrame(card, fg_color="transparent")
            top.pack(fill="x", padx=10, pady=(6, 2))

            lvl = item.get("level", "INFO")
            color = level_colors.get(lvl, ThemeColors.TEXT_SECONDARY)
            ctk.CTkLabel(
                top,
                text=f"[{lvl}]",
                font=ctk.CTkFont(family="Consolas", size=11, weight="bold"),
                text_color=color[0] if isinstance(color, tuple) else color,
            ).pack(side="left")

            ctk.CTkLabel(
                top,
                text=item.get("time", ""),
                font=ctk.CTkFont(family="Consolas", size=10),
                text_color=ThemeColors.TEXT_MUTED,
            ).pack(side="right")

            ctk.CTkLabel(
                card,
                text=item.get("message", ""),
                font=ctk.CTkFont(family="Segoe UI", size=12),
                text_color=ThemeColors.TEXT_PRIMARY,
                anchor="w",
                wraplength=620,
                justify="left",
            ).pack(fill="x", padx=10, pady=(0, 6))

    def _render_job_steps(self):
        filtered = self._job_steps
        if self._level_filter == "SUCCESS":
            filtered = [s for s in filtered if s.get("success") is True]
        elif self._level_filter == "FAILED":
            filtered = [s for s in filtered if s.get("success") is False]
        if self._search_query:
            filtered = [
                s for s in filtered
                if self._search_query in s.get("goal", "").lower()
                or self._search_query in s.get("action_type", "").lower()
                or self._search_query in s.get("tool_name", "").lower()
            ]

        if not filtered:
            self._show_empty_state("No job execution step audit records found.")
            return

        for item in filtered[:100]:
            card = ctk.CTkFrame(self._scroll_frame, fg_color=ThemeColors.WORKSPACE_BG, corner_radius=6)
            card.pack(fill="x", padx=8, pady=3)

            top = ctk.CTkFrame(card, fg_color="transparent")
            top.pack(fill="x", padx=10, pady=(6, 2))

            is_succ = item.get("success")
            sym = "✓ SUCCESS" if is_succ is True else ("✕ FAILED" if is_succ is False else "⏳ RUNNING")
            color = "#059669" if is_succ is True else ("#DC2626" if is_succ is False else "#D97706")

            tool_info = f" • Tool: {item['tool_name']}" if item.get("tool_name") else ""
            dur_info = f" [{item['duration_ms']}ms]" if item.get("duration_ms") else ""

            ctk.CTkLabel(
                top,
                text=f"Job [{item['job_id']}] {item['action_type']}{tool_info}",
                font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
                text_color=ThemeColors.TEXT_PRIMARY,
            ).pack(side="left")

            ctk.CTkLabel(
                top,
                text=f"{sym}{dur_info}   {item['time']}",
                font=ctk.CTkFont(family="Segoe UI", size=10, weight="bold"),
                text_color=color,
            ).pack(side="right")

            if item.get("reasoning"):
                ctk.CTkLabel(
                    card,
                    text=f"Reasoning: {item['reasoning']}",
                    font=ctk.CTkFont(family="Segoe UI", size=11),
                    text_color=ThemeColors.TEXT_SECONDARY,
                    anchor="w",
                    wraplength=620,
                    justify="left",
                ).pack(fill="x", padx=10, pady=(0, 2))

            if item.get("error"):
                ctk.CTkLabel(
                    card,
                    text=f"Error: {item['error']}",
                    font=ctk.CTkFont(family="Segoe UI", size=11),
                    text_color="#DC2626",
                    anchor="w",
                    wraplength=620,
                    justify="left",
                ).pack(fill="x", padx=10, pady=(0, 6))

    def _render_conversations(self):
        filtered = self._conversation_turns
        if self._search_query:
            filtered = [t for t in filtered if self._search_query in t.get("content", "").lower()]

        if not filtered:
            self._show_empty_state("No conversation turns recorded yet.")
            return

        for item in filtered[:100]:
            card = ctk.CTkFrame(self._scroll_frame, fg_color=ThemeColors.WORKSPACE_BG, corner_radius=6)
            card.pack(fill="x", padx=8, pady=3)

            top = ctk.CTkFrame(card, fg_color="transparent")
            top.pack(fill="x", padx=10, pady=(6, 2))

            is_user = (item.get("role") == "user")
            badge_text = "👤 User" if is_user else "🍊 Orange AI"
            badge_color = ThemeColors.TEXT_SECONDARY if is_user else ThemeColors.ORANGE_PRIMARY

            ctk.CTkLabel(
                top,
                text=badge_text,
                font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
                text_color=badge_color,
            ).pack(side="left")

            ctk.CTkLabel(
                top,
                text=item.get("time", ""),
                font=ctk.CTkFont(family="Consolas", size=10),
                text_color=ThemeColors.TEXT_MUTED,
            ).pack(side="right")

            ctk.CTkLabel(
                card,
                text=item.get("content", ""),
                font=ctk.CTkFont(family="Segoe UI", size=12),
                text_color=ThemeColors.TEXT_PRIMARY,
                anchor="w",
                wraplength=620,
                justify="left",
            ).pack(fill="x", padx=10, pady=(0, 6))

    def _show_empty_state(self, message: str):
        ctk.CTkLabel(
            self._scroll_frame,
            text=message,
            font=ctk.CTkFont(family="Segoe UI", size=13),
            text_color=ThemeColors.TEXT_MUTED,
            justify="center",
        ).pack(pady=40)

    def _on_theme_changed(self, theme: str):
        self.configure(fg_color=ThemeColors.WORKSPACE_BG)
        self._title_lbl.configure(text_color=ThemeColors.TEXT_PRIMARY)
        self._sub_lbl.configure(text_color=ThemeColors.TEXT_SECONDARY)

"""
gui/views/settings_view.py - Desktop Settings & Configuration View (Stage 4).

Renders user interface preferences and runtime configuration:
- Persisted Theme Selector (Light vs Dark mode via gui.settings & theme_manager)
- Orange API connection endpoint status and live connectivity test
- Runtime assistant parameters and security disclosure (zero secrets on desktop)
- Storage & database locations (memory.db, gui_settings.json, orange_ai.log)
"""

import os
import threading
import time
from typing import Optional, Dict, Any
import customtkinter as ctk

from gui.theme import ThemeColors, theme_manager
from gui.settings import (
    get_theme_preference,
    set_theme_preference,
    get_morning_briefing_enabled,
    set_morning_briefing_enabled,
    get_preferred_briefing_time,
    set_preferred_briefing_time,
    validate_briefing_time,
    get_contextual_advisories_enabled,
    set_contextual_advisories_enabled,
    get_advisory_cooldown_hours,
    set_advisory_cooldown_hours,
)
from brain.api_client import OrangeAPIClient, DEFAULT_ORANGE_API_URL, DEFAULT_ASSIST_TIMEOUT
from brain.paths import get_project_root


class SettingsView(ctk.CTkFrame):
    """View managing desktop settings, theme preference, and backend connectivity inspection."""

    def __init__(self, master, **kwargs):
        super().__init__(
            master,
            fg_color=ThemeColors.WORKSPACE_BG,
            corner_radius=0,
            **kwargs,
        )
        self._api_client = OrangeAPIClient()
        self._is_testing_api = False
        self._last_activation: float = 0.0

        self._build_ui()
        theme_manager.add_listener(self._on_theme_changed)

    def on_activated(self):
        """Lifecycle hook invoked when view is brought to the foreground."""
        now = time.time()
        self._refresh_proactive_controls()
        if now - self._last_activation > 1.0:
            self._last_activation = now
            self._test_api_connection()

    def _build_ui(self):
        # 1. Header
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=28, pady=(20, 10))

        self._title_lbl = ctk.CTkLabel(
            header,
            text="Settings",
            font=ctk.CTkFont(family="Segoe UI", size=22, weight="bold"),
            text_color=ThemeColors.TEXT_PRIMARY,
        )
        self._title_lbl.pack(anchor="w")

        self._sub_lbl = ctk.CTkLabel(
            header,
            text="Configure desktop display preferences, view connection status, and inspect runtime storage.",
            font=ctk.CTkFont(family="Segoe UI", size=13),
            text_color=ThemeColors.TEXT_SECONDARY,
        )
        self._sub_lbl.pack(anchor="w", pady=(2, 0))

        # 2. Main Scrollable Container
        self._scroll_frame = ctk.CTkScrollableFrame(
            self,
            fg_color="transparent",
            corner_radius=0,
        )
        self._scroll_frame.pack(fill="both", expand=True, padx=28, pady=(0, 20))
        self._scroll_frame.grid_columnconfigure(0, weight=1)

        # Section 1: Appearance & Theme
        self._build_appearance_section(self._scroll_frame)

        # Section 2: Proactive Personal Assistance (Stage 4.1)
        self._build_proactive_section(self._scroll_frame)

        # Section 3: Orange API Connection & Status
        self._build_connection_section(self._scroll_frame)

        # Section 4: Model Routing & Security Disclosure
        self._build_model_section(self._scroll_frame)

        # Section 5: Data & File Locations
        self._build_storage_section(self._scroll_frame)

    def _build_appearance_section(self, parent):
        card = ctk.CTkFrame(
            parent,
            fg_color=ThemeColors.SURFACE_CARD_BG,
            border_width=1,
            border_color=ThemeColors.BORDER,
            corner_radius=10,
        )
        card.pack(fill="x", pady=6)

        header = ctk.CTkFrame(card, fg_color="transparent")
        header.pack(fill="x", padx=16, pady=(14, 4))

        ctk.CTkLabel(
            header,
            text="Appearance & Theme",
            font=ctk.CTkFont(family="Segoe UI", size=14, weight="bold"),
            text_color=ThemeColors.TEXT_PRIMARY,
        ).pack(side="left")

        ctk.CTkLabel(
            card,
            text="Choose between the default crisp Light theme and high-contrast Dark theme. Stored in gui_settings.json.",
            font=ctk.CTkFont(family="Segoe UI", size=12),
            text_color=ThemeColors.TEXT_SECONDARY,
            anchor="w",
        ).pack(fill="x", padx=16, pady=(0, 10))

        # Theme Selector Buttons
        btn_box = ctk.CTkFrame(card, fg_color="transparent")
        btn_box.pack(fill="x", padx=16, pady=(0, 14))

        current_theme = get_theme_preference()
        self._theme_light_btn = ctk.CTkButton(
            btn_box,
            text="☀️  Light Mode (Default)",
            width=160,
            height=32,
            corner_radius=16,
            fg_color=ThemeColors.ORANGE_PRIMARY if current_theme == "light" else ThemeColors.WORKSPACE_BG,
            border_width=1,
            border_color=ThemeColors.BORDER,
            hover_color=ThemeColors.SURFACE_CARD_HOVER,
            text_color="#FFFFFF" if current_theme == "light" else ThemeColors.TEXT_PRIMARY,
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold" if current_theme == "light" else "normal"),
            command=lambda: self._select_theme("light"),
        )
        self._theme_light_btn.pack(side="left", padx=(0, 10))

        self._theme_dark_btn = ctk.CTkButton(
            btn_box,
            text="🌙  Dark Mode",
            width=140,
            height=32,
            corner_radius=16,
            fg_color=ThemeColors.ORANGE_PRIMARY if current_theme == "dark" else ThemeColors.WORKSPACE_BG,
            border_width=1,
            border_color=ThemeColors.BORDER,
            hover_color=ThemeColors.SURFACE_CARD_HOVER,
            text_color="#FFFFFF" if current_theme == "dark" else ThemeColors.TEXT_PRIMARY,
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold" if current_theme == "dark" else "normal"),
            command=lambda: self._select_theme("dark"),
        )
        self._theme_dark_btn.pack(side="left")

    def _select_theme(self, theme_val: str):
        theme_manager.set_theme(theme_val)
        set_theme_preference(theme_val)
        self._update_theme_buttons(theme_val)

    def _update_theme_buttons(self, active_theme: str):
        self._theme_light_btn.configure(
            fg_color=ThemeColors.ORANGE_PRIMARY if active_theme == "light" else ThemeColors.WORKSPACE_BG,
            text_color="#FFFFFF" if active_theme == "light" else ThemeColors.TEXT_PRIMARY,
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold" if active_theme == "light" else "normal"),
        )
        self._theme_dark_btn.configure(
            fg_color=ThemeColors.ORANGE_PRIMARY if active_theme == "dark" else ThemeColors.WORKSPACE_BG,
            text_color="#FFFFFF" if active_theme == "dark" else ThemeColors.TEXT_PRIMARY,
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold" if active_theme == "dark" else "normal"),
        )

    # ========================================================================
    # Section: Proactive Personal Assistance (Stage 4.1)
    # ========================================================================

    def _build_proactive_section(self, parent):
        self._proactive_card = ctk.CTkFrame(
            parent,
            fg_color=ThemeColors.SURFACE_CARD_BG,
            border_width=1,
            border_color=ThemeColors.BORDER,
            corner_radius=10,
        )
        self._proactive_card.pack(fill="x", pady=6)

        header = ctk.CTkFrame(self._proactive_card, fg_color="transparent")
        header.pack(fill="x", padx=16, pady=(14, 4))

        ctk.CTkLabel(
            header,
            text="Proactive Personal Assistance",
            font=ctk.CTkFont(family="Segoe UI", size=14, weight="bold"),
            text_color=ThemeColors.TEXT_PRIMARY,
        ).pack(side="left")

        self._proactive_status_lbl = ctk.CTkLabel(
            header,
            text="",
            font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            text_color="#059669",
        )
        self._proactive_status_lbl.pack(side="right")

        ctk.CTkLabel(
            self._proactive_card,
            text="Configure morning briefing schedule and contextual recommendations in conversational chat.",
            font=ctk.CTkFont(family="Segoe UI", size=12),
            text_color=ThemeColors.TEXT_SECONDARY,
            anchor="w",
        ).pack(fill="x", padx=16, pady=(0, 10))

        # Control 1: Morning Briefing Toggle
        row_briefing = ctk.CTkFrame(self._proactive_card, fg_color="transparent")
        row_briefing.pack(fill="x", padx=16, pady=4)

        self._briefing_switch = ctk.CTkSwitch(
            row_briefing,
            text="Morning Briefing",
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
            text_color=ThemeColors.TEXT_PRIMARY,
            progress_color=ThemeColors.ORANGE_PRIMARY,
            command=self._on_briefing_toggle,
        )
        if get_morning_briefing_enabled():
            self._briefing_switch.select()
        else:
            self._briefing_switch.deselect()
        self._briefing_switch.pack(side="left")

        ctk.CTkLabel(
            self._proactive_card,
            text="Deliver a daily summary of project status, deadlines, and system reminders on first launch.",
            font=ctk.CTkFont(family="Segoe UI", size=11),
            text_color=ThemeColors.TEXT_MUTED,
            anchor="w",
        ).pack(fill="x", padx=16, pady=(0, 8))

        # Control 2: Preferred Delivery Time
        row_time = ctk.CTkFrame(self._proactive_card, fg_color="transparent")
        row_time.pack(fill="x", padx=16, pady=4)

        ctk.CTkLabel(
            row_time,
            text="Preferred Delivery Time (24h):",
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
            text_color=ThemeColors.TEXT_PRIMARY,
        ).pack(side="left")

        self._time_entry = ctk.CTkEntry(
            row_time,
            width=80,
            height=28,
            font=ctk.CTkFont(family="Consolas", size=12),
        )
        self._time_entry.insert(0, get_preferred_briefing_time())
        self._time_entry.pack(side="left", padx=(10, 8))

        self._save_time_btn = ctk.CTkButton(
            row_time,
            text="Save Time",
            width=76,
            height=28,
            corner_radius=14,
            fg_color=ThemeColors.WORKSPACE_BG,
            border_width=1,
            border_color=ThemeColors.BORDER,
            hover_color=ThemeColors.SURFACE_CARD_HOVER,
            text_color=ThemeColors.TEXT_PRIMARY,
            font=ctk.CTkFont(family="Segoe UI", size=11),
            command=self._on_save_time,
        )
        self._save_time_btn.pack(side="left")

        self._time_validation_lbl = ctk.CTkLabel(
            row_time,
            text="",
            font=ctk.CTkFont(family="Segoe UI", size=11),
            text_color=ThemeColors.TEXT_MUTED,
        )
        self._time_validation_lbl.pack(side="left", padx=(10, 0))

        ctk.CTkLabel(
            self._proactive_card,
            text="Target delivery time in HH:MM format (default 08:30). Consumed by the Stage 4.3 periodic delivery scheduler.",
            font=ctk.CTkFont(family="Segoe UI", size=11),
            text_color=ThemeColors.TEXT_MUTED,
            anchor="w",
        ).pack(fill="x", padx=16, pady=(0, 8))

        # Control 3: Contextual Chat Advisories Toggle
        row_advisories = ctk.CTkFrame(self._proactive_card, fg_color="transparent")
        row_advisories.pack(fill="x", padx=16, pady=4)

        self._advisories_switch = ctk.CTkSwitch(
            row_advisories,
            text="Contextual Chat Advisories",
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
            text_color=ThemeColors.TEXT_PRIMARY,
            progress_color=ThemeColors.ORANGE_PRIMARY,
            command=self._on_advisories_toggle,
        )
        if get_contextual_advisories_enabled():
            self._advisories_switch.select()
        else:
            self._advisories_switch.deselect()
        self._advisories_switch.pack(side="left")

        ctk.CTkLabel(
            self._proactive_card,
            text="Append contextual recommendations to conversational responses when discussing active projects or jobs.",
            font=ctk.CTkFont(family="Segoe UI", size=11),
            text_color=ThemeColors.TEXT_MUTED,
            anchor="w",
        ).pack(fill="x", padx=16, pady=(0, 8))

        # Control 4: Advisory Cooldown Dropdown
        row_cooldown = ctk.CTkFrame(self._proactive_card, fg_color="transparent")
        row_cooldown.pack(fill="x", padx=16, pady=4)

        ctk.CTkLabel(
            row_cooldown,
            text="Minimum Advisory Cooldown:",
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
            text_color=ThemeColors.TEXT_PRIMARY,
        ).pack(side="left")

        current_hours = get_advisory_cooldown_hours()
        cooldown_map = {2: "2 hours", 4: "4 hours (Default)", 8: "8 hours"}
        initial_val = cooldown_map.get(current_hours, "4 hours (Default)")

        self._cooldown_dropdown = ctk.CTkOptionMenu(
            row_cooldown,
            values=["2 hours", "4 hours (Default)", "8 hours"],
            width=150,
            height=28,
            fg_color=ThemeColors.WORKSPACE_BG,
            button_color=ThemeColors.WORKSPACE_BG,
            button_hover_color=ThemeColors.SURFACE_CARD_HOVER,
            text_color=ThemeColors.TEXT_PRIMARY,
            font=ctk.CTkFont(family="Segoe UI", size=11),
            dropdown_font=ctk.CTkFont(family="Segoe UI", size=11),
            command=self._on_cooldown_selected,
        )
        self._cooldown_dropdown.set(initial_val)
        self._cooldown_dropdown.pack(side="left", padx=(10, 0))

        ctk.CTkLabel(
            self._proactive_card,
            text="Minimum quiet period between consecutive chat advisories for the same entity unless explicitly requested.",
            font=ctk.CTkFont(family="Segoe UI", size=11),
            text_color=ThemeColors.TEXT_MUTED,
            anchor="w",
        ).pack(fill="x", padx=16, pady=(0, 14))

    def _flash_proactive_status(self, msg: str, is_error: bool = False):
        if hasattr(self, "_proactive_status_lbl") and self._proactive_status_lbl.winfo_exists():
            self._proactive_status_lbl.configure(
                text=msg,
                text_color="#DC2626" if is_error else "#059669",
            )
            try:
                self.after(3000, self._clear_proactive_status)
            except Exception:
                pass

    def _clear_proactive_status(self):
        if hasattr(self, "_proactive_status_lbl") and self._proactive_status_lbl.winfo_exists():
            self._proactive_status_lbl.configure(text="")

    def _on_briefing_toggle(self):
        val = bool(self._briefing_switch.get())
        set_morning_briefing_enabled(val)
        txt = "✓ Morning Briefing Enabled" if val else "✓ Morning Briefing Disabled"
        self._flash_proactive_status(txt)

    def _on_save_time(self):
        val = self._time_entry.get().strip()
        if validate_briefing_time(val):
            set_preferred_briefing_time(val)
            if hasattr(self, "_time_validation_lbl") and self._time_validation_lbl.winfo_exists():
                self._time_validation_lbl.configure(text="✓ Saved", text_color="#059669")
            self._flash_proactive_status(f"✓ Briefing Time Saved ({val})")
        else:
            current = get_preferred_briefing_time()
            if hasattr(self, "_time_validation_lbl") and self._time_validation_lbl.winfo_exists():
                self._time_validation_lbl.configure(text=f"Invalid (reverted to {current})", text_color="#DC2626")
            self._time_entry.delete(0, "end")
            self._time_entry.insert(0, current)
            self._flash_proactive_status("Error: Use HH:MM format", is_error=True)

    def _on_advisories_toggle(self):
        val = bool(self._advisories_switch.get())
        set_contextual_advisories_enabled(val)
        txt = "✓ Contextual Advisories Enabled" if val else "✓ Contextual Advisories Disabled"
        self._flash_proactive_status(txt)

    def _on_cooldown_selected(self, choice: str):
        choice_map = {"2 hours": 2, "4 hours (Default)": 4, "8 hours": 8}
        hours = choice_map.get(choice, 4)
        set_advisory_cooldown_hours(hours)
        self._flash_proactive_status(f"✓ Cooldown set to {hours}h")

    def _refresh_proactive_controls(self):
        """Synchronize UI control states with persisted backend settings without triggering save callbacks."""
        try:
            # 1. Morning briefing switch
            if hasattr(self, "_briefing_switch") and self._briefing_switch.winfo_exists():
                if get_morning_briefing_enabled():
                    self._briefing_switch.select()
                else:
                    self._briefing_switch.deselect()

            # 2. Preferred time entry
            if hasattr(self, "_time_entry") and self._time_entry.winfo_exists():
                cur_val = get_preferred_briefing_time()
                self._time_entry.delete(0, "end")
                self._time_entry.insert(0, cur_val)
                if hasattr(self, "_time_validation_lbl") and self._time_validation_lbl.winfo_exists():
                    self._time_validation_lbl.configure(text="")

            # 3. Contextual advisories switch
            if hasattr(self, "_advisories_switch") and self._advisories_switch.winfo_exists():
                if get_contextual_advisories_enabled():
                    self._advisories_switch.select()
                else:
                    self._advisories_switch.deselect()

            # 4. Cooldown dropdown
            if hasattr(self, "_cooldown_dropdown") and self._cooldown_dropdown.winfo_exists():
                hrs = get_advisory_cooldown_hours()
                cooldown_map = {2: "2 hours", 4: "4 hours (Default)", 8: "8 hours"}
                self._cooldown_dropdown.set(cooldown_map.get(hrs, "4 hours (Default)"))
        except Exception:
            pass

    def _build_connection_section(self, parent):
        card = ctk.CTkFrame(
            parent,
            fg_color=ThemeColors.SURFACE_CARD_BG,
            border_width=1,
            border_color=ThemeColors.BORDER,
            corner_radius=10,
        )
        card.pack(fill="x", pady=6)

        header = ctk.CTkFrame(card, fg_color="transparent")
        header.pack(fill="x", padx=16, pady=(14, 4))

        ctk.CTkLabel(
            header,
            text="Orange API Backend Connection",
            font=ctk.CTkFont(family="Segoe UI", size=14, weight="bold"),
            text_color=ThemeColors.TEXT_PRIMARY,
        ).pack(side="left")

        self._api_status_badge = ctk.CTkLabel(
            header,
            text="● Checking...",
            font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            text_color=ThemeColors.TEXT_MUTED,
        )
        self._api_status_badge.pack(side="right")

        ctk.CTkLabel(
            card,
            text="Configured HTTP service endpoint for conversational AI inference and remote tools.",
            font=ctk.CTkFont(family="Segoe UI", size=12),
            text_color=ThemeColors.TEXT_SECONDARY,
            anchor="w",
        ).pack(fill="x", padx=16, pady=(0, 10))

        # Endpoint display row
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(0, 14))

        ctk.CTkLabel(
            row,
            text=f"Endpoint: {self._api_client.api_url}",
            font=ctk.CTkFont(family="Consolas", size=12),
            text_color=ThemeColors.TEXT_PRIMARY,
        ).pack(side="left")

        self._test_conn_btn = ctk.CTkButton(
            row,
            text="Test Connection",
            width=120,
            height=28,
            corner_radius=14,
            fg_color=ThemeColors.WORKSPACE_BG,
            border_width=1,
            border_color=ThemeColors.BORDER,
            hover_color=ThemeColors.SURFACE_CARD_HOVER,
            text_color=ThemeColors.TEXT_PRIMARY,
            font=ctk.CTkFont(family="Segoe UI", size=11),
            command=self._test_api_connection,
        )
        self._test_conn_btn.pack(side="right")

        # Initial check
        self._test_api_connection()

    def _test_api_connection(self):
        if self._is_testing_api:
            return
        self._is_testing_api = True
        self._api_status_badge.configure(text="● Testing...", text_color=ThemeColors.TEXT_MUTED)
        self._test_conn_btn.configure(state="disabled")

        def _worker():
            online = self._api_client.is_available(timeout=2.0)
            try:
                if self.winfo_exists():
                    self.after(0, self._on_connection_tested, online)
            except Exception:
                pass

        threading.Thread(target=_worker, daemon=True, name="ApiTester").start()

    def _on_connection_tested(self, online: bool):
        self._is_testing_api = False
        self._test_conn_btn.configure(state="normal")
        if online:
            self._api_status_badge.configure(text="● Online & Connected", text_color="#059669")
        else:
            self._api_status_badge.configure(text="● Offline / Unreachable", text_color="#DC2626")

    def _build_model_section(self, parent):
        card = ctk.CTkFrame(
            parent,
            fg_color=ThemeColors.SURFACE_CARD_BG,
            border_width=1,
            border_color=ThemeColors.BORDER,
            corner_radius=10,
        )
        card.pack(fill="x", pady=6)

        ctk.CTkLabel(
            card,
            text="Architecture & Privacy Disclosure",
            font=ctk.CTkFont(family="Segoe UI", size=14, weight="bold"),
            text_color=ThemeColors.TEXT_PRIMARY,
            anchor="w",
        ).pack(fill="x", padx=16, pady=(14, 4))

        ctk.CTkLabel(
            card,
            text="• Assistant Engine: Phase 4 Autonomous Agent Loop with reflection and recovery.\n• Router: Deterministic intent classifier with zero external latency.\n• Privacy & Security: Zero API keys or LLM provider secrets are stored on the Desktop client. Secrets are held exclusively within the isolated backend environment.",
            font=ctk.CTkFont(family="Segoe UI", size=12),
            text_color=ThemeColors.TEXT_SECONDARY,
            anchor="w",
            wraplength=640,
            justify="left",
        ).pack(fill="x", padx=16, pady=(0, 14))

    def _build_storage_section(self, parent):
        card = ctk.CTkFrame(
            parent,
            fg_color=ThemeColors.SURFACE_CARD_BG,
            border_width=1,
            border_color=ThemeColors.BORDER,
            corner_radius=10,
        )
        card.pack(fill="x", pady=6)

        ctk.CTkLabel(
            card,
            text="Persistent Storage & Data Files",
            font=ctk.CTkFont(family="Segoe UI", size=14, weight="bold"),
            text_color=ThemeColors.TEXT_PRIMARY,
            anchor="w",
        ).pack(fill="x", padx=16, pady=(14, 4))

        root_dir = get_project_root()
        paths = [
            ("Memory Database (SQLite)", os.path.join(root_dir, "memory.db")),
            ("Persistent Jobs Database", os.path.join(root_dir, "jobs.db")),
            ("UI Settings (JSON)", os.path.join(root_dir, "gui_settings.json")),
            ("Persistent System Logs", os.path.join(root_dir, "orange_ai.log")),
        ]

        for label, path in paths:
            row = ctk.CTkFrame(card, fg_color="transparent")
            row.pack(fill="x", padx=16, pady=2)

            ctk.CTkLabel(
                row,
                text=f"{label}:",
                width=180,
                anchor="w",
                font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
                text_color=ThemeColors.TEXT_SECONDARY,
            ).pack(side="left")

            ctk.CTkLabel(
                row,
                text=path,
                anchor="w",
                font=ctk.CTkFont(family="Consolas", size=11),
                text_color=ThemeColors.TEXT_MUTED,
            ).pack(side="left", fill="x", expand=True)

        ctk.CTkFrame(card, height=8, fg_color="transparent").pack()

    def _on_theme_changed(self, theme: str):
        self.configure(fg_color=ThemeColors.WORKSPACE_BG)
        self._title_lbl.configure(text_color=ThemeColors.TEXT_PRIMARY)
        self._sub_lbl.configure(text_color=ThemeColors.TEXT_SECONDARY)
        self._update_theme_buttons(theme)
        if hasattr(self, "_proactive_card") and self._proactive_card.winfo_exists():
            self._proactive_card.configure(
                fg_color=ThemeColors.SURFACE_CARD_BG,
                border_color=ThemeColors.BORDER,
            )

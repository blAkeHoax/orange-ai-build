"""
gui/views/tools_view.py - Tool Registry & Capabilities Catalog View (Stage 4).

Renders audited Desktop tools registered in the central ToolRegistry:
- Tool names, descriptions, categories (system, files, processes, apps)
- Risk level classification (SAFE, CONFIRMATION_REQUIRED)
- Fast search and category/risk filtering
- Safe "Run Query in Chat" capability routing through existing assistant engine
- Zero direct execution of protected tool functions from UI callbacks
"""

import time
from typing import Optional, List, Dict, Any
import customtkinter as ctk

from gui.theme import ThemeColors, theme_manager
from tools.registry import Tool, registry
from tools.permissions import RiskLevel


class ToolsView(ctk.CTkFrame):
    """View presenting registered tools from ToolRegistry with search and category filters."""

    def __init__(self, master, on_run_in_chat=None, **kwargs):
        super().__init__(
            master,
            fg_color=ThemeColors.WORKSPACE_BG,
            corner_radius=0,
            **kwargs,
        )
        self._on_run_in_chat = on_run_in_chat
        self._all_tools: List[Tool] = []
        self._active_category: Optional[str] = None
        self._active_risk: Optional[str] = None
        self._search_query: str = ""
        self._last_activation: float = 0.0

        self._build_ui()
        theme_manager.add_listener(self._on_theme_changed)
        self._load_tools()

    def on_activated(self):
        """Lifecycle hook invoked when view is brought to the foreground."""
        now = time.time()
        if now - self._last_activation > 1.0:
            self._last_activation = now
            self._load_tools()

    def _build_ui(self):
        # 1. Header
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=28, pady=(20, 10))

        title_box = ctk.CTkFrame(header, fg_color="transparent")
        title_box.pack(side="left")

        self._title_lbl = ctk.CTkLabel(
            title_box,
            text="Tools Catalog",
            font=ctk.CTkFont(family="Segoe UI", size=22, weight="bold"),
            text_color=ThemeColors.TEXT_PRIMARY,
        )
        self._title_lbl.pack(anchor="w")

        self._sub_lbl = ctk.CTkLabel(
            title_box,
            text="Audited system, file, process, and application tools available to Orange AI.",
            font=ctk.CTkFont(family="Segoe UI", size=13),
            text_color=ThemeColors.TEXT_SECONDARY,
        )
        self._sub_lbl.pack(anchor="w", pady=(2, 0))

        # Stats Badge
        self._count_badge = ctk.CTkLabel(
            header,
            text="0 Tools Registered",
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
            text_color=ThemeColors.ORANGE_PRIMARY,
        )
        self._count_badge.pack(side="right")

        # 2. Filter & Search Controls Bar
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

        cat_box = ctk.CTkFrame(controls, fg_color="transparent")
        cat_box.pack(side="left", padx=12, pady=8)

        self._cat_buttons: Dict[str, ctk.CTkButton] = {}
        cats = [
            ("All", None),
            ("System", "system"),
            ("Files", "files"),
            ("Processes", "processes"),
            ("Apps", "apps"),
        ]

        for label, val in cats:
            btn = ctk.CTkButton(
                cat_box,
                text=label,
                width=65,
                height=26,
                corner_radius=13,
                fg_color=ThemeColors.ORANGE_PRIMARY if val == self._active_category else "transparent",
                hover_color=ThemeColors.SURFACE_CARD_HOVER,
                text_color="#FFFFFF" if val == self._active_category else ThemeColors.TEXT_SECONDARY,
                font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold" if val == self._active_category else "normal"),
                command=lambda v=val: self._set_category(v),
            )
            btn.pack(side="left", padx=2)
            self._cat_buttons[label] = btn

        # Search field
        self._search_entry = ctk.CTkEntry(
            controls,
            placeholder_text="Search tools by name or description...",
            placeholder_text_color=ThemeColors.TEXT_MUTED,
            height=28,
            width=240,
            border_width=1,
            border_color=ThemeColors.BORDER,
            fg_color=ThemeColors.WORKSPACE_BG,
            font=ctk.CTkFont(family="Segoe UI", size=12),
        )
        self._search_entry.pack(side="right", padx=12, pady=10)
        self._search_entry.bind("<KeyRelease>", self._on_search_changed)

        # 3. Main Tools Scrollable Grid
        self._scroll_frame = ctk.CTkScrollableFrame(
            self,
            fg_color=ThemeColors.SURFACE_CARD_BG,
            border_width=1,
            border_color=ThemeColors.BORDER,
            corner_radius=12,
        )
        self._scroll_frame.pack(fill="both", expand=True, padx=28, pady=(0, 20))
        self._scroll_frame.grid_columnconfigure((0, 1), weight=1)

    def _load_tools(self):
        """Fetch all tools directly from the live ToolRegistry."""
        try:
            self._all_tools = registry.list_tools()
        except Exception:
            self._all_tools = []
        self._count_badge.configure(text=f"{len(self._all_tools)} Tools Registered")
        self._render_tools()

    def _set_category(self, category: Optional[str]):
        self._active_category = category
        for label, btn in self._cat_buttons.items():
            val = None if label == "All" else label.lower()
            is_active = (val == self._active_category)
            btn.configure(
                fg_color=ThemeColors.ORANGE_PRIMARY if is_active else "transparent",
                text_color="#FFFFFF" if is_active else ThemeColors.TEXT_SECONDARY,
                font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold" if is_active else "normal"),
            )
        self._render_tools()

    def _on_search_changed(self, event=None):
        self._search_query = self._search_entry.get().strip().lower()
        self._render_tools()

    def _render_tools(self):
        for child in self._scroll_frame.winfo_children():
            child.destroy()

        filtered = self._all_tools
        if self._active_category:
            filtered = [t for t in filtered if t.category.lower() == self._active_category]
        if self._search_query:
            filtered = [
                t for t in filtered
                if self._search_query in t.name.lower() or self._search_query in t.description.lower() or self._search_query in t.category.lower()
            ]

        if not filtered:
            ctk.CTkLabel(
                self._scroll_frame,
                text="No tools match the selected category or search filter.",
                font=ctk.CTkFont(family="Segoe UI", size=13),
                text_color=ThemeColors.TEXT_MUTED,
                justify="center",
            ).pack(pady=40)
            return

        # Render in a clean 2-column card layout
        for idx, tool in enumerate(filtered):
            row = idx // 2
            col = idx % 2
            card = self._create_tool_card(self._scroll_frame, tool)
            card.grid(row=row, column=col, sticky="nsew", padx=6, pady=6)

    def _create_tool_card(self, parent, tool: Tool) -> ctk.CTkFrame:
        card = ctk.CTkFrame(
            parent,
            fg_color=ThemeColors.WORKSPACE_BG,
            border_width=1,
            border_color=ThemeColors.BORDER,
            corner_radius=8,
        )

        top_row = ctk.CTkFrame(card, fg_color="transparent")
        top_row.pack(fill="x", padx=12, pady=(10, 4))

        # Tool Name
        name_lbl = ctk.CTkLabel(
            top_row,
            text=tool.name,
            font=ctk.CTkFont(family="Consolas", size=12, weight="bold"),
            text_color=ThemeColors.TEXT_PRIMARY,
        )
        name_lbl.pack(side="left")

        # Category Badge
        cat_badge = ctk.CTkLabel(
            top_row,
            text=f"[{tool.category.upper()}]",
            font=ctk.CTkFont(family="Segoe UI", size=10, weight="bold"),
            text_color=ThemeColors.TEXT_MUTED,
        )
        cat_badge.pack(side="left", padx=6)

        # Risk Badge
        risk_val = tool.risk_level.value
        risk_color = "#059669" if risk_val == "SAFE" else ("#D97706" if risk_val == "CONFIRMATION_REQUIRED" else "#DC2626")
        risk_lbl = ctk.CTkLabel(
            top_row,
            text=risk_val,
            font=ctk.CTkFont(family="Segoe UI", size=10, weight="bold"),
            text_color=risk_color,
        )
        risk_lbl.pack(side="right")

        # Description
        desc_lbl = ctk.CTkLabel(
            card,
            text=tool.description,
            font=ctk.CTkFont(family="Segoe UI", size=12),
            text_color=ThemeColors.TEXT_SECONDARY,
            anchor="w",
            wraplength=340,
            justify="left",
        )
        desc_lbl.pack(fill="x", padx=12, pady=(0, 6))

        # Action Row (Safe Handoff to Chat Composer)
        bottom_row = ctk.CTkFrame(card, fg_color="transparent")
        bottom_row.pack(fill="x", padx=12, pady=(0, 10))

        run_btn = ctk.CTkButton(
            bottom_row,
            text="Use in Chat 💬",
            width=105,
            height=26,
            corner_radius=13,
            fg_color=ThemeColors.ORANGE_PRIMARY,
            hover_color=ThemeColors.ORANGE_HOVER,
            text_color="#FFFFFF",
            font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            command=lambda: self._on_run_tool(tool),
        )
        run_btn.pack(side="right")

        return card

    def _on_run_tool(self, tool: Tool):
        """Safely hand off tool query to Chat composer without direct execution."""
        if self._on_run_in_chat:
            query = f"Use tool {tool.name}"
            self._on_run_in_chat(query)

    def _on_theme_changed(self, theme: str):
        self.configure(fg_color=ThemeColors.WORKSPACE_BG)
        self._title_lbl.configure(text_color=ThemeColors.TEXT_PRIMARY)
        self._sub_lbl.configure(text_color=ThemeColors.TEXT_SECONDARY)

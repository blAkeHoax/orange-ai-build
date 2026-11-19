"""
gui/views/memory_view.py - Memory 2.0 Inspection & Management View (Stage 4).

Renders long-term and structured memories using official Memory 2.0 backend:
- Structured memories categorized by Profile, Projects, Tools, Preferences, Facts
- Legacy unstructured memory review
- Real-time search across keys, values, and content
- Privacy-aware deletion and retraction with explicit confirmation dialogs
- Stats summary (total structured, categories, legacy)
- Non-blocking background data fetching
"""

import threading
import time
from typing import Optional, List, Dict, Any
import customtkinter as ctk

from gui.theme import ThemeColors, theme_manager
import brain.memory as mem


class MemoryView(ctk.CTkFrame):
    """View presenting stored memories, category inspection, and privacy retractions."""

    def __init__(self, master, **kwargs):
        super().__init__(
            master,
            fg_color=ThemeColors.WORKSPACE_BG,
            corner_radius=0,
            **kwargs,
        )
        self._structured_memories: List[Dict[str, Any]] = []
        self._legacy_memories: List[str] = []
        self._active_category: Optional[str] = None
        self._search_query: str = ""
        self._is_loading: bool = False
        self._last_activation: float = 0.0

        self._build_ui()
        theme_manager.add_listener(self._on_theme_changed)
        self.refresh_memories()

    def on_activated(self):
        """Lifecycle hook invoked when view is brought to the foreground."""
        now = time.time()
        if now - self._last_activation > 1.0:
            self._last_activation = now
            self.refresh_memories()

    def _build_ui(self):
        # 1. Header Area
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=28, pady=(20, 10))

        title_box = ctk.CTkFrame(header, fg_color="transparent")
        title_box.pack(side="left")

        self._title_lbl = ctk.CTkLabel(
            title_box,
            text="Memory",
            font=ctk.CTkFont(family="Segoe UI", size=22, weight="bold"),
            text_color=ThemeColors.TEXT_PRIMARY,
        )
        self._title_lbl.pack(anchor="w")

        self._sub_lbl = ctk.CTkLabel(
            title_box,
            text="Browse, search, and manage your stored profile, projects, tools, and preferences.",
            font=ctk.CTkFont(family="Segoe UI", size=13),
            text_color=ThemeColors.TEXT_SECONDARY,
        )
        self._sub_lbl.pack(anchor="w", pady=(2, 0))

        # Top Buttons: Add Memory & Refresh
        top_btns = ctk.CTkFrame(header, fg_color="transparent")
        top_btns.pack(side="right")

        self._add_btn = ctk.CTkButton(
            top_btns,
            text="+ Remember Fact",
            width=120,
            height=32,
            corner_radius=16,
            fg_color=ThemeColors.ORANGE_PRIMARY,
            hover_color=ThemeColors.ORANGE_HOVER,
            text_color="#FFFFFF",
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
            command=self._show_add_dialog,
        )
        self._add_btn.pack(side="left", padx=(0, 8))

        self._refresh_btn = ctk.CTkButton(
            top_btns,
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
            command=self.refresh_memories,
        )
        self._refresh_btn.pack(side="left")

        # 2. Stats Bar (3 Metric Cards)
        self._stats_frame = ctk.CTkFrame(self, fg_color="transparent")
        self._stats_frame.pack(fill="x", padx=28, pady=(0, 12))
        self._stats_frame.grid_columnconfigure((0, 1, 2), weight=1)

        self._stat_structured = self._create_stat_card(self._stats_frame, 0, "Structured Facts", "0")
        self._stat_categories = self._create_stat_card(self._stats_frame, 1, "Categories", "0")
        self._stat_legacy = self._create_stat_card(self._stats_frame, 2, "Legacy Memories", "0")

        # 3. Filter & Search Controls Bar
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

        # Category Filter Pills
        cat_box = ctk.CTkFrame(controls, fg_color="transparent")
        cat_box.pack(side="left", padx=12, pady=8)

        self._cat_buttons: Dict[str, ctk.CTkButton] = {}
        categories = [
            ("All", None),
            ("Profile", "user"),
            ("Projects", "project"),
            ("Tools", "tool"),
            ("Preferences", "preference"),
            ("Legacy", "legacy"),
        ]

        for label, val in categories:
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
                command=lambda v=val: self._set_category_filter(v),
            )
            btn.pack(side="left", padx=2)
            self._cat_buttons[label] = btn

        # Search field
        self._search_entry = ctk.CTkEntry(
            controls,
            placeholder_text="Search memories by keyword...",
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

        # 4. Confirmation / Dialog Container
        self._dialog_container = ctk.CTkFrame(self, fg_color="transparent")
        self._dialog_container.pack(fill="x", padx=28, pady=(0, 6))

        # 5. Main Scrollable List Area
        self._scroll_frame = ctk.CTkScrollableFrame(
            self,
            fg_color=ThemeColors.SURFACE_CARD_BG,
            border_width=1,
            border_color=ThemeColors.BORDER,
            corner_radius=12,
        )
        self._scroll_frame.pack(fill="both", expand=True, padx=28, pady=(0, 20))
        self._scroll_frame.grid_columnconfigure(0, weight=1)

    def _create_stat_card(self, parent, col: int, title: str, value: str) -> ctk.CTkLabel:
        card = ctk.CTkFrame(
            parent,
            fg_color=ThemeColors.SURFACE_CARD_BG,
            border_width=1,
            border_color=ThemeColors.BORDER,
            corner_radius=10,
            height=58,
        )
        card.grid(row=0, column=col, sticky="nsew", padx=4)
        card.pack_propagate(False)

        ctk.CTkLabel(
            card,
            text=title,
            font=ctk.CTkFont(family="Segoe UI", size=11),
            text_color=ThemeColors.TEXT_SECONDARY,
        ).pack(anchor="w", padx=12, pady=(8, 0))

        val_lbl = ctk.CTkLabel(
            card,
            text=value,
            font=ctk.CTkFont(family="Segoe UI", size=18, weight="bold"),
            text_color=ThemeColors.TEXT_PRIMARY,
        )
        val_lbl.pack(anchor="w", padx=12, pady=(0, 6))
        return val_lbl

    def _set_category_filter(self, category: Optional[str]):
        self._active_category = category
        for label, btn in self._cat_buttons.items():
            val = None if label == "All" else ("legacy" if label == "Legacy" else label.lower().rstrip("s"))
            if label == "Profile":
                val = "user"
            is_active = (val == self._active_category)
            btn.configure(
                fg_color=ThemeColors.ORANGE_PRIMARY if is_active else "transparent",
                text_color="#FFFFFF" if is_active else ThemeColors.TEXT_SECONDARY,
                font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold" if is_active else "normal"),
            )
        self._render_memory_items()

    def _on_search_changed(self, event=None):
        self._search_query = self._search_entry.get().strip().lower()
        self._render_memory_items()

    def refresh_memories(self):
        """Load structured and legacy memories in a worker thread."""
        if self._is_loading:
            return
        self._is_loading = True
        self._refresh_btn.configure(text="⏳ Loading...", state="disabled")

        def _worker():
            try:
                mem.initialize_memory()
                structured = mem.recall_structured()
                legacy = mem.recall(limit=50)
                stats = mem.memory_stats()
            except Exception:
                structured, legacy, stats = [], [], {}
            try:
                if self.winfo_exists():
                    self.after(0, self._on_memories_loaded, structured, legacy, stats)
            except Exception:
                pass

        threading.Thread(target=_worker, daemon=True, name="MemoryLoader").start()

    def _on_memories_loaded(self, structured: List[Dict[str, Any]], legacy: List[str], stats: Dict[str, Any]):
        self._is_loading = False
        self._refresh_btn.configure(text="🔄 Refresh", state="normal")
        self._structured_memories = structured
        self._legacy_memories = legacy

        # Update stats cards
        self._stat_structured.configure(text=str(len(self._structured_memories)))
        categories = set(m.get("category", "") for m in self._structured_memories)
        self._stat_categories.configure(text=str(len(categories)))
        self._stat_legacy.configure(text=str(len(self._legacy_memories)))

        self._render_memory_items()

    def _render_memory_items(self):
        for child in self._scroll_frame.winfo_children():
            child.destroy()

        items_rendered = 0

        # 1. Render Structured Memories if not strictly filtered to legacy
        if self._active_category != "legacy":
            for m in self._structured_memories:
                cat = m.get("category", "").lower()
                key = m.get("key", "")
                val = m.get("value", "")

                if self._active_category and cat != self._active_category:
                    continue
                if self._search_query and (self._search_query not in key.lower() and self._search_query not in val.lower() and self._search_query not in cat):
                    continue

                card = self._create_structured_card(self._scroll_frame, m)
                card.pack(fill="x", padx=8, pady=4)
                items_rendered += 1

        # 2. Render Legacy Memories if 'All' or 'Legacy'
        if self._active_category in (None, "legacy"):
            for text in self._legacy_memories:
                if self._search_query and self._search_query not in text.lower():
                    continue

                card = self._create_legacy_card(self._scroll_frame, text)
                card.pack(fill="x", padx=8, pady=4)
                items_rendered += 1

        if items_rendered == 0:
            msg = "No memories match the current filter or search." if (self._structured_memories or self._legacy_memories) else "No memories stored yet.\nTeach Orange AI facts in Chat or click '+ Remember Fact'."
            empty_lbl = ctk.CTkLabel(
                self._scroll_frame,
                text=msg,
                font=ctk.CTkFont(family="Segoe UI", size=13),
                text_color=ThemeColors.TEXT_MUTED,
                justify="center",
            )
            empty_lbl.pack(pady=40)

    def _create_structured_card(self, parent, m: Dict[str, Any]) -> ctk.CTkFrame:
        card = ctk.CTkFrame(
            parent,
            fg_color=ThemeColors.WORKSPACE_BG,
            border_width=1,
            border_color=ThemeColors.BORDER,
            corner_radius=8,
        )

        top_row = ctk.CTkFrame(card, fg_color="transparent")
        top_row.pack(fill="x", padx=12, pady=(8, 2))

        # Category Badge
        cat_name = m.get("category", "FACT").upper()
        badge = ctk.CTkLabel(
            top_row,
            text=f"[{cat_name}]",
            font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            text_color=ThemeColors.ORANGE_PRIMARY,
        )
        badge.pack(side="left")

        # Memory Key
        key_lbl = ctk.CTkLabel(
            top_row,
            text=f"  {m.get('key', '')}",
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
            text_color=ThemeColors.TEXT_PRIMARY,
        )
        key_lbl.pack(side="left")

        # Forget / Retire Button
        del_btn = ctk.CTkButton(
            top_row,
            text="Forget",
            width=60,
            height=22,
            corner_radius=11,
            fg_color="transparent",
            hover_color=ThemeColors.SURFACE_CARD_HOVER,
            text_color=ThemeColors.TEXT_MUTED,
            font=ctk.CTkFont(family="Segoe UI", size=11),
            command=lambda: self._prompt_forget_structured(m),
        )
        del_btn.pack(side="right")

        # Memory Value
        val_lbl = ctk.CTkLabel(
            card,
            text=m.get("value", ""),
            font=ctk.CTkFont(family="Segoe UI", size=13),
            text_color=ThemeColors.TEXT_PRIMARY,
            anchor="w",
            wraplength=600,
            justify="left",
        )
        val_lbl.pack(fill="x", padx=12, pady=(2, 8))

        return card

    def _create_legacy_card(self, parent, text: str) -> ctk.CTkFrame:
        card = ctk.CTkFrame(
            parent,
            fg_color=ThemeColors.WORKSPACE_BG,
            border_width=1,
            border_color=ThemeColors.BORDER,
            corner_radius=8,
        )

        top_row = ctk.CTkFrame(card, fg_color="transparent")
        top_row.pack(fill="x", padx=12, pady=(8, 2))

        badge = ctk.CTkLabel(
            top_row,
            text="[LEGACY]",
            font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            text_color=ThemeColors.TEXT_MUTED,
        )
        badge.pack(side="left")

        del_btn = ctk.CTkButton(
            top_row,
            text="Forget",
            width=60,
            height=22,
            corner_radius=11,
            fg_color="transparent",
            hover_color=ThemeColors.SURFACE_CARD_HOVER,
            text_color=ThemeColors.TEXT_MUTED,
            font=ctk.CTkFont(family="Segoe UI", size=11),
            command=lambda: self._prompt_forget_legacy(text),
        )
        del_btn.pack(side="right")

        val_lbl = ctk.CTkLabel(
            card,
            text=text,
            font=ctk.CTkFont(family="Segoe UI", size=13),
            text_color=ThemeColors.TEXT_PRIMARY,
            anchor="w",
            wraplength=600,
            justify="left",
        )
        val_lbl.pack(fill="x", padx=12, pady=(2, 8))

        return card

    def _prompt_forget_structured(self, m: Dict[str, Any]):
        """Show explicit confirmation card before retracting a structured memory."""
        self._clear_dialogs()
        cat = m.get("category", "")
        key = m.get("key", "")

        card = ctk.CTkFrame(
            self._dialog_container,
            fg_color=ThemeColors.SURFACE_CARD_BG,
            border_width=1,
            border_color=ThemeColors.ORANGE_PRIMARY,
            corner_radius=10,
        )
        card.pack(fill="x", pady=4)

        msg = f"⚠️ Forget structured memory [{cat.upper()}] '{key}'? This will permanently retract the stored fact and prune history."
        ctk.CTkLabel(
            card,
            text=msg,
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
            text_color=ThemeColors.TEXT_PRIMARY,
            wraplength=500,
            justify="left",
        ).pack(side="left", padx=16, pady=10)

        btn_box = ctk.CTkFrame(card, fg_color="transparent")
        btn_box.pack(side="right", padx=16, pady=8)

        ctk.CTkButton(
            btn_box,
            text="Confirm Forget",
            width=110,
            height=28,
            corner_radius=14,
            fg_color="#DC2626",
            hover_color="#EF4444",
            font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            command=lambda: self._execute_retraction(cat, key),
        ).pack(side="left", padx=(0, 6))

        ctk.CTkButton(
            btn_box,
            text="Cancel",
            width=70,
            height=28,
            corner_radius=14,
            fg_color="transparent",
            hover_color=ThemeColors.SURFACE_CARD_HOVER,
            text_color=ThemeColors.TEXT_SECONDARY,
            font=ctk.CTkFont(family="Segoe UI", size=11),
            command=self._clear_dialogs,
        ).pack(side="left")

    def _prompt_forget_legacy(self, text: str):
        """Show explicit confirmation card before forgetting a legacy memory."""
        self._clear_dialogs()
        card = ctk.CTkFrame(
            self._dialog_container,
            fg_color=ThemeColors.SURFACE_CARD_BG,
            border_width=1,
            border_color=ThemeColors.ORANGE_PRIMARY,
            corner_radius=10,
        )
        card.pack(fill="x", pady=4)

        short_text = text[:40] + ("..." if len(text) > 40 else "")
        msg = f"⚠️ Forget legacy memory matching '{short_text}'?"
        ctk.CTkLabel(
            card,
            text=msg,
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
            text_color=ThemeColors.TEXT_PRIMARY,
        ).pack(side="left", padx=16, pady=10)

        btn_box = ctk.CTkFrame(card, fg_color="transparent")
        btn_box.pack(side="right", padx=16, pady=8)

        ctk.CTkButton(
            btn_box,
            text="Confirm Forget",
            width=110,
            height=28,
            corner_radius=14,
            fg_color="#DC2626",
            hover_color="#EF4444",
            font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            command=lambda: self._execute_forget_legacy(text),
        ).pack(side="left", padx=(0, 6))

        ctk.CTkButton(
            btn_box,
            text="Cancel",
            width=70,
            height=28,
            corner_radius=14,
            fg_color="transparent",
            hover_color=ThemeColors.SURFACE_CARD_HOVER,
            text_color=ThemeColors.TEXT_SECONDARY,
            font=ctk.CTkFont(family="Segoe UI", size=11),
            command=self._clear_dialogs,
        ).pack(side="left")

    def _execute_retraction(self, category: str, key: str):
        self._clear_dialogs()
        def _worker():
            try:
                if category in ("project", "tool", "preference"):
                    mem.retract_entity(category, key)
                else:
                    mem.retire_structured(category, key)
            except Exception:
                pass
            try:
                if self.winfo_exists():
                    self.after(0, self.refresh_memories)
            except Exception:
                pass

        threading.Thread(target=_worker, daemon=True, name="MemoryRetract").start()

    def _execute_forget_legacy(self, text: str):
        self._clear_dialogs()
        def _worker():
            try:
                # Use first few keywords to forget safely
                clean = text.strip()[:30]
                mem.forget(clean)
            except Exception:
                pass
            try:
                if self.winfo_exists():
                    self.after(0, self.refresh_memories)
            except Exception:
                pass

        threading.Thread(target=_worker, daemon=True, name="MemoryForget").start()

    def _show_add_dialog(self):
        """Display an inline form to save a new memory."""
        self._clear_dialogs()
        card = ctk.CTkFrame(
            self._dialog_container,
            fg_color=ThemeColors.SURFACE_CARD_BG,
            border_width=1,
            border_color=ThemeColors.BORDER,
            corner_radius=10,
        )
        card.pack(fill="x", pady=4)

        ctk.CTkLabel(
            card,
            text="Add New Fact:",
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
            text_color=ThemeColors.TEXT_PRIMARY,
        ).pack(side="left", padx=14, pady=10)

        entry = ctk.CTkEntry(
            card,
            placeholder_text="e.g. My favorite editor is VS Code",
            font=ctk.CTkFont(family="Segoe UI", size=12),
            height=30,
        )
        entry.pack(side="left", fill="x", expand=True, padx=8, pady=8)

        btn_box = ctk.CTkFrame(card, fg_color="transparent")
        btn_box.pack(side="right", padx=12, pady=8)

        def _on_save():
            fact = entry.get().strip()
            if not fact:
                return
            self._clear_dialogs()
            def _add_worker():
                try:
                    mem.remember(fact)
                except Exception:
                    pass
                try:
                    if self.winfo_exists():
                        self.after(0, self.refresh_memories)
                except Exception:
                    pass
            threading.Thread(target=_add_worker, daemon=True, name="MemoryAdd").start()

        ctk.CTkButton(
            btn_box,
            text="Save Fact",
            width=90,
            height=28,
            corner_radius=14,
            fg_color=ThemeColors.ORANGE_PRIMARY,
            hover_color=ThemeColors.ORANGE_HOVER,
            font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            command=_on_save,
        ).pack(side="left", padx=(0, 6))

        ctk.CTkButton(
            btn_box,
            text="Cancel",
            width=70,
            height=28,
            corner_radius=14,
            fg_color="transparent",
            hover_color=ThemeColors.SURFACE_CARD_HOVER,
            text_color=ThemeColors.TEXT_SECONDARY,
            font=ctk.CTkFont(family="Segoe UI", size=11),
            command=self._clear_dialogs,
        ).pack(side="left")

    def _clear_dialogs(self):
        for child in self._dialog_container.winfo_children():
            child.destroy()

    def _on_theme_changed(self, theme: str):
        self.configure(fg_color=ThemeColors.WORKSPACE_BG)
        self._title_lbl.configure(text_color=ThemeColors.TEXT_PRIMARY)
        self._sub_lbl.configure(text_color=ThemeColors.TEXT_SECONDARY)

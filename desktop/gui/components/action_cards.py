"""
gui/components/action_cards.py - Approved Contextual Action Cards.

Renders the four approved action cards from the design specification:
1. Work on a project ("Continue where we left off")
2. Analyze files ("Read, summarize and get insights")
3. Plan my day ("Show tasks and reminders")
4. Use my computer ("Open apps and automate tasks")

Each card connects directly to existing, verified assistant capabilities.
"""

from typing import Callable, List, Tuple
import customtkinter as ctk

from gui.theme import ThemeColors, theme_manager


CARDS_DATA: List[Tuple[str, str, str, str, str]] = [
    (
        "project",
        "📁",
        "Work on a project",
        "Continue where we left off",
        "show projects",
    ),
    (
        "files",
        "📄",
        "Analyze files",
        "Read, summarize and get insights",
        "list files",
    ),
    (
        "plan",
        "📅",
        "Plan my day",
        "Show tasks and reminders",
        "show reminders",
    ),
    (
        "system",
        "🖥️",
        "Use my computer",
        "Open apps and automate tasks",
        "cpu usage",
    ),
]


class ActionCard(ctk.CTkFrame):
    """A single interactive action card."""

    def __init__(
        self,
        master,
        key: str,
        icon: str,
        title: str,
        subtitle: str,
        query: str,
        on_click: Callable[[str], None],
        **kwargs,
    ):
        super().__init__(
            master,
            fg_color=ThemeColors.SURFACE_CARD_BG,
            border_width=1,
            border_color=ThemeColors.BORDER,
            corner_radius=12,
            cursor="hand2",
            **kwargs,
        )
        self.key = key
        self.query = query
        self.on_click = on_click
        self._is_hovered = False

        self._build_ui(icon, title, subtitle)
        self._bind_events()

    def _build_ui(self, icon: str, title: str, subtitle: str):
        self.grid_columnconfigure(0, weight=1)

        # Header Row: Icon + Title
        header_frame = ctk.CTkFrame(self, fg_color="transparent")
        header_frame.pack(fill="x", padx=12, pady=(12, 4))

        self._icon_lbl = ctk.CTkLabel(
            header_frame,
            text=icon,
            font=ctk.CTkFont(size=16),
            width=24,
            height=24,
        )
        self._icon_lbl.pack(side="left", padx=(0, 6))

        self._title_lbl = ctk.CTkLabel(
            header_frame,
            text=title,
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
            text_color=ThemeColors.TEXT_PRIMARY,
            anchor="w",
        )
        self._title_lbl.pack(side="left", fill="x", expand=True)

        # Subtitle Row
        self._sub_lbl = ctk.CTkLabel(
            self,
            text=subtitle,
            font=ctk.CTkFont(family="Segoe UI", size=10),
            text_color=ThemeColors.TEXT_MUTED,
            anchor="w",
            wraplength=160,
            justify="left",
        )
        self._sub_lbl.pack(fill="x", padx=14, pady=(0, 12))

    def _bind_events(self):
        """Bind mouse events to all child widgets for unified click and hover."""
        widgets = [self, self._icon_lbl, self._title_lbl, self._sub_lbl]
        for w in widgets:
            w.bind("<Button-1>", self._handle_click)
            w.bind("<Enter>", self._handle_enter)
            w.bind("<Leave>", self._handle_leave)

    def _handle_click(self, event=None):
        if self.on_click:
            self.on_click(self.query)

    def _handle_enter(self, event=None):
        self.configure(
            fg_color=ThemeColors.SURFACE_CARD_HOVER,
            border_color=ThemeColors.ORANGE_PRIMARY,
        )

    def _handle_leave(self, event=None):
        self.configure(
            fg_color=ThemeColors.SURFACE_CARD_BG,
            border_color=ThemeColors.BORDER,
        )

    def update_theme(self):
        """Update colors on theme change."""
        self.configure(
            fg_color=ThemeColors.SURFACE_CARD_BG,
            border_color=ThemeColors.BORDER,
        )
        self._title_lbl.configure(text_color=ThemeColors.TEXT_PRIMARY)
        self._sub_lbl.configure(text_color=ThemeColors.TEXT_MUTED)


class ActionCardsGrid(ctk.CTkFrame):
    """Row of the four approved action cards."""

    def __init__(
        self,
        master,
        on_card_click: Callable[[str], None],
        **kwargs,
    ):
        super().__init__(master, fg_color="transparent", **kwargs)
        self._on_card_click = on_card_click
        self._cards: List[ActionCard] = []

        self._build_ui()
        theme_manager.add_listener(self._on_theme_changed)

    def _build_ui(self):
        for col_idx in range(4):
            self.grid_columnconfigure(col_idx, weight=1, uniform="card_col")

        for idx, (key, icon, title, subtitle, query) in enumerate(CARDS_DATA):
            card = ActionCard(
                self,
                key=key,
                icon=icon,
                title=title,
                subtitle=subtitle,
                query=query,
                on_click=self._on_card_click,
            )
            card.grid(row=0, column=idx, padx=6, pady=4, sticky="nsew")
            self._cards.append(card)

    def _on_theme_changed(self, theme: str):
        for card in self._cards:
            card.update_theme()

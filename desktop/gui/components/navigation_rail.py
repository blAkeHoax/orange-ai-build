"""
Orange AI Desktop - Left Navigation Rail Component.

Renders a slim, persistent navigation rail following the approved design:
- Geometric faceted orange orb with native text wordmark
- Navigation entries: Home, Chat, Agent Jobs, Memory, Activity, Permissions, Tools, Settings
- Active state indicator and hover styling
- Live theme toggle control and version status badge at the bottom
"""

import os
from typing import Callable, Dict, Optional
import customtkinter as ctk
from PIL import Image

from gui.theme import ThemeColors, theme_manager


NAV_ITEMS = [
    ("home", "Home", "⌂"),
    ("chat", "Chat", "💬"),
    ("jobs", "Agent Jobs", "⚡"),
    ("memory", "Memory", "🧠"),
    ("activity", "Activity", "📈"),
    ("permissions", "Permissions", "🛡"),
    ("tools", "Tools", "🔧"),
    ("settings", "Settings", "⚙"),
]


class NavigationRail(ctk.CTkFrame):
    """Slim persistent left rail containing branding, navigation, and theme toggle."""

    def __init__(
        self,
        master,
        on_navigate: Optional[Callable[[str], None]] = None,
        **kwargs,
    ):
        super().__init__(
            master,
            width=220,
            fg_color=ThemeColors.NAV_RAIL_BG,
            corner_radius=0,
            border_width=1,
            border_color=ThemeColors.BORDER,
            **kwargs,
        )
        self.grid_propagate(False)
        self._on_navigate = on_navigate
        self._active_key = "home"
        self._nav_buttons: Dict[str, ctk.CTkButton] = {}

        self._load_logo()
        self._build_ui()

        # Listen to theme updates
        theme_manager.add_listener(self._on_theme_changed)

    def _load_logo(self):
        """Load the approved faceted geometric orb asset."""
        from brain.paths import get_asset_path
        logo_path = get_asset_path("logo_orb_64.png")
        if os.path.exists(logo_path):
            try:
                with Image.open(logo_path) as im:
                    pil_img = im.copy()
                self._logo_image = ctk.CTkImage(
                    light_image=pil_img,
                    dark_image=pil_img,
                    size=(30, 30),
                )
            except Exception:
                self._logo_image = None
        else:
            self._logo_image = None

    def _build_ui(self):
        """Construct the rail layout: Brand Header, Navigation List, Theme Toggle."""
        # 1. Branding Header
        header_frame = ctk.CTkFrame(self, fg_color="transparent")
        header_frame.pack(fill="x", padx=16, pady=(20, 24))

        if self._logo_image:
            logo_label = ctk.CTkLabel(header_frame, image=self._logo_image, text="")
            logo_label.pack(side="left", padx=(0, 10))

        wordmark_frame = ctk.CTkFrame(header_frame, fg_color="transparent")
        wordmark_frame.pack(side="left", fill="both", expand=True)

        # Wordmark: "Orange" in neutral, "AI" in brand orange
        title_frame = ctk.CTkFrame(wordmark_frame, fg_color="transparent")
        title_frame.pack(anchor="w")

        orange_lbl = ctk.CTkLabel(
            title_frame,
            text="Orange ",
            font=ctk.CTkFont(family="Segoe UI", size=18, weight="bold"),
            text_color=ThemeColors.TEXT_PRIMARY,
        )
        orange_lbl.pack(side="left")

        ai_lbl = ctk.CTkLabel(
            title_frame,
            text="AI",
            font=ctk.CTkFont(family="Segoe UI", size=18, weight="bold"),
            text_color=ThemeColors.ORANGE_PRIMARY,
        )
        ai_lbl.pack(side="left")

        tagline_lbl = ctk.CTkLabel(
            wordmark_frame,
            text="Personal AI Assistant",
            font=ctk.CTkFont(family="Segoe UI", size=10),
            text_color=ThemeColors.TEXT_MUTED,
        )
        tagline_lbl.pack(anchor="w", pady=(1, 0))

        # 2. Navigation Items List
        self._nav_list_frame = ctk.CTkFrame(self, fg_color="transparent")
        self._nav_list_frame.pack(fill="both", expand=True, padx=12, pady=0)

        for key, label, icon in NAV_ITEMS:
            btn = ctk.CTkButton(
                self._nav_list_frame,
                text=f"  {icon}   {label}",
                font=ctk.CTkFont(family="Segoe UI", size=13, weight="normal"),
                anchor="w",
                height=38,
                corner_radius=8,
                command=lambda k=key: self._on_item_clicked(k),
            )
            btn.pack(fill="x", pady=2)
            self._nav_buttons[key] = btn

        self._refresh_nav_visuals()

        # 3. Bottom Controls Area (Theme Toggle & Status)
        bottom_frame = ctk.CTkFrame(
            self,
            fg_color="transparent",
            border_width=1,
            border_color=ThemeColors.BORDER_SUBTLE,
        )
        bottom_frame.pack(fill="x", side="bottom", padx=12, pady=16)

        # Theme Selector / Toggle Button
        theme_icon = "🌙" if theme_manager.current_theme == "dark" else "☀️"
        theme_text = "Dark Mode" if theme_manager.current_theme == "dark" else "Light Mode"

        self._theme_btn = ctk.CTkButton(
            bottom_frame,
            text=f"{theme_icon}  {theme_text}",
            font=ctk.CTkFont(family="Segoe UI", size=12),
            height=34,
            corner_radius=8,
            fg_color=ThemeColors.SURFACE_CARD_BG,
            hover_color=ThemeColors.SURFACE_CARD_HOVER,
            text_color=ThemeColors.TEXT_PRIMARY,
            border_width=1,
            border_color=ThemeColors.BORDER,
            command=self._on_theme_toggle_clicked,
        )
        self._theme_btn.pack(fill="x", pady=(8, 8), padx=4)

        # Desktop Shell Status
        self._status_lbl = ctk.CTkLabel(
            bottom_frame,
            text="Orange AI Desktop v2.0",
            font=ctk.CTkFont(family="Segoe UI", size=10),
            text_color=ThemeColors.TEXT_MUTED,
        )
        self._status_lbl.pack(anchor="center", pady=(0, 4))

    def set_active(self, key: str):
        """Programmatically update the active navigation item."""
        self._active_key = key
        self._refresh_nav_visuals()

    def _on_item_clicked(self, key: str):
        """Handle user clicking a navigation item."""
        self._active_key = key
        self._refresh_nav_visuals()
        if self._on_navigate:
            self._on_navigate(key)

    def _on_theme_toggle_clicked(self):
        """Handle theme toggle click."""
        theme_manager.toggle_theme()

    def _on_theme_changed(self, theme: str):
        """React to dynamic theme change."""
        theme_icon = "🌙" if theme == "dark" else "☀️"
        theme_text = "Dark Mode" if theme == "dark" else "Light Mode"
        if hasattr(self, "_theme_btn"):
            self._theme_btn.configure(text=f"{theme_icon}  {theme_text}")
        self._refresh_nav_visuals()

    def _refresh_nav_visuals(self):
        """Update button colors and styling based on active state."""
        is_dark = theme_manager.is_dark

        for key, btn in self._nav_buttons.items():
            if key == self._active_key:
                # Active item: warm peach/amber tint and orange text
                fg = ThemeColors.ORANGE_TINT_DARK if is_dark else ThemeColors.ORANGE_TINT_LIGHT
                text_col = ThemeColors.ORANGE_PRIMARY
                border_col = ThemeColors.ORANGE_PRIMARY
                weight = "bold"
                border_w = 1
            else:
                # Inactive item
                fg = "transparent"
                text_col = "#9CA3AF" if is_dark else "#4B5563"
                border_col = "#0B0E14" if is_dark else "#FFFFFF"
                weight = "normal"
                border_w = 0

            hover = "#161B22" if is_dark else "#F3F4F6"

            btn.configure(
                fg_color=fg,
                text_color=text_col,
                hover_color=hover,
                border_color=border_col,
                border_width=border_w,
                font=ctk.CTkFont(family="Segoe UI", size=13, weight=weight),
            )

    def set_active(self, key: str):
        """Programmatically set the active navigation item."""
        if key in self._nav_buttons:
            self._active_key = key
            self._refresh_nav_visuals()

    @property
    def active_key(self) -> str:
        return self._active_key

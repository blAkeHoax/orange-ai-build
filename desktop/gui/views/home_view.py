"""
Orange AI Desktop - Central Workspace Views (Stage 2 Shell).

Implements the Stage 2 placeholder workspace according to strict specifications:
- Clean, theme-aware central surface
- Dynamic welcome greeting generated safely from brain.personality.greeting()
- Approved faceted geometric orb artwork presentation
- Honest, accessible placeholder states for non-Home navigation items
"""

import os
from typing import Optional
import customtkinter as ctk
from PIL import Image

from gui.theme import ThemeColors, theme_manager
from gui.utils.greeting_helper import get_user_greeting


class HomeView(ctk.CTkFrame):
    """Stage 2 foundational central workspace surface."""

    def __init__(self, master, **kwargs):
        super().__init__(
            master,
            fg_color=ThemeColors.WORKSPACE_BG,
            corner_radius=0,
            **kwargs,
        )
        self._load_orb_asset()
        self._build_ui()
        theme_manager.add_listener(self._on_theme_changed)

    def _load_orb_asset(self):
        """Load the approved 128px faceted geometric orb artwork."""
        from brain.paths import get_asset_path
        orb_path = get_asset_path("logo_orb_128.png")
        if os.path.exists(orb_path):
            try:
                with Image.open(orb_path) as im:
                    pil_img = im.copy()
                self._orb_image = ctk.CTkImage(
                    light_image=pil_img,
                    dark_image=pil_img,
                    size=(120, 120),
                )
            except Exception:
                self._orb_image = None
        else:
            self._orb_image = None

    def _build_ui(self):
        """Construct the clean Stage 2 shell layout."""
        # Central Container
        center_frame = ctk.CTkFrame(self, fg_color="transparent")
        center_frame.place(relx=0.5, rely=0.48, anchor="center")

        # 1. Subtle, restrained orb hero visual
        if self._orb_image:
            orb_label = ctk.CTkLabel(center_frame, image=self._orb_image, text="")
            orb_label.pack(pady=(0, 20))

        # 2. Dynamic Greeting
        greet_text = get_user_greeting()
        self._greet_lbl = ctk.CTkLabel(
            center_frame,
            text=greet_text,
            font=ctk.CTkFont(family="Segoe UI", size=26, weight="bold"),
            text_color=ThemeColors.TEXT_PRIMARY,
        )
        self._greet_lbl.pack(pady=(0, 6))

        # 3. Supporting Tagline & Assistant Identification
        self._sub_lbl = ctk.CTkLabel(
            center_frame,
            text="Personal AI Assistant",
            font=ctk.CTkFont(family="Segoe UI", size=15),
            text_color=ThemeColors.TEXT_SECONDARY,
        )
        self._sub_lbl.pack(pady=(0, 18))

        # 4. Status Badge (Stage 2 Shell Active)
        badge_frame = ctk.CTkFrame(
            center_frame,
            fg_color=ThemeColors.SURFACE_CARD_BG,
            border_width=1,
            border_color=ThemeColors.BORDER,
            corner_radius=20,
        )
        badge_frame.pack(pady=(4, 0), padx=20)

        badge_lbl = ctk.CTkLabel(
            badge_frame,
            text="● Core Shell & Theme Engine Initialized",
            font=ctk.CTkFont(family="Segoe UI", size=12),
            text_color=ThemeColors.ORANGE_PRIMARY,
            padx=16,
            pady=6,
        )
        badge_lbl.pack()

    def _on_theme_changed(self, theme: str):
        """Update any theme-sensitive visuals."""
        pass


class ComingSoonView(ctk.CTkFrame):
    """Honest, restrained placeholder for non-Home navigation items in Stage 2."""

    def __init__(self, master, feature_name: str, **kwargs):
        super().__init__(
            master,
            fg_color=ThemeColors.WORKSPACE_BG,
            corner_radius=0,
            **kwargs,
        )
        self._feature_name = feature_name
        self._build_ui()

    def _build_ui(self):
        container = ctk.CTkFrame(self, fg_color="transparent")
        container.place(relx=0.5, rely=0.5, anchor="center")

        title_lbl = ctk.CTkLabel(
            container,
            text=f"{self._feature_name}",
            font=ctk.CTkFont(family="Segoe UI", size=22, weight="bold"),
            text_color=ThemeColors.TEXT_PRIMARY,
        )
        title_lbl.pack(pady=(0, 8))

        msg_lbl = ctk.CTkLabel(
            container,
            text="This section will be implemented in a subsequent approved stage.",
            font=ctk.CTkFont(family="Segoe UI", size=13),
            text_color=ThemeColors.TEXT_SECONDARY,
        )
        msg_lbl.pack(pady=(0, 16))

        info_badge = ctk.CTkFrame(
            container,
            fg_color=ThemeColors.SURFACE_CARD_BG,
            border_width=1,
            border_color=ThemeColors.BORDER,
            corner_radius=16,
        )
        info_badge.pack()

        badge_text = ctk.CTkLabel(
            info_badge,
            text="Stage 2 Scope: Foundational Shell & Theme Engine Only",
            font=ctk.CTkFont(family="Segoe UI", size=11),
            text_color=ThemeColors.TEXT_MUTED,
            padx=14,
            pady=5,
        )
        badge_text.pack()

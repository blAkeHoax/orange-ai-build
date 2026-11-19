"""
gui/components/confirmation_card.py - Interactive Permission Confirmation Card.

Renders an explicit consent UI for actions requiring user authorization:
- Clearly identifies the tool action and arguments
- Informs the user of system state change risk
- Provides distinct Approve and Deny controls
- Disables controls once decided to prevent duplicate submission
- Preserves full security without invoking terminal input()
"""

import re
from typing import Callable, Optional
import customtkinter as ctk

from gui.theme import ThemeColors, theme_manager


class ConfirmationCard(ctk.CTkFrame):
    """
    Inline confirmation card presented inside the conversation timeline.
    """

    def __init__(
        self,
        master,
        request_id: str,
        prompt_text: str,
        on_decision: Callable[[str], None],
        **kwargs,
    ):
        super().__init__(
            master,
            fg_color=ThemeColors.SURFACE_CARD_BG,
            border_width=1,
            border_color=ThemeColors.ORANGE_PRIMARY,
            corner_radius=14,
            **kwargs,
        )
        self.request_id = request_id
        self.prompt_text = prompt_text
        self.on_decision = on_decision
        self._is_decided = False

        self._build_ui()
        theme_manager.add_listener(self._on_theme_changed)

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)

        # 1. Header with Warning Badge
        header_frame = ctk.CTkFrame(self, fg_color="transparent")
        header_frame.pack(fill="x", padx=16, pady=(14, 4))

        badge = ctk.CTkLabel(
            header_frame,
            text="⚠️  Confirmation Required",
            font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
            text_color=ThemeColors.ORANGE_PRIMARY,
        )
        badge.pack(side="left")

        # 2. Main Explanation Text
        clean_text = self._format_prompt(self.prompt_text)
        self._desc_lbl = ctk.CTkLabel(
            self,
            text=clean_text,
            font=ctk.CTkFont(family="Segoe UI", size=13),
            text_color=ThemeColors.TEXT_PRIMARY,
            wraplength=520,
            justify="left",
            anchor="w",
        )
        self._desc_lbl.pack(fill="x", padx=16, pady=(4, 6))

        # 3. Security Risk Notice
        self._risk_lbl = ctk.CTkLabel(
            self,
            text="This action will cause system or application state changes. Explicit approval is required.",
            font=ctk.CTkFont(family="Segoe UI", size=11),
            text_color=ThemeColors.TEXT_MUTED,
            wraplength=520,
            justify="left",
            anchor="w",
        )
        self._risk_lbl.pack(fill="x", padx=16, pady=(0, 12))

        # 4. Action Buttons Container
        self._btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        self._btn_frame.pack(fill="x", padx=16, pady=(0, 14))

        self._approve_btn = ctk.CTkButton(
            self._btn_frame,
            text="Approve",
            width=90,
            height=32,
            corner_radius=16,
            fg_color=ThemeColors.ORANGE_PRIMARY,
            hover_color=ThemeColors.ORANGE_HOVER,
            text_color="#FFFFFF",
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
            command=self._on_approve,
        )
        self._approve_btn.pack(side="left", padx=(0, 8))

        self._deny_btn = ctk.CTkButton(
            self._btn_frame,
            text="Deny",
            width=80,
            height=32,
            corner_radius=16,
            fg_color=ThemeColors.SURFACE_CARD_BG,
            border_width=1,
            border_color=ThemeColors.BORDER,
            hover_color=ThemeColors.SURFACE_CARD_HOVER,
            text_color=ThemeColors.TEXT_SECONDARY,
            font=ctk.CTkFont(family="Segoe UI", size=12),
            command=self._on_deny,
        )
        self._deny_btn.pack(side="left")

        # Status badge placeholder (shown after decision)
        self._status_badge = ctk.CTkLabel(
            self._btn_frame,
            text="",
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
        )

    def _format_prompt(self, raw_prompt: str) -> str:
        """Clean up CLI artifacts from prompt string for desktop presentation."""
        cleaned = raw_prompt.replace("\n", " ").strip()
        cleaned = re.sub(r"\[y/N\]:?", "", cleaned, flags=re.IGNORECASE).strip()
        cleaned = re.sub(r"Proceed\??", "", cleaned, flags=re.IGNORECASE).strip()
        return cleaned

    def _on_approve(self):
        if self._is_decided:
            return
        self._is_decided = True
        self._approve_btn.pack_forget()
        self._deny_btn.pack_forget()

        self._status_badge.configure(
            text="✓ Approved — Executing action...",
            text_color="#2ECC71",
        )
        self._status_badge.pack(side="left")

        if self.on_decision:
            self.on_decision("yes")

    def _on_deny(self):
        if self._is_decided:
            return
        self._is_decided = True
        self._approve_btn.pack_forget()
        self._deny_btn.pack_forget()

        self._status_badge.configure(
            text="✕ Denied — Action cancelled.",
            text_color=ThemeColors.TEXT_MUTED,
        )
        self._status_badge.pack(side="left")

        if self.on_decision:
            self.on_decision("no")

    def _on_theme_changed(self, theme: str):
        self.configure(
            fg_color=ThemeColors.SURFACE_CARD_BG,
            border_color=ThemeColors.ORANGE_PRIMARY,
        )
        self._desc_lbl.configure(text_color=ThemeColors.TEXT_PRIMARY)
        self._risk_lbl.configure(text_color=ThemeColors.TEXT_MUTED)
        self._deny_btn.configure(
            fg_color=ThemeColors.SURFACE_CARD_BG,
            border_color=ThemeColors.BORDER,
            hover_color=ThemeColors.SURFACE_CARD_HOVER,
            text_color=ThemeColors.TEXT_SECONDARY,
        )

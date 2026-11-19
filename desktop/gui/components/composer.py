"""
gui/components/composer.py - Approved Message Composer Component.

Renders the bottom input bar strictly following the approved design reference:
- Paperclip attachment button (informational placeholder)
- Clean, theme-aware text input entry with placeholder
- Microphone button (informational placeholder)
- Solid orange send pill button
- Keyboard Enter-to-send binding
- Robust disabled/enabled state during in-flight requests
"""

from typing import Callable, Optional
import customtkinter as ctk

from gui.theme import ThemeColors, theme_manager


class ComposerBar(ctk.CTkFrame):
    """
    Bottom message composer anchored to the chat workspace.
    Follows approved reference proportions, typography, and styling.
    """

    def __init__(
        self,
        master,
        on_send: Callable[[str], None],
        on_notice: Optional[Callable[[str], None]] = None,
        **kwargs,
    ):
        super().__init__(
            master,
            fg_color="transparent",
            corner_radius=0,
            **kwargs,
        )
        self._on_send = on_send
        self._on_notice = on_notice
        self._is_enabled = True

        self._build_ui()
        theme_manager.add_listener(self._on_theme_changed)

    def _build_ui(self):
        # Outer pill container matching design reference
        self._pill_frame = ctk.CTkFrame(
            self,
            fg_color=ThemeColors.SURFACE_CARD_BG,
            border_width=1,
            border_color=ThemeColors.BORDER,
            corner_radius=24,
            height=54,
        )
        self._pill_frame.pack(fill="x", padx=28, pady=(8, 20))
        self._pill_frame.pack_propagate(False)

        # 1. Attachment / Paperclip button (Informational placeholder)
        self._attach_btn = ctk.CTkButton(
            self._pill_frame,
            text="📎",
            width=36,
            height=36,
            corner_radius=18,
            fg_color="transparent",
            hover_color=ThemeColors.SURFACE_CARD_HOVER,
            text_color=ThemeColors.TEXT_SECONDARY,
            font=ctk.CTkFont(size=16),
            command=self._on_attach_clicked,
        )
        self._attach_btn.pack(side="left", padx=(12, 6), pady=8)

        # 2. Text Input Entry (middle expandable area)
        self._entry = ctk.CTkEntry(
            self._pill_frame,
            placeholder_text="Type a message to Orange AI...",
            placeholder_text_color=ThemeColors.TEXT_MUTED,
            fg_color="transparent",
            text_color=ThemeColors.TEXT_PRIMARY,
            border_width=0,
            font=ctk.CTkFont(family="Segoe UI", size=14),
        )
        self._entry.pack(side="left", fill="both", expand=True, padx=4, pady=8)
        self._entry.bind("<Return>", self._on_enter_pressed)

        # 3. Solid Orange Pill Send Button (packed first to sit on the far right)
        self._send_btn = ctk.CTkButton(
            self._pill_frame,
            text="➤",
            width=46,
            height=38,
            corner_radius=19,
            fg_color=ThemeColors.ORANGE_PRIMARY,
            hover_color=ThemeColors.ORANGE_HOVER,
            text_color="#FFFFFF",
            font=ctk.CTkFont(family="Segoe UI", size=14, weight="bold"),
            command=self._on_send_clicked,
        )
        self._send_btn.pack(side="right", padx=(2, 8), pady=8)

        # 4. Microphone button (packed second to sit to the left of the send button)
        self._mic_btn = ctk.CTkButton(
            self._pill_frame,
            text="🎙️",
            width=36,
            height=36,
            corner_radius=18,
            fg_color="transparent",
            hover_color=ThemeColors.SURFACE_CARD_HOVER,
            text_color=ThemeColors.TEXT_SECONDARY,
            font=ctk.CTkFont(size=16),
            command=self._on_mic_clicked,
        )
        self._mic_btn.pack(side="right", padx=(4, 6), pady=8)

    def _on_enter_pressed(self, event=None):
        """Handle Return/Enter key event."""
        self._on_send_clicked()
        return "break"

    def _on_send_clicked(self):
        """Extract text and invoke callback if enabled and non-empty."""
        if not self._is_enabled:
            return

        text = self.get_text()
        if not text:
            return

        self._entry.delete(0, "end")
        if self._on_send:
            self._on_send(text)

    def _on_attach_clicked(self):
        """Informational placeholder notice for file attachments."""
        if self._on_notice:
            self._on_notice("File attachments are not yet supported in this version.")

    def _on_mic_clicked(self):
        """Informational placeholder notice for voice input."""
        if self._on_notice:
            self._on_notice("Voice input is not yet supported in this version.")

    def get_text(self) -> str:
        """Return the current trimmed text in the composer."""
        return self._entry.get().strip()

    def set_text(self, text: str):
        """Set composer text and place cursor at end."""
        self._entry.delete(0, "end")
        self._entry.insert(0, text)
        self._entry.focus_set()

    def clear(self):
        """Clear composer text."""
        self._entry.delete(0, "end")

    def focus(self):
        """Focus the input entry."""
        self._entry.focus_set()

    def set_enabled(self, enabled: bool):
        """Enable or disable input and buttons during in-flight requests."""
        self._is_enabled = enabled
        entry_state = "normal" if enabled else "disabled"
        btn_state = "normal" if enabled else "disabled"

        self._entry.configure(state=entry_state)
        self._send_btn.configure(state=btn_state)
        self._attach_btn.configure(state=btn_state)
        self._mic_btn.configure(state=btn_state)

        if enabled:
            self._send_btn.configure(fg_color=ThemeColors.ORANGE_PRIMARY)
            self._entry.focus_set()
        else:
            self._send_btn.configure(fg_color=ThemeColors.BORDER)

    def _on_theme_changed(self, theme: str):
        """Update dynamic colors upon theme switch."""
        self._pill_frame.configure(
            fg_color=ThemeColors.SURFACE_CARD_BG,
            border_color=ThemeColors.BORDER,
        )
        self._entry.configure(
            text_color=ThemeColors.TEXT_PRIMARY,
            placeholder_text_color=ThemeColors.TEXT_MUTED,
        )
        self._attach_btn.configure(
            text_color=ThemeColors.TEXT_SECONDARY,
            hover_color=ThemeColors.SURFACE_CARD_HOVER,
        )
        self._mic_btn.configure(
            text_color=ThemeColors.TEXT_SECONDARY,
            hover_color=ThemeColors.SURFACE_CARD_HOVER,
        )
        if self._is_enabled:
            self._send_btn.configure(
                fg_color=ThemeColors.ORANGE_PRIMARY,
                hover_color=ThemeColors.ORANGE_HOVER,
            )

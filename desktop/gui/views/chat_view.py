"""
gui/views/chat_view.py - Complete Orange AI Chat Workspace (Stage 3).

Integrates the approved visual design and assistant engine:
- Dynamic greeting and subtle faceted geometric orb artwork
- Four contextual action cards grounded in real capabilities
- Scrollable conversation timeline with user/assistant bubbles
- Interactive permission confirmation cards
- Pinned bottom composer with attachment/microphone placeholders
- Seamless workspace transition from initial welcome to active chat
- Full Light and Dark theme responsiveness
"""

import os
import re
from typing import Optional, Callable, Dict, Any
import customtkinter as ctk
from PIL import Image

from gui.theme import ThemeColors, theme_manager
from gui.utils.greeting_helper import get_user_greeting
from gui.components.action_cards import ActionCardsGrid
from gui.components.composer import ComposerBar
from gui.components.message_timeline import MessageTimeline
from gui.services.assistant_bridge import AssistantBridge


class ChatView(ctk.CTkFrame):
    """
    Main Chat Workspace for Orange AI Desktop.
    """

    def __init__(
        self,
        master,
        assistant_bridge: Optional[AssistantBridge] = None,
        **kwargs,
    ):
        super().__init__(
            master,
            fg_color=ThemeColors.WORKSPACE_BG,
            corner_radius=0,
            **kwargs,
        )
        self._bridge = assistant_bridge
        self._feature_name = "Chat"
        self._orb_image = None
        self._notice_frame = None

        self._load_orb_asset()
        self._build_ui()
        theme_manager.add_listener(self._on_theme_changed)

    def on_activated(self):
        """Lifecycle hook: preserve active timeline and unsent composer draft without resets."""
        try:
            self._greet_lbl.configure(text=get_user_greeting())
        except Exception:
            pass

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
                    size=(100, 100),
                )
            except Exception:
                self._orb_image = None

    def _build_ui(self):
        # 1. Pinned Bottom Composer Bar (packed first with side='bottom' to ensure full visibility)
        self._composer = ComposerBar(
            self,
            on_send=self._on_send_message,
            on_notice=self._show_notice,
        )
        self._composer.pack(side="bottom", fill="x")

        # 2. Informational Notice Banner Container (unpacked initially to avoid taking 200px void space)
        self._banner_container = ctk.CTkFrame(self, fg_color="transparent")

        # 3. Main Central Scrollable Frame (fills all remaining height)
        self._scroll_frame = ctk.CTkScrollableFrame(
            self,
            fg_color="transparent",
            corner_radius=0,
        )
        self._scroll_frame.pack(side="top", fill="both", expand=True, padx=4, pady=(4, 0))
        self._scroll_frame.grid_columnconfigure(0, weight=1)

        # 3A. Welcome Header Section (Greeting + Orb + Action Cards)
        self._welcome_section = ctk.CTkFrame(self._scroll_frame, fg_color="transparent")
        self._welcome_section.pack(fill="x", padx=20, pady=(36, 16))

        # Orb Artwork
        if self._orb_image:
            self._orb_lbl = ctk.CTkLabel(self._welcome_section, image=self._orb_image, text="")
            self._orb_lbl.pack(pady=(0, 12))
        else:
            self._orb_lbl = None

        # Dynamic Greeting
        greet_str = get_user_greeting()
        self._greet_lbl = ctk.CTkLabel(
            self._welcome_section,
            text=greet_str,
            font=ctk.CTkFont(family="Segoe UI", size=26, weight="bold"),
            text_color=ThemeColors.TEXT_PRIMARY,
        )
        self._greet_lbl.pack(pady=(0, 4))

        # Subtitle
        self._sub_lbl = ctk.CTkLabel(
            self._welcome_section,
            text="How can I help you today?",
            font=ctk.CTkFont(family="Segoe UI", size=15),
            text_color=ThemeColors.TEXT_SECONDARY,
        )
        self._sub_lbl.pack(pady=(0, 20))

        # Action Cards Grid
        self._action_cards = ActionCardsGrid(
            self._welcome_section,
            on_card_click=self._on_action_card_clicked,
        )
        self._action_cards.pack(fill="x", padx=10, pady=(0, 8))

        # 3B. Message Timeline
        self._timeline = MessageTimeline(self._scroll_frame)
        self._timeline.pack(fill="both", expand=True, padx=4, pady=4)

    def set_bridge(self, bridge: AssistantBridge):
        """Configure or replace the assistant bridge."""
        self._bridge = bridge

    def _on_action_card_clicked(self, query: str):
        """Trigger message submission when an action card is clicked."""
        self._on_send_message(query)

    def _on_send_message(self, text: str):
        """Handle submission of user message from composer or action card."""
        clean_text = text.strip()
        if not clean_text:
            return

        # Transition workspace: compact welcome section after first message
        if self._timeline.get_message_count() == 0:
            self._welcome_section.pack_configure(pady=(12, 6))

        # 1. Append user message to timeline
        self._timeline.add_user_message(clean_text)

        # 2. Show thinking indicator & disable composer
        self._timeline.show_thinking()
        self._composer.set_enabled(False)
        self._scroll_to_bottom()

        # 3. Dispatch to AssistantBridge
        if self._bridge:
            req_id = self._bridge.send_message(clean_text)
            if not req_id:
                # Bridge was busy
                self._timeline.hide_thinking()
                self._composer.set_enabled(True)
                self._timeline.add_status_message("Another request is currently in flight. Please wait.", is_error=True)
                self._scroll_to_bottom()
        else:
            # Fallback for standalone preview / mock
            self.after(500, self._mock_reply, clean_text)

    def _mock_reply(self, query: str):
        """Mock fallback when no bridge is wired."""
        self._timeline.hide_thinking()
        self._timeline.add_assistant_message(f"Echo response for: '{query}'", model_name="local-preview")
        self._composer.set_enabled(True)
        self._scroll_to_bottom()

    def on_assistant_result(self, request_id: str, outcome: Dict[str, Any]):
        """Callback invoked from UI thread when assistant query finishes."""
        self._timeline.hide_thinking()
        self._composer.set_enabled(True)

        reply = outcome.get("reply", "")
        model_name = outcome.get("model", "")
        msg_type = outcome.get("type", "assistant")
        is_error = (msg_type == "error")

        single_rec = outcome.get("recommendation")
        recs_list = outcome.get("recommendations", [])

        if is_error:
            self._timeline.add_status_message(f"⚠️ {reply}", is_error=True)
        elif single_rec and "💡 **Orange AI Advisory**" in reply:
            # Strip embedded text advisory block so recommendation card takes its place
            clean_reply = re.split(r'\n+💡\s+\*\*Orange AI Advisory\*\*', reply)[0].strip()
            if clean_reply:
                self._timeline.add_assistant_message(clean_reply, model_name=model_name, msg_type=msg_type)
            self._timeline.add_recommendation_card(single_rec, on_action=self._on_recommendation_action)
        elif recs_list:
            if reply and not reply.startswith("Contextual Recommendations:"):
                self._timeline.add_assistant_message(reply, model_name=model_name, msg_type=msg_type)
            for r in recs_list:
                self._timeline.add_recommendation_card(r, on_action=self._on_recommendation_action)
        elif single_rec:
            if reply:
                self._timeline.add_assistant_message(reply, model_name=model_name, msg_type=msg_type)
            self._timeline.add_recommendation_card(single_rec, on_action=self._on_recommendation_action)
        else:
            self._timeline.add_assistant_message(reply, model_name=model_name, msg_type=msg_type)

        self._scroll_to_bottom()

    def _on_recommendation_action(self, rec_id: str, action: str):
        """Callback invoked after a card lifecycle action succeeds."""
        pass

    def on_assistant_confirmation(self, request_id: str, prompt_text: str, decision_cb: Callable[[str], None]):
        """Callback invoked from UI thread when tool/agent requires user consent."""
        self._timeline.hide_thinking()
        self._timeline.add_confirmation_card(
            request_id=request_id,
            prompt_text=prompt_text,
            on_decision=decision_cb,
        )
        self._scroll_to_bottom()

    def on_assistant_status(self, request_id: str, status_msg: str):
        """Callback invoked from UI thread for status notifications."""
        self._timeline.add_status_message(status_msg, is_error=False)
        self._scroll_to_bottom()

    def _show_notice(self, notice_text: str):
        """Display a dismissible informational notice pill above the composer."""
        # Clear existing notice if present
        for child in self._banner_container.winfo_children():
            try:
                child.destroy()
            except Exception:
                pass

        if not self._banner_container.winfo_manager():
            self._banner_container.pack(side="bottom", fill="x", padx=28, before=self._composer)

        banner = ctk.CTkFrame(
            self._banner_container,
            fg_color=ThemeColors.SURFACE_CARD_BG,
            border_width=1,
            border_color=ThemeColors.BORDER,
            corner_radius=14,
        )
        banner.pack(anchor="center", pady=(0, 6))

        msg_lbl = ctk.CTkLabel(
            banner,
            text=f"ℹ️  {notice_text}",
            font=ctk.CTkFont(family="Segoe UI", size=12),
            text_color=ThemeColors.TEXT_SECONDARY,
            padx=14,
            pady=4,
        )
        msg_lbl.pack(side="left")

        def _dismiss():
            self._safe_destroy(banner)
            if not self._banner_container.winfo_children():
                self._banner_container.pack_forget()

        close_btn = ctk.CTkButton(
            banner,
            text="✕",
            width=24,
            height=24,
            corner_radius=12,
            fg_color="transparent",
            hover_color=ThemeColors.SURFACE_CARD_HOVER,
            text_color=ThemeColors.TEXT_MUTED,
            font=ctk.CTkFont(size=11),
            command=_dismiss,
        )
        close_btn.pack(side="right", padx=(2, 8))

        # Auto-dismiss after 5 seconds
        self.after(5000, _dismiss)

    def _safe_destroy(self, widget):
        try:
            if widget.winfo_exists():
                widget.destroy()
        except Exception:
            pass

    def _scroll_to_bottom(self):
        """Smoothly scroll to the bottom of the timeline with geometry update synchronization."""
        try:
            self.update_idletasks()
            canvas = getattr(self._scroll_frame, "_parent_canvas", None)
            if canvas:
                bbox = canvas.bbox("all")
                if bbox:
                    canvas.configure(scrollregion=bbox)
                canvas.yview_moveto(1.0)
            self.after(50, self._sync_scroll)
        except Exception:
            pass

    def _sync_scroll(self):
        try:
            canvas = getattr(self._scroll_frame, "_parent_canvas", None)
            if canvas and canvas.winfo_exists():
                bbox = canvas.bbox("all")
                if bbox:
                    canvas.configure(scrollregion=bbox)
                canvas.yview_moveto(1.0)
        except Exception:
            pass

    def _on_theme_changed(self, theme: str):
        self.configure(fg_color=ThemeColors.WORKSPACE_BG)
        self._greet_lbl.configure(text_color=ThemeColors.TEXT_PRIMARY)
        self._sub_lbl.configure(text_color=ThemeColors.TEXT_SECONDARY)

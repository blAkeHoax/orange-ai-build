"""
gui/components/message_timeline.py - Scrollable Conversation Timeline (Stage 4.3).

Renders user, assistant, tool, status, error, and confirmation messages:
- Left-aligned assistant messages with small faceted orb avatar and primary Orange AI branding
- Markdown-rendered rich content (headers, bold/italic, code blocks, lists, blockquotes)
- Visually distinct structured ToolResultCard for tool execution outputs
- Secondary collapsible technical metadata drawer (Model / Engine / Diagnostic info)
- Right-aligned user message bubbles with adaptive word wrapping
- Inline interactive confirmation cards
- Dynamic thinking/waiting indicator
- Smooth automatic scrolling to newest message
- Full Light and Dark theme parity
"""

import os
import re
from typing import Optional, Callable, Dict, Any, List
import customtkinter as ctk
from PIL import Image

from gui.theme import ThemeColors, theme_manager
from gui.components.confirmation_card import ConfirmationCard
from gui.components.markdown_renderer import MarkdownRenderer
from gui.components.tool_result_card import ToolResultCard
from gui.components.recommendation_card import RecommendationCard


def _sanitize_metadata(text: str) -> str:
    """Scrub potential API keys or sensitive authorization tokens from diagnostic display."""
    if not text:
        return ""
    cleaned = re.sub(r'(bearer\s+)[a-zA-Z0-9_\-\.]+', r'\1[REDACTED]', text, flags=re.IGNORECASE)
    cleaned = re.sub(r'(api_?key[:=\s]+)[a-zA-Z0-9_\-\.]+', r'\1[REDACTED]', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'sk-[a-zA-Z0-9_\-\.]{10,}', '[REDACTED]', cleaned)
    return cleaned


class MessageTimeline(ctk.CTkFrame):
    """
    Container managing conversation messages inside the chat workspace.
    """

    def __init__(self, master, **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        self._message_count = 0
        self._thinking_card = None
        self._load_orb_avatar()

        theme_manager.add_listener(self._on_theme_changed)

    def _load_orb_avatar(self):
        """Load small 20px faceted orb for assistant messages."""
        from brain.paths import get_asset_path
        orb_path = get_asset_path("logo_orb_32.png")
        if os.path.exists(orb_path):
            try:
                with Image.open(orb_path) as im:
                    pil_img = im.copy()
                self._orb_avatar = ctk.CTkImage(
                    light_image=pil_img,
                    dark_image=pil_img,
                    size=(20, 20),
                )
            except Exception:
                self._orb_avatar = None
        else:
            self._orb_avatar = None

    def get_message_count(self) -> int:
        """Return the number of user + assistant turns currently displayed."""
        return self._message_count

    def add_user_message(self, text: str):
        """Add a right-aligned user message bubble with adaptive word wrapping."""
        self._message_count += 1

        wrapper = ctk.CTkFrame(self, fg_color="transparent")
        wrapper.pack(fill="x", padx=20, pady=4, anchor="e")

        bubble = ctk.CTkFrame(
            wrapper,
            fg_color=ThemeColors.SURFACE_CARD_BG,
            border_width=1,
            border_color=ThemeColors.BORDER,
            corner_radius=16,
        )
        bubble.pack(side="right", padx=(60, 0))

        lbl = ctk.CTkLabel(
            bubble,
            text=text,
            font=ctk.CTkFont(family="Segoe UI", size=13),
            text_color=ThemeColors.TEXT_PRIMARY,
            wraplength=540,
            justify="left",
            anchor="w",
            padx=14,
            pady=10,
        )
        lbl.pack()

        # Dynamic responsive wraplength on resize
        def _on_wrap_configure(event):
            try:
                if lbl.winfo_exists():
                    avail = max(320, min(700, event.width - 120))
                    lbl.configure(wraplength=avail)
            except Exception:
                pass

        wrapper.bind("<Configure>", _on_wrap_configure)
        return bubble

    def add_assistant_message(self, text: str, model_name: str = "", msg_type: str = "assistant"):
        """
        Add a left-aligned assistant message card.
        - msg_type == 'tool': Renders visually distinct ToolResultCard.
        - Regular assistant: Formats Markdown, promotes Orange AI identity, demotes technical model tag
          to a subtle collapsible diagnostic details drawer.
        """
        self.hide_thinking()
        self._message_count += 1

        wrapper = ctk.CTkFrame(self, fg_color="transparent")
        wrapper.pack(fill="x", padx=20, pady=6, anchor="w")

        # Avatar Column
        avatar_col = ctk.CTkFrame(wrapper, fg_color="transparent", width=28, height=28)
        avatar_col.pack(side="left", anchor="n", padx=(0, 8), pady=2)
        avatar_col.pack_propagate(False)

        if self._orb_avatar:
            avatar_lbl = ctk.CTkLabel(avatar_col, image=self._orb_avatar, text="")
            avatar_lbl.pack()
        else:
            avatar_lbl = ctk.CTkLabel(avatar_col, text="🍊", font=ctk.CTkFont(size=14))
            avatar_lbl.pack()

        # Route tool outcomes to dedicated ToolResultCard
        if msg_type == "tool":
            status = "success"
            lowered = text.lower()
            if "action blocked" in lowered or "action cancelled" in lowered or "declined confirmation" in lowered:
                status = "denied"
            elif "something went wrong" in lowered or "error" in lowered or "failed" in lowered:
                status = "failed"

            tool_name = "Desktop Tool"
            if "file" in lowered or "folder" in lowered:
                tool_name = "File Explorer"
            elif "process" in lowered or "app" in lowered:
                tool_name = "Process Manager"
            elif "cpu" in lowered or "ram" in lowered or "disk" in lowered or "battery" in lowered:
                tool_name = "System Diagnostics"
            elif "search" in lowered:
                tool_name = "Web Search"
            elif "reminder" in lowered:
                tool_name = "Reminder Service"

            tool_card = ToolResultCard(
                wrapper,
                tool_name=tool_name,
                status=status,
                output=text,
            )
            tool_card.pack(side="left", fill="x", expand=True, padx=(0, 40))
            return tool_card

        # Content Card for Assistant Conversation
        bubble = ctk.CTkFrame(
            wrapper,
            fg_color=ThemeColors.SURFACE_CARD_BG,
            border_width=1,
            border_color=ThemeColors.BORDER,
            corner_radius=16,
        )
        bubble.pack(side="left", fill="x", expand=True, padx=(0, 40))

        # 1. Header Row: [Orange AI Primary Identity] ... [ℹ️ Details Toggle]
        header_frame = ctk.CTkFrame(bubble, fg_color="transparent")
        header_frame.pack(fill="x", padx=14, pady=(10, 4))

        sender_title = "Orange AI"
        if msg_type == "agent":
            sender_title = "Orange AI (Agent)"

        name_lbl = ctk.CTkLabel(
            header_frame,
            text=sender_title,
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
            text_color=ThemeColors.ORANGE_PRIMARY,
        )
        name_lbl.pack(side="left")

        # 2. Secondary diagnostic details toggle (demoting raw model badge)
        has_metadata = bool(model_name and model_name not in ("unknown", "system"))
        if has_metadata:
            drawer_container = ctk.CTkFrame(bubble, fg_color="transparent")
            details_state = {"expanded": False}

            def _toggle_details():
                if details_state["expanded"]:
                    # Collapse
                    details_state["expanded"] = False
                    details_btn.configure(text="ℹ️ Details")
                    for child in drawer_container.winfo_children():
                        child.destroy()
                    drawer_container.pack_forget()
                else:
                    # Expand
                    details_state["expanded"] = True
                    details_btn.configure(text="✕ Close Details")

                    drawer_container.pack(fill="x", padx=14, pady=(0, 4), before=md_presenter)
                    drawer = ctk.CTkFrame(
                        drawer_container,
                        fg_color=ThemeColors.METADATA_BG,
                        border_width=1,
                        border_color=ThemeColors.METADATA_BORDER,
                        corner_radius=8,
                    )
                    drawer.pack(fill="x", pady=(2, 6))

                    clean_model = _sanitize_metadata(model_name)
                    mode_text = "Conversational Inference" if msg_type == "assistant" else "Autonomous Agent Task"
                    diag_text = (
                        f"Model: {clean_model}  |  Engine: Orange AI Assistant Engine\n"
                        f"Execution: {mode_text}  |  Security: Permission Guard Enforced"
                    )
                    diag_lbl = ctk.CTkLabel(
                        drawer,
                        text=diag_text,
                        font=ctk.CTkFont(family="Segoe UI", size=10),
                        text_color=ThemeColors.METADATA_TEXT,
                        justify="left",
                        anchor="w",
                        padx=10,
                        pady=6,
                    )
                    diag_lbl.pack(fill="x")

            details_btn = ctk.CTkButton(
                header_frame,
                text="ℹ️ Details",
                width=64,
                height=20,
                corner_radius=6,
                fg_color="transparent",
                hover_color=ThemeColors.SURFACE_CARD_HOVER,
                text_color=ThemeColors.TEXT_MUTED,
                font=ctk.CTkFont(family="Segoe UI", size=10),
                command=_toggle_details,
            )
            details_btn.pack(side="right")

        # 3. Rich Markdown Rendered Body
        md_presenter = MarkdownRenderer(bubble, text)
        md_presenter.pack(fill="x", expand=True, padx=14, pady=(2, 10))

        return bubble

    def add_tool_result(self, tool_name: str, status: str, output: str, command_arg: str = ""):
        """Explicit entry point to render structured tool execution results."""
        self.hide_thinking()
        self._message_count += 1

        wrapper = ctk.CTkFrame(self, fg_color="transparent")
        wrapper.pack(fill="x", padx=20, pady=6, anchor="w")

        # Avatar Column
        avatar_col = ctk.CTkFrame(wrapper, fg_color="transparent", width=28, height=28)
        avatar_col.pack(side="left", anchor="n", padx=(0, 8), pady=2)
        avatar_col.pack_propagate(False)

        if self._orb_avatar:
            avatar_lbl = ctk.CTkLabel(avatar_col, image=self._orb_avatar, text="")
            avatar_lbl.pack()
        else:
            avatar_lbl = ctk.CTkLabel(avatar_col, text="🍊", font=ctk.CTkFont(size=14))
            avatar_lbl.pack()

        tool_card = ToolResultCard(
            wrapper,
            tool_name=tool_name,
            status=status,
            output=output,
            command_arg=command_arg,
        )
        tool_card.pack(side="left", fill="x", expand=True, padx=(0, 40))
        return tool_card

    def add_status_message(self, text: str, is_error: bool = False):
        """Add a centered or subtle status / error pill."""
        self.hide_thinking()

        wrapper = ctk.CTkFrame(self, fg_color="transparent")
        wrapper.pack(fill="x", padx=20, pady=4)

        border_col = "#E74C3C" if is_error else ThemeColors.BORDER
        text_col = "#E74C3C" if is_error else ThemeColors.TEXT_MUTED

        pill = ctk.CTkFrame(
            wrapper,
            fg_color=ThemeColors.SURFACE_CARD_BG,
            border_width=1,
            border_color=border_col,
            corner_radius=14,
        )
        pill.pack(anchor="center")

        lbl = ctk.CTkLabel(
            pill,
            text=text,
            font=ctk.CTkFont(family="Segoe UI", size=11),
            text_color=text_col,
            padx=14,
            pady=4,
        )
        lbl.pack()
        return pill

    def add_confirmation_card(self, request_id: str, prompt_text: str, on_decision: Callable[[str], None]):
        """Embed an inline ConfirmationCard in the timeline."""
        self.hide_thinking()

        wrapper = ctk.CTkFrame(self, fg_color="transparent")
        wrapper.pack(fill="x", padx=20, pady=8, anchor="w")

        card = ConfirmationCard(
            wrapper,
            request_id=request_id,
            prompt_text=prompt_text,
            on_decision=on_decision,
        )
        card.pack(fill="x", padx=(28, 60))
        return card

    def add_recommendation_card(
        self,
        recommendation: Any,
        on_action: Optional[Callable[[str, str], None]] = None,
        store: Optional[Any] = None,
    ) -> RecommendationCard:
        """Embed an interactive RecommendationCard in the timeline."""
        self.hide_thinking()

        wrapper = ctk.CTkFrame(self, fg_color="transparent")
        wrapper.pack(fill="x", padx=20, pady=8, anchor="w")

        card = RecommendationCard(
            wrapper,
            recommendation=recommendation,
            store=store,
            on_action_callback=on_action,
        )
        card.pack(fill="x", padx=(28, 60))
        return card

    def show_thinking(self):
        """Display an indicator that Orange AI is processing."""
        if self._thinking_card is not None:
            return

        wrapper = ctk.CTkFrame(self, fg_color="transparent")
        wrapper.pack(fill="x", padx=20, pady=6, anchor="w")
        self._thinking_card = wrapper

        avatar_col = ctk.CTkFrame(wrapper, fg_color="transparent", width=28, height=28)
        avatar_col.pack(side="left", anchor="n", padx=(0, 8), pady=2)
        avatar_col.pack_propagate(False)

        if self._orb_avatar:
            avatar_lbl = ctk.CTkLabel(avatar_col, image=self._orb_avatar, text="")
            avatar_lbl.pack()
        else:
            avatar_lbl = ctk.CTkLabel(avatar_col, text="🍊", font=ctk.CTkFont(size=14))
            avatar_lbl.pack()

        bubble = ctk.CTkFrame(
            wrapper,
            fg_color=ThemeColors.SURFACE_CARD_BG,
            border_width=1,
            border_color=ThemeColors.BORDER,
            corner_radius=14,
        )
        bubble.pack(side="left")

        lbl = ctk.CTkLabel(
            bubble,
            text="● Thinking...",
            font=ctk.CTkFont(family="Segoe UI", size=12),
            text_color=ThemeColors.ORANGE_PRIMARY,
            padx=12,
            pady=6,
        )
        lbl.pack()

    def hide_thinking(self):
        """Remove the thinking indicator if present."""
        if self._thinking_card is not None:
            try:
                self._thinking_card.destroy()
            except Exception:
                pass
            self._thinking_card = None

    def _on_theme_changed(self, theme: str):
        """Ensure container background stays transparent."""
        self.configure(fg_color="transparent")

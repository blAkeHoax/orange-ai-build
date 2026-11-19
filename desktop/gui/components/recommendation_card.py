"""
gui/components/recommendation_card.py - Interactive Recommendation Card & Lifecycle Component.

Stage 4.2: Contextual Recommendations UI & Action Cards
Implements:
1. Native desktop card presentation for contextual recommendations with priority pills,
   explanation, suggested action, and short ID.
2. User lifecycle controls:
   - Dismiss: suppresses recommendation for 7 days via RecommendationStore.
   - Snooze: defers recommendation for 24 hours via RecommendationStore.
   - Mark Handled: marks recommendation as HANDLED with material fingerprint locking.
   - View Evidence: toggles expandable view of underlying operational facts & signals.
3. Clear feedback upon transition and safe handling of stale / already-transitioned items.
4. Strict security boundary: manages lifecycle metadata only; zero tool execution or permission bypass.
5. Full light and dark theme parity via ThemeColors and theme_manager.
"""

from datetime import datetime, timezone, timedelta
from typing import Callable, Optional, Dict, Any, List, Union
import customtkinter as ctk

from gui.theme import ThemeColors, theme_manager
from brain.briefing.recommendations import (
    Recommendation,
    RecommendationPriority,
    RecommendationState,
    RecommendationStore,
)


class RecommendationCard(ctk.CTkFrame):
    """
    Interactive recommendation card presented inside the conversation timeline
    or recommendation management views.
    """

    def __init__(
        self,
        master,
        recommendation: Union[Recommendation, Dict[str, Any]],
        store: Optional[RecommendationStore] = None,
        on_action_callback: Optional[Callable[[str, str], None]] = None,
        **kwargs,
    ):
        # Extract recommendation model if dict was provided
        if isinstance(recommendation, dict):
            self.rec = Recommendation.from_dict(recommendation)
        else:
            self.rec = recommendation

        self.store = store or RecommendationStore()
        self.on_action_callback = on_action_callback
        self._is_transitioned = (self.rec.state != RecommendationState.ACTIVE)
        self._is_evidence_visible = False

        # Priority-specific border color
        border_col = (
            ThemeColors.ORANGE_PRIMARY
            if self.rec.priority in (RecommendationPriority.CRITICAL, RecommendationPriority.HIGH)
            else ThemeColors.BORDER
        )

        super().__init__(
            master,
            fg_color=ThemeColors.SURFACE_CARD_BG,
            border_width=1,
            border_color=border_col,
            corner_radius=14,
            **kwargs,
        )

        self._build_ui()
        theme_manager.add_listener(self._on_theme_changed)

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)

        # 1. Header Frame: [💡 Category] [Priority Pill] [State Pill] [spacer] [Short ID]
        self._header_frame = ctk.CTkFrame(self, fg_color="transparent")
        self._header_frame.pack(fill="x", padx=16, pady=(12, 6))

        # Category icon & text
        cat_display = self.rec.category.replace("_", " ").title()
        self._category_lbl = ctk.CTkLabel(
            self._header_frame,
            text=f"💡 {cat_display}",
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
            text_color=ThemeColors.ORANGE_PRIMARY,
        )
        self._category_lbl.pack(side="left", padx=(0, 8))

        # Priority Pill
        prio_color = self._get_priority_color(self.rec.priority)
        self._priority_badge = ctk.CTkFrame(
            self._header_frame,
            fg_color="transparent",
            border_width=1,
            border_color=prio_color,
            corner_radius=10,
        )
        self._priority_badge.pack(side="left", padx=(0, 6))

        self._priority_lbl = ctk.CTkLabel(
            self._priority_badge,
            text=self.rec.priority.value,
            font=ctk.CTkFont(family="Segoe UI", size=10, weight="bold"),
            text_color=prio_color,
            padx=8,
            pady=2,
        )
        self._priority_lbl.pack()

        # State Pill (ACTIVE / DISMISSED / DEFERRED / HANDLED)
        self._state_badge = ctk.CTkFrame(
            self._header_frame,
            fg_color="transparent",
            border_width=1,
            border_color=ThemeColors.BORDER,
            corner_radius=10,
        )
        self._state_badge.pack(side="left", padx=(0, 6))

        self._state_lbl = ctk.CTkLabel(
            self._state_badge,
            text=self.rec.state.value,
            font=ctk.CTkFont(family="Segoe UI", size=10),
            text_color=ThemeColors.TEXT_MUTED,
            padx=8,
            pady=2,
        )
        self._state_lbl.pack()

        # Short ID badge on the right
        self._id_lbl = ctk.CTkLabel(
            self._header_frame,
            text=f"#{self.rec.short_id}",
            font=ctk.CTkFont(family="Consolas", size=11),
            text_color=ThemeColors.TEXT_MUTED,
        )
        self._id_lbl.pack(side="right")

        # 2. Recommendation Title
        self._title_lbl = ctk.CTkLabel(
            self,
            text=self.rec.title,
            font=ctk.CTkFont(family="Segoe UI", size=14, weight="bold"),
            text_color=ThemeColors.TEXT_PRIMARY,
            wraplength=540,
            justify="left",
            anchor="w",
        )
        self._title_lbl.pack(fill="x", padx=16, pady=(0, 4))

        # 3. Recommendation Explanation
        self._exp_lbl = ctk.CTkLabel(
            self,
            text=self.rec.explanation,
            font=ctk.CTkFont(family="Segoe UI", size=12),
            text_color=ThemeColors.TEXT_SECONDARY,
            wraplength=540,
            justify="left",
            anchor="w",
        )
        self._exp_lbl.pack(fill="x", padx=16, pady=(0, 8))

        # 4. Suggested Action Box
        if self.rec.suggested_action:
            self._suggestion_box = ctk.CTkFrame(
                self,
                fg_color=ThemeColors.WORKSPACE_BG,
                border_width=1,
                border_color=ThemeColors.BORDER,
                corner_radius=8,
            )
            self._suggestion_box.pack(fill="x", padx=16, pady=(0, 10))

            self._sugg_lbl = ctk.CTkLabel(
                self._suggestion_box,
                text=f"👉 Suggested Action: {self.rec.suggested_action}",
                font=ctk.CTkFont(family="Segoe UI", size=12),
                text_color=ThemeColors.TEXT_PRIMARY,
                wraplength=520,
                justify="left",
                anchor="w",
                padx=10,
                pady=6,
            )
            self._sugg_lbl.pack(fill="x")

        # 5. Expandable Evidence Container (hidden initially)
        self._evidence_container = ctk.CTkFrame(
            self,
            fg_color=ThemeColors.WORKSPACE_BG,
            border_width=1,
            border_color=ThemeColors.BORDER,
            corner_radius=8,
        )

        # 6. Status Feedback Badge (shown after lifecycle action)
        self._feedback_lbl = ctk.CTkLabel(
            self,
            text="",
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
            anchor="w",
        )

        # 7. Action Controls Row: [Dismiss (7d)] [Snooze (24h)] [Mark Handled] [View Evidence]
        self._actions_frame = ctk.CTkFrame(self, fg_color="transparent")
        self._actions_frame.pack(fill="x", padx=16, pady=(2, 14))

        # Dismiss Button (7 days)
        self._dismiss_btn = ctk.CTkButton(
            self._actions_frame,
            text="Dismiss (7d)",
            width=90,
            height=28,
            corner_radius=14,
            fg_color=ThemeColors.SURFACE_CARD_BG,
            border_width=1,
            border_color=ThemeColors.BORDER,
            hover_color=ThemeColors.SURFACE_CARD_HOVER,
            text_color=ThemeColors.TEXT_SECONDARY,
            font=ctk.CTkFont(family="Segoe UI", size=11),
            command=self._on_dismiss_clicked,
        )
        self._dismiss_btn.pack(side="left", padx=(0, 6))

        # Snooze Button (24 hours)
        self._snooze_btn = ctk.CTkButton(
            self._actions_frame,
            text="Snooze (24h)",
            width=90,
            height=28,
            corner_radius=14,
            fg_color=ThemeColors.SURFACE_CARD_BG,
            border_width=1,
            border_color=ThemeColors.BORDER,
            hover_color=ThemeColors.SURFACE_CARD_HOVER,
            text_color=ThemeColors.TEXT_SECONDARY,
            font=ctk.CTkFont(family="Segoe UI", size=11),
            command=self._on_snooze_clicked,
        )
        self._snooze_btn.pack(side="left", padx=(0, 6))

        # Mark Handled Button
        self._handle_btn = ctk.CTkButton(
            self._actions_frame,
            text="Mark Handled",
            width=100,
            height=28,
            corner_radius=14,
            fg_color=ThemeColors.ORANGE_PRIMARY,
            hover_color=ThemeColors.ORANGE_HOVER,
            text_color="#FFFFFF",
            font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            command=self._on_handle_clicked,
        )
        self._handle_btn.pack(side="left", padx=(0, 10))

        # View Evidence Toggle Button
        self._evidence_btn = ctk.CTkButton(
            self._actions_frame,
            text="View Evidence ▾",
            width=100,
            height=28,
            corner_radius=14,
            fg_color="transparent",
            hover_color=ThemeColors.SURFACE_CARD_HOVER,
            text_color=ThemeColors.TEXT_MUTED,
            font=ctk.CTkFont(family="Segoe UI", size=11),
            command=self._toggle_evidence_view,
        )
        self._evidence_btn.pack(side="right")

        # If already transitioned when initialized, reflect that state
        if self._is_transitioned:
            self._reflect_transition_state(self.rec.state, self.rec.suppressed_until)

    def _get_priority_color(self, priority: RecommendationPriority) -> str:
        """Map priority to standard visual colors."""
        if priority == RecommendationPriority.CRITICAL:
            return "#E74C3C"  # Red
        elif priority == RecommendationPriority.HIGH:
            return ThemeColors.ORANGE_PRIMARY
        elif priority == RecommendationPriority.NORMAL:
            return "#3498DB"  # Blue
        return ThemeColors.TEXT_MUTED

    def _toggle_evidence_view(self):
        """Expand or collapse the evidence detail container."""
        if self._is_evidence_visible:
            self._evidence_container.pack_forget()
            self._evidence_btn.configure(text="View Evidence ▾")
            self._is_evidence_visible = False
        else:
            self._render_evidence_details()
            # Pack evidence container right before action buttons
            self._evidence_container.pack(fill="x", padx=16, pady=(0, 10), before=self._actions_frame)
            self._evidence_btn.configure(text="Hide Evidence ▴")
            self._is_evidence_visible = True

    def _render_evidence_details(self):
        """Construct the evidence details view displaying underlying facts."""
        for child in self._evidence_container.winfo_children():
            try:
                child.destroy()
            except Exception:
                pass

        header_lbl = ctk.CTkLabel(
            self._evidence_container,
            text="🔍 Supporting Operational Evidence & Signals:",
            font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            text_color=ThemeColors.TEXT_PRIMARY,
            anchor="w",
        )
        header_lbl.pack(fill="x", padx=10, pady=(8, 4))

        # Render genuine evidence list or honest empty state
        if self.rec.evidence:
            for item in self.rec.evidence:
                item_lbl = ctk.CTkLabel(
                    self._evidence_container,
                    text=f"• {item}",
                    font=ctk.CTkFont(family="Segoe UI", size=11),
                    text_color=ThemeColors.TEXT_SECONDARY,
                    wraplength=500,
                    justify="left",
                    anchor="w",
                )
                item_lbl.pack(fill="x", padx=14, pady=2)
        else:
            empty_lbl = ctk.CTkLabel(
                self._evidence_container,
                text="• No underlying evidence signals recorded for this recommendation.",
                font=ctk.CTkFont(family="Segoe UI", size=11, slant="italic"),
                text_color=ThemeColors.TEXT_MUTED,
                anchor="w",
            )
            empty_lbl.pack(fill="x", padx=14, pady=2)

        # Provenance Metadata
        fp_short = self.rec.evidence_fingerprint[:16] if self.rec.evidence_fingerprint else "none"
        meta_lbl = ctk.CTkLabel(
            self._evidence_container,
            text=f"Entity ID: {self.rec.entity_id} | Fingerprint: {fp_short}...",
            font=ctk.CTkFont(family="Consolas", size=10),
            text_color=ThemeColors.TEXT_MUTED,
            anchor="w",
        )
        meta_lbl.pack(fill="x", padx=10, pady=(4, 2))

        disclaimer_lbl = ctk.CTkLabel(
            self._evidence_container,
            text="Note: Evidence signals above are observed system facts; the recommendation title and suggestions are inferred.",
            font=ctk.CTkFont(family="Segoe UI", size=10, slant="italic"),
            text_color=ThemeColors.TEXT_MUTED,
            wraplength=500,
            justify="left",
            anchor="w",
        )
        disclaimer_lbl.pack(fill="x", padx=10, pady=(2, 6))

    def _on_dismiss_clicked(self):
        """Handle user dismissal for 7 days."""
        if self._is_transitioned:
            return

        success = self.store.dismiss(self.rec.rec_id)
        if success:
            self._reflect_transition_state(
                RecommendationState.DISMISSED,
                (datetime.now(timezone.utc) + timedelta(days=7)).strftime("%Y-%m-%d"),
                msg="✓ Dismissed for 7 days.",
                msg_color="#2ECC71",
            )
            if self.on_action_callback:
                self.on_action_callback(self.rec.rec_id, "dismiss")
        else:
            self._show_error_feedback("Could not dismiss recommendation (target missing or stale).")

    def _on_snooze_clicked(self):
        """Handle user deferral for 24 hours."""
        if self._is_transitioned:
            return

        success = self.store.defer(self.rec.rec_id, "24h")
        if success:
            until_dt = (datetime.now() + timedelta(hours=24)).strftime("%Y-%m-%d %H:%M")
            self._reflect_transition_state(
                RecommendationState.DEFERRED,
                until_dt,
                msg=f"✓ Snoozed for 24 hours (until {until_dt}).",
                msg_color="#3498DB",
            )
            if self.on_action_callback:
                self.on_action_callback(self.rec.rec_id, "snooze")
        else:
            self._show_error_feedback("Could not snooze recommendation (target missing or stale).")

    def _on_handle_clicked(self):
        """Handle user marking recommendation as handled."""
        if self._is_transitioned:
            return

        success = self.store.handle(self.rec.rec_id)
        if success:
            self._reflect_transition_state(
                RecommendationState.HANDLED,
                None,
                msg="✓ Marked Handled — closed unless material evidence changes.",
                msg_color="#2ECC71",
            )
            if self.on_action_callback:
                self.on_action_callback(self.rec.rec_id, "handle")
        else:
            self._show_error_feedback("Could not mark handled (target missing or stale).")

    def _reflect_transition_state(
        self,
        new_state: RecommendationState,
        suppressed_until: Optional[str] = None,
        msg: Optional[str] = None,
        msg_color: str = "#2ECC71",
    ):
        """Update UI to reflect completed transition and disable repeated actions."""
        self._is_transitioned = True
        self.rec.state = new_state
        self.rec.suppressed_until = suppressed_until

        # Update state badge
        self._state_lbl.configure(text=new_state.value)

        # Disable mutating buttons to prevent repeat clicks
        self._dismiss_btn.configure(state="disabled")
        self._snooze_btn.configure(state="disabled")
        self._handle_btn.configure(state="disabled")

        # Show feedback badge if message provided
        if msg:
            self._feedback_lbl.configure(text=msg, text_color=msg_color)
            self._feedback_lbl.pack(fill="x", padx=16, pady=(0, 6), before=self._actions_frame)

    def _show_error_feedback(self, err_text: str):
        """Show error feedback without permanently disabling controls."""
        self._feedback_lbl.configure(text=f"⚠️ {err_text}", text_color="#E74C3C")
        self._feedback_lbl.pack(fill="x", padx=16, pady=(0, 6), before=self._actions_frame)

    def _on_theme_changed(self, theme: str):
        """Update theme-sensitive styles on dynamic theme toggle."""
        border_col = (
            ThemeColors.ORANGE_PRIMARY
            if self.rec.priority in (RecommendationPriority.CRITICAL, RecommendationPriority.HIGH)
            else ThemeColors.BORDER
        )
        self.configure(
            fg_color=ThemeColors.SURFACE_CARD_BG,
            border_color=border_col,
        )
        self._title_lbl.configure(text_color=ThemeColors.TEXT_PRIMARY)
        self._exp_lbl.configure(text_color=ThemeColors.TEXT_SECONDARY)
        self._category_lbl.configure(text_color=ThemeColors.ORANGE_PRIMARY)
        self._id_lbl.configure(text_color=ThemeColors.TEXT_MUTED)
        self._state_badge.configure(border_color=ThemeColors.BORDER)
        self._state_lbl.configure(text_color=ThemeColors.TEXT_MUTED)

        if hasattr(self, "_suggestion_box"):
            self._suggestion_box.configure(
                fg_color=ThemeColors.WORKSPACE_BG,
                border_color=ThemeColors.BORDER,
            )
            self._sugg_lbl.configure(text_color=ThemeColors.TEXT_PRIMARY)

        self._evidence_container.configure(
            fg_color=ThemeColors.WORKSPACE_BG,
            border_color=ThemeColors.BORDER,
        )
        self._dismiss_btn.configure(
            fg_color=ThemeColors.SURFACE_CARD_BG,
            border_color=ThemeColors.BORDER,
            hover_color=ThemeColors.SURFACE_CARD_HOVER,
            text_color=ThemeColors.TEXT_SECONDARY,
        )
        self._snooze_btn.configure(
            fg_color=ThemeColors.SURFACE_CARD_BG,
            border_color=ThemeColors.BORDER,
            hover_color=ThemeColors.SURFACE_CARD_HOVER,
            text_color=ThemeColors.TEXT_SECONDARY,
        )
        self._handle_btn.configure(
            fg_color=ThemeColors.ORANGE_PRIMARY,
            hover_color=ThemeColors.ORANGE_HOVER,
        )
        self._evidence_btn.configure(
            hover_color=ThemeColors.SURFACE_CARD_HOVER,
            text_color=ThemeColors.TEXT_MUTED,
        )

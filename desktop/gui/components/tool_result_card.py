"""
gui/components/tool_result_card.py - Structured Tool Execution Result Presentation.

Renders tool execution results in visually distinct, structured cards:
- Tool icon and name header
- Status indicator pill (Success / Failed / Denied)
- Formatted monospace output block preserving indentation, tables, and JSON
- 1-click clipboard copy button
- Collapsible view for long tool outputs (>10 lines)
- Full Light and Dark theme parity via ThemeColors and theme_manager
"""

from typing import Optional
import tkinter as tk
import customtkinter as ctk

from gui.theme import ThemeColors, theme_manager


class ToolResultCard(ctk.CTkFrame):
    """
    Visually distinct card for presenting tool execution results in the conversation.
    """

    def __init__(
        self,
        master,
        tool_name: str,
        status: str,
        output: str,
        command_arg: str = "",
        **kwargs,
    ):
        super().__init__(
            master,
            fg_color=ThemeColors.TOOL_CARD_BG,
            border_width=1,
            border_color=ThemeColors.TOOL_CARD_BORDER,
            corner_radius=12,
            **kwargs,
        )
        self._tool_name = tool_name or "Tool Execution"
        self._status = status.lower()
        self._output = output or "(no output)"
        self._command_arg = command_arg
        self._is_collapsed = False
        self._lines_count = len(self._output.strip().splitlines())

        self._build_ui()
        theme_manager.add_listener(self._on_theme_changed)

    def _build_ui(self):
        # 1. Header Row: [🔧 Tool Name] [Status Pill] [spacer] [Copy Button] [Collapse Button]
        self._header = ctk.CTkFrame(self, fg_color="transparent")
        self._header.pack(fill="x", padx=12, pady=(10, 6))

        # Tool icon & name
        icon_lbl = ctk.CTkLabel(
            self._header,
            text="🔧",
            font=ctk.CTkFont(size=13),
        )
        icon_lbl.pack(side="left", padx=(0, 6))

        display_name = self._tool_name
        if self._command_arg:
            display_name = f"{self._tool_name} ({self._command_arg})"

        self._title_lbl = ctk.CTkLabel(
            self._header,
            text=display_name,
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
            text_color=ThemeColors.TOOL_HEADER_TEXT,
        )
        self._title_lbl.pack(side="left", padx=(0, 10))

        # Status Pill
        self._pill_frame = ctk.CTkFrame(
            self._header,
            fg_color=ThemeColors.WORKSPACE_BG,
            corner_radius=10,
            border_width=1,
            border_color=ThemeColors.BORDER,
        )
        self._pill_frame.pack(side="left")

        if "fail" in self._status or "error" in self._status:
            status_text = "● Failed"
            status_color = ThemeColors.TOOL_ERROR_TEXT
        elif "denied" in self._status or "cancel" in self._status or "block" in self._status:
            status_text = "● Denied"
            status_color = ThemeColors.ORANGE_PRIMARY
        else:
            status_text = "● Success"
            status_color = ThemeColors.TOOL_SUCCESS_TEXT

        self._status_lbl = ctk.CTkLabel(
            self._pill_frame,
            text=status_text,
            font=ctk.CTkFont(family="Segoe UI", size=10, weight="bold"),
            text_color=status_color,
            padx=8,
            pady=1,
        )
        self._status_lbl.pack()

        # Collapse / Expand button (for long output > 8 lines)
        if self._lines_count > 8:
            self._collapse_btn = ctk.CTkButton(
                self._header,
                text="▲ Collapse",
                width=76,
                height=22,
                corner_radius=6,
                fg_color="transparent",
                hover_color=ThemeColors.SURFACE_CARD_HOVER,
                text_color=ThemeColors.TEXT_MUTED,
                font=ctk.CTkFont(family="Segoe UI", size=10),
                command=self._toggle_collapse,
            )
            self._collapse_btn.pack(side="right", padx=(4, 0))
        else:
            self._collapse_btn = None

        # Copy button
        self._copy_btn = ctk.CTkButton(
            self._header,
            text="📋 Copy",
            width=58,
            height=22,
            corner_radius=6,
            fg_color="transparent",
            hover_color=ThemeColors.SURFACE_CARD_HOVER,
            text_color=ThemeColors.TEXT_MUTED,
            font=ctk.CTkFont(family="Segoe UI", size=10),
            command=self._copy_output,
        )
        self._copy_btn.pack(side="right", padx=(0, 4))

        # 2. Output Box Container
        self._output_container = ctk.CTkFrame(
            self,
            fg_color=ThemeColors.CODE_BG,
            border_width=1,
            border_color=ThemeColors.CODE_BORDER,
            corner_radius=8,
        )
        self._output_container.pack(fill="x", padx=12, pady=(0, 10))

        # Monospace Text display (auto-sizing, no scrollbar to avoid nested scrollbar traps)
        displayed_lines = max(1, self._lines_count)
        is_dark = theme_manager.is_dark
        code_bg = ThemeColors.CODE_BG[1] if is_dark else ThemeColors.CODE_BG[0]
        code_fg = ThemeColors.CODE_TEXT[1] if is_dark else ThemeColors.CODE_TEXT[0]

        self._text_widget = tk.Text(
            self._output_container,
            bg=code_bg,
            fg=code_fg,
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            font=("Consolas", 10),
            wrap="word",
            height=displayed_lines,
            padx=10,
            pady=8,
        )
        self._text_widget.pack(fill="x", expand=True)
        self._text_widget.insert("1.0", self._output)
        self._text_widget.configure(state="disabled")

        def _adjust_height(event=None):
            try:
                if not self._text_widget.winfo_exists():
                    return
                if self._is_collapsed:
                    return
                logical_lines = max(1, int(self._text_widget.index("end-1c").split(".")[0]))
                dl = self._text_widget.count("1.0", "end", "displaylines")
                display_lines = dl[0] if (dl and dl[0] > 0) else logical_lines
                target = max(1, logical_lines, display_lines)
                if int(self._text_widget.cget("height")) != target:
                    self._text_widget.configure(height=target)
            except Exception:
                pass

        self._text_widget.bind("<Configure>", _adjust_height)
        self.after(20, _adjust_height)

    def _toggle_collapse(self):
        """Toggle between expanded view and compact 4-line preview."""
        if not self._collapse_btn:
            return

        if self._is_collapsed:
            # Expand
            self._is_collapsed = False
            self._collapse_btn.configure(text="▲ Collapse")
            self._text_widget.configure(state="normal")
            self._text_widget.delete("1.0", "end")
            self._text_widget.insert("1.0", self._output)
            displayed_lines = max(1, self._lines_count)
            self._text_widget.configure(height=displayed_lines, state="disabled")
            self.after(20, lambda: self._text_widget.event_generate("<Configure>"))
        else:
            # Collapse to 3 lines preview
            self._is_collapsed = True
            self._collapse_btn.configure(text="▼ Expand")
            lines = self._output.strip().splitlines()
            preview = "\n".join(lines[:3]) + f"\n... ({len(lines) - 3} more lines)"
            self._text_widget.configure(state="normal")
            self._text_widget.delete("1.0", "end")
            self._text_widget.insert("1.0", preview)
            self._text_widget.configure(height=4, state="disabled")

    def _copy_output(self):
        """Copy tool execution output to clipboard."""
        try:
            self.clipboard_clear()
            self.clipboard_append(self._output)
            self._copy_btn.configure(text="✓ Copied!")
            self.after(1500, lambda: self._copy_btn.configure(text="📋 Copy"))
        except Exception:
            pass

    def _on_theme_changed(self, theme: str):
        """Update colors on theme switch."""
        self.configure(
            fg_color=ThemeColors.TOOL_CARD_BG,
            border_color=ThemeColors.TOOL_CARD_BORDER,
        )
        self._title_lbl.configure(text_color=ThemeColors.TOOL_HEADER_TEXT)
        self._pill_frame.configure(
            fg_color=ThemeColors.WORKSPACE_BG,
            border_color=ThemeColors.BORDER,
        )
        if "fail" in self._status or "error" in self._status:
            status_color = ThemeColors.TOOL_ERROR_TEXT
        elif "denied" in self._status or "cancel" in self._status or "block" in self._status:
            status_color = ThemeColors.ORANGE_PRIMARY
        else:
            status_color = ThemeColors.TOOL_SUCCESS_TEXT
        self._status_lbl.configure(text_color=status_color)

        self._copy_btn.configure(
            hover_color=ThemeColors.SURFACE_CARD_HOVER,
            text_color=ThemeColors.TEXT_MUTED,
        )
        if self._collapse_btn:
            self._collapse_btn.configure(
                hover_color=ThemeColors.SURFACE_CARD_HOVER,
                text_color=ThemeColors.TEXT_MUTED,
            )

        self._output_container.configure(
            fg_color=ThemeColors.CODE_BG,
            border_color=ThemeColors.CODE_BORDER,
        )
        code_bg = ThemeColors.CODE_BG[1] if is_dark else ThemeColors.CODE_BG[0]
        code_fg = ThemeColors.CODE_TEXT[1] if is_dark else ThemeColors.CODE_TEXT[0]
        self._text_widget.configure(
            bg=code_bg,
            fg=code_fg,
        )

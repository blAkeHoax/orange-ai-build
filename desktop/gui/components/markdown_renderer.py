"""
gui/components/markdown_renderer.py - Lightweight Native Markdown Presenter for Orange AI.

Renders rich formatted assistant responses without heavyweight external dependencies:
- Headings (#, ##, ###) with proper scale and weight
- Inline bold (**bold**), italic (*italic*), and bold-italic (***text***)
- Inline code (`code`) with distinct monospace styling and background
- Multiline fenced code blocks (```lang ... ```) with syntax background, language pill, and 1-click Copy
- Bulleted (- / *) and numbered (1.) lists with clean indentation
- Blockquotes (> quote) with accent borders
- Preserves full Unicode, emojis, Bengali, Hindi, and multilingual text
- Automatic word-wrapping without hardcoded pixel limits
- Full Light and Dark theme responsiveness
"""

import re
import tkinter as tk
from typing import List, Dict, Any, Optional
import customtkinter as ctk

from gui.theme import ThemeColors, theme_manager

# Regex for tokenizing inline markdown spans
INLINE_TOKEN_RE = re.compile(
    r"(`[^`\n]+`|\*\*\*[^*\n]+\*\*\*|\*\*[^*\n]+\*\*|\*[^*\n]+\*|___[^_\n]+___|__[^_\n]+__|_[^_\n]+_)"
)

# Regex for detecting markdown headers
HEADER_RE = re.compile(r"^(#{1,3})\s+(.*)$")

# Regex for detecting bullet lists
BULLET_RE = re.compile(r"^(\s*)[*\-+•]\s+(.*)$")

# Regex for detecting numbered lists
NUMBERED_RE = re.compile(r"^(\s*\d+\.)\s+(.*)$")


def parse_markdown_blocks(raw_text: str) -> List[Dict[str, Any]]:
    """
    Parse a markdown string into a sequential list of structured blocks:
    - {'type': 'header', 'level': 1|2|3, 'text': '...'}
    - {'type': 'code', 'lang': '...', 'content': '...'}
    - {'type': 'quote', 'text': '...'}
    - {'type': 'text', 'lines': [...]}
    """
    normalized = raw_text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    blocks = []
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]

        # 1. Fenced Code Block: ```lang ... ```
        if line.strip().startswith("```"):
            fence = line.strip()
            lang = fence[3:].strip()
            code_lines = []
            i += 1
            while i < n and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            if i < n:  # consume closing ```
                i += 1
            code_str = "\n".join(code_lines)
            blocks.append({
                "type": "code",
                "lang": lang or "code",
                "content": code_str,
                "code": code_str,
            })
            continue

        # 2. Header: # Title, ## Subtitle, ### Section
        m_head = HEADER_RE.match(line)
        if m_head:
            blocks.append({
                "type": "header",
                "level": len(m_head.group(1)),
                "text": m_head.group(2).strip(),
            })
            i += 1
            continue

        # 3. Blockquote: > Quote text
        if line.startswith(">"):
            quote_lines = [line[1:].strip()]
            i += 1
            while i < n and lines[i].startswith(">"):
                quote_lines.append(lines[i][1:].strip())
                i += 1
            quote_str = "\n".join(quote_lines)
            blocks.append({
                "type": "blockquote",
                "text": quote_str,
                "lines": quote_lines,
            })
            continue

        # 4. List block: - item or 1. item
        if BULLET_RE.match(line) or NUMBERED_RE.match(line):
            list_lines = [line]
            i += 1
            while i < n:
                next_line = lines[i]
                if BULLET_RE.match(next_line) or NUMBERED_RE.match(next_line):
                    list_lines.append(next_line)
                    i += 1
                else:
                    break
            blocks.append({
                "type": "list",
                "lines": list_lines,
            })
            continue

        # 5. Empty lines: skip or separate
        if not line.strip():
            i += 1
            continue

        # 6. Regular text or paragraphs
        text_lines = [line]
        i += 1
        while i < n:
            next_line = lines[i]
            # Stop text block at fence, header, blockquote, or list item
            if (
                next_line.strip().startswith("```")
                or HEADER_RE.match(next_line)
                or next_line.startswith(">")
                or BULLET_RE.match(next_line)
                or NUMBERED_RE.match(next_line)
            ):
                break
            text_lines.append(next_line)
            i += 1

        blocks.append({
            "type": "text",
            "lines": text_lines,
        })

    return blocks


class CodeBlockCard(ctk.CTkFrame):
    """
    Dedicated code block component with syntax-friendly styling,
    language label, and 1-click clipboard copy.
    """

    def __init__(self, master, lang: str, code: str, **kwargs):
        super().__init__(
            master,
            fg_color=ThemeColors.CODE_BG,
            border_width=1,
            border_color=ThemeColors.CODE_BORDER,
            corner_radius=8,
            **kwargs,
        )
        self.lang = lang.upper() if lang else "CODE"
        self.code = code
        self._build_ui()
        theme_manager.add_listener(self._on_theme_changed)

    def _build_ui(self):
        # 1. Header Bar
        self._header = ctk.CTkFrame(
            self,
            fg_color=ThemeColors.CODE_HEADER_BG,
            corner_radius=0,
            height=26,
        )
        self._header.pack(fill="x", side="top")
        self._header.pack_propagate(False)

        self._lang_lbl = ctk.CTkLabel(
            self._header,
            text=self.lang,
            font=ctk.CTkFont(family="Segoe UI", size=9, weight="bold"),
            text_color=ThemeColors.TEXT_MUTED,
        )
        self._lang_lbl.pack(side="left", padx=10)

        self._copy_btn = ctk.CTkButton(
            self._header,
            text="📋 Copy",
            width=54,
            height=20,
            corner_radius=10,
            fg_color="transparent",
            hover_color=ThemeColors.SURFACE_CARD_BG,
            text_color=ThemeColors.TEXT_SECONDARY,
            font=ctk.CTkFont(family="Segoe UI", size=10),
            command=self._on_copy_clicked,
        )
        self._copy_btn.pack(side="right", padx=6)

        # 2. Monospace Code Area
        lines = self.code.split("\n")
        line_count = max(1, len(lines))

        is_dark = theme_manager.is_dark
        bg_col = ThemeColors.CODE_BG[1] if is_dark else ThemeColors.CODE_BG[0]
        fg_col = ThemeColors.CODE_TEXT[1] if is_dark else ThemeColors.CODE_TEXT[0]

        self._code_text = tk.Text(
            self,
            font=("Consolas", 10),
            bg=bg_col,
            fg=fg_col,
            insertbackground=fg_col,
            selectbackground=ThemeColors.ORANGE_PRIMARY,
            selectforeground="#FFFFFF",
            relief="flat",
            bd=0,
            highlightthickness=0,
            wrap="none",
            height=line_count,
            padx=10,
            pady=8,
        )
        self._code_text.pack(fill="x", expand=True)
        self._code_text.insert("1.0", self.code)
        self._code_text.configure(state="disabled")

    def _on_copy_clicked(self):
        """Copy code to system clipboard and display temporary confirmation."""
        try:
            self.clipboard_clear()
            self.clipboard_append(self.code)
            self._copy_btn.configure(text="✓ Copied!")
            self.after(2000, lambda: self._copy_btn.configure(text="📋 Copy"))
        except Exception:
            pass

    def _on_theme_changed(self, theme: str):
        is_dark = (theme == "dark")
        bg_col = ThemeColors.CODE_BG[1] if is_dark else ThemeColors.CODE_BG[0]
        fg_col = ThemeColors.CODE_TEXT[1] if is_dark else ThemeColors.CODE_TEXT[0]
        border_col = ThemeColors.CODE_BORDER[1] if is_dark else ThemeColors.CODE_BORDER[0]
        header_col = ThemeColors.CODE_HEADER_BG[1] if is_dark else ThemeColors.CODE_HEADER_BG[0]

        self.configure(fg_color=bg_col, border_color=border_col)
        self._header.configure(fg_color=header_col)
        self._code_text.configure(bg=bg_col, fg=fg_col, insertbackground=fg_col)


class MarkdownRenderer(ctk.CTkFrame):
    """
    Renders structured Markdown blocks within an assistant message bubble.
    """

    def __init__(self, master, markdown_text: str, **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        self.markdown_text = markdown_text
        self._text_widgets: List[tk.Text] = []
        self._code_cards: List[CodeBlockCard] = []
        self._header_labels: List[ctk.CTkLabel] = []
        self._quote_frames: List[ctk.CTkFrame] = []

        self._render_blocks()
        theme_manager.add_listener(self._on_theme_changed)

    def _render_blocks(self):
        blocks = parse_markdown_blocks(self.markdown_text)

        for b in blocks:
            b_type = b["type"]

            if b_type == "header":
                self._render_header(b["level"], b["text"])
            elif b_type == "code":
                self._render_code(b["lang"], b.get("code") or b.get("content", ""))
            elif b_type in ("quote", "blockquote"):
                self._render_quote(b["text"])
            elif b_type in ("text", "list"):
                self._render_text_block(b["lines"])

    def _render_header(self, level: int, text: str):
        size_map = {1: 15, 2: 13, 3: 12}
        font_size = size_map.get(level, 12)

        lbl = ctk.CTkLabel(
            self,
            text=text,
            font=ctk.CTkFont(family="Segoe UI", size=font_size, weight="bold"),
            text_color=ThemeColors.TEXT_PRIMARY,
            anchor="w",
            justify="left",
        )
        lbl.pack(fill="x", padx=4, pady=(6, 2))
        self._header_labels.append(lbl)

    def _render_code(self, lang: str, content: str):
        card = CodeBlockCard(self, lang=lang, code=content)
        card.pack(fill="x", padx=4, pady=(4, 6))
        self._code_cards.append(card)

    def _render_quote(self, text: str):
        quote_frame = ctk.CTkFrame(
            self,
            fg_color=ThemeColors.SURFACE_CARD_HOVER,
            corner_radius=6,
            border_width=1,
            border_color=ThemeColors.BORDER,
        )
        quote_frame.pack(fill="x", padx=4, pady=(4, 4))
        self._quote_frames.append(quote_frame)

        # Left orange accent line
        accent = ctk.CTkFrame(
            quote_frame,
            fg_color=ThemeColors.ORANGE_PRIMARY,
            width=3,
            height=0,
            corner_radius=2,
        )
        accent.pack(side="left", fill="y", padx=(4, 8), pady=4)

        lbl = ctk.CTkLabel(
            quote_frame,
            text=text,
            font=ctk.CTkFont(family="Segoe UI", size=12, slant="italic"),
            text_color=ThemeColors.TEXT_SECONDARY,
            anchor="w",
            justify="left",
            wraplength=540,
        )
        lbl.pack(side="left", fill="both", expand=True, padx=4, pady=6)

    def _render_text_block(self, lines: List[str]):
        is_dark = theme_manager.is_dark
        bg_col = ThemeColors.SURFACE_CARD_BG[1] if is_dark else ThemeColors.SURFACE_CARD_BG[0]
        fg_col = ThemeColors.TEXT_PRIMARY[1] if is_dark else ThemeColors.TEXT_PRIMARY[0]
        code_bg = ThemeColors.CODE_INLINE_BG[1] if is_dark else ThemeColors.CODE_INLINE_BG[0]
        code_fg = ThemeColors.CODE_INLINE_FG[1] if is_dark else ThemeColors.CODE_INLINE_FG[0]

        txt = tk.Text(
            self,
            font=("Segoe UI", 10),
            bg=bg_col,
            fg=fg_col,
            insertbackground=fg_col,
            selectbackground=ThemeColors.ORANGE_PRIMARY,
            selectforeground="#FFFFFF",
            relief="flat",
            bd=0,
            highlightthickness=0,
            wrap="word",
            padx=0,
            pady=2,
        )
        txt.pack(fill="x", expand=True, padx=4, pady=(1, 3))
        self._text_widgets.append(txt)

        # Configure formatting tags
        txt.tag_configure("bold", font=("Segoe UI", 10, "bold"))
        txt.tag_configure("italic", font=("Segoe UI", 10, "italic"))
        txt.tag_configure("bold_italic", font=("Segoe UI", 10, "bold italic"))
        txt.tag_configure(
            "inline_code",
            font=("Consolas", 9),
            background=code_bg,
            foreground=code_fg,
        )
        txt.tag_configure("bullet", lmargin1=4, lmargin2=18)
        txt.tag_configure("num_bullet", lmargin1=4, lmargin2=22)

        # Process lines and inline tokens
        for line_idx, line in enumerate(lines):
            line_tags = []
            prefix = ""

            # Check for bullet list
            m_bullet = BULLET_RE.match(line)
            m_num = NUMBERED_RE.match(line)

            if m_bullet:
                indent = m_bullet.group(1)
                content = m_bullet.group(2)
                prefix = f"{indent}• "
                line_tags.append("bullet")
                txt.insert("end", prefix, tuple(line_tags))
                self._insert_formatted_spans(txt, content, base_tags=line_tags)
            elif m_num:
                num_tag = m_num.group(1)
                content = m_num.group(2)
                prefix = f"{num_tag} "
                line_tags.append("num_bullet")
                txt.insert("end", prefix, tuple(line_tags))
                self._insert_formatted_spans(txt, content, base_tags=line_tags)
            else:
                self._insert_formatted_spans(txt, line, base_tags=line_tags)

            if line_idx < len(lines) - 1:
                txt.insert("end", "\n")

        txt.configure(state="disabled")
        self._fit_text_height(txt)

    def _insert_formatted_spans(self, txt: tk.Text, text_segment: str, base_tags: List[str]):
        """Tokenize inline text and insert styled spans."""
        tokens = INLINE_TOKEN_RE.split(text_segment)
        for tok in tokens:
            if not tok:
                continue

            if tok.startswith("`") and tok.endswith("`") and len(tok) >= 2:
                # Inline code
                inner = tok[1:-1]
                txt.insert("end", inner, tuple(base_tags + ["inline_code"]))
            elif (tok.startswith("***") and tok.endswith("***") and len(tok) >= 6) or \
                 (tok.startswith("___") and tok.endswith("___") and len(tok) >= 6):
                # Bold Italic
                inner = tok[3:-3]
                txt.insert("end", inner, tuple(base_tags + ["bold_italic"]))
            elif (tok.startswith("**") and tok.endswith("**") and len(tok) >= 4) or \
                 (tok.startswith("__") and tok.endswith("__") and len(tok) >= 4):
                # Bold
                inner = tok[2:-2]
                txt.insert("end", inner, tuple(base_tags + ["bold"]))
            elif (tok.startswith("*") and tok.endswith("*") and len(tok) >= 2) or \
                 (tok.startswith("_") and tok.endswith("_") and len(tok) >= 2):
                # Italic
                inner = tok[1:-1]
                txt.insert("end", inner, tuple(base_tags + ["italic"]))
            else:
                # Regular text
                txt.insert("end", tok, tuple(base_tags) if base_tags else None)

    def _fit_text_height(self, txt: tk.Text):
        """Fit height dynamically to display lines without scrollbars."""
        def _adjust(event=None):
            try:
                if not txt.winfo_exists():
                    return
                # Calculate display lines after text is rendered
                logical_lines = max(1, int(txt.index("end-1c").split(".")[0]))
                dl = txt.count("1.0", "end", "displaylines")
                display_lines = dl[0] if (dl and dl[0] > 0) else logical_lines
                target = max(1, logical_lines, display_lines)
                if int(txt.cget("height")) != target:
                    txt.configure(height=target)
            except Exception:
                pass

        # Initial height from line breaks
        try:
            line_count = int(txt.index("end-1c").split(".")[0])
            txt.configure(height=max(1, line_count))
        except Exception:
            txt.configure(height=1)

        # Bind to configure for responsive line-wrapping
        txt.bind("<Configure>", _adjust)
        self.after(20, _adjust)

    def _on_theme_changed(self, theme: str):
        is_dark = (theme == "dark")
        bg_col = ThemeColors.SURFACE_CARD_BG[1] if is_dark else ThemeColors.SURFACE_CARD_BG[0]
        fg_col = ThemeColors.TEXT_PRIMARY[1] if is_dark else ThemeColors.TEXT_PRIMARY[0]
        code_bg = ThemeColors.CODE_INLINE_BG[1] if is_dark else ThemeColors.CODE_INLINE_BG[0]
        code_fg = ThemeColors.CODE_INLINE_FG[1] if is_dark else ThemeColors.CODE_INLINE_FG[0]

        for txt in self._text_widgets:
            try:
                if txt.winfo_exists():
                    txt.configure(bg=bg_col, fg=fg_col, insertbackground=fg_col)
                    txt.tag_configure("inline_code", background=code_bg, foreground=code_fg)
            except Exception:
                pass

        for lbl in self._header_labels:
            try:
                if lbl.winfo_exists():
                    lbl.configure(text_color=ThemeColors.TEXT_PRIMARY)
            except Exception:
                pass

        for q in self._quote_frames:
            try:
                if q.winfo_exists():
                    q.configure(
                        fg_color=ThemeColors.SURFACE_CARD_HOVER,
                        border_color=ThemeColors.BORDER,
                    )
            except Exception:
                pass

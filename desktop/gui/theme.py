"""
Orange AI Desktop - Centralized Design Tokens & Theme Engine.

Defines design tokens (colors, typography, spacing) for both Light (default)
and Dark themes. Provides dynamic theme switching without restarting and
safely sets Windows DWM title-bar caption colors where supported.
"""

import sys
import ctypes
from typing import Callable, List, Tuple
import customtkinter as ctk
from gui.settings import get_theme_preference, set_theme_preference


class ThemeColors:
    """Design tokens structured as (Light, Dark) tuples for CustomTkinter."""

    # Brand Colors
    ORANGE_PRIMARY = "#FF6B00"
    ORANGE_HOVER = "#FF7F24"
    ORANGE_PRESSED = "#E55F00"
    ORANGE_TINT_LIGHT = "#FFF3E8"
    ORANGE_TINT_DARK = "#261B12"

    # Surfaces & Backgrounds (Light, Dark)
    WINDOW_BG: Tuple[str, str] = ("#F8F9FA", "#0B0E14")
    WORKSPACE_BG: Tuple[str, str] = ("#F8F9FA", "#0D1117")
    NAV_RAIL_BG: Tuple[str, str] = ("#FFFFFF", "#0B0E14")
    SURFACE_CARD_BG: Tuple[str, str] = ("#FFFFFF", "#161B22")
    SURFACE_CARD_HOVER: Tuple[str, str] = ("#F3F4F6", "#1C2128")

    # Borders & Dividers
    BORDER: Tuple[str, str] = ("#E5E7EB", "#21262D")
    BORDER_SUBTLE: Tuple[str, str] = ("#F3F4F6", "#30363D")
    BORDER_ACTIVE: str = "#FF6B00"

    # Typography
    TEXT_PRIMARY: Tuple[str, str] = ("#111827", "#FFFFFF")
    TEXT_SECONDARY: Tuple[str, str] = ("#4B5563", "#9CA3AF")
    TEXT_MUTED: Tuple[str, str] = ("#9CA3AF", "#6B7280")
    TEXT_BRAND: str = "#FF6B00"

    # Navigation Items
    NAV_ITEM_DEFAULT_BG: Tuple[str, str] = ("transparent", "transparent")
    NAV_ITEM_HOVER_BG: Tuple[str, str] = ("#F3F4F6", "#161B22")
    NAV_ITEM_ACTIVE_BG: Tuple[str, str] = ("#FFF3E8", "#261B12")
    NAV_ITEM_DEFAULT_TEXT: Tuple[str, str] = ("#4B5563", "#9CA3AF")
    NAV_ITEM_ACTIVE_TEXT: Tuple[str, str] = ("#FF6B00", "#FF8C33")

    # Code Blocks & Monospace Styling (Light, Dark)
    CODE_BG: Tuple[str, str] = ("#F6F8FA", "#0D1117")
    CODE_BORDER: Tuple[str, str] = ("#E1E4E8", "#30363D")
    CODE_TEXT: Tuple[str, str] = ("#1F2328", "#E6EDF3")
    CODE_HEADER_BG: Tuple[str, str] = ("#EAECEF", "#161B22")
    CODE_INLINE_BG: Tuple[str, str] = ("#F0F2F5", "#21262D")
    CODE_INLINE_FG: Tuple[str, str] = ("#C2410C", "#FB923C")

    # Tool Execution Results (Light, Dark)
    TOOL_CARD_BG: Tuple[str, str] = ("#F8FAFC", "#0E1520")
    TOOL_CARD_BORDER: Tuple[str, str] = ("#CBD5E1", "#1E293B")
    TOOL_HEADER_TEXT: Tuple[str, str] = ("#334155", "#94A3B8")
    TOOL_SUCCESS_TEXT: Tuple[str, str] = ("#059669", "#34D399")
    TOOL_ERROR_TEXT: Tuple[str, str] = ("#DC2626", "#F87171")

    # Metadata & Diagnostics (Light, Dark)
    METADATA_BG: Tuple[str, str] = ("#F1F5F9", "#1A202C")
    METADATA_BORDER: Tuple[str, str] = ("#E2E8F0", "#2D3748")
    METADATA_TEXT: Tuple[str, str] = ("#64748B", "#94A3B8")


class ThemeManager:
    """Manages active theme state, observer notifications, and persistence."""

    def __init__(self, initial_theme: str = None):
        self._current_theme = initial_theme or get_theme_preference()
        self._listeners: List[Callable[[str], None]] = []
        self._root_window = None

    @property
    def current_theme(self) -> str:
        return self._current_theme

    @property
    def is_dark(self) -> bool:
        return self._current_theme == "dark"

    def register_root(self, root_window):
        """Register the main window for title-bar theme customization."""
        self._root_window = root_window
        self.apply_theme_to_window()

    def add_listener(self, callback: Callable[[str], None]):
        """Register a callback for dynamic theme change notifications."""
        if callback not in self._listeners:
            self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[str], None]):
        """Unregister a theme change callback."""
        if callback in self._listeners:
            self._listeners.remove(callback)

    def set_theme(self, theme: str):
        """Set and persist the theme, updating all widgets and title-bar."""
        theme_clean = str(theme).strip().lower()
        if theme_clean not in ("light", "dark"):
            return

        self._current_theme = theme_clean
        set_theme_preference(theme_clean)

        # Apply to CustomTkinter
        ctk.set_appearance_mode(theme_clean)

        # Apply to native Windows title bar
        self.apply_theme_to_window()

        # Notify observers
        for listener in list(self._listeners):
            try:
                listener(theme_clean)
            except Exception:
                pass

    def toggle_theme(self):
        """Toggle between light and dark themes."""
        new_theme = "light" if self._current_theme == "dark" else "dark"
        self.set_theme(new_theme)

    def apply_theme_to_window(self):
        """Apply Windows DWM caption styling safely if on Windows."""
        if not self._root_window or sys.platform != "win32":
            return
        apply_titlebar_theme(self._root_window, self.is_dark)


def apply_titlebar_theme(window, is_dark: bool):
    """
    Safely configure native Windows title bar theme matching using DWM API.
    Silently falls back if unsupported.
    """
    if sys.platform != "win32":
        return
    try:
        window.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        if not hwnd:
            hwnd = window.winfo_id()

        DWMWA_USE_IMMERSIVE_DARK_MODE = 20
        DWMWA_USE_IMMERSIVE_DARK_MODE_FALLBACK = 19
        val = ctypes.c_int(1 if is_dark else 0)

        res = ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd,
            DWMWA_USE_IMMERSIVE_DARK_MODE,
            ctypes.byref(val),
            ctypes.sizeof(val),
        )
        if res != 0:
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd,
                DWMWA_USE_IMMERSIVE_DARK_MODE_FALLBACK,
                ctypes.byref(val),
                ctypes.sizeof(val),
            )
    except Exception:
        pass


# Global singleton theme manager
theme_manager = ThemeManager()

"""
Orange AI Desktop - GUI Package.

Provides the foundational desktop GUI shell, left navigation rail,
and theme engine for Orange AI.
"""

from gui.theme import ThemeColors, ThemeManager, theme_manager
from gui.settings import get_theme_preference, set_theme_preference
from gui.app import OrangeAIApp, launch_gui

__all__ = [
    "ThemeColors",
    "ThemeManager",
    "theme_manager",
    "get_theme_preference",
    "set_theme_preference",
    "OrangeAIApp",
    "launch_gui",
]

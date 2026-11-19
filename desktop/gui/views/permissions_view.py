"""
gui/views/permissions_view.py - Security Policy & Permissions View (Stage 4).

Reflects the actual existing permission policy and risk classifications:
- Current process privilege level (Administrator vs Standard User)
- Three locked risk classification tiers (SAFE, CONFIRMATION_REQUIRED, HIGH_RISK)
- Comprehensive audit of all registered tools and their enforcement rules
- Strictly read-only policy presentation (prevents elevation or bypass)
"""

from typing import Dict, Any, List
import customtkinter as ctk

from gui.theme import ThemeColors, theme_manager
from tools.permissions import RiskLevel, PermissionStatus, check_permission
from tools.system import is_running_as_admin
from tools.registry import registry


class PermissionsView(ctk.CTkFrame):
    """View presenting the real security policy, privilege status, and tool permissions."""

    def __init__(self, master, **kwargs):
        super().__init__(
            master,
            fg_color=ThemeColors.WORKSPACE_BG,
            corner_radius=0,
            **kwargs,
        )
        self._build_ui()
        theme_manager.add_listener(self._on_theme_changed)

    def _build_ui(self):
        # 1. Header
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=28, pady=(20, 10))

        self._title_lbl = ctk.CTkLabel(
            header,
            text="Permissions & Security Policy",
            font=ctk.CTkFont(family="Segoe UI", size=22, weight="bold"),
            text_color=ThemeColors.TEXT_PRIMARY,
        )
        self._title_lbl.pack(anchor="w")

        self._sub_lbl = ctk.CTkLabel(
            header,
            text="Centralized permission architecture, risk tiers, and execution security boundaries.",
            font=ctk.CTkFont(family="Segoe UI", size=13),
            text_color=ThemeColors.TEXT_SECONDARY,
        )
        self._sub_lbl.pack(anchor="w", pady=(2, 0))

        # 2. Runtime Security Status Banner
        admin_status = is_running_as_admin()
        self._admin_status = admin_status
        status_bg = "#FEF3C7" if admin_status else ThemeColors.SURFACE_CARD_BG
        status_border = "#F59E0B" if admin_status else ThemeColors.BORDER

        status_card = ctk.CTkFrame(
            self,
            fg_color=status_bg,
            border_width=1,
            border_color=status_border,
            corner_radius=10,
        )
        status_card.pack(fill="x", padx=28, pady=(0, 14))
        self._status_card = status_card

        status_icon = "🛡️" if admin_status else "👤"
        status_title = "Elevated: Windows Administrator Rights" if admin_status else "Standard User Privilege (UAC Standard)"
        status_color = "#92400E" if admin_status else ThemeColors.TEXT_PRIMARY

        top_status = ctk.CTkFrame(status_card, fg_color="transparent")
        top_status.pack(fill="x", padx=14, pady=(10, 2))

        self._elevation_label = ctk.CTkLabel(
            top_status,
            text=f"{status_icon} Process Privilege Level: {status_title}",
            font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
            text_color=status_color,
        )
        self._elevation_label.pack(side="left")

        ctk.CTkLabel(
            status_card,
            text="Immutable Security Policy: Operating system administrator privileges do NOT bypass confirmation requirements or unblock high-risk actions. All actions strictly adhere to the locked permission tiers below.",
            font=ctk.CTkFont(family="Segoe UI", size=11),
            text_color="#78350F" if admin_status else ThemeColors.TEXT_MUTED,
            wraplength=640,
            justify="left",
        ).pack(fill="x", padx=14, pady=(0, 10))

        # 3. Main Scrollable Container for Policy Tiers & Tool Audit
        self._scroll_frame = ctk.CTkScrollableFrame(
            self,
            fg_color="transparent",
            corner_radius=0,
        )
        self._scroll_frame.pack(fill="both", expand=True, padx=28, pady=(0, 20))
        self._scroll_frame.grid_columnconfigure(0, weight=1)

        # 4. Three Policy Tiers
        self._render_policy_tier(
            self._scroll_frame,
            title="Tier 1: Read-Only Actions (SAFE)",
            badge_text="✓ ALLOWED DIRECTLY",
            badge_color="#059669",
            desc="Read-only operations that inspect system hardware, check time, search files, or read documents without altering machine state. Allowed to execute directly upon user request.",
            example_tools=["get_time", "get_date", "get_cpu_usage", "get_ram_usage", "list_files", "read_file", "list_processes", "find_file"],
        )

        self._render_policy_tier(
            self._scroll_frame,
            title="Tier 2: State-Changing Actions (CONFIRMATION_REQUIRED)",
            badge_text="⚠️ EXPLICIT CONSENT REQUIRED",
            badge_color="#D97706",
            desc="Actions that modify system state, launch desktop applications, or terminate processes. Orange AI will present an inline Approve/Deny confirmation card in the chat workspace. Execution proceeds ONLY upon affirmative approval.",
            example_tools=["open_app", "close_process", "open_folder", "run plan"],
        )

        self._render_policy_tier(
            self._scroll_frame,
            title="Tier 3: Critical Operations (HIGH_RISK)",
            badge_text="⛔ PERMANENTLY BLOCKED",
            badge_color="#DC2626",
            desc="High-risk, destructive, or arbitrary system operations (e.g. raw shell execution, recursive deletion, system configuration tampering). Permanently blocked by default. UAC or administrator privileges cannot override this boundary.",
            example_tools=["raw_shell_exec", "recursive_delete", "system_tamper"],
        )

        # 5. Tool Audit Table
        ctk.CTkLabel(
            self._scroll_frame,
            text="Registered Tool Policy Audit",
            font=ctk.CTkFont(family="Segoe UI", size=15, weight="bold"),
            text_color=ThemeColors.TEXT_PRIMARY,
            anchor="w",
        ).pack(fill="x", pady=(16, 6))

        table_card = ctk.CTkFrame(
            self._scroll_frame,
            fg_color=ThemeColors.SURFACE_CARD_BG,
            border_width=1,
            border_color=ThemeColors.BORDER,
            corner_radius=10,
        )
        table_card.pack(fill="x", pady=(0, 16))

        # Table Header
        th = ctk.CTkFrame(table_card, fg_color=ThemeColors.WORKSPACE_BG, corner_radius=6)
        th.pack(fill="x", padx=6, pady=6)
        ctk.CTkLabel(th, text="Tool Name", width=140, anchor="w", font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"), text_color=ThemeColors.TEXT_MUTED).pack(side="left", padx=10)
        ctk.CTkLabel(th, text="Category", width=90, anchor="w", font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"), text_color=ThemeColors.TEXT_MUTED).pack(side="left", padx=4)
        ctk.CTkLabel(th, text="Risk Level", width=160, anchor="w", font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"), text_color=ThemeColors.TEXT_MUTED).pack(side="left", padx=4)
        ctk.CTkLabel(th, text="Enforcement Rule", anchor="w", font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"), text_color=ThemeColors.TEXT_MUTED).pack(side="left", padx=4, fill="x", expand=True)

        tools = registry.list_tools()
        for idx, tool in enumerate(tools):
            tr = ctk.CTkFrame(table_card, fg_color="transparent")
            tr.pack(fill="x", padx=6, pady=2)

            risk_val = tool.risk_level.value
            if risk_val == "SAFE":
                risk_color = "#059669"
                rule_text = "Allowed directly (read-only)"
            elif risk_val == "CONFIRMATION_REQUIRED":
                risk_color = "#D97706"
                rule_text = "Requires inline user confirmation card"
            else:
                risk_color = "#DC2626"
                rule_text = "Denied by default"

            ctk.CTkLabel(tr, text=tool.name, width=140, anchor="w", font=ctk.CTkFont(family="Consolas", size=11, weight="bold"), text_color=ThemeColors.TEXT_PRIMARY).pack(side="left", padx=10)
            ctk.CTkLabel(tr, text=tool.category.upper(), width=90, anchor="w", font=ctk.CTkFont(family="Segoe UI", size=11), text_color=ThemeColors.TEXT_SECONDARY).pack(side="left", padx=4)
            ctk.CTkLabel(tr, text=f"● {risk_val}", width=160, anchor="w", font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"), text_color=risk_color).pack(side="left", padx=4)
            ctk.CTkLabel(tr, text=rule_text, anchor="w", font=ctk.CTkFont(family="Segoe UI", size=11), text_color=ThemeColors.TEXT_MUTED).pack(side="left", padx=4, fill="x", expand=True)

    def _render_policy_tier(self, parent, title: str, badge_text: str, badge_color: str, desc: str, example_tools: List[str]):
        card = ctk.CTkFrame(
            parent,
            fg_color=ThemeColors.SURFACE_CARD_BG,
            border_width=1,
            border_color=ThemeColors.BORDER,
            corner_radius=10,
        )
        card.pack(fill="x", pady=5)

        header = ctk.CTkFrame(card, fg_color="transparent")
        header.pack(fill="x", padx=14, pady=(10, 4))

        ctk.CTkLabel(
            header,
            text=title,
            font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
            text_color=ThemeColors.TEXT_PRIMARY,
        ).pack(side="left")

        ctk.CTkLabel(
            header,
            text=badge_text,
            font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            text_color=badge_color,
        ).pack(side="right")

        ctk.CTkLabel(
            card,
            text=desc,
            font=ctk.CTkFont(family="Segoe UI", size=12),
            text_color=ThemeColors.TEXT_SECONDARY,
            anchor="w",
            wraplength=640,
            justify="left",
        ).pack(fill="x", padx=14, pady=2)

        examples_str = "Examples: " + ", ".join(f"`{t}`" for t in example_tools)
        ctk.CTkLabel(
            card,
            text=examples_str,
            font=ctk.CTkFont(family="Consolas", size=11),
            text_color=ThemeColors.TEXT_MUTED,
            anchor="w",
        ).pack(fill="x", padx=14, pady=(2, 10))

    def _on_theme_changed(self, theme: str):
        self.configure(fg_color=ThemeColors.WORKSPACE_BG)
        self._title_lbl.configure(text_color=ThemeColors.TEXT_PRIMARY)
        self._sub_lbl.configure(text_color=ThemeColors.TEXT_SECONDARY)

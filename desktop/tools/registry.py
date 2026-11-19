"""
tools/registry.py - Centralized Desktop Tool Registry for Orange AI.

Provides registration, deterministic lookup, and safe metadata inspection for
all local Desktop capabilities. Tool registration and inspection never execute
underlying tools.
"""

from dataclasses import dataclass
from typing import Callable, Dict, List, Any, Optional

from tools.permissions import (
    RiskLevel,
    execute_with_permission,
)

# Tool imports from existing desktop tools
from tools.system import (
    get_time,
    get_date,
    get_cpu_usage,
    get_ram_usage,
    get_disk_usage,
    get_battery,
    is_running_as_admin,
)
from tools.files import (
    list_files,
    read_file,
    edit_file,
    find_file,
    find_folder,
)
from tools.processes import (
    list_processes,
    is_running,
    close_process,
)
from tools.apps import (
    open_app,
    open_folder,
)
from tools.todo import (
    todo_write,
)
from tools.command import (
    run_command,
)


@dataclass
class Tool:
    """Represents a registered tool with its metadata and callable."""
    name: str
    description: str
    risk_level: RiskLevel
    func: Callable
    category: str = "general"

    @property
    def requires_confirmation(self) -> bool:
        return self.risk_level != RiskLevel.SAFE

    def to_dict(self) -> Dict[str, Any]:
        """Return safe, serializable metadata without exposing callables or secrets."""
        return {
            "name": self.name,
            "description": self.description,
            "risk_level": self.risk_level.value,
            "requires_confirmation": self.requires_confirmation,
            "category": self.category,
        }


class ToolRegistry:
    """
    Central registry for Desktop tools.
    Prevents duplicate registrations and enforces permission checks on execution.
    """

    def __init__(self):
        self._tools: Dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """Register a new tool. Raises ValueError if the tool name is already registered."""
        if tool.name in self._tools:
            raise ValueError(f"Tool '{tool.name}' is already registered in the ToolRegistry.")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[Tool]:
        """Retrieve a registered tool by name without executing it."""
        return self._tools.get(name)

    def list_tools(self) -> List[Tool]:
        """Return a list of all registered tools without executing them."""
        return list(self._tools.values())

    def list_metadata(self) -> List[Dict[str, Any]]:
        """Return serializable metadata for all registered tools."""
        return [tool.to_dict() for tool in self._tools.values()]

    def contains(self, name: str) -> bool:
        """Check if a tool is registered by name."""
        return name in self._tools

    def execute(
        self,
        name: str,
        *args: Any,
        is_admin: bool = False,
        prompt_fn: Optional[Callable[[str], str]] = None,
        **kwargs: Any,
    ) -> Any:
        """
        Look up the tool by name and execute it strictly through the permission check.
        """
        tool = self.get(name)
        if tool is None:
            return f"Error: Tool '{name}' is not registered."

        return execute_with_permission(
            tool=tool,
            args=args,
            kwargs=kwargs,
            is_admin=is_admin,
            prompt_fn=prompt_fn,
        )


def create_default_registry() -> ToolRegistry:
    """Create and pre-populate the registry with all audited Desktop tools."""
    reg = ToolRegistry()

    # System tools (all SAFE, read-only)
    reg.register(Tool("get_time", "Get the current time.", RiskLevel.SAFE, get_time, "system"))
    reg.register(Tool("get_date", "Get today's date.", RiskLevel.SAFE, get_date, "system"))
    reg.register(Tool("get_cpu_usage", "Get current CPU usage percentage.", RiskLevel.SAFE, get_cpu_usage, "system"))
    reg.register(Tool("get_ram_usage", "Get current RAM usage percentage.", RiskLevel.SAFE, get_ram_usage, "system"))
    reg.register(Tool("get_disk_usage", "Get disk space usage.", RiskLevel.SAFE, get_disk_usage, "system"))
    reg.register(Tool("get_battery", "Get battery level and charging status.", RiskLevel.SAFE, get_battery, "system"))
    reg.register(Tool("is_running_as_admin", "Check if running with administrator rights.", RiskLevel.SAFE, is_running_as_admin, "system"))

    # File tools
    reg.register(Tool("list_files", "List files in a directory.", RiskLevel.SAFE, list_files, "files"))
    reg.register(Tool("read_file", "Read text contents of a file.", RiskLevel.SAFE, read_file, "files"))
    reg.register(Tool("edit_file", "Perform exact-match text replacement in a file.", RiskLevel.CONFIRMATION_REQUIRED, edit_file, "files"))
    reg.register(Tool("find_file", "Search for files matching name.", RiskLevel.SAFE, find_file, "files"))
    reg.register(Tool("find_folder", "Search for folders matching name.", RiskLevel.SAFE, find_folder, "files"))

    # Process tools
    reg.register(Tool("list_processes", "List running processes with PID and name.", RiskLevel.SAFE, list_processes, "processes"))
    reg.register(Tool("is_running", "Check if a specific process is currently running.", RiskLevel.SAFE, is_running, "processes"))
    reg.register(Tool("close_process", "Terminate running processes matching name.", RiskLevel.CONFIRMATION_REQUIRED, close_process, "processes"))

    # Application tools
    reg.register(Tool("open_app", "Launch a desktop application by name.", RiskLevel.CONFIRMATION_REQUIRED, open_app, "apps"))
    reg.register(Tool("open_folder", "Open a folder in Windows Explorer.", RiskLevel.CONFIRMATION_REQUIRED, open_folder, "apps"))

    # Planning tools
    reg.register(Tool("todo_write", "Update or replace the structured task checklist for multi-step goals.", RiskLevel.SAFE, todo_write, "planning"))

    # Command tools
    reg.register(Tool("run_command", "Execute a read-only Git command under strict containment.", RiskLevel.CONFIRMATION_REQUIRED, run_command, "command"))

    return reg


# Default pre-populated instance
registry = create_default_registry()

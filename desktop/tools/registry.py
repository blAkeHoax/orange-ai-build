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
    get_clipboard,
    set_clipboard,
)
from tools.files import (
    list_files,
    read_file,
    edit_file,
    find_file,
    find_folder,
    write_file,
    append_file,
    delete_file,
    move_path,
    create_directory,
)
from tools.processes import (
    list_processes,
    is_running,
    close_process,
    manage_process,
)
from tools.apps import (
    open_app,
    open_folder,
    launch_app,
)
from tools.todo import (
    todo_write,
)
from tools.command import (
    run_command,
)
from tools.shell import (
    run_powershell,
)
from tools.skills import (
    run_skill,
)


@dataclass
class Tool:
    """Represents a registered tool with its metadata and callable."""
    name: str
    description: str
    risk_level: RiskLevel
    func: Callable
    category: str = "general"
    parameters: Optional[Dict[str, Any]] = None

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
            "parameters": self.parameters or {},
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
        full_access_mode: Optional[str] = None,
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
            full_access_mode=full_access_mode,
        )


def create_default_registry() -> ToolRegistry:
    """Create and pre-populate the registry with all audited Desktop tools."""
    reg = ToolRegistry()

    # System tools (SAFE, read-only)
    reg.register(Tool("get_time", "Get the current time.", RiskLevel.SAFE, get_time, "system", {
        "type": "object", "properties": {}, "required": []
    }))
    reg.register(Tool("get_date", "Get today's date.", RiskLevel.SAFE, get_date, "system", {
        "type": "object", "properties": {}, "required": []
    }))
    reg.register(Tool("get_cpu_usage", "Get current CPU usage percentage.", RiskLevel.SAFE, get_cpu_usage, "system", {
        "type": "object", "properties": {}, "required": []
    }))
    reg.register(Tool("get_ram_usage", "Get current RAM usage percentage.", RiskLevel.SAFE, get_ram_usage, "system", {
        "type": "object", "properties": {}, "required": []
    }))
    reg.register(Tool("get_disk_usage", "Get disk space usage.", RiskLevel.SAFE, get_disk_usage, "system", {
        "type": "object", "properties": {}, "required": []
    }))
    reg.register(Tool("get_battery", "Get battery level and charging status.", RiskLevel.SAFE, get_battery, "system", {
        "type": "object", "properties": {}, "required": []
    }))
    reg.register(Tool("is_running_as_admin", "Check if running with administrator rights.", RiskLevel.SAFE, is_running_as_admin, "system", {
        "type": "object", "properties": {}, "required": []
    }))
    reg.register(Tool("get_clipboard", "Read text contents from the Windows clipboard.", RiskLevel.SAFE, get_clipboard, "system", {
        "type": "object", "properties": {}, "required": []
    }))
    reg.register(Tool("set_clipboard", "Set text content onto the Windows clipboard.", RiskLevel.CONFIRMATION_REQUIRED, set_clipboard, "system", {
        "type": "object",
        "properties": {"text": {"type": "string", "description": "Text to place on clipboard."}},
        "required": ["text"]
    }))

    # File tools
    reg.register(Tool("list_files", "List files in a directory.", RiskLevel.SAFE, list_files, "files", {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Directory path to list (default: current directory)."}},
        "required": []
    }))
    reg.register(Tool("read_file", "Read text contents of a file.", RiskLevel.SAFE, read_file, "files", {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Path of file to read."}},
        "required": ["path"]
    }))
    reg.register(Tool("edit_file", "Perform exact-match text replacement in a file.", RiskLevel.CONFIRMATION_REQUIRED, edit_file, "files", {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Target file path."},
            "old_string": {"type": "string", "description": "Existing text to replace."},
            "new_string": {"type": "string", "description": "Replacement text."}
        },
        "required": ["path", "old_string", "new_string"]
    }))
    reg.register(Tool("find_file", "Search for files matching name.", RiskLevel.SAFE, find_file, "files", {
        "type": "object",
        "properties": {"filename": {"type": "string", "description": "Filename pattern to search."}},
        "required": ["filename"]
    }))
    reg.register(Tool("find_folder", "Search for folders matching name.", RiskLevel.SAFE, find_folder, "files", {
        "type": "object",
        "properties": {"folder_name": {"type": "string", "description": "Folder name to search."}},
        "required": ["folder_name"]
    }))
    reg.register(Tool("write_file", "Create or overwrite a file with text content across any local drive.", RiskLevel.CONFIRMATION_REQUIRED, write_file, "files", {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Destination file path."},
            "content": {"type": "string", "description": "File text content."}
        },
        "required": ["path", "content"]
    }))
    reg.register(Tool("append_file", "Append text content to a file across any local drive.", RiskLevel.CONFIRMATION_REQUIRED, append_file, "files", {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Target file path."},
            "content": {"type": "string", "description": "Text content to append."}
        },
        "required": ["path", "content"]
    }))
    reg.register(Tool("delete_file", "Delete a file or directory across any local drive.", RiskLevel.CONFIRMATION_REQUIRED, delete_file, "files", {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "File or directory path to delete."}},
        "required": ["path"]
    }))
    reg.register(Tool("move_path", "Move or rename a file or directory from src to dst.", RiskLevel.CONFIRMATION_REQUIRED, move_path, "files", {
        "type": "object",
        "properties": {
            "src": {"type": "string", "description": "Source path."},
            "dst": {"type": "string", "description": "Destination path."}
        },
        "required": ["src", "dst"]
    }))
    reg.register(Tool("create_directory", "Create a directory and any parent directories across any local drive.", RiskLevel.CONFIRMATION_REQUIRED, create_directory, "files", {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Directory path to create."}},
        "required": ["path"]
    }))

    # Process tools
    reg.register(Tool("list_processes", "List running processes with PID and name.", RiskLevel.SAFE, list_processes, "processes", {
        "type": "object", "properties": {}, "required": []
    }))
    reg.register(Tool("is_running", "Check if a specific process is currently running.", RiskLevel.SAFE, is_running, "processes", {
        "type": "object",
        "properties": {"process_name": {"type": "string", "description": "Process name to check."}},
        "required": ["process_name"]
    }))
    reg.register(Tool("close_process", "Terminate running processes matching name.", RiskLevel.CONFIRMATION_REQUIRED, close_process, "processes", {
        "type": "object",
        "properties": {"process_name": {"type": "string", "description": "Process name to terminate."}},
        "required": ["process_name"]
    }))
    reg.register(Tool("manage_process", "Inspect, list, or terminate system processes by name or PID.", RiskLevel.CONFIRMATION_REQUIRED, manage_process, "processes", {
        "type": "object",
        "properties": {
            "action": {"type": "string", "description": "Action: 'status', 'list', 'kill', 'terminate'."},
            "name_or_pid": {"type": "string", "description": "Process name or PID."}
        },
        "required": ["action", "name_or_pid"]
    }))

    # Application tools
    reg.register(Tool("open_app", "Launch a desktop application by name.", RiskLevel.CONFIRMATION_REQUIRED, open_app, "apps", {
        "type": "object",
        "properties": {"app_name": {"type": "string", "description": "Application name to open."}},
        "required": ["app_name"]
    }))
    reg.register(Tool("open_folder", "Open a folder in Windows Explorer.", RiskLevel.CONFIRMATION_REQUIRED, open_folder, "apps", {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Folder path to open in Explorer."}},
        "required": ["path"]
    }))
    reg.register(Tool("launch_app", "Launch an application or executable with optional arguments across all local drives.", RiskLevel.CONFIRMATION_REQUIRED, launch_app, "apps", {
        "type": "object",
        "properties": {
            "target": {"type": "string", "description": "Application or executable path."},
            "args": {"type": "string", "description": "Optional arguments."}
        },
        "required": ["target"]
    }))

    # Planning tools
    reg.register(Tool("todo_write", "Update or replace the structured task checklist for multi-step goals.", RiskLevel.SAFE, todo_write, "planning", {
        "type": "object",
        "properties": {"checklist": {"type": "array", "description": "Task items list."}},
        "required": ["checklist"]
    }))

    # Command tools
    reg.register(Tool("run_command", "Execute a read-only Git command under strict containment.", RiskLevel.CONFIRMATION_REQUIRED, run_command, "command", {
        "type": "object",
        "properties": {
            "executable": {"type": "string", "description": "Path to executable (git.exe)."},
            "args": {"type": "array", "description": "Argument array."},
            "cwd": {"type": "string", "description": "Optional working directory."},
            "timeout_seconds": {"type": "integer", "description": "Timeout in seconds."}
        },
        "required": ["executable", "args"]
    }))
    reg.register(Tool("run_powershell", "Execute a PowerShell command with captured stdout, stderr, and exit_code.", RiskLevel.CONFIRMATION_REQUIRED, run_powershell, "command", {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "PowerShell command or script to execute."},
            "cwd": {"type": "string", "description": "Optional working directory."},
            "timeout": {"type": "integer", "description": "Timeout in seconds (default: 60)."}
        },
        "required": ["command"]
    }))

    # Skills tools
    reg.register(Tool("run_skill", "Execute an Orange Skill by slug.", RiskLevel.SAFE, run_skill, "skills", {
        "type": "object",
        "properties": {
            "slug": {"type": "string", "description": "The unique identifier of the skill to execute (e.g. 'repo-health-audit')."},
            "input": {"type": "string", "description": "Optional input text to provide to the skill."}
        },
        "required": ["slug"]
    }))

    return reg


# Default pre-populated instance
registry = create_default_registry()

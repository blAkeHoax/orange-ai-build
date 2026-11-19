"""
brain/agent.py - Controlled Agent Loop for Orange AI Desktop.

Controls the execution lifecycle of multi-step agent tasks:
Goal -> Context -> Plan -> Action -> Validate -> Permission -> Execute -> Observe -> Evaluate -> Next/Done

Adheres strictly to:
- The LLM proposes actions; the Agent Engine controls the lifecycle.
- Zero arbitrary code or shell execution.
- Tool actions pass strictly through ToolRegistry and the Phase 3 permission system.
- WAITING_CONFIRMATION pauses execution; resumes the same logical run on approval; cancels cleanly on rejection.
- Configurable MAX_AGENT_STEPS and MAX_RETRIES limits.
"""

import json
import re
import uuid
import inspect
import time
from typing import Optional, Dict, Any, List, Callable, Tuple

from brain.agent_models import (
    AgentState,
    AgentStatus,
    AgentAction,
    ActionType,
    ToolResult,
    LLMDecision,
    TaskItem,
    TaskStatus,
    normalize_tool_result,
)
from brain.agent_context import build_agent_context, format_decision_prompt
from brain.api_client import (
    OrangeAPIClient,
    OrangeAPIError,
    OrangeAPIConnectionError,
    OrangeAPIResponseError,
)
from brain.logger import logger
from brain import planner
from tools.registry import registry
from tools.permissions import check_permission, PermissionStatus, RiskLevel


MAX_AGENT_STEPS = 10
MAX_RETRIES = 2


def extract_json_decision(reply_text: str) -> Dict[str, Any]:
    """
    Robustly extract and parse a JSON object from model output.
    Handles markdown code fences, embedded JSON in prose, and extra whitespace.
    """
    text = (reply_text or "").strip()
    if not text:
        raise ValueError("Model returned empty response.")

    # 1. If wrapped in markdown code fence, try extracting fenced content
    if "```" in text:
        match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
        if match:
            fenced = match.group(1).strip()
            try:
                data = json.loads(fenced)
                if isinstance(data, dict):
                    return data
            except json.JSONDecodeError:
                pass

    # 2. Try direct json.loads on entire text
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    # 3. Fallback: extract outermost bracketed JSON object {...}
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = text[start : end + 1]
        data = json.loads(candidate)
        if isinstance(data, dict):
            return data

    raise ValueError("Could not find a valid JSON object in model response.")


class AgentEngine:
    """
    Controlled Agent Loop Engine.
    Executes agent tasks step-by-step with permission gates and in-memory run resumption.
    """

    def __init__(
        self,
        api_client: Optional[OrangeAPIClient] = None,
        max_steps: int = MAX_AGENT_STEPS,
        max_retries: int = MAX_RETRIES,
    ):
        self.api_client = api_client or OrangeAPIClient()
        self.max_steps = max_steps
        self.max_retries = max_retries
        self._active_runs: Dict[str, AgentState] = {}
        self._last_query_error: Optional[str] = None
        # DEF-04: Explicit observation callback for persistent job integration.
        # When set, called AFTER _record_observation with (state, action, result).
        # Avoids monkey-patching _record_observation on the shared engine instance.
        self._observation_callback: Optional[Callable] = None

    def get_run(self, run_id: str) -> Optional[AgentState]:
        """Retrieve an active or completed run state by run_id."""
        return self._active_runs.get(run_id)

    def start_run(
        self,
        goal: str,
        conversation_history: Optional[List[Dict[str, str]]] = None,
        observation_callback: Optional[Callable] = None,
    ) -> AgentState:
        """
        Initialize and begin a new controlled agent run.
        """
        clean_goal = goal.strip()
        run_id = uuid.uuid4().hex[:12]

        logger.info("[AGENT %s] Started run for goal: '%s'", run_id, clean_goal)

        # 1. Provisional plan using existing brain/planner.py without modifying it
        provisional_plan = None
        try:
            provisional_plan = planner.create_plan(clean_goal)
        except Exception as e:
            logger.warning("[AGENT %s] Provisional planner notice: %s", run_id, e)

        # 2. Create initial AgentState
        state = AgentState(
            run_id=run_id,
            goal=clean_goal,
            original_request=clean_goal,
            status=AgentStatus.RUNNING,
            plan=provisional_plan,
            step_count=0,
            retry_count=0,
            observations=[],
            completed_steps=[],
        )
        self._active_runs[run_id] = state

        # 3. Enter the controlled loop
        return self._step_loop(state, conversation_history, observation_callback=observation_callback)

    def resume_run(
        self,
        run_id: str,
        user_response: str,
        conversation_history: Optional[List[Dict[str, str]]] = None,
        observation_callback: Optional[Callable] = None,
    ) -> AgentState:
        """
        Resume an existing in-memory run that was paused for confirmation or user input.
        """
        state = self._active_runs.get(run_id)
        if not state:
            logger.error("[AGENT %s] Attempted to resume non-existent run.", run_id)
            return AgentState(
                run_id=run_id,
                goal="",
                original_request="",
                status=AgentStatus.FAILED,
                final_response=f"Error: No active agent run found with ID '{run_id}'.",
            )

        # Case A: Resuming from WAITING_CONFIRMATION
        if state.status == AgentStatus.WAITING_CONFIRMATION:
            pending_action = state.pending_confirmation
            clean_resp = user_response.strip().lower()

            logger.info("[AGENT %s] Confirmation response received: '%s'", run_id, clean_resp)

            if clean_resp in ("y", "yes"):
                # Approved -> Execute the pending tool action
                state.pending_confirmation = None
                state.status = AgentStatus.RUNNING

                tool_result = self._execute_tool_action(pending_action, is_confirmed=True, state=state)
                self._record_observation(state, pending_action, tool_result, observation_callback=observation_callback)

                # Continue the loop
                return self._step_loop(state, conversation_history, observation_callback=observation_callback)
            else:
                # Declined -> Correction 2: Record rejection, do not execute, do not re-plan, cancel run
                state.pending_confirmation = None
                state.status = AgentStatus.CANCELLED
                tool_name = pending_action.tool_name if pending_action else "action"
                decline_msg = f"Agent run cancelled: user declined confirmation to execute '{tool_name}'."
                state.final_response = decline_msg
                logger.info("[AGENT %s] %s", run_id, decline_msg)
                return state

        # Case B: Resuming from WAITING_USER (answered a clarification question)
        if state.status == AgentStatus.WAITING_USER:
            state.waiting_for_user = False
            state.status = AgentStatus.RUNNING
            state.observations.append({
                "type": "USER_ANSWER",
                "question": state.last_action.question if state.last_action else "",
                "answer": user_response.strip(),
            })
            return self._step_loop(state, conversation_history, observation_callback=observation_callback)

        return state

    def _step_loop(
        self,
        state: AgentState,
        conversation_history: Optional[List[Dict[str, str]]] = None,
        observation_callback: Optional[Callable] = None,
    ) -> AgentState:
        """
        The inner controlled agent loop.
        Executes one action per iteration until done, paused, or a limit is hit.
        """
        while state.status == AgentStatus.RUNNING:
            # 1. Step Limit Check
            if state.step_count >= self.max_steps:
                state.status = AgentStatus.MAX_STEPS_REACHED
                state.final_response = (
                    f"Agent stopped: maximum step limit ({self.max_steps}) reached."
                )
                logger.warning("[AGENT %s] %s", state.run_id, state.final_response)
                break

            # 2. Build targeted AgentContext
            context = build_agent_context(
                goal=state.goal,
                original_request=state.original_request,
                state=state,
                conversation_history=conversation_history,
            )
            state.context = context

            # 3. Format Provisional Plan summary if available
            plan_str = None
            if state.plan:
                plan_str = planner.format_plan(state.plan)

            prompt = format_decision_prompt(context, provisional_plan=plan_str)

            # 4. Obtain structured decision from LLM via Orange API
            decision = self._query_llm_decision(prompt)
            if not decision:
                # LLM query failure / malformed JSON / API error
                state.retry_count += 1
                if state.retry_count >= self.max_retries:
                    state.status = AgentStatus.MAX_RETRIES_REACHED
                    state.final_response = (
                        f"Agent stopped: maximum retries ({self.max_retries}) reached after model errors."
                    )
                    break
                err_detail = self._last_query_error or "Failed to parse model response into a valid AgentAction JSON."
                state.observations.append({
                    "type": "ERROR",
                    "error": err_detail,
                })
                # If the error was an API communication error, brief pause before retrying
                if "Orange API communication error" in err_detail:
                    time.sleep(1.0)
                continue

            action = decision.action
            state.last_action = action
            state.step_count += 1

            logger.info(
                "[AGENT %s] Step %d: Proposed Action '%s' (Tool: %s) - Reason: %s",
                state.run_id,
                state.step_count,
                action.type.value,
                action.tool_name,
                action.reasoning_summary,
            )

            # 5. Process Action Type
            if action.type == ActionType.FINISH:
                state.status = AgentStatus.COMPLETED
                state.final_response = action.response or "Task completed successfully."
                logger.info("[AGENT %s] Completed: %s", state.run_id, state.final_response)
                break

            if action.type == ActionType.RESPOND:
                state.status = AgentStatus.COMPLETED
                state.final_response = action.response or action.reasoning_summary
                logger.info("[AGENT %s] Responded: %s", state.run_id, state.final_response)
                break

            if action.type == ActionType.ASK_USER:
                state.status = AgentStatus.WAITING_USER
                state.waiting_for_user = True
                state.final_response = action.question or "Could you clarify your request?"
                logger.info("[AGENT %s] Paused for user question: %s", state.run_id, state.final_response)
                break

            if action.type == ActionType.CALL_TOOL:
                # Validate tool name
                if not action.tool_name or not registry.contains(action.tool_name):
                    logger.warning("[AGENT %s] Unknown tool requested: '%s'", state.run_id, action.tool_name)
                    state.retry_count += 1
                    if state.retry_count >= self.max_retries:
                        state.status = AgentStatus.MAX_RETRIES_REACHED
                        state.final_response = f"Agent stopped: unknown tool '{action.tool_name}' requested repeatedly."
                        break

                    state.observations.append({
                        "action": action.to_dict(),
                        "result": {
                            "success": False,
                            "tool_name": action.tool_name or "none",
                            "error": f"Tool '{action.tool_name}' does not exist in ToolRegistry.",
                        },
                    })
                    continue

                tool = registry.get(action.tool_name)

                # Permission evaluation
                perm = check_permission(tool)
                logger.info("[AGENT %s] Tool '%s' permission status: %s", state.run_id, action.tool_name, perm.status.value)

                if perm.status == PermissionStatus.DENIED:
                    # High risk or denied action -> Record blocked observation and stop safely
                    state.status = AgentStatus.FAILED
                    state.final_response = f"Action blocked: Tool '{action.tool_name}' is not permitted."
                    logger.warning("[AGENT %s] %s", state.run_id, state.final_response)
                    break

                if perm.status == PermissionStatus.REQUIRES_CONFIRMATION:
                    # Pause for confirmation
                    state.status = AgentStatus.WAITING_CONFIRMATION
                    state.pending_confirmation = action
                    state.final_response = (
                        f"Orange AI requests permission to execute '{action.tool_name}'."
                    )
                    logger.info("[AGENT %s] Paused in WAITING_CONFIRMATION for tool '%s'", state.run_id, action.tool_name)
                    break

                # SAFE -> Execute single tool directly
                tool_result = self._execute_tool_action(action, is_confirmed=False, state=state)
                self._record_observation(state, action, tool_result, observation_callback=observation_callback)

                # Evaluate observation progress
                self._evaluate_step(state, action, tool_result)

        return state

    def _execute_tool_action(
        self,
        action: AgentAction,
        is_confirmed: bool = False,
        state: Optional[AgentState] = None,
    ) -> ToolResult:
        """
        Safely execute a single tool action through ToolRegistry.
        Binds arguments to tool parameters gracefully.
        """
        tool = registry.get(action.tool_name)
        if not tool:
            return ToolResult(
                success=False,
                tool_name=action.tool_name or "unknown",
                data=None,
                error=f"Tool '{action.tool_name}' not found in registry.",
            )

        # Normalize arguments
        raw_args = action.arguments or {}
        bound_args = []
        bound_kwargs = {}

        sig = inspect.signature(tool.func)
        params = list(sig.parameters.values())

        if not params:
            # 0 parameters expected
            pass
        elif len(params) == 1 and not isinstance(raw_args, dict):
            # Single argument passed directly
            bound_args.append(raw_args)
        elif isinstance(raw_args, dict):
            # Match dictionary keys to parameter names
            param_names = [p.name for p in params]
            for p in params:
                if p.name in raw_args:
                    bound_kwargs[p.name] = raw_args[p.name]
                elif len(raw_args) == 1 and p.name != "state":
                    # Map single dict value to parameter if key differed slightly
                    val = next(iter(raw_args.values()))
                    bound_kwargs[p.name] = val
        elif isinstance(raw_args, (list, tuple)):
            bound_args.extend(raw_args)

        # Explicit context injection for state-aware tools (e.g. todo_write)
        if "state" in [p.name for p in params] and state is not None:
            bound_kwargs["state"] = state

        try:
            # If confirmation was already approved, prompt_fn returns 'y'
            prompt_fn = (lambda _: "y") if is_confirmed else None
            raw_result = registry.execute(
                action.tool_name,
                *bound_args,
                prompt_fn=prompt_fn,
                **bound_kwargs,
            )
            tool_result = normalize_tool_result(action.tool_name, raw_result)

            # Defensive verification: ensure active AgentState reflects successfully updated checklist
            if action.tool_name == "todo_write" and tool_result.success and state is not None:
                if isinstance(tool_result.data, dict) and "checklist" in tool_result.data:
                    state.checklist = [
                        TaskItem.from_dict(t) for t in tool_result.data["checklist"]
                    ]

            return tool_result
        except Exception as e:
            logger.error("[AGENT] Tool '%s' execution threw exception: %s", action.tool_name, e)
            return ToolResult(
                success=False,
                tool_name=action.tool_name,
                data=None,
                error=f"{type(e).__name__}: {str(e)}",
            )

    def _record_observation(
        self,
        state: AgentState,
        action: AgentAction,
        result: ToolResult,
        observation_callback: Optional[Callable] = None,
    ) -> None:
        """Record an executed tool observation in the agent run state."""
        state.last_result = result
        obs_entry = {
            "step": state.step_count,
            "action": action.to_dict(),
            "result": result.to_dict(),
        }
        state.observations.append(obs_entry)

        if result.success:
            state.completed_steps.append(f"{action.tool_name}: {str(result.data)[:60]}")
            state.retry_count = 0  # Reset retry counter on success
        else:
            state.retry_count += 1

        # DEF-04: Invoke observation callback if registered.
        # Run-specific callback takes precedence over engine-level callback.
        cb = observation_callback or self._observation_callback
        if cb is not None:
            cb(state, action, result)

    def _evaluate_step(self, state: AgentState, action: AgentAction, result: ToolResult) -> None:
        """
        Evaluate tool output to determine progress and dynamic re-planning.
        """
        # If tool execution failed repeatedly, limit enforcement handles it
        if not result.success:
            logger.warning("[AGENT %s] Step failed: %s", state.run_id, result.error)
            return

        # Re-planning check: If checking process and it is already running, or checking file
        # and it is already found, record that in completed state.
        if action.tool_name == "is_running" and result.data:
            if result.data.get("result") is True or "true" in str(result.data).lower():
                logger.info("[AGENT %s] Re-planning insight: Target process is already active.", state.run_id)

    def _query_llm_decision(self, prompt: str) -> Optional[LLMDecision]:
        """
        Send prompt to Orange API /assist and extract structured JSON decision.
        """
        self._last_query_error = None
        try:
            api_resp = self.api_client.assist(
                message=prompt,
                system_instruction=(
                    "You are the reasoning component for an autonomous controlled desktop agent. "
                    "You MUST output valid JSON only. Do not include markdown code fences."
                ),
                task_type="agent_decision",
            )
            reply_text = api_resp.get("reply", "").strip()
            data = extract_json_decision(reply_text)

            action_data = data.get("action", {})
            if not isinstance(action_data, dict):
                if "type" in data:
                    action_data = data
                else:
                    self._last_query_error = "Model response JSON missing 'action' dictionary."
                    return None

            action = AgentAction.from_dict(action_data)
            summary = data.get("summary", action.reasoning_summary)

            return LLMDecision(action=action, summary=summary)
        except (OrangeAPIConnectionError, OrangeAPIResponseError, OrangeAPIError) as e:
            logger.warning("[AGENT] Orange API error during LLM decision: %s", e)
            self._last_query_error = f"Orange API communication error: {e}"
            return None
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning("[AGENT] LLM decision parsing error: %s", e)
            self._last_query_error = f"Failed to parse model response into a valid AgentAction JSON: {e}"
            return None
        except Exception as e:
            logger.warning("[AGENT] Unexpected error during LLM decision: %s", e)
            self._last_query_error = f"Unexpected error during LLM decision: {e}"
            return None

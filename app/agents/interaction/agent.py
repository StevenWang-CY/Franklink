"""Interaction Agent - conductor using Task + Tool architecture.

This agent:
1. Decides if it can handle a request directly or needs to delegate
2. Assigns tasks to ExecutionAgent(s) to fulfill user requests
3. Runs an evaluation loop to ensure requests are fully addressed
4. Synthesizes ALL user-facing responses using Frank's persona

IMPORTANT: The Interaction Agent is the ONLY component that generates
user-facing text. Execution agents return structured data only.
"""

import asyncio
import json
import logging
import re
from typing import Any, Dict, List, Optional

from app.agents.base import BaseAgent
from app.agents.execution.agent import GenericExecutionAgent
from app.agents.execution.state import ExecutionResult
from app.agents.interaction.state import TaskExecutionState, IterationContext
from app.agents.memory.task_history import TaskHistorySaver
from app.agents.tasks.base import Task
from app.agents.tasks.networking import NetworkingTask
from app.agents.tasks.onboarding import OnboardingTask
from app.agents.tasks.update import UpdateTask
from app.agents.tasks.onboarding_handler import handle_onboarding_message
from app.agents.interaction.prompts.base_persona import (
    build_synthesis_prompt,
    build_completeness_prompt,
    build_direct_handling_prompt,
    build_direct_response_prompt,
    build_reassignment_prompt,
)
from app.models.state import create_initial_graph_state
from app.integrations.azure_openai_client import AzureOpenAIClient
from app.utils.json_utils import strip_code_fences, parse_llm_json

logger = logging.getLogger(__name__)


# Allowed fields in task instructions to prevent injection
ALLOWED_TASK_INSTRUCTION_FIELDS = {
    # Common fields
    "case",
    "instruction",
    "intent",
    # Networking-specific
    "request_id",
    "request_ids",
    "confirmed_purposes",  # Purposes selected from suggestions
    "target_name",
    "initiator_name",
    "accept",
    "match_type_preference",  # "one_person" | "multiple_people"
    "selected_purpose",  # Single purpose selected from suggestions
    # Update-specific
    "op",
    "field",
    "value",
    "values",
}


def _sanitize_task_instruction(instruction: Dict[str, Any]) -> Dict[str, Any]:
    """Sanitize task instruction to only include allowed fields.

    This prevents potential injection attacks where user input might leak
    into task instructions and be interpreted by the ExecutionAgent.

    Args:
        instruction: Raw task instruction dict from LLM

    Returns:
        Sanitized instruction with only allowed fields
    """
    if not isinstance(instruction, dict):
        return {}

    sanitized = {}
    for key, value in instruction.items():
        if key in ALLOWED_TASK_INSTRUCTION_FIELDS:
            # Also sanitize string values to prevent prompt injection
            if isinstance(value, str):
                # Remove potential prompt injection patterns
                sanitized[key] = value.replace("{{", "").replace("}}", "")
            elif isinstance(value, list):
                # Sanitize list items
                sanitized[key] = [
                    item.replace("{{", "").replace("}}", "") if isinstance(item, str) else item
                    for item in value
                ]
            else:
                sanitized[key] = value
        else:
            logger.warning(f"[INTERACTION] Stripped unknown field from task_instruction: {key}")

    return sanitized


def _sanitize_all_task_instructions(instructions: Dict[str, Any]) -> Dict[str, Any]:
    """Sanitize all task instructions in a dict.

    Args:
        instructions: Dict of task_name -> instruction

    Returns:
        Sanitized instructions dict
    """
    if not isinstance(instructions, dict):
        return {}

    return {
        task_key: _sanitize_task_instruction(instr)
        for task_key, instr in instructions.items()
        if isinstance(instr, dict)
    }


def _build_error_response(
    error: str,
    response_text: str,
    intent: str = "error",
    status: str = "failed",
) -> Dict[str, Any]:
    """Build a consistent error response dict.

    Ensures all error responses have the same structure for predictable handling.

    Args:
        error: The error message/description
        response_text: User-facing response text
        intent: The intent classification (default: "error")
        status: The status string (default: "failed")

    Returns:
        Consistent error response dict
    """
    return {
        "success": False,
        "error": error,
        "response_text": response_text,
        "intent": intent,
        "status": status,
    }


class InteractionAgentNew(BaseAgent):
    """Interaction Agent using Task + Tool architecture with evaluation loop.

    Responsibilities:
    1. Decide if request can be handled directly or needs delegation
    2. Assign tasks to ExecutionAgent(s) to fulfill user requests
    3. Run evaluation loop to ensure request is fully addressed
    4. Synthesize ALL user-facing responses with Frank's persona

    IMPORTANT: This is the ONLY component that talks to users.
    Execution agents return structured data, this agent synthesizes responses.
    """

    # Maximum iterations for the interaction-level loop
    MAX_INTERACTION_ITERATIONS = 2

    def __init__(self, db, photon, openai=None):
        """Initialize the Interaction Agent.

        Args:
            db: DatabaseClient instance
            photon: PhotonClient instance
            openai: Optional AzureOpenAIClient
        """
        super().__init__(agent_type="interaction", db=db, openai=openai)
        self.photon = photon
        self.openai = openai or AzureOpenAIClient()

        # Initialize components
        self.execution_agent = GenericExecutionAgent(db=db, openai=self.openai)
        self.task_history = TaskHistorySaver(db)

        # Task registry
        self.tasks = {
            "networking": NetworkingTask,
            "onboarding": OnboardingTask,
            "update": UpdateTask,
        }

        logger.info("[INTERACTION] Initialized with %d tasks", len(self.tasks))

    async def process_message(
        self,
        phone_number: str,
        message_content: str,
        user: Dict[str, Any],
        webhook_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Process an incoming message.

        Args:
            phone_number: User's phone number
            message_content: Message text
            user: User profile from database
            webhook_data: Additional webhook data

        Returns:
            Response dictionary with synthesized user-facing text
        """
        logger.info(
            "[INTERACTION] Processing: %s: %s",
            phone_number,
            message_content[:50] + "..." if len(message_content) > 50 else message_content,
        )

        # Build state
        state = create_initial_graph_state(
            phone_number=phone_number,
            user_id=user["id"],
            message_content=message_content,
            media_url=webhook_data.get("media_url"),
            chat_guid=webhook_data.get("chat_guid"),
            message_id=webhook_data.get("message_id"),
        )
        self._populate_user_profile(state, user)

        # Load all context data in parallel for better performance
        conversation_history, recent_tasks, active_connection = await asyncio.gather(
            self._load_conversation_history(user["id"]),
            self.task_history.get_recent_tasks(user["id"], limit=3),
            self._load_active_connection_context(user["id"]),
        )
        state["conversation_history"] = conversation_history
        state["recent_task_context"] = self.task_history.format_for_context(recent_tasks)
        state["active_connection"] = active_connection

        try:
            # Check if onboarding is required - use streamlined handler
            if self._requires_onboarding(state):
                return await self._handle_onboarding(state, message_content)

            # Decide if we can handle directly or need to delegate
            decision = await self._should_handle_directly(state)

            if decision.get("can_handle_directly"):
                # Handle directly without ExecutionAgent
                return await self._handle_direct_response(state, message_content, decision)
            else:
                # Delegate to ExecutionAgent(s) to handle the request
                # Supports multiple tasks: ["networking", "update"]
                tasks = decision.get("tasks", [])
                if not tasks:
                    # No tasks identified, handle directly
                    return await self._handle_direct_response(state, message_content, decision)
                # Store task_instructions in state for ExecutionAgent to use
                # SECURITY: Sanitize to prevent injection attacks
                raw_instructions = decision.get("task_instructions", {})
                state["task_instructions"] = _sanitize_all_task_instructions(raw_instructions)
                logger.info(f"[INTERACTION] task_instructions: {state['task_instructions']}")
                return await self._interaction_loop(state, message_content, tasks)

        except Exception as e:
            logger.error(f"[INTERACTION] Error: {e}", exc_info=True)
            # Synthesize an error response
            error_response = await self._synthesize_error_response(state, str(e))
            return _build_error_response(
                error=str(e),
                response_text=error_response,
            )

    async def _interaction_loop(
        self,
        state: Dict[str, Any],
        message: str,
        tasks: List[str],
    ) -> Dict[str, Any]:
        """Run the interaction-level evaluation loop with parallel execution.

        This loop:
        1. Executes tasks in parallel using asyncio.gather
        2. Tracks which tasks are complete vs need more work
        3. Evaluates if re-assignment is needed (max 2 iterations)
        4. Aggregates waiting signals for combined user asks
        5. Synthesizes a user-facing response at the end

        Args:
            state: Current state dictionary
            message: User's message
            tasks: List of task names to execute

        Returns:
            Response dictionary with synthesized user-facing text
        """
        # Send immediate acknowledgment for networking tasks
        # This gives the user instant feedback while we process (3-5+ seconds)
        await self._send_early_acknowledgment_if_needed(state, tasks)

        iteration = 0
        accumulated_results: List[Dict[str, Any]] = []
        current_task_states: Dict[str, TaskExecutionState] = {}
        tasks_to_run = tasks.copy()

        while iteration < self.MAX_INTERACTION_ITERATIONS and tasks_to_run:
            iteration += 1
            logger.info(
                f"[INTERACTION] Loop iteration {iteration}/{self.MAX_INTERACTION_ITERATIONS}, "
                f"tasks: {tasks_to_run}"
            )

            # Execute tasks in parallel
            new_states = await self._execute_tasks_parallel(tasks_to_run, state)
            current_task_states.update(new_states)

            # Build iteration context
            ctx = IterationContext(iteration=iteration, task_states=current_task_states)

            # Accumulate results for synthesis and save task history
            for task_name, task_state in new_states.items():
                if task_state.result:
                    accumulated_results.append({
                        "task": task_name,
                        "result": task_state.result,
                        "task_name": task_name,
                    })
                    # Save task state for future context
                    await self._save_task_history(
                        state=state,
                        task_key=task_name,
                        task_state=task_state,
                    )

            # CASE 1: All tasks complete
            if ctx.all_complete:
                logger.info("[INTERACTION] All tasks completed")
                break

            # CASE 2: Any task waiting for user input - stop and ask
            if ctx.any_waiting:
                logger.info("[INTERACTION] Task waiting for user input, breaking loop")
                break

            # CASE 3: Any failed - decide on retry
            if ctx.any_failed:
                if iteration < self.MAX_INTERACTION_ITERATIONS:
                    reassignment = await self._evaluate_for_reassignment(
                        message=message,
                        iteration_context=ctx,
                        state=state,
                    )

                    if reassignment.get("should_continue"):
                        tasks_to_run = reassignment.get("tasks_to_rerun", [])
                        new_tasks = reassignment.get("new_tasks", [])
                        tasks_to_run.extend(new_tasks)

                        if tasks_to_run:
                            logger.info(f"[INTERACTION] Re-assignment: {tasks_to_run}")
                            continue

                # No retry - synthesize failure response
                response_text = await self._synthesize_response(
                    message=message,
                    results=accumulated_results,
                    state=state,
                    status="failed",
                )
                return self._build_final_response(
                    response_text=response_text,
                    result=accumulated_results[-1]["result"] if accumulated_results else None,
                    tasks=tasks,
                    status="failed",
                )

            # CASE 4: Incomplete but not waiting/failed - evaluate completeness
            evaluation = await self._evaluate_completeness(
                message=message,
                results=accumulated_results,
                state=state,
            )

            if evaluation.get("is_complete", True):
                logger.info("[INTERACTION] Request fully addressed")
                break

            # Not complete - evaluate for re-assignment
            if iteration < self.MAX_INTERACTION_ITERATIONS:
                reassignment = await self._evaluate_for_reassignment(
                    message=message,
                    iteration_context=ctx,
                    state=state,
                )

                if reassignment.get("should_continue"):
                    tasks_to_run = reassignment.get("tasks_to_rerun", [])
                    tasks_to_run.extend(reassignment.get("new_tasks", []))

                    if not tasks_to_run:
                        # Nothing more to do
                        break
                else:
                    break
            else:
                break

        # Determine final status - check if any task is waiting for user
        final_status = "complete"
        if ctx.any_waiting:
            final_status = "waiting"

        # Synthesize final response
        response_text = await self._synthesize_response(
            message=message,
            results=accumulated_results,
            state=state,
            status=final_status,
        )

        last_result = accumulated_results[-1]["result"] if accumulated_results else None
        return self._build_final_response(
            response_text=response_text,
            result=last_result,
            tasks=tasks,
            status=final_status,
        )

    async def _execute_tasks_parallel(
        self,
        task_names: List[str],
        state: Dict[str, Any],
    ) -> Dict[str, TaskExecutionState]:
        """Execute multiple tasks in parallel using asyncio.gather.

        Supports compound requests with indexed task instructions:
        - Single task: task_instructions["networking"]
        - Multiple same-type tasks: task_instructions["networking_0"], ["networking_1"], etc.

        Args:
            task_names: List of task names to execute (may have duplicates for compound requests)
            state: Current state dictionary

        Returns:
            Dictionary mapping task_key -> TaskExecutionState
            (task_key is "networking_0", "networking_1" for duplicates, or just "networking" for single)
        """
        # Count occurrences to detect compound requests
        task_counts: Dict[str, int] = {}
        for name in task_names:
            task_counts[name] = task_counts.get(name, 0) + 1

        # Build list of (task_key, task_name) pairs
        # task_key is used for instruction lookup and result tracking
        # task_name is the actual task type for execution
        task_pairs: List[tuple] = []
        seen_counts: Dict[str, int] = {}

        for name in task_names:
            if task_counts[name] > 1:
                # Multiple of same type - use indexed key
                idx = seen_counts.get(name, 0)
                task_key = f"{name}_{idx}"
                seen_counts[name] = idx + 1
            else:
                # Single task - use task name directly
                task_key = name
            task_pairs.append((task_key, name))

        async def execute_single(task_key: str, task_name: str) -> TaskExecutionState:
            task = self._get_task(task_name)
            if not task:
                logger.warning(f"[INTERACTION] No task found: {task_name}")
                return TaskExecutionState(
                    task_name=task_key,
                    status="failed",
                )

            try:
                result = await self._execute_task(task, state, task_key=task_key)
                return TaskExecutionState(
                    task_name=task_key,
                    status=result.status,
                    result=result,
                    waiting_for=result.waiting_for,
                )
            except Exception as e:
                logger.error(f"[INTERACTION] Task {task_key} execution error: {e}")
                return TaskExecutionState(
                    task_name=task_key,
                    status="failed",
                )

        # Execute all tasks in parallel
        results = await asyncio.gather(
            *[execute_single(key, name) for key, name in task_pairs],
            return_exceptions=True,
        )

        # Build result dictionary, handling exceptions
        task_states = {}
        for (task_key, _task_name), result in zip(task_pairs, results):
            if isinstance(result, Exception):
                logger.error(f"[INTERACTION] Task {task_key} raised exception: {result}")
                task_states[task_key] = TaskExecutionState(
                    task_name=task_key,
                    status="failed",
                )
            else:
                task_states[task_key] = result

        return task_states

    async def _evaluate_for_reassignment(
        self,
        message: str,
        iteration_context: IterationContext,
        state: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Evaluate incomplete results and decide on re-assignment.

        Args:
            message: User's original message
            iteration_context: Current iteration state
            state: Current state dictionary

        Returns:
            Dict with tasks_to_rerun, new_tasks, reasoning, should_continue
        """
        prompt = build_reassignment_prompt(
            user_message=message,
            iteration_context=iteration_context,
            state=state,
            max_iterations=self.MAX_INTERACTION_ITERATIONS,
        )

        try:
            response = await self.openai.generate_response(
                messages=[{"role": "user", "content": prompt}],
                model="gpt-4o-mini",
                temperature=0.1,
                max_tokens=300,
                trace_label="reassignment_eval",
            )

            # Parse JSON response
            cleaned = self._clean_json_response(response)
            return json.loads(cleaned)

        except Exception as e:
            logger.warning(f"[INTERACTION] Re-assignment evaluation failed: {e}")
            # Default: don't continue if evaluation fails
            return {
                "tasks_to_rerun": [],
                "new_tasks": [],
                "reasoning": "Evaluation failed",
                "should_continue": False,
            }

    def _clean_json_response(self, response: str) -> str:
        """Clean LLM response to extract JSON.

        Uses the shared utility function for consistency.
        """
        return strip_code_fences(response)

    async def _evaluate_completeness(
        self,
        message: str,
        results: List[Dict[str, Any]],
        state: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Use LLM to evaluate if user's request is fully addressed.

        Args:
            message: User's original message
            results: List of execution results
            state: Current state

        Returns:
            Evaluation dict with is_complete, reasoning, missing_elements
        """
        user_profile = state.get("user_profile", {})
        conversation_history = state.get("conversation_history", [])
        task_name = results[-1].get("task", results[-1].get("task_name", "unknown")) if results else "unknown"

        prompt = build_completeness_prompt(
            user_message=message,
            execution_results=results,
            user_profile=user_profile,
            intent=task_name,  # TODO: rename intent param in build_completeness_prompt
            conversation_history=conversation_history,
        )

        try:
            response = await self.openai.generate_response(
                messages=[{"role": "user", "content": prompt}],
                model="gpt-4o-mini",
                temperature=0.1,
                max_tokens=200,
                trace_label="completeness_eval",
            )

            # Parse JSON response using utility
            return parse_llm_json(
                response,
                default={"is_complete": True, "reasoning": "Parse failed, assuming complete"},
                context="completeness evaluation",
            )

        except Exception as e:
            logger.warning(f"[INTERACTION] Completeness evaluation failed: {e}")
            # Default to complete if evaluation fails
            return {"is_complete": True, "reasoning": "Evaluation failed, assuming complete"}

    async def _synthesize_response(
        self,
        message: str,
        results: List[Dict[str, Any]],
        state: Dict[str, Any],
        status: str,
    ) -> str:
        """Use LLM to generate user-facing response with Frank's persona.

        This is the ONLY place where user-facing text is generated.

        Args:
            message: User's original message
            results: List of execution results
            state: Current state
            status: Current status (complete, waiting, failed)

        Returns:
            Synthesized response text
        """
        user_profile = state.get("user_profile", {})
        conversation_history = state.get("conversation_history", [])

        # Build actions summary from results
        actions_summary = self._build_actions_summary(results)

        # Aggregate data from all results
        relevant_data = self._aggregate_data(results)

        # Extract waiting_for from results (if any task is waiting)
        waiting_for = None
        for r in results:
            exec_result = r.get("result")
            if exec_result and hasattr(exec_result, "waiting_for") and exec_result.waiting_for:
                waiting_for = exec_result.waiting_for
                break

        prompt = build_synthesis_prompt(
            user_message=message,
            actions_summary=actions_summary,
            relevant_data=relevant_data,
            user_profile=user_profile,
            status=status,
            conversation_history=conversation_history,
            waiting_for=waiting_for,
        )

        try:
            response = await self.openai.generate_response(
                messages=[{"role": "user", "content": prompt}],
                model="gpt-4o-mini",
                temperature=0.7,
                max_tokens=300,
                trace_label="response_synthesis",
            )

            return response.strip()

        except Exception as e:
            logger.error(f"[INTERACTION] Response synthesis failed: {e}")
            # Fallback response
            return "hey, something went wrong on my end. can you try that again"

    async def _synthesize_error_response(
        self,
        state: Dict[str, Any],
        error: str,
    ) -> str:
        """Synthesize a user-friendly error response.

        Args:
            state: Current state
            error: Error message

        Returns:
            User-friendly error response
        """
        # Simple fallback without LLM call
        return "sorry, i hit a snag processing that. mind trying again"

    def _build_actions_summary(self, results: List[Dict[str, Any]]) -> str:
        """Build human-readable summary of all actions taken.

        Args:
            results: List of execution results

        Returns:
            Formatted actions summary
        """
        if not results:
            return "No actions taken."

        summaries = []
        for r in results:
            task_name = r.get("task", r.get("task_name", "unknown"))
            exec_result = r.get("result")
            if exec_result:
                action_summary = exec_result.summarize_actions()
                summaries.append(f"[{task_name}]:\n{action_summary}")

        return "\n\n".join(summaries) if summaries else "No actions taken."

    def _aggregate_data(self, results: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Aggregate data collected from all execution results.

        Args:
            results: List of execution results

        Returns:
            Aggregated data dictionary
        """
        aggregated = {}
        for r in results:
            exec_result = r.get("result")
            if exec_result:
                # Get data from new structured fields
                data_collected = getattr(exec_result, "data_collected", {}) or {}
                state_changes = getattr(exec_result, "state_changes", {}) or {}
                aggregated.update(data_collected)
                aggregated.update(state_changes)

                # CRITICAL: Flatten nested tool results from state_changes
                # state_changes stores results as {"tool_name": {data...}}
                # but we need the data at top-level for _extract_match_names
                # This ensures all matches are surfaced even if LLM doesn't copy them correctly
                for key, value in state_changes.items():
                    if isinstance(value, dict):
                        # This is a nested tool result - merge important fields to top level
                        # Only merge if the key doesn't already exist (prefer LLM-specified data)
                        if "matches" in value and "matches" not in aggregated:
                            aggregated["matches"] = value["matches"]
                        if "request_ids" in value and "request_ids" not in aggregated:
                            aggregated["request_ids"] = value["request_ids"]
                        if "target_name" in value and "target_name" not in aggregated:
                            aggregated["target_name"] = value["target_name"]
                        if "connection_request_id" in value and "connection_request_id" not in aggregated:
                            aggregated["connection_request_id"] = value["connection_request_id"]

                # Backward compatibility: also check old result field
                if exec_result.result:
                    # Filter out deprecated response_text
                    for key, value in exec_result.result.items():
                        if key != "response_text":
                            aggregated[key] = value

        # CRITICAL: Extract and prominently display match_names for synthesis
        # This helps the response synthesis LLM use ONLY the correct names
        match_names = self._extract_match_names(aggregated)
        if match_names:
            aggregated["match_names"] = match_names
            aggregated["CRITICAL_match_names_USE_ONLY_THESE"] = match_names

        return aggregated

    def _extract_match_names(self, data: Dict[str, Any]) -> List[str]:
        """Extract all match names from aggregated data.

        Looks for match names in various data structures:
        - matches array (from find_multi_matches)
        - target_name (from find_match)
        - sent_to (from confirm_and_send_invitation)
        - Nested tool results (e.g., data["find_multi_matches"]["matches"])

        Args:
            data: Aggregated data dictionary

        Returns:
            List of match names found
        """
        names = []

        # From multi-match results (top-level)
        matches = data.get("matches", [])
        if isinstance(matches, list):
            for m in matches:
                if isinstance(m, dict) and m.get("target_name"):
                    names.append(m["target_name"])

        # From single match result (top-level)
        target_name = data.get("target_name")
        if target_name and target_name not in names:
            names.append(target_name)

        # From confirmation results
        sent_to = data.get("sent_to_names", [])
        if isinstance(sent_to, list):
            for name in sent_to:
                if name and name not in names:
                    names.append(name)

        # CRITICAL: Also check nested tool results
        # state_changes stores results as {"tool_name": {data...}}
        # LLM may not correctly copy all matches to top-level data
        for key in ("find_multi_matches", "find_match"):
            nested = data.get(key)
            if isinstance(nested, dict):
                # Check for matches array in nested result
                nested_matches = nested.get("matches", [])
                if isinstance(nested_matches, list):
                    for m in nested_matches:
                        if isinstance(m, dict) and m.get("target_name"):
                            name = m["target_name"]
                            if name not in names:
                                names.append(name)
                # Check for single target_name
                nested_target = nested.get("target_name")
                if nested_target and nested_target not in names:
                    names.append(nested_target)

        return names

    def _build_final_response(
        self,
        response_text: str,
        result: Optional[ExecutionResult],
        tasks: List[str],
        status: str,
    ) -> Dict[str, Any]:
        """Build the final response dictionary.

        Args:
            response_text: Synthesized response text
            result: Last execution result
            tasks: List of tasks executed
            status: Final status

        Returns:
            Response dictionary
        """
        return {
            "success": status != "failed",
            "response_text": response_text,
            "task": tasks[-1] if tasks else None,
            "status": status,
            "error": result.error if result else None,
        }

    async def execute(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """Execute for BaseAgent compatibility."""
        phone_number = state.get("user_profile", {}).get("phone_number", "")
        message = state.get("current_message", {}).get("content", "")
        user = state.get("user_profile", {})
        webhook_data = {
            "chat_guid": state.get("current_message", {}).get("chat_guid"),
            "message_id": state.get("current_message", {}).get("message_id"),
        }

        result = await self.process_message(
            phone_number=phone_number,
            message_content=message,
            user=user,
            webhook_data=webhook_data,
        )

        # Update state with result
        state["response"] = {"response_text": result.get("response_text")}
        return state

    async def _load_conversation_history(
        self,
        user_id: str,
        limit: int = 10,
    ) -> List[Dict[str, str]]:
        """Load recent conversation history for the user.

        Args:
            user_id: User's UUID
            limit: Maximum number of messages to load

        Returns:
            List of message dicts with 'role' and 'content' keys
        """
        try:
            messages = await self.db.get_recent_messages(user_id, limit=limit)
            logger.debug(f"[INTERACTION] Loaded {len(messages)} messages from history")
            return messages
        except Exception as e:
            logger.warning(f"[INTERACTION] Failed to load conversation history: {e}")
            return []

    async def _load_active_connection_context(
        self,
        user_id: str,
    ) -> Dict[str, Any]:
        """Load active connection context for routing decisions.

        This helps the routing LLM reliably detect:
        - CASE B: User is initiator responding to a pending match suggestion
        - CASE C: User is target responding to an incoming invitation
        - CASE D: User asking about recent connections (provides context)

        Returns lists of all pending requests so the LLM can disambiguate
        which one the user is responding to based on conversation context.

        Args:
            user_id: User's UUID

        Returns:
            Dict with lists of pending requests and recent completed connections
        """
        result = {
            "pending_as_initiator": [],  # List of pending match suggestions
            "pending_as_target": [],     # List of pending invitations
            "recent_connections": [],
        }

        try:
            # Fetch all request lists in parallel
            initiator_requests, target_requests, connections = await asyncio.gather(
                self.db.list_pending_requests_for_initiator(user_id, limit=5),
                self.db.list_pending_requests_for_target(user_id, limit=5),
                self.db.get_user_connections(user_id, limit=3),
            )

            # Collect all user IDs we need to look up (batch to avoid N+1 queries)
            user_ids_to_fetch = set()
            for req in initiator_requests:
                if req.get("target_user_id"):
                    user_ids_to_fetch.add(req.get("target_user_id"))
            for req in target_requests:
                if req.get("initiator_user_id"):
                    user_ids_to_fetch.add(req.get("initiator_user_id"))

            # Batch fetch all users in parallel
            users_by_id = {}
            if user_ids_to_fetch:
                fetched_users = await asyncio.gather(
                    *[self.db.get_user_by_id(uid) for uid in user_ids_to_fetch]
                )
                users_by_id = {
                    u["id"]: u for u in fetched_users if u and u.get("id")
                }

            # Build initiator requests using cached user lookups
            for req in initiator_requests:
                target_user = users_by_id.get(req.get("target_user_id"), {})
                result["pending_as_initiator"].append({
                    "request_id": req.get("id"),
                    "status": req.get("status"),
                    "target_name": target_user.get("name", "Unknown"),
                    "target_school": target_user.get("university", ""),
                    "match_reason": ", ".join(req.get("matching_reasons", [])[:2]),
                })

            if result["pending_as_initiator"]:
                logger.debug(
                    f"[INTERACTION] Found {len(result['pending_as_initiator'])} pending initiator requests"
                )

            # Build target requests using cached user lookups
            for req in target_requests:
                initiator_user = users_by_id.get(req.get("initiator_user_id"), {})
                result["pending_as_target"].append({
                    "request_id": req.get("id"),
                    "status": req.get("status"),
                    "initiator_name": initiator_user.get("name", "Unknown"),
                    "initiator_school": initiator_user.get("university", ""),
                    "match_reason": ", ".join(req.get("matching_reasons", [])[:2]),
                })

            if result["pending_as_target"]:
                logger.debug(
                    f"[INTERACTION] Found {len(result['pending_as_target'])} pending target requests"
                )

            # Build recent connections list
            for conn in connections:
                result["recent_connections"].append({
                    "connected_with_name": conn.get("connected_with_name", "Unknown"),
                    "match_reason": ", ".join(conn.get("matching_reasons", [])[:2]),
                })

            if result["recent_connections"]:
                logger.debug(
                    f"[INTERACTION] Found {len(result['recent_connections'])} recent connections"
                )

        except Exception as e:
            logger.warning(f"[INTERACTION] Failed to load active connection context: {e}")

        return result

    async def _should_handle_directly(
        self,
        state: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Decide if InteractionAgent should respond directly without ExecutionAgent.

        Args:
            state: Current state with user_profile, conversation_history, current_message

        Returns:
            Decision dict: {can_handle_directly: bool, reasoning: str, tasks: list}
        """
        message = state.get("current_message", {}).get("content", "")
        user_profile = state.get("user_profile", {})
        conversation_history = state.get("conversation_history", [])
        recent_task_context = state.get("recent_task_context", "")
        active_connection = state.get("active_connection", {})

        prompt = build_direct_handling_prompt(
            user_message=message,
            user_profile=user_profile,
            conversation_history=conversation_history,
            recent_task_context=recent_task_context,
            active_connection=active_connection,
        )

        try:
            response = await self.openai.generate_response(
                messages=[{"role": "user", "content": prompt}],
                model="gpt-4o-mini",
                temperature=0.1,
                max_tokens=150,
                trace_label="direct_handling_decision",
            )

            # Parse JSON response with robust extraction
            cleaned = response.strip()

            # Remove markdown code blocks more thoroughly
            if "```" in cleaned:
                # Find content between ``` markers
                parts = cleaned.split("```")
                for part in parts:
                    part = part.strip()
                    if part.startswith("json"):
                        part = part[4:].strip()
                    if part.startswith("{"):
                        cleaned = part
                        break

            # Extract JSON object from any surrounding text
            start = cleaned.find("{")
            end = cleaned.rfind("}")
            if start != -1 and end != -1 and end > start:
                cleaned = cleaned[start:end + 1]

            # Try to fix common JSON formatting issues
            # Remove trailing commas before } or ]
            cleaned = re.sub(r',(\s*[}\]])', r'\1', cleaned)

            return json.loads(cleaned)

        except Exception as e:
            logger.warning(f"[INTERACTION] Direct handling decision failed: {e}")
            # Default to handling directly when decision fails
            return {"can_handle_directly": True, "reasoning": "decision failed, defaulting to direct"}

    async def _send_early_acknowledgment_if_needed(
        self,
        state: Dict[str, Any],
        tasks: List[str],
    ) -> None:
        """Send an immediate acknowledgment for long-running tasks.

        This gives users instant feedback (within ~100ms) while we process
        networking operations that can take 3-5+ seconds.

        The acknowledgment is only sent for:
        - Networking tasks (vague or specific matching)
        - New networking requests (not continuation of previous flow)

        Args:
            state: Current state dictionary
            tasks: List of task names to execute
        """
        # Only send acknowledgment for networking tasks
        if "networking" not in tasks:
            return

        # Note: We used to check for "waiting_for" in task history to avoid
        # double-acking, but this was unreliable. The early ack is cheap and
        # provides good UX for all networking requests, so we now always send it.
        # If the user is continuing a flow (e.g., selecting from options), the
        # LLM will classify and handle appropriately.

        try:
            # phone_number can be in multiple places depending on state structure
            phone_number = (
                state.get("phone_number")
                or state.get("user_profile", {}).get("phone_number")
            )
            user_id = (
                state.get("user_id")
                or state.get("user_profile", {}).get("user_id")
            )

            if not phone_number:
                logger.debug("[INTERACTION] No phone_number in state, skipping early acknowledgment")
                return

            # Choose stage-specific acknowledgment based on task_instructions
            acknowledgment = self._get_networking_acknowledgment(state)

            await self.photon.send_message(
                to_number=phone_number,
                content=acknowledgment,
            )

            # Store the acknowledgment in conversation history
            if user_id:
                await self.db.store_message(
                    user_id=user_id,
                    content=acknowledgment,
                    message_type="bot",
                    metadata={"type": "early_acknowledgment", "tasks": tasks},
                )

            logger.info(
                f"[INTERACTION] Sent early acknowledgment for networking task to {phone_number}"
            )

        except Exception as e:
            # Don't fail the request if acknowledgment fails
            logger.warning(f"[INTERACTION] Failed to send early acknowledgment: {e}")

    def _get_networking_acknowledgment(self, state: Dict[str, Any]) -> str:
        """Get stage-specific acknowledgment message for networking tasks.

        Provides contextual feedback to users based on what stage of the
        networking flow they're in.

        Args:
            state: Current state containing task_instructions

        Returns:
            Acknowledgment message appropriate for the current stage
        """
        task_instructions = state.get("task_instructions", {})
        networking_inst = task_instructions.get("networking", {})

        # Get key fields to determine the stage
        case = networking_inst.get("case", "").upper()
        instruction = networking_inst.get("instruction", "").lower()
        request_id = networking_inst.get("request_id")
        request_ids = networking_inst.get("request_ids")
        confirmed_purposes = networking_inst.get("confirmed_purposes")
        selected_purpose = networking_inst.get("selected_purpose")
        accept = networking_inst.get("accept")

        # CASE B: Initiator confirming match(es)
        if case == "B":
            if request_ids or (request_id and "multi" in instruction):
                # Multi-match confirmation - sending multiple invites
                return "sending the invites..."
            elif "confirm" in instruction or "yes" in instruction:
                return "sending the invite..."
            elif "cancel" in instruction or "no" in instruction:
                return "got it, canceling..."
            return "on it..."

        # CASE C: Target responding to invitation
        if case == "C":
            if accept is True or "accept" in instruction:
                return "nice, connecting you two..."
            elif accept is False or "decline" in instruction:
                return "got it..."
            return "on it..."

        # CASE D: Inquiry about connections
        if case == "D":
            return "let me check..."

        # CASE A: New networking request (various sub-flows)
        if case == "A":
            # Purpose confirmation step (user confirmed purpose suggestions)
            if confirmed_purposes:
                return "got it, finding matches..."

            # Purpose selected (user chose from Zep suggestions)
            if selected_purpose:
                return "got it, searching..."

            # Check instruction for clues about the request type (email-based or vague)
            if "email" in instruction or "inbox" in instruction:
                return "looking at your activity..."

            # Vague request patterns
            vague_patterns = [
                "connect someone",
                "find me someone",
                "find someone",
                "help me network",
                "find connections",
                "meet someone",
                "connect with someone",
                "want to connect",
                "wants to connect",
            ]
            if any(pattern in instruction for pattern in vague_patterns):
                return "let me see what might be relevant..."

            # Specific request - searching for matches
            return "searching..."

        # Default fallback
        return "on it, one sec..."

    async def _handle_direct_response(
        self,
        state: Dict[str, Any],
        message: str,
        decision: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Generate a direct response without ExecutionAgent.

        Args:
            state: Current state
            message: User's message
            decision: The decision dict from _should_handle_directly

        Returns:
            Response dictionary
        """
        user_profile = state.get("user_profile", {})
        conversation_history = state.get("conversation_history", [])

        prompt = build_direct_response_prompt(
            user_message=message,
            user_profile=user_profile,
            conversation_history=conversation_history,
        )

        try:
            response_text = await self.openai.generate_response(
                messages=[{"role": "user", "content": prompt}],
                model="gpt-4o-mini",
                temperature=0.7,
                max_tokens=300,
                trace_label="direct_response",
            )

            return {
                "success": True,
                "response_text": response_text.strip(),
                "intent": "direct",
                "status": "complete",
                "handled_directly": True,
            }

        except Exception as e:
            logger.error(f"[INTERACTION] Direct response generation failed: {e}")
            return _build_error_response(
                error=str(e),
                response_text="hey, something went wrong on my end. can you try that again",
            )

    def _requires_onboarding(self, state: Dict[str, Any]) -> bool:
        """Check if user needs onboarding."""
        profile = state.get("user_profile", {})

        # Already onboarded
        if profile.get("is_onboarded"):
            return False

        # Check stage
        stage = profile.get("onboarding_stage", "name")
        if stage == "complete":
            return False
        if stage == "rejected":
            return False

        return True

    async def _handle_onboarding(self, state: Dict[str, Any], message_content: str) -> Dict[str, Any]:
        """Handle onboarding using streamlined handler (bypasses ReAct loop).

        This reduces LLM calls from 4-5 to just 2 per message.
        Onboarding has its own response generation, so we don't synthesize here.
        """
        user_profile = state.get("user_profile", {})
        current_message = state.get("current_message", {})

        # Get conversation history from memory if available
        conversation_history = state.get("conversation_history", [])

        # Call streamlined handler
        result = await handle_onboarding_message(
            message=message_content,
            user_profile=user_profile,
            conversation_history=conversation_history,
            current_message=current_message,
        )

        # Build response in expected format
        responses = []
        if result.get("response_text"):
            responses.append({
                "response_text": result["response_text"],
                "intent": "onboarding",
                "task": "onboarding",
            })

        # Add additional messages as separate response items
        for msg in result.get("additional_messages", []):
            responses.append({
                "response_text": msg,
                "intent": "onboarding",
                "task": "onboarding",
            })

        return {
            "success": result.get("success", True),
            "responses": responses if responses else None,
            "response_text": result.get("response_text"),
            "intent": "onboarding",
            "state": state,
            "waiting_for": result.get("waiting_for"),
        }

    def _get_task(self, task_name: str) -> Optional[Task]:
        """Get task by name."""
        return self.tasks.get(task_name)

    async def _execute_task(
        self, task: Task, state: Dict[str, Any], task_key: str = ""
    ) -> ExecutionResult:
        """Execute a task with the execution agent.

        Args:
            task: The Task to execute
            state: Current state dictionary
            task_key: Key for looking up task instructions (may be indexed like "networking_0")

        Returns:
            ExecutionResult from the execution agent
        """
        context = self._build_task_context(state, task_key or task.name)
        return await self.execution_agent.execute_task(task, context)

    def _build_task_context(self, state: Dict[str, Any], task_key: str = "") -> Dict[str, Any]:
        """Build context dictionary for task execution.

        Args:
            state: Current state with user_profile, task_instructions, etc.
            task_key: Key for looking up task-specific instructions.
                      For compound requests, this may be indexed (e.g., "networking_0", "networking_1").
                      Falls back to base task name if indexed key not found.

        Returns:
            Context dictionary for ExecutionAgent
        """
        # Get task-specific instruction if available
        task_instructions = state.get("task_instructions", {})

        # Try indexed key first (e.g., "networking_0"), fall back to base name (e.g., "networking")
        task_instruction = task_instructions.get(task_key, {})
        if not task_instruction and "_" in task_key:
            # Fall back to base task name without index
            base_name = task_key.rsplit("_", 1)[0]
            task_instruction = task_instructions.get(base_name, {})

        return {
            "user_profile": state.get("user_profile", {}),
            "task_instruction": task_instruction,  # Structured instruction from InteractionAgent
            "message_id": state.get("current_message", {}).get("message_id"),
            "chat_guid": state.get("current_message", {}).get("chat_guid"),
            "from_number": state.get("current_message", {}).get("from_number"),
        }

    async def _save_task_history(
        self,
        state: Dict[str, Any],
        task_key: str,
        task_state: TaskExecutionState,
    ) -> None:
        """Save completed task state for future context.

        Args:
            state: Current state with user_profile, task_instructions
            task_key: Key of the task (may be indexed like "networking_0" for compound requests)
            task_state: The completed task execution state
        """
        user_id = state.get("user_profile", {}).get("user_id")
        if not user_id:
            return

        result = task_state.result
        if not result:
            return

        # Extract base task name for storage (e.g., "networking_0" -> "networking")
        if "_" in task_key and task_key.rsplit("_", 1)[1].isdigit():
            base_task_name = task_key.rsplit("_", 1)[0]
        else:
            base_task_name = task_key

        # Get the instruction that was executed
        # Support indexed keys (e.g., "networking_0") with fallback to base name
        task_instructions = state.get("task_instructions", {})
        task_instruction = task_instructions.get(task_key, {})
        if not task_instruction:
            task_instruction = task_instructions.get(base_task_name, {})
        instruction = task_instruction.get("instruction", "")

        # Build concise outcome from result
        outcome = result.data_collected.get("summary", "") if result.data_collected else ""
        if not outcome and result.error:
            outcome = f"Error: {result.error[:100]}"
        elif not outcome:
            outcome = "Completed" if result.status == "complete" else "Failed"

        # Extract key data points (keep it small)
        key_data = {}

        # CRITICAL: Include waiting_for for flow continuation
        # This allows the LLM to recognize when user is responding to a prompt
        if result.waiting_for:
            key_data["waiting_for"] = result.waiting_for

        if result.data_collected:
            # Only include important fields, limit size
            # CRITICAL: Include request_id/request_ids for connection confirmation routing
            # CRITICAL: Include signals for CASE A email signal flow (signal selection step)
            # CRITICAL: Include suggestions for CASE A purpose selection flow
            # CRITICAL: Include selected_purpose for match_type_preference flow continuation
            for key in [
                "match_found",
                "match_name",
                "field_updated",
                "clarification_question",
                "request_id",
                "request_ids",
                "connection_request_id",
                "signals",  # For CASE A email signal flow - user needs to select from these
                "suggestions",  # For CASE A purpose selection flow - user picks from these
                "selected_purpose",  # For match_type_preference flow - preserve the purpose
            ]:
                if key in result.data_collected:
                    key_data[key] = result.data_collected[key]

            # Also extract from nested match_details if present
            match_details = result.data_collected.get("match_details", {})
            if match_details:
                if match_details.get("request_id") and "request_id" not in key_data:
                    key_data["request_id"] = match_details["request_id"]
                if match_details.get("target_name"):
                    key_data["match_name"] = match_details["target_name"]

            # Extract from matches array for multi-match scenarios
            matches = result.data_collected.get("matches", [])
            if matches and isinstance(matches, list):
                # Collect all target names for context
                match_names = [m.get("target_name") for m in matches if m.get("target_name")]
                if match_names:
                    key_data["match_names"] = match_names

        await self.task_history.save_task(
            user_id=user_id,
            task_name=base_task_name,
            instruction=instruction,
            outcome=outcome,
            status=result.status,
            key_data=key_data,
        )

    def _populate_user_profile(self, state: Dict[str, Any], user: Dict[str, Any]) -> None:
        """Fill state.user_profile from DB user record."""
        profile = state.get("user_profile", {})
        profile["phone_number"] = user.get("phone_number", "")
        profile["user_id"] = user.get("id", "")
        profile["name"] = user.get("name")
        profile["email"] = user.get("email")
        profile["linkedin_url"] = user.get("linkedin_url")
        profile["demand_history"] = user.get("demand_history", []) or []
        profile["value_history"] = user.get("value_history", []) or []
        profile["latest_demand"] = user.get("latest_demand")
        profile["all_demand"] = user.get("all_demand")
        profile["all_value"] = user.get("all_value")
        profile["intro_fee_cents"] = user.get("intro_fee_cents")
        profile["university"] = user.get("university")
        profile["major"] = user.get("major")
        profile["year"] = user.get("year")
        profile["career_interests"] = user.get("career_interests", [])
        profile["needs"] = user.get("needs", [])
        profile["career_goals"] = user.get("career_goals")
        profile["personal_facts"] = user.get("personal_facts", {}) or {}
        profile["networking_clarification"] = user.get("networking_clarification")
        profile["networking_limitation"] = user.get("networking_limitation")
        profile["is_onboarded"] = user.get("is_onboarded", False)

        # Determine onboarding stage
        pf = profile["personal_facts"]
        pf_stage = pf.get("onboarding_stage") if isinstance(pf, dict) else None
        valid_stages = {
            "value_eval", "needs_eval", "rejected", "name", "school",
            "career_interest", "email_connect", "share_to_complete", "complete"
        }

        if profile["is_onboarded"]:
            profile["onboarding_stage"] = "complete"
        elif pf_stage in valid_stages:
            profile["onboarding_stage"] = pf_stage
        elif not profile.get("name"):
            profile["onboarding_stage"] = "name"
        elif not profile.get("university"):
            profile["onboarding_stage"] = "school"
        elif not profile.get("career_interests"):
            profile["onboarding_stage"] = "career_interest"
        else:
            email_state = pf.get("email_connect", {}) if isinstance(pf, dict) else {}
            email_status = str(email_state.get("status", "")).lower() if isinstance(email_state, dict) else ""
            need_state = pf.get("frank_need_eval", {}) if isinstance(pf, dict) else {}
            need_status = str(need_state.get("status", "")).lower() if isinstance(need_state, dict) else ""
            value_state = pf.get("frank_value_eval", {}) if isinstance(pf, dict) else {}
            value_has_turns = bool(value_state.get("asked_questions")) if isinstance(value_state, dict) else False

            if value_has_turns or need_status == "accepted":
                profile["onboarding_stage"] = "value_eval"
            elif need_state.get("asked_questions") or email_status == "connected":
                profile["onboarding_stage"] = "needs_eval"
            else:
                profile["onboarding_stage"] = "email_connect"

        state["user_profile"] = profile

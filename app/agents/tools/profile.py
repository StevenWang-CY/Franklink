"""Profile management tools for demand/value history and profile field updates.

These tools handle:
- Managing demand history (what users want)
- Managing value history (what users offer)
- Computing derived fields and embeddings
- Updating user profile fields (name, school, year, major, career_interests)
"""

import logging
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from app.agents.tools.base import tool, ToolResult
from app.database.client import DatabaseClient
from app.utils.demand_value_history import normalize_history

logger = logging.getLogger(__name__)


# =============================================================================
# Error Codes for Structured Error Handling
# =============================================================================


class UpdateErrorCode(str, Enum):
    """Standardized error codes for update operations."""

    INVALID_INDEX = "INVALID_INDEX"
    EMPTY_HISTORY = "EMPTY_HISTORY"
    INVALID_OPERATION = "INVALID_OPERATION"
    INVALID_MODE = "INVALID_MODE"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    DB_ERROR = "DB_ERROR"
    HISTORY_CHANGED = "HISTORY_CHANGED"
    DELETE_NOT_EXPLICIT = "DELETE_NOT_EXPLICIT"
    INVALID_FIELD = "INVALID_FIELD"


# =============================================================================
# Field Validation Rules
# =============================================================================


VALIDATION_RULES = {
    "year": {"type": int, "min": 2000, "max": 2100},
    "name": {"type": str, "min_length": 1, "max_length": 100},
    "university": {"type": str, "min_length": 1, "max_length": 200},
    "major": {"type": str, "min_length": 1, "max_length": 200},
}

ALLOWED_FIELDS = {"name", "university", "year", "major", "career_interests"}


def validate_field(field: str, value: Any) -> tuple[bool, Optional[str]]:
    """Validate a profile field value.

    Args:
        field: Field name
        value: Value to validate

    Returns:
        Tuple of (is_valid, error_message)
    """
    if field not in ALLOWED_FIELDS:
        return False, f"Invalid field: {field}. Allowed: {ALLOWED_FIELDS}"

    rules = VALIDATION_RULES.get(field)
    if not rules:
        return True, None

    expected_type = rules.get("type")
    if expected_type and not isinstance(value, expected_type):
        return False, f"{field} must be {expected_type.__name__}, got {type(value).__name__}"

    if expected_type == int:
        min_val = rules.get("min")
        max_val = rules.get("max")
        if min_val is not None and value < min_val:
            return False, f"{field} must be >= {min_val}"
        if max_val is not None and value > max_val:
            return False, f"{field} must be <= {max_val}"

    if expected_type == str:
        min_len = rules.get("min_length", 0)
        max_len = rules.get("max_length")
        if len(value.strip()) < min_len:
            return False, f"{field} must have at least {min_len} character(s)"
        if max_len and len(value) > max_len:
            return False, f"{field} must be <= {max_len} characters"

    return True, None


@tool(
    name="append_demand_history",
    description="Add a new entry to user's demand history. "
    "Demand represents what the user is looking for.",
)
async def append_demand_history(
    user_id: str,
    demand_text: str,
) -> ToolResult:
    """Append to user's demand history.

    Args:
        user_id: User's ID
        demand_text: The demand text to append

    Returns:
        ToolResult indicating success
    """
    try:
        from datetime import datetime

        db = DatabaseClient()

        await db.append_demand_value_history(
            user_id=user_id,
            demand_update=demand_text,
            value_update=None,
            created_at=datetime.utcnow().isoformat(),
        )

        return ToolResult(
            success=True,
            data={
                "appended": True,
                "demand": demand_text,
            },
        )

    except Exception as e:
        logger.error(f"[PROFILE] append_demand_history failed: {e}", exc_info=True)
        return ToolResult(
            success=False,
            error=f"Failed to append demand: {str(e)}",
        )


@tool(
    name="append_value_history",
    description="Add a new entry to user's value history. "
    "Value represents what the user can offer to others.",
)
async def append_value_history(
    user_id: str,
    value_text: str,
) -> ToolResult:
    """Append to user's value history.

    Args:
        user_id: User's ID
        value_text: The value text to append

    Returns:
        ToolResult indicating success
    """
    try:
        from datetime import datetime

        db = DatabaseClient()

        await db.append_demand_value_history(
            user_id=user_id,
            demand_update=None,
            value_update=value_text,
            created_at=datetime.utcnow().isoformat(),
        )

        return ToolResult(
            success=True,
            data={
                "appended": True,
                "value": value_text,
            },
        )

    except Exception as e:
        logger.error(f"[PROFILE] append_value_history failed: {e}", exc_info=True)
        return ToolResult(
            success=False,
            error=f"Failed to append value: {str(e)}",
        )


@tool(
    name="update_derived_fields",
    description="Recompute derived fields (latest_demand, all_demand, all_value) and refresh embeddings.",
)
async def update_derived_fields(user_id: str) -> ToolResult:
    """Update derived fields and embeddings.

    Args:
        user_id: User's ID

    Returns:
        ToolResult indicating success
    """
    try:
        from app.utils.demand_value_derived_fields import (
            update_demand_value_derived_fields,
        )

        db = DatabaseClient()
        await update_demand_value_derived_fields(db, user_id)

        return ToolResult(
            success=True,
            data={"updated": True},
        )

    except Exception as e:
        logger.error(f"[PROFILE] update_derived_fields failed: {e}", exc_info=True)
        return ToolResult(
            success=False,
            error=f"Failed to update derived fields: {str(e)}",
        )


@tool(
    name="interpret_demand_value_update",
    description="Use LLM to interpret and extract demand/value updates from conversation.",
)
async def interpret_demand_value_update(
    session_messages: List[Dict[str, Any]],
    demand_hint: Optional[str] = None,
    value_hint: Optional[str] = None,
) -> ToolResult:
    """Interpret demand/value from conversation.

    Args:
        session_messages: Recent conversation messages
        demand_hint: Optional hint about demand
        value_hint: Optional hint about value

    Returns:
        ToolResult with interpreted demand/value
    """
    try:
        from app.utils.demand_value_interpreter import interpret_demand_value_update as _interpret

        result = await _interpret(
            session_messages=session_messages,
            demand_hint=demand_hint,
            value_hint=value_hint,
        )

        return ToolResult(
            success=True,
            data={
                "demand_update": result.get("demand_update"),
                "value_update": result.get("value_update"),
                "confidence": result.get("confidence", 0.0),
            },
        )

    except Exception as e:
        logger.error(f"[PROFILE] interpret_demand_value_update failed: {e}", exc_info=True)
        return ToolResult(
            success=False,
            error=f"Interpretation failed: {str(e)}",
        )


@tool(
    name="apply_demand_value_updates",
    description="Apply demand/value updates to user profile with interpretation and embedding refresh.",
)
async def apply_demand_value_updates(
    user_id: str,
    demand_update: Optional[str] = None,
    value_update: Optional[str] = None,
) -> ToolResult:
    """Apply demand/value updates with full processing.

    Args:
        user_id: User's ID
        demand_update: Demand text to add/update
        value_update: Value text to add/update

    Returns:
        ToolResult indicating success
    """
    try:
        from app.utils.demand_value_updates import apply_demand_value_updates as _apply

        db = DatabaseClient()
        await _apply(
            db=db,
            user_id=user_id,
            demand_update=demand_update,
            value_update=value_update,
        )

        return ToolResult(
            success=True,
            data={
                "applied": True,
                "demand_updated": bool(demand_update),
                "value_updated": bool(value_update),
            },
        )

    except Exception as e:
        logger.error(f"[PROFILE] apply_demand_value_updates failed: {e}", exc_info=True)
        return ToolResult(
            success=False,
            error=f"Update application failed: {str(e)}",
        )


@tool(
    name="get_demand_value_state",
    description="Get current demand and value state for a user.",
)
async def get_demand_value_state(user_id: str) -> ToolResult:
    """Get user's demand/value state.

    Args:
        user_id: User's ID

    Returns:
        ToolResult with demand/value history
    """
    try:
        db = DatabaseClient()
        state = await db.get_demand_value_state(user_id)

        return ToolResult(
            success=True,
            data={
                "demand_history": state.get("demand_history", []),
                "value_history": state.get("value_history", []),
                "latest_demand": state.get("latest_demand"),
                "all_demand": state.get("all_demand"),
                "all_value": state.get("all_value"),
            },
        )

    except Exception as e:
        logger.error(f"[PROFILE] get_demand_value_state failed: {e}", exc_info=True)
        return ToolResult(
            success=False,
            error=f"Failed to get state: {str(e)}",
        )


# =============================================================================
# Profile Field Update Tools
# =============================================================================


@tool(
    name="change_name",
    description="Update the user's name.",
)
async def change_name(user_id: str, new_name: str) -> ToolResult:
    """Change user's name.

    Args:
        user_id: User's ID
        new_name: The new name to set

    Returns:
        ToolResult indicating success with old and new values
    """
    try:
        # Validate input
        is_valid, error_msg = validate_field("name", new_name)
        if not is_valid:
            return ToolResult(
                success=False,
                error=error_msg,
                data={"error_code": UpdateErrorCode.VALIDATION_FAILED},
            )

        db = DatabaseClient()

        # Get current name for reporting
        user = await db.get_user_by_id(user_id)
        old_name = user.get("name") if user else None

        # Update the name
        await db.update_user_profile(user_id, {"name": new_name})

        return ToolResult(
            success=True,
            data={
                "field": "name",
                "old_value": old_name,
                "new_value": new_name,
            },
        )

    except Exception as e:
        logger.error(f"[PROFILE] change_name failed: {e}", exc_info=True)
        return ToolResult(
            success=False,
            error=f"Failed to change name: {str(e)}",
            data={"error_code": UpdateErrorCode.DB_ERROR},
        )


@tool(
    name="change_school",
    description="Update the user's school/university.",
)
async def change_school(user_id: str, new_school: str) -> ToolResult:
    """Change user's school/university.

    Args:
        user_id: User's ID
        new_school: The new school/university name

    Returns:
        ToolResult indicating success with old and new values
    """
    try:
        # Validate input
        is_valid, error_msg = validate_field("university", new_school)
        if not is_valid:
            return ToolResult(
                success=False,
                error=error_msg,
                data={"error_code": UpdateErrorCode.VALIDATION_FAILED},
            )

        db = DatabaseClient()

        # Get current school for reporting
        user = await db.get_user_by_id(user_id)
        old_school = user.get("university") if user else None

        # Update the school
        await db.update_user_profile(user_id, {"university": new_school})

        return ToolResult(
            success=True,
            data={
                "field": "university",
                "old_value": old_school,
                "new_value": new_school,
            },
        )

    except Exception as e:
        logger.error(f"[PROFILE] change_school failed: {e}", exc_info=True)
        return ToolResult(
            success=False,
            error=f"Failed to change school: {str(e)}",
            data={"error_code": UpdateErrorCode.DB_ERROR},
        )


@tool(
    name="change_year",
    description="Update the user's graduation year (e.g., 2028, 2029).",
)
async def change_year(user_id: str, new_year: int) -> ToolResult:
    """Change user's graduation year.

    Args:
        user_id: User's ID
        new_year: The new graduation year (e.g., 2028, 2029)

    Returns:
        ToolResult indicating success with old and new values
    """
    try:
        # Validate input
        is_valid, error_msg = validate_field("year", new_year)
        if not is_valid:
            return ToolResult(
                success=False,
                error=error_msg,
                data={"error_code": UpdateErrorCode.VALIDATION_FAILED},
            )

        db = DatabaseClient()

        # Get current year for reporting
        user = await db.get_user_by_id(user_id)
        old_year = user.get("year") if user else None

        # Update the year
        await db.update_user_profile(user_id, {"year": new_year})

        return ToolResult(
            success=True,
            data={
                "field": "year",
                "old_value": old_year,
                "new_value": new_year,
            },
        )

    except Exception as e:
        logger.error(f"[PROFILE] change_year failed: {e}", exc_info=True)
        return ToolResult(
            success=False,
            error=f"Failed to change year: {str(e)}",
            data={"error_code": UpdateErrorCode.DB_ERROR},
        )


@tool(
    name="change_major",
    description="Update the user's major/field of study.",
)
async def change_major(user_id: str, new_major: str) -> ToolResult:
    """Change user's major.

    Args:
        user_id: User's ID
        new_major: The new major/field of study

    Returns:
        ToolResult indicating success with old and new values
    """
    try:
        # Validate input
        is_valid, error_msg = validate_field("major", new_major)
        if not is_valid:
            return ToolResult(
                success=False,
                error=error_msg,
                data={"error_code": UpdateErrorCode.VALIDATION_FAILED},
            )

        db = DatabaseClient()

        # Get current major for reporting
        user = await db.get_user_by_id(user_id)
        old_major = user.get("major") if user else None

        # Update the major
        await db.update_user_profile(user_id, {"major": new_major})

        return ToolResult(
            success=True,
            data={
                "field": "major",
                "old_value": old_major,
                "new_value": new_major,
            },
        )

    except Exception as e:
        logger.error(f"[PROFILE] change_major failed: {e}", exc_info=True)
        return ToolResult(
            success=False,
            error=f"Failed to change major: {str(e)}",
            data={"error_code": UpdateErrorCode.DB_ERROR},
        )


@tool(
    name="change_career_interest",
    description="Update the user's career interests. "
    "Mode can be 'replace' (replace all), 'append' (add to existing), or 'remove' (remove specified).",
)
async def change_career_interest(
    user_id: str,
    interests: List[str],
    mode: str = "replace",
) -> ToolResult:
    """Change user's career interests.

    Args:
        user_id: User's ID
        interests: List of interests to set/add/remove
        mode: Operation mode - 'replace', 'append', or 'remove'

    Returns:
        ToolResult indicating success with old and new values
    """
    try:
        db = DatabaseClient()

        # Get current interests
        user = await db.get_user_by_id(user_id)
        old_interests = user.get("career_interests", []) if user else []

        # Normalize for comparison (lowercase, trimmed)
        normalized_input = [i.strip().lower() for i in interests]
        normalized_existing = [i.strip().lower() for i in old_interests]

        # Calculate new interests based on mode
        if mode == "replace":
            new_interests = interests
        elif mode == "append":
            # Dedupe while preserving original casing
            new_interests = old_interests + [
                i for i, norm in zip(interests, normalized_input)
                if norm not in normalized_existing
            ]
        elif mode == "remove":
            new_interests = [
                i for i in old_interests
                if i.strip().lower() not in normalized_input
            ]
        else:
            return ToolResult(
                success=False,
                error=f"Invalid mode: {mode}. Use 'replace', 'append', or 'remove'.",
                data={"error_code": UpdateErrorCode.INVALID_MODE},
            )

        # Update the interests
        await db.update_user_profile(user_id, {"career_interests": new_interests})

        return ToolResult(
            success=True,
            data={
                "field": "career_interests",
                "old_value": old_interests,
                "new_value": new_interests,
                "mode": mode,
            },
        )

    except Exception as e:
        logger.error(f"[PROFILE] change_career_interest failed: {e}", exc_info=True)
        return ToolResult(
            success=False,
            error=f"Failed to change career interests: {str(e)}",
        )


# =============================================================================
# History Modification Tools (by index)
# =============================================================================


@tool(
    name="change_demand_history",
    description="Modify or delete an entry in the user's demand history by index. "
    "Use operation='modify' to change text at index, operation='delete' to remove entry at index. "
    "For safety, provide expected_text to verify the entry hasn't changed. "
    "Delete operations require source='user_explicit_delete' and reason.",
)
async def change_demand_history(
    user_id: str,
    operation: str,
    index: int,
    new_value: Optional[str] = None,
    expected_text: Optional[str] = None,
    source: Optional[str] = None,
    reason: Optional[str] = None,
) -> ToolResult:
    """Modify or delete an entry in demand_history by index.

    Args:
        user_id: User's ID
        operation: 'modify' to replace text, 'delete' to remove entry
        index: 0-based index of the entry to modify/delete
        new_value: New text value (required for 'modify' operation)
        expected_text: Expected current text at index (race condition safety)
        source: For delete ops, must be 'user_explicit_delete'
        reason: For delete ops, explanation of why deletion is requested

    Returns:
        ToolResult indicating success with operation details
    """
    try:
        db = DatabaseClient()

        # Get current demand history and normalize to proper format
        state = await db.get_demand_value_state(user_id)
        demand_history = normalize_history(state.get("demand_history", []))

        if not demand_history:
            return ToolResult(
                success=False,
                error="Demand history is empty. Nothing to modify.",
                data={"error_code": UpdateErrorCode.EMPTY_HISTORY},
            )

        if index < 0 or index >= len(demand_history):
            return ToolResult(
                success=False,
                error=f"Index {index} out of range. Valid range: 0-{len(demand_history) - 1}",
                data={"error_code": UpdateErrorCode.INVALID_INDEX},
            )

        old_entry = demand_history[index]
        old_text = old_entry.get("text", "")

        # Race condition safety: verify expected_text matches current text
        if expected_text is not None and old_text != expected_text:
            return ToolResult(
                success=False,
                error="Entry at index has changed. Please re-fetch state.",
                data={
                    "error_code": UpdateErrorCode.HISTORY_CHANGED,
                    "current_text": old_text,
                    "expected_text": expected_text,
                },
            )

        if operation == "modify":
            if not new_value:
                return ToolResult(
                    success=False,
                    error="new_value is required for 'modify' operation.",
                    data={"error_code": UpdateErrorCode.VALIDATION_FAILED},
                )
            # Update the entry text, preserving created_at
            created_at = old_entry.get("created_at")
            new_entry = {"text": new_value.strip()}
            if created_at:
                new_entry["created_at"] = created_at
            demand_history[index] = new_entry

        elif operation == "delete":
            # Delete safety: require explicit user confirmation
            if source != "user_explicit_delete":
                return ToolResult(
                    success=False,
                    error="Delete requires explicit user confirmation. Set source='user_explicit_delete'.",
                    data={
                        "error_code": UpdateErrorCode.DELETE_NOT_EXPLICIT,
                        "needs_clarification": True,
                    },
                )
            if not reason:
                return ToolResult(
                    success=False,
                    error="Delete requires a reason explaining why the user wants to delete.",
                    data={
                        "error_code": UpdateErrorCode.DELETE_NOT_EXPLICIT,
                        "needs_clarification": True,
                    },
                )
            demand_history.pop(index)

        else:
            return ToolResult(
                success=False,
                error=f"Invalid operation: {operation}. Use 'modify' or 'delete'.",
                data={"error_code": UpdateErrorCode.INVALID_OPERATION},
            )

        # Update database
        await db.update_user_profile(user_id, {"demand_history": demand_history})

        # Regenerate derived fields (latest_demand, all_demand) and embeddings
        from app.utils.demand_value_derived_fields import update_demand_value_derived_fields
        try:
            await update_demand_value_derived_fields(
                db=db,
                user_id=user_id,
                demand_history=demand_history,
                # value_history=None means it will be fetched from DB
            )
        except Exception as derived_error:
            logger.warning(f"[PROFILE] Failed to update derived fields: {derived_error}")
            # Continue - the main operation succeeded

        return ToolResult(
            success=True,
            data={
                "operation": operation,
                "index": index,
                "old_value": old_text,
                "new_value": new_value if operation == "modify" else None,
                "history_length": len(demand_history),
            },
        )

    except Exception as e:
        logger.error(f"[PROFILE] change_demand_history failed: {e}", exc_info=True)
        return ToolResult(
            success=False,
            error=f"Failed to change demand history: {str(e)}",
            data={"error_code": UpdateErrorCode.DB_ERROR},
        )


@tool(
    name="change_value_history",
    description="Modify or delete an entry in the user's value history by index. "
    "Use operation='modify' to change text at index, operation='delete' to remove entry at index. "
    "For safety, provide expected_text to verify the entry hasn't changed. "
    "Delete operations require source='user_explicit_delete' and reason.",
)
async def change_value_history(
    user_id: str,
    operation: str,
    index: int,
    new_value: Optional[str] = None,
    expected_text: Optional[str] = None,
    source: Optional[str] = None,
    reason: Optional[str] = None,
) -> ToolResult:
    """Modify or delete an entry in value_history by index.

    Args:
        user_id: User's ID
        operation: 'modify' to replace text, 'delete' to remove entry
        index: 0-based index of the entry to modify/delete
        new_value: New text value (required for 'modify' operation)
        expected_text: Expected current text at index (race condition safety)
        source: For delete ops, must be 'user_explicit_delete'
        reason: For delete ops, explanation of why deletion is requested

    Returns:
        ToolResult indicating success with operation details
    """
    try:
        db = DatabaseClient()

        # Get current value history and normalize to proper format
        state = await db.get_demand_value_state(user_id)
        value_history = normalize_history(state.get("value_history", []))

        if not value_history:
            return ToolResult(
                success=False,
                error="Value history is empty. Nothing to modify.",
                data={"error_code": UpdateErrorCode.EMPTY_HISTORY},
            )

        if index < 0 or index >= len(value_history):
            return ToolResult(
                success=False,
                error=f"Index {index} out of range. Valid range: 0-{len(value_history) - 1}",
                data={"error_code": UpdateErrorCode.INVALID_INDEX},
            )

        old_entry = value_history[index]
        old_text = old_entry.get("text", "")

        # Race condition safety: verify expected_text matches current text
        if expected_text is not None and old_text != expected_text:
            return ToolResult(
                success=False,
                error="Entry at index has changed. Please re-fetch state.",
                data={
                    "error_code": UpdateErrorCode.HISTORY_CHANGED,
                    "current_text": old_text,
                    "expected_text": expected_text,
                },
            )

        if operation == "modify":
            if not new_value:
                return ToolResult(
                    success=False,
                    error="new_value is required for 'modify' operation.",
                    data={"error_code": UpdateErrorCode.VALIDATION_FAILED},
                )
            # Update the entry text, preserving created_at
            created_at = old_entry.get("created_at")
            new_entry = {"text": new_value.strip()}
            if created_at:
                new_entry["created_at"] = created_at
            value_history[index] = new_entry

        elif operation == "delete":
            # Delete safety: require explicit user confirmation
            if source != "user_explicit_delete":
                return ToolResult(
                    success=False,
                    error="Delete requires explicit user confirmation. Set source='user_explicit_delete'.",
                    data={
                        "error_code": UpdateErrorCode.DELETE_NOT_EXPLICIT,
                        "needs_clarification": True,
                    },
                )
            if not reason:
                return ToolResult(
                    success=False,
                    error="Delete requires a reason explaining why the user wants to delete.",
                    data={
                        "error_code": UpdateErrorCode.DELETE_NOT_EXPLICIT,
                        "needs_clarification": True,
                    },
                )
            value_history.pop(index)

        else:
            return ToolResult(
                success=False,
                error=f"Invalid operation: {operation}. Use 'modify' or 'delete'.",
                data={"error_code": UpdateErrorCode.INVALID_OPERATION},
            )

        # Update database
        await db.update_user_profile(user_id, {"value_history": value_history})

        # Regenerate derived fields (all_value) and embeddings
        from app.utils.demand_value_derived_fields import update_demand_value_derived_fields
        try:
            await update_demand_value_derived_fields(
                db=db,
                user_id=user_id,
                # demand_history=None means it will be fetched from DB
                value_history=value_history,
            )
        except Exception as derived_error:
            logger.warning(f"[PROFILE] Failed to update derived fields: {derived_error}")
            # Continue - the main operation succeeded

        return ToolResult(
            success=True,
            data={
                "operation": operation,
                "index": index,
                "old_value": old_text,
                "new_value": new_value if operation == "modify" else None,
                "history_length": len(value_history),
            },
        )

    except Exception as e:
        logger.error(f"[PROFILE] change_value_history failed: {e}", exc_info=True)
        return ToolResult(
            success=False,
            error=f"Failed to change value history: {str(e)}",
            data={"error_code": UpdateErrorCode.DB_ERROR},
        )

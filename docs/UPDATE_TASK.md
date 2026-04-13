# Update Task Implementation Guide

> **Last Updated**: January 2026
> **Status**: Production-ready
> **Files**: `profile.py`, `update.py`, `base_persona.py`

## Overview

The Update Task handles user profile modifications in Franklink. It follows a two-tier agent architecture where the **InteractionAgent** (conductor) routes requests and the **ExecutionAgent** (worker) executes them.

```
User Message → InteractionAgent → Structured Payload → ExecutionAgent → Database
                    ↓                                        ↓
              Extracts exact values              Validates & executes tools
              from natural language                  (no interpretation)
```

## Architecture: The "Dumb Executor" Pattern

### Why This Pattern?

The ExecutionAgent is intentionally "dumb" - it validates and executes, but **never interprets natural language**. This design:

1. **Reduces brittleness** - Interpretation happens in one place (InteractionAgent)
2. **Enables testing** - ExecutionAgent can be tested with deterministic inputs
3. **Prevents confusion** - Clear separation of responsibilities
4. **Enables auditing** - Structured payloads are easy to log and debug

### Data Flow

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           InteractionAgent                                   │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │ Receives: "change my first demand to web dev"                        │   │
│  │                                                                       │   │
│  │ Has access to:                                                        │   │
│  │   - User's demand_history: [{text: "interested in ML"}, ...]         │   │
│  │   - User's value_history: [...]                                       │   │
│  │                                                                       │   │
│  │ Outputs structured payload:                                           │   │
│  │   {                                                                   │   │
│  │     "case": "B",                                                      │   │
│  │     "op": "modify",                                                   │   │
│  │     "index": 0,                                                       │   │
│  │     "new_value": "interested in web development",                     │   │
│  │     "expected_text": "interested in ML"  ← Copied from history        │   │
│  │   }                                                                   │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                           ExecutionAgent                                     │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │ Receives structured payload (no interpretation needed)               │   │
│  │                                                                       │   │
│  │ 1. Validates: index=0 is valid, expected_text matches                │   │
│  │ 2. Calls: change_demand_history(user_id, "modify", 0, new_value,    │   │
│  │           expected_text="interested in ML")                          │   │
│  │ 3. Calls: apply_demand_value_updates(user_id)                        │   │
│  │ 4. Returns: structured result with old/new values                    │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Update Cases

### CASE A: Profile Field Update

Updates static profile fields: `name`, `university`, `year`, `major`, `career_interests`

```json
{
  "case": "A",
  "update_type": "profile",
  "op": "set",
  "field": "university",
  "value": "USC"
}
```

**For career_interests (array field):**
```json
{
  "case": "A",
  "update_type": "profile",
  "op": "set",
  "field": "career_interests",
  "value": ["finance", "consulting"],
  "mode": "append"  // or "replace" or "remove"
}
```

### CASE B: Demand Update (Networking Needs)

Updates what the user is looking for through Franklink connections.

**ADD:**
```json
{
  "case": "B",
  "update_type": "demand",
  "op": "add",
  "demand_text": "looking for ML researchers to connect with"
}
```

**MODIFY:**
```json
{
  "case": "B",
  "update_type": "demand",
  "op": "modify",
  "index": 0,
  "new_value": "interested in web development",
  "expected_text": "interested in ML"
}
```

**DELETE:**
```json
{
  "case": "B",
  "update_type": "demand",
  "op": "delete",
  "index": 1,
  "expected_text": "looking for VC mentors",
  "source": "user_explicit_delete",
  "reason": "User said: remove my second demand"
}
```

### CASE C: Value Update (What User Offers)

Same structure as CASE B, but for `value_history` (what the user can offer others).

### CASE D: Multiple Updates

Combines multiple operations in one request.

```json
{
  "case": "D",
  "update_type": "multiple",
  "operations": [
    {"op": "set", "field": "university", "value": "USC"},
    {"op": "add", "update_type": "demand", "demand_text": "ML research"}
  ]
}
```

## Safety Features

### 1. Race Condition Prevention (`expected_text`)

When modifying/deleting by index, the `expected_text` parameter verifies the entry hasn't changed between when InteractionAgent saw it and when ExecutionAgent modifies it.

```python
# In change_demand_history tool
if expected_text is not None and old_text != expected_text:
    return ToolResult(
        success=False,
        error="Entry at index has changed. Please re-fetch state.",
        data={
            "error_code": "HISTORY_CHANGED",
            "current_text": old_text,
            "expected_text": expected_text,
        },
    )
```

### 2. Explicit Delete Confirmation

Delete operations require explicit confirmation to prevent accidental data loss.

```python
# Required for all delete operations
if source != "user_explicit_delete":
    return ToolResult(
        success=False,
        error="Delete requires explicit user confirmation.",
        data={"error_code": "DELETE_NOT_EXPLICIT", "needs_clarification": True},
    )

if not reason:
    return ToolResult(
        success=False,
        error="Delete requires a reason explaining why.",
        data={"error_code": "DELETE_NOT_EXPLICIT"},
    )
```

### 3. Field Validation

Profile fields are validated before update:

```python
VALIDATION_RULES = {
    "year": {"type": int, "min": 2000, "max": 2100},
    "name": {"type": str, "min_length": 1, "max_length": 100},
    "university": {"type": str, "min_length": 1, "max_length": 200},
    "major": {"type": str, "min_length": 1, "max_length": 200},
}
```

### 4. Career Interests Deduplication

When appending career interests, duplicates are automatically filtered:

```python
# Normalize for comparison (lowercase, trimmed)
normalized_input = [i.strip().lower() for i in interests]
normalized_existing = [i.strip().lower() for i in old_interests]

if mode == "append":
    # Dedupe while preserving original casing
    new_interests = old_interests + [
        i for i, norm in zip(interests, normalized_input)
        if norm not in normalized_existing
    ]
```

## Error Codes

| Code | Description |
|------|-------------|
| `INVALID_INDEX` | Index out of range for history array |
| `EMPTY_HISTORY` | History is empty, nothing to modify/delete |
| `INVALID_OPERATION` | Unknown operation type (not set/add/modify/delete) |
| `INVALID_MODE` | Unknown mode for career_interests |
| `VALIDATION_FAILED` | Value doesn't meet validation rules |
| `DB_ERROR` | Database operation failed |
| `HISTORY_CHANGED` | `expected_text` doesn't match current (race condition) |
| `DELETE_NOT_EXPLICIT` | Delete missing `source='user_explicit_delete'` or `reason` |
| `INVALID_FIELD` | Unknown profile field |

## Important Distinction: career_interests vs demand

This is a common source of confusion. Here's the rule:

| Type | Purpose | Storage | Trigger Phrases |
|------|---------|---------|-----------------|
| **career_interests** | General career fields/industries | `users.career_interests` (array) | "my interests are...", "I'm into [field]" |
| **demand** | Networking needs - what to achieve through connections | `users.demand_history` (JSON) | "I want to meet...", "looking for...", "connect me with..." |

**Examples:**

| User Message | Route To | Reason |
|--------------|----------|--------|
| "I'm into finance" | CASE A (career_interests) | General field, no networking ask |
| "Looking for ML mentors" | CASE B (demand) | Seeking connections |
| "Connect me with someone in VC" | CASE B (demand) | Connection request |
| "I'm interested in ML" | CASE B (demand) | Ambiguous → default to demand on networking platform |

**Rule of thumb:** If it's about WHO to meet or WHAT to achieve through connections → demand. If it's about general career FIELDS → career_interests.

## Index Resolution

When users reference entries by position:

| User Says | Index |
|-----------|-------|
| "first", "my first" | 0 |
| "second" | 1 |
| "third" | 2 |
| "last", "most recent" | length - 1 |
| "the one about X" | Find index where text contains X |

## Handling Ambiguous Messages

### Delete Intent (use DELETE)
- "remove", "delete", "clear"
- "no longer interested in X"
- "don't want X anymore"
- "forget about X"

### Replacement Intent (use MODIFY)
- "instead of X, I want Y"
- "change X to Y"

### Add Intent (use ADD)
- "I want to focus on Y now" (without mentioning removal)

### Requires Clarification
- "Update my interests" (no specifics)
- "Change my demand" (multiple exist, none specified)

## Files Reference

### `app/agents/tools/profile.py`

Contains all profile update tools:

- `change_name(user_id, new_name)`
- `change_school(user_id, new_school)`
- `change_year(user_id, new_year)`
- `change_major(user_id, new_major)`
- `change_career_interest(user_id, interests, mode)`
- `append_demand_history(user_id, demand_text)`
- `append_value_history(user_id, value_text)`
- `change_demand_history(user_id, operation, index, new_value, expected_text, source, reason)`
- `change_value_history(user_id, operation, index, new_value, expected_text, source, reason)`
- `apply_demand_value_updates(user_id)` - Refreshes embeddings after demand/value changes
- `get_demand_value_state(user_id)` - Gets current history state

### `app/agents/tasks/update.py`

Defines the UpdateTask with:
- System prompt for ExecutionAgent
- Tool list
- Completion criteria

### `app/agents/interaction/prompts/base_persona.py`

Contains routing logic in `DIRECT_HANDLING_DECISION_PROMPT`:
- Determines which CASE applies
- Extracts exact values from natural language
- Constructs structured payloads

## Testing Checklist

When testing the update task, verify:

- [ ] Simple profile field updates (name, school, year, major)
- [ ] Career interests append/replace/remove with deduplication
- [ ] Demand history add/modify/delete
- [ ] Value history add/modify/delete
- [ ] Index-based operations ("first", "second", "last")
- [ ] Race condition handling (expected_text mismatch)
- [ ] Delete safety (source + reason required)
- [ ] Ambiguous message handling
- [ ] career_interests vs demand distinction
- [ ] CASE D multiple operations with partial failure
- [ ] Empty history handling (can't modify/delete nothing)
- [ ] Embeddings refresh after demand/value changes

## Common Issues & Solutions

### Issue: "Entry at index has changed"
**Cause:** Race condition - history changed between InteractionAgent seeing it and ExecutionAgent modifying it.
**Solution:** Re-fetch state and retry with updated expected_text.

### Issue: "Delete requires explicit user confirmation"
**Cause:** Delete operation missing `source="user_explicit_delete"` or `reason`.
**Solution:** Ensure InteractionAgent includes these fields for delete operations.

### Issue: career_interests treated as demand (or vice versa)
**Cause:** Ambiguous user message.
**Solution:** Check routing prompt rules. If truly ambiguous, default to demand (networking platform context).

### Issue: Index out of range
**Cause:** User said "third" but only 2 entries exist.
**Solution:** InteractionAgent should check history length before constructing payload. Handle directly if index would be invalid.

## Contributing

When modifying the update task:

1. **Keep ExecutionAgent dumb** - Don't add natural language interpretation
2. **Add safety checks** - Validate early, fail gracefully
3. **Use error codes** - Return structured errors for consistent handling
4. **Update tests** - Cover new edge cases
5. **Update this doc** - Keep documentation in sync with code

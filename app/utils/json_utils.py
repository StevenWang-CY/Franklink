"""JSON utility functions for parsing LLM responses."""

import json
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def strip_code_fences(text: str) -> str:
    """Strip markdown code fences from LLM JSON responses.

    LLMs often wrap JSON responses in markdown code blocks like:
    ```json
    {"key": "value"}
    ```

    This function removes those wrappers to get clean JSON.

    Args:
        text: Raw text that may contain code fences

    Returns:
        Cleaned text with code fences removed
    """
    cleaned = (text or "").strip()

    # Remove ```json prefix
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    elif cleaned.startswith("```"):
        cleaned = cleaned[3:]

    # Remove ``` suffix
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]

    return cleaned.strip()


def parse_llm_json(
    response: str,
    default: Optional[Dict[str, Any]] = None,
    context: str = "LLM response",
) -> Dict[str, Any]:
    """Parse JSON from an LLM response, handling common formatting issues.

    This function:
    1. Strips markdown code fences
    2. Parses the JSON
    3. Returns a default value on parse failure

    Args:
        response: Raw LLM response text
        default: Default value to return on parse failure (default: empty dict)
        context: Context string for error logging

    Returns:
        Parsed JSON dict, or default value on failure
    """
    if default is None:
        default = {}

    if not response:
        logger.warning(f"[JSON] Empty {context}")
        return default

    try:
        cleaned = strip_code_fences(response)
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        logger.warning(f"[JSON] Failed to parse {context}: {e}")
        logger.debug(f"[JSON] Raw response: {response[:500]}...")
        return default

"""Phone number validation utility for SendBlue iMessage delivery.

This module provides functions to validate and normalize phone numbers
before attempting to send messages via SendBlue API.
"""

import re
import logging
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


def is_valid_phone_number(phone: str) -> bool:
    """
    Check if a phone number is valid for SendBlue iMessage delivery.

    Args:
        phone: Phone number string to validate

    Returns:
        True if valid, False otherwise

    Examples:
        >>> is_valid_phone_number("+16463707933")
        True
        >>> is_valid_phone_number("646-370-7933")
        True
        >>> is_valid_phone_number("user@example.com")
        False
    """
    if not phone:
        return False

    # Remove common formatting characters for validation
    cleaned = re.sub(r'[\s\-\(\)\.]+', '', phone)

    # Check for email addresses
    if '@' in phone or '.' in phone and '@' not in phone:
        logger.warning(f"Phone number appears to be an email or URL: {phone}")
        return False

    # Check for URLs
    if phone.startswith('http') or phone.startswith('www'):
        logger.warning(f"Phone number appears to be a URL: {phone}")
        return False

    # E.164 format: +[country code][number]
    # Should start with + and have 10-15 digits
    e164_pattern = r'^\+[1-9]\d{9,14}$'
    if re.match(e164_pattern, cleaned):
        return True

    # US format without +1: 10 digits
    us_pattern = r'^[2-9]\d{9}$'
    if re.match(us_pattern, cleaned):
        return True

    # International format without +: 10-15 digits
    intl_pattern = r'^[1-9]\d{9,14}$'
    if re.match(intl_pattern, cleaned):
        return True

    logger.warning(f"Phone number failed validation: {phone}")
    return False


def normalize_phone_number(phone: str) -> Optional[str]:
    """
    Normalize a phone number to E.164 format.

    Args:
        phone: Phone number string to normalize

    Returns:
        Normalized phone number in E.164 format (+1234567890) or None if invalid

    Examples:
        >>> normalize_phone_number("646-370-7933")
        '+16463707933'
        >>> normalize_phone_number("+1 (646) 370-7933")
        '+16463707933'
        >>> normalize_phone_number("16463707933")
        '+16463707933'
    """
    if not phone:
        return None

    # Remove all formatting characters
    cleaned = re.sub(r'[\s\-\(\)\.]+', '', phone)

    # If already has +, validate and return
    if cleaned.startswith('+'):
        # Special case: +9099019967 (missing country code 1 for US)
        # US numbers should be +1 followed by 10 digits
        if len(cleaned) == 11 and cleaned[1] in '23456789':  # Starts with valid US area code digit
            # This is likely a US number missing the country code
            cleaned = '+1' + cleaned[1:]

        if is_valid_phone_number(cleaned):
            return cleaned
        return None

    # Check if it's a valid US number (10 digits)
    us_pattern = r'^([2-9]\d{9})$'
    match = re.match(us_pattern, cleaned)
    if match:
        return f"+1{match.group(1)}"

    # Check if it has country code but missing +
    # US with country code: 1234567890 (11 digits starting with 1)
    if cleaned.startswith('1') and len(cleaned) == 11:
        return f"+{cleaned}"

    # International number without + (10-15 digits)
    intl_pattern = r'^([1-9]\d{9,14})$'
    match = re.match(intl_pattern, cleaned)
    if match:
        return f"+{match.group(1)}"

    logger.warning(f"Could not normalize phone number: {phone}")
    return None


def validate_and_normalize(phone: str) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Validate and normalize a phone number, returning detailed results.

    Args:
        phone: Phone number string to validate

    Returns:
        Tuple of (is_valid, normalized_phone, error_message)

    Examples:
        >>> validate_and_normalize("646-370-7933")
        (True, '+16463707933', None)
        >>> validate_and_normalize("user@example.com")
        (False, None, 'Phone number appears to be an email')
    """
    if not phone:
        return (False, None, "Phone number is empty")

    # Check for email
    if '@' in phone:
        return (False, None, "Phone number appears to be an email")

    # Check for URL
    if phone.startswith('http') or phone.startswith('www'):
        return (False, None, "Phone number appears to be a URL")

    # Try to normalize
    normalized = normalize_phone_number(phone)

    if not normalized:
        return (False, None, f"Invalid phone number format: {phone}")

    # Final validation
    if is_valid_phone_number(normalized):
        return (True, normalized, None)

    return (False, None, "Phone number failed final validation")


def get_invalid_phone_reason(phone: str) -> str:
    """
    Get a human-readable reason why a phone number is invalid.

    Args:
        phone: Phone number string to check

    Returns:
        Reason string explaining why the number is invalid, or empty string if valid
    """
    if not phone:
        return "Phone number is empty or None"

    if '@' in phone:
        return "Contains '@' symbol - appears to be an email address"

    if phone.startswith('http') or phone.startswith('www'):
        return "Starts with http/www - appears to be a URL"

    cleaned = re.sub(r'[\s\-\(\)\.]+', '', phone)

    if not cleaned.replace('+', '').isdigit():
        return "Contains non-digit characters (excluding +, spaces, dashes, parentheses)"

    if cleaned.startswith('+'):
        if len(cleaned) < 11 or len(cleaned) > 16:
            return f"Invalid length: {len(cleaned)-1} digits (expected 10-15 with country code)"
    else:
        if len(cleaned) < 10 or len(cleaned) > 15:
            return f"Invalid length: {len(cleaned)} digits (expected 10-15)"

    return "Phone number format not recognized (expected E.164 format like +16463707933)"

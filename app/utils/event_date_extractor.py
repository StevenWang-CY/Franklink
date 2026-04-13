"""Extract event dates mentioned in email content.

This module parses email text to find event dates (hackathons, midterms, deadlines, etc.)
and converts them to structured data for time-sensitive connection suggestions.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Month name to number mapping
MONTH_MAP = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}

# Day name to weekday number (Monday=0, Sunday=6)
WEEKDAY_MAP = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

# Event keywords that signal an event date follows or precedes
EVENT_KEYWORDS = [
    "hackathon", "midterm", "exam", "final", "finals", "deadline", "due",
    "interview", "meeting", "event", "session", "workshop", "webinar",
    "conference", "info session", "demo day", "presentation", "pitch",
    "study group", "office hours", "career fair", "networking event",
    "application", "submit", "submission", "rsvp", "register", "registration",
    "orientation", "seminar", "lecture", "class", "quiz", "test",
]

# Compiled regex patterns for efficiency
_MONTH_DAY_PATTERN = re.compile(
    r"\b(january|jan|february|feb|march|mar|april|apr|may|june|jun|july|jul|"
    r"august|aug|september|sep|sept|october|oct|november|nov|december|dec)\s+"
    r"(\d{1,2})(?:st|nd|rd|th)?(?:,?\s*(\d{4}))?\b",
    re.IGNORECASE
)

_NUMERIC_DATE_PATTERN = re.compile(
    r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b"
)

_ISO_DATE_PATTERN = re.compile(
    r"\b(\d{4})-(\d{2})-(\d{2})\b"
)

_WEEKDAY_PATTERN = re.compile(
    r"\b(this|next|coming)?\s*(monday|tuesday|wednesday|thursday|friday|saturday|sunday"
    r"|mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)\b",
    re.IGNORECASE
)

_TOMORROW_PATTERN = re.compile(r"\btomorrow\b", re.IGNORECASE)
_TODAY_TONIGHT_PATTERN = re.compile(r"\b(today|tonight)\b", re.IGNORECASE)
_WEEKEND_PATTERN = re.compile(r"\b(this|next|coming)?\s*weekend\b", re.IGNORECASE)
_IN_DAYS_PATTERN = re.compile(r"\bin\s+(\d+)\s+days?\b", re.IGNORECASE)


def extract_event_dates(
    text: str,
    reference_date: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """
    Extract event dates mentioned in email text.

    Args:
        text: Email content to analyze (subject + body)
        reference_date: Reference date for relative dates (defaults to now)

    Returns:
        List of extracted events with:
        - event_type: Type of event (hackathon, midterm, etc.) or "event"
        - date_str: Formatted date string (YYYY-MM-DD)
        - parsed_date: datetime object
        - raw_match: Original matched text
        - day_name: Day of week name (e.g., "Thursday")
    """
    if not text:
        return []

    reference_date = reference_date or datetime.now()
    text_lower = text.lower()
    events: List[Dict[str, Any]] = []
    seen_dates: set = set()  # Avoid duplicate dates

    # Find all date mentions
    date_mentions = _find_all_dates(text, reference_date)

    # Associate dates with nearby event keywords
    for date_info in date_mentions:
        parsed_date = date_info["parsed_date"]
        date_key = parsed_date.strftime("%Y-%m-%d")

        # Skip duplicates
        if date_key in seen_dates:
            continue
        seen_dates.add(date_key)

        # Find associated event type
        event_type = _find_nearby_event_keyword(
            text_lower,
            date_info["start_pos"],
            date_info["end_pos"],
        )

        events.append({
            "event_type": event_type,
            "date_str": date_key,
            "parsed_date": parsed_date,
            "raw_match": date_info["raw_match"],
            "day_name": parsed_date.strftime("%A"),
        })

    # Sort by date
    events.sort(key=lambda x: x["parsed_date"])

    return events


def _find_all_dates(
    text: str,
    reference_date: datetime,
) -> List[Dict[str, Any]]:
    """Find all date mentions in text with their positions."""
    dates = []

    # Month + day pattern (January 25, Jan 25th)
    for match in _MONTH_DAY_PATTERN.finditer(text):
        month_name = match.group(1).lower()
        day = int(match.group(2))
        year = int(match.group(3)) if match.group(3) else None

        month = MONTH_MAP.get(month_name)
        if month and 1 <= day <= 31:
            parsed = _resolve_month_day(month, day, year, reference_date)
            if parsed:
                dates.append({
                    "parsed_date": parsed,
                    "raw_match": match.group(0),
                    "start_pos": match.start(),
                    "end_pos": match.end(),
                })

    # Numeric date pattern (1/25, 01/25/2026)
    for match in _NUMERIC_DATE_PATTERN.finditer(text):
        month = int(match.group(1))
        day = int(match.group(2))
        year = int(match.group(3)) if match.group(3) else None

        if year and year < 100:
            year += 2000

        if 1 <= month <= 12 and 1 <= day <= 31:
            parsed = _resolve_month_day(month, day, year, reference_date)
            if parsed:
                dates.append({
                    "parsed_date": parsed,
                    "raw_match": match.group(0),
                    "start_pos": match.start(),
                    "end_pos": match.end(),
                })

    # ISO date pattern (2026-01-25)
    for match in _ISO_DATE_PATTERN.finditer(text):
        try:
            parsed = datetime(
                int(match.group(1)),
                int(match.group(2)),
                int(match.group(3)),
            )
            dates.append({
                "parsed_date": parsed,
                "raw_match": match.group(0),
                "start_pos": match.start(),
                "end_pos": match.end(),
            })
        except ValueError:
            pass

    # Weekday patterns (this Monday, next Friday)
    for match in _WEEKDAY_PATTERN.finditer(text):
        modifier = (match.group(1) or "").lower()
        day_name = match.group(2).lower()

        # Normalize short names
        for full_name, weekday_num in WEEKDAY_MAP.items():
            if day_name == full_name or day_name.startswith(full_name[:3]):
                parsed = _resolve_weekday(weekday_num, modifier, reference_date)
                if parsed:
                    dates.append({
                        "parsed_date": parsed,
                        "raw_match": match.group(0),
                        "start_pos": match.start(),
                        "end_pos": match.end(),
                    })
                break

    # Tomorrow
    for match in _TOMORROW_PATTERN.finditer(text):
        parsed = reference_date + timedelta(days=1)
        dates.append({
            "parsed_date": parsed,
            "raw_match": match.group(0),
            "start_pos": match.start(),
            "end_pos": match.end(),
        })

    # Today/tonight
    for match in _TODAY_TONIGHT_PATTERN.finditer(text):
        dates.append({
            "parsed_date": reference_date,
            "raw_match": match.group(0),
            "start_pos": match.start(),
            "end_pos": match.end(),
        })

    # This/next weekend
    for match in _WEEKEND_PATTERN.finditer(text):
        modifier = (match.group(1) or "").lower()
        parsed = _resolve_weekend(modifier, reference_date)
        if parsed:
            dates.append({
                "parsed_date": parsed,
                "raw_match": match.group(0),
                "start_pos": match.start(),
                "end_pos": match.end(),
            })

    # "in X days"
    for match in _IN_DAYS_PATTERN.finditer(text):
        days = int(match.group(1))
        if days <= 365:  # Reasonable limit
            parsed = reference_date + timedelta(days=days)
            dates.append({
                "parsed_date": parsed,
                "raw_match": match.group(0),
                "start_pos": match.start(),
                "end_pos": match.end(),
            })

    return dates


def _resolve_month_day(
    month: int,
    day: int,
    year: Optional[int],
    reference_date: datetime,
) -> Optional[datetime]:
    """Resolve month/day to a datetime, handling year rollover."""
    if year:
        try:
            return datetime(year, month, day)
        except ValueError:
            return None

    # No year specified - infer based on reference date
    current_year = reference_date.year

    try:
        candidate = datetime(current_year, month, day)
    except ValueError:
        return None

    # If the date is more than 2 months in the past, assume next year
    # This handles December emails mentioning January events
    if candidate < reference_date - timedelta(days=60):
        try:
            return datetime(current_year + 1, month, day)
        except ValueError:
            return None

    return candidate


def _resolve_weekday(
    target_weekday: int,
    modifier: str,
    reference_date: datetime,
) -> Optional[datetime]:
    """Resolve weekday name to datetime.

    Args:
        target_weekday: Target day (Monday=0, Sunday=6)
        modifier: "this", "next", "coming", or empty
        reference_date: Reference date
    """
    current_weekday = reference_date.weekday()
    days_ahead = target_weekday - current_weekday

    if modifier == "next":
        # "next Monday" means the Monday of next week
        if days_ahead <= 0:
            days_ahead += 7
        else:
            days_ahead += 7
    else:
        # "this Monday" or just "Monday" means upcoming Monday
        if days_ahead <= 0:
            days_ahead += 7

    return reference_date + timedelta(days=days_ahead)


def _resolve_weekend(modifier: str, reference_date: datetime) -> Optional[datetime]:
    """Resolve 'weekend' to Saturday datetime."""
    current_weekday = reference_date.weekday()

    # Saturday is weekday 5
    days_to_saturday = 5 - current_weekday
    if days_to_saturday < 0:
        days_to_saturday += 7

    if modifier == "next":
        days_to_saturday += 7

    return reference_date + timedelta(days=days_to_saturday)


def _find_nearby_event_keyword(
    text_lower: str,
    start_pos: int,
    end_pos: int,
    search_window: int = 100,
) -> str:
    """Find event keyword near a date mention.

    Args:
        text_lower: Lowercase text to search
        start_pos: Start position of date mention
        end_pos: End position of date mention
        search_window: Characters to search before/after date

    Returns:
        Event type if found, otherwise "event"
    """
    # Get surrounding text
    context_start = max(0, start_pos - search_window)
    context_end = min(len(text_lower), end_pos + search_window)
    context = text_lower[context_start:context_end]

    # Search for event keywords
    for keyword in EVENT_KEYWORDS:
        if keyword in context:
            return keyword

    return "event"


def format_event_dates_for_zep(events: List[Dict[str, Any]]) -> str:
    """
    Format extracted events as text annotation for Zep ingestion.

    Args:
        events: List of extracted event dictionaries

    Returns:
        Formatted string for Zep, e.g., "[hackathon: 2026-01-25 (Saturday), deadline: 2026-01-28 (Tuesday)]"
    """
    if not events:
        return ""

    parts = []
    for event in events:
        event_type = event.get("event_type", "event")
        date_str = event.get("date_str", "")
        day_name = event.get("day_name", "")

        if date_str:
            if day_name:
                parts.append(f"{event_type}: {date_str} ({day_name})")
            else:
                parts.append(f"{event_type}: {date_str}")

    if not parts:
        return ""

    return "[" + ", ".join(parts) + "]"


def resolve_relative_date(
    relative_expr: str,
    reference_date: Optional[datetime] = None,
) -> Optional[datetime]:
    """
    Convert a relative date expression to absolute datetime.

    This is a convenience function for testing and external use.

    Args:
        relative_expr: Expression like "next Monday", "tomorrow", "this weekend"
        reference_date: Reference date (defaults to now)

    Returns:
        Resolved datetime or None if cannot parse
    """
    reference_date = reference_date or datetime.now()
    dates = _find_all_dates(relative_expr, reference_date)

    if dates:
        return dates[0]["parsed_date"]
    return None

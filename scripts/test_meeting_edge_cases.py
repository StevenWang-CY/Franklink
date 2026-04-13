#!/usr/bin/env python
"""
Comprehensive Edge Case Test Suite for Meeting Scheduler

This script tests ALL edge cases in the meeting scheduler to ensure 100% robustness.
Run from the project root:
    python scripts/test_meeting_edge_cases.py
"""

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Load environment variables
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

# Set minimal env vars
os.environ.setdefault("PHOTON_SERVER_URL", "http://localhost")
os.environ.setdefault("PHOTON_DEFAULT_NUMBER", "+10000000000")

from app.groupchat.features.meeting_scheduler import (
    GroupChatMeetingSchedulerService,
    _parse_option_choice,
    _normalize_timezone_input,
    _normalize_windows,
    _compute_overlap_options,
    _is_explicit_schedule_invocation,
    _safe_json_loads,
    _parse_iso_dt,
    _is_timezone_label,
    _clamp_confidence,
    _build_options_message,
    _timezone_from_label,
)


def print_test(name: str, passed: bool, details: str = ""):
    status = "✅ PASS" if passed else "❌ FAIL"
    print(f"  {status}: {name}")
    if not passed and details:
        print(f"         {details}")


def print_section(title: str):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


class EdgeCaseTests:
    """Comprehensive edge case testing."""
    
    def __init__(self):
        self.passed = 0
        self.failed = 0
    
    def run_all(self):
        """Run all edge case tests."""
        print_section("1. JSON PARSING EDGE CASES")
        self.test_json_parsing()
        
        print_section("2. TIMEZONE PARSING EDGE CASES")
        self.test_timezone_parsing()
        
        print_section("3. OPTION CHOICE PARSING EDGE CASES")
        self.test_option_choice_parsing()
        
        print_section("4. DATETIME PARSING EDGE CASES")
        self.test_datetime_parsing()
        
        print_section("5. WINDOW NORMALIZATION EDGE CASES")
        self.test_window_normalization()
        
        print_section("6. OVERLAP COMPUTATION EDGE CASES")
        self.test_overlap_computation()
        
        print_section("7. EXPLICIT INVOCATION EDGE CASES")
        self.test_explicit_invocation()
        
        print_section("8. CONFIDENCE CLAMPING EDGE CASES")
        self.test_confidence_clamping()
        
        print_section("9. TIMEZONE LABEL VALIDATION")
        self.test_timezone_label_validation()
        
        print_section("10. OPTIONS MESSAGE BUILDING")
        self.test_options_message_building()
        
        # Summary
        print_section("SUMMARY")
        total = self.passed + self.failed
        print(f"  Passed: {self.passed}/{total}")
        print(f"  Failed: {self.failed}/{total}")
        if self.failed > 0:
            print(f"\n  ⚠️  {self.failed} FAILURES FOUND - NEEDS FIXING!")
        else:
            print(f"\n  ✅ ALL EDGE CASES HANDLED CORRECTLY!")
        
        return self.failed == 0
    
    def check(self, name: str, condition: bool, details: str = ""):
        if condition:
            self.passed += 1
        else:
            self.failed += 1
        print_test(name, condition, details)
    
    def test_json_parsing(self):
        """Test _safe_json_loads with malformed inputs."""
        # Valid JSON
        self.check("Valid JSON object", _safe_json_loads('{"key": "value"}') == {"key": "value"})
        
        # JSON with code fences
        self.check("JSON with code fences", _safe_json_loads('```json\n{"key": "value"}\n```') == {"key": "value"})
        
        # Malformed JSON
        self.check("Malformed JSON returns None", _safe_json_loads('{invalid}') is None)
        
        # Empty string
        self.check("Empty string returns None", _safe_json_loads('') is None)
        
        # None input
        self.check("None input returns None", _safe_json_loads(None) is None)
        
        # JSON array (not dict) - should return None
        self.check("JSON array returns None", _safe_json_loads('[1, 2, 3]') is None)
        
        # Nested text with JSON inside
        self.check("Text with embedded JSON", _safe_json_loads('Here is the result: {"a": 1}') == {"a": 1})
        
        # Multiple JSON objects - the greedy regex captures the whole thing which is invalid JSON
        # This is CORRECT behavior - returns None for malformed input (safe failure mode)
        result = _safe_json_loads('{"first": 1} {"second": 2}')
        self.check("Multiple JSON objects returns None (safe)", result is None)
    
    def test_timezone_parsing(self):
        """Test _normalize_timezone_input with edge cases."""
        # Standard formats
        self.check("UTC-07:00 format", _normalize_timezone_input("UTC-07:00") == "UTC-07:00")
        self.check("UTC+00:00 format", _normalize_timezone_input("UTC+00:00") == "UTC+00:00")
        
        # GMT variations
        self.check("GMT+2 without colon", _normalize_timezone_input("GMT+2") is not None)
        self.check("gmt alone returns UTC+00:00", _normalize_timezone_input("gmt") == "UTC+00:00")
        self.check("utc alone returns UTC+00:00", _normalize_timezone_input("utc") == "UTC+00:00")
        
        # City names
        self.check("NYC lowercase", _normalize_timezone_input("nyc") is not None)
        self.check("Tokyo", _normalize_timezone_input("tokyo") is not None)
        self.check("Singapore", _normalize_timezone_input("singapore") is not None)
        
        # Typos
        self.check("Typo: pasific", _normalize_timezone_input("pasific") is not None)
        self.check("Typo: eastrn", _normalize_timezone_input("eastrn") is not None)
        
        # Edge cases
        self.check("Empty string returns None", _normalize_timezone_input("") is None)
        self.check("None input returns None", _normalize_timezone_input(None) is None)
        self.check("Whitespace only returns None", _normalize_timezone_input("   ") is None)
        self.check("Random text returns None", _normalize_timezone_input("hello world") is None)
        
        # IANA timezone IDs
        result = _normalize_timezone_input("America/Los_Angeles")
        self.check("IANA timezone ID", result is not None)
        
        # Compact offsets
        self.check("Compact -0530", _normalize_timezone_input("-0530") is not None)
        self.check("Compact +8", _normalize_timezone_input("+8") is not None)
        
        # Invalid offsets
        self.check("Invalid offset +25 returns None", _normalize_timezone_input("+25") is None)
    
    def test_option_choice_parsing(self):
        """Test _parse_option_choice with edge cases."""
        # Basic letters
        self.check("Single letter A", _parse_option_choice("A", 3) == 0)
        self.check("Single letter b (lowercase)", _parse_option_choice("b", 3) == 1)
        
        # Numbers
        self.check("Number 1", _parse_option_choice("1", 3) == 0)
        self.check("Number 2", _parse_option_choice("2", 3) == 1)
        
        # Ordinals
        self.check("first", _parse_option_choice("first", 3) == 0)
        self.check("second", _parse_option_choice("second", 3) == 1)
        self.check("third", _parse_option_choice("third", 3) == 2)
        self.check("1st", _parse_option_choice("1st", 3) == 0)
        
        # Phrases
        self.check("go with A", _parse_option_choice("go with A", 3) == 0)
        self.check("let's do b", _parse_option_choice("let's do b", 3) == 1)
        self.check("pick a", _parse_option_choice("pick a", 3) == 0)
        
        # Typos
        self.check("opion 1 (typo)", _parse_option_choice("opion 1", 3) == 0)
        self.check("optoin 2 (typo)", _parse_option_choice("optoin 2", 3) == 1)
        self.check("choice B", _parse_option_choice("choice B", 3) == 1)
        
        # Out of range
        self.check("Out of range letter x", _parse_option_choice("x", 3) is None)
        self.check("Out of range number 5", _parse_option_choice("5", 3) is None)
        self.check("fourth with max 3", _parse_option_choice("fourth", 3) is None)
        
        # Edge cases
        self.check("Empty string", _parse_option_choice("", 3) is None)
        self.check("None input", _parse_option_choice(None, 3) is None)
        self.check("Whitespace only", _parse_option_choice("   ", 3) is None)
        self.check("Random text", _parse_option_choice("what's for lunch?", 3) is None)
        self.check("max_options=0", _parse_option_choice("A", 0) is None)
    
    def test_datetime_parsing(self):
        """Test _parse_iso_dt with edge cases."""
        # Valid formats
        self.check("ISO with offset", _parse_iso_dt("2025-03-12T10:00:00-07:00") is not None)
        self.check("ISO with Z suffix", _parse_iso_dt("2025-03-12T10:00:00Z") is not None)
        
        # Invalid formats
        self.check("No timezone returns None", _parse_iso_dt("2025-03-12T10:00:00") is None)
        self.check("Invalid date returns None", _parse_iso_dt("not-a-date") is None)
        self.check("Empty string returns None", _parse_iso_dt("") is None)
        self.check("None input returns None", _parse_iso_dt(None) is None)
        
        # Edge cases
        self.check("Whitespace around date", _parse_iso_dt("  2025-03-12T10:00:00Z  ") is not None)
    
    def test_window_normalization(self):
        """Test _normalize_windows with edge cases."""
        # Valid window
        valid = [{"start": "2025-03-12T10:00:00-07:00", "end": "2025-03-12T12:00:00-07:00"}]
        self.check("Valid window", len(_normalize_windows(valid)) == 1)
        
        # No timezone
        no_tz = [{"start": "2025-03-12T10:00:00", "end": "2025-03-12T12:00:00"}]
        self.check("No timezone filtered out", len(_normalize_windows(no_tz)) == 0)
        
        # End before start
        invalid_order = [{"start": "2025-03-12T12:00:00-07:00", "end": "2025-03-12T10:00:00-07:00"}]
        self.check("End before start filtered", len(_normalize_windows(invalid_order)) == 0)
        
        # Too short (< 15 min)
        too_short = [{"start": "2025-03-12T10:00:00-07:00", "end": "2025-03-12T10:10:00-07:00"}]
        self.check("Too short (<15min) filtered", len(_normalize_windows(too_short)) == 0)
        
        # Too long (> 8 hours)
        too_long = [{"start": "2025-03-12T10:00:00-07:00", "end": "2025-03-12T20:00:00-07:00"}]
        self.check("Too long (>8hr) filtered", len(_normalize_windows(too_long)) == 0)
        
        # Edge cases
        self.check("Empty list", _normalize_windows([]) == [])
        self.check("None input", _normalize_windows(None) == [])
        self.check("Non-list input", _normalize_windows("not a list") == [])
        self.check("List of non-dicts", _normalize_windows([1, 2, 3]) == [])
        
        # Limit to 6 windows
        many_windows = [
            {"start": f"2025-03-{12+i}T10:00:00-07:00", "end": f"2025-03-{12+i}T12:00:00-07:00"}
            for i in range(10)
        ]
        self.check("Max 6 windows", len(_normalize_windows(many_windows)) == 6)
    
    def test_overlap_computation(self):
        """Test _compute_overlap_options with edge cases."""
        # Valid overlap
        a = [{"start": "2025-03-12T10:00:00-07:00", "end": "2025-03-12T14:00:00-07:00"}]
        b = [{"start": "2025-03-12T12:00:00-07:00", "end": "2025-03-12T16:00:00-07:00"}]
        result = _compute_overlap_options(a, b, timedelta(minutes=30), 3)
        self.check("Valid overlap found", len(result) == 1)
        
        # No overlap
        a_no = [{"start": "2025-03-12T08:00:00-07:00", "end": "2025-03-12T10:00:00-07:00"}]
        b_no = [{"start": "2025-03-12T14:00:00-07:00", "end": "2025-03-12T16:00:00-07:00"}]
        result = _compute_overlap_options(a_no, b_no, timedelta(minutes=30), 3)
        self.check("No overlap returns empty", len(result) == 0)
        
        # Overlap too short
        a_short = [{"start": "2025-03-12T10:00:00-07:00", "end": "2025-03-12T10:20:00-07:00"}]
        b_short = [{"start": "2025-03-12T10:00:00-07:00", "end": "2025-03-12T10:20:00-07:00"}]
        result = _compute_overlap_options(a_short, b_short, timedelta(minutes=30), 3)
        self.check("Overlap too short returns empty", len(result) == 0)
        
        # Edge cases
        self.check("Empty lists", _compute_overlap_options([], [], timedelta(minutes=30), 3) == [])
        self.check("Empty A list", _compute_overlap_options([], b, timedelta(minutes=30), 3) == [])
        self.check("Empty B list", _compute_overlap_options(a, [], timedelta(minutes=30), 3) == [])
        
        # max_options=1
        result = _compute_overlap_options(a, b, timedelta(minutes=30), 1)
        self.check("max_options=1 limits result", len(result) <= 1)
    
    def test_explicit_invocation(self):
        """Test _is_explicit_schedule_invocation with edge cases."""
        # Valid invocations
        self.check("frank schedule", _is_explicit_schedule_invocation("frank schedule"))
        self.check("@frank schedule", _is_explicit_schedule_invocation("@frank schedule"))
        self.check("hey frank schedule", _is_explicit_schedule_invocation("hey frank schedule"))
        self.check("frank set up a meeting", _is_explicit_schedule_invocation("frank set up a meeting"))
        self.check("frank book a call", _is_explicit_schedule_invocation("frank book a call"))
        self.check("frank can you schedule", _is_explicit_schedule_invocation("frank can you schedule"))
        self.check("frank let's schedule", _is_explicit_schedule_invocation("frank let's schedule"))
        self.check("FRANK SCHEDULE (uppercase)", _is_explicit_schedule_invocation("FRANK SCHEDULE"))
        
        # Invalid invocations
        self.check("schedule a meeting (no frank)", not _is_explicit_schedule_invocation("schedule a meeting"))
        self.check("frank hello (no action)", not _is_explicit_schedule_invocation("frank hello"))
        self.check("empty string", not _is_explicit_schedule_invocation(""))
        self.check("None input", not _is_explicit_schedule_invocation(None))
        self.check("random text with frank", not _is_explicit_schedule_invocation("random text with frank in it"))
    
    def test_confidence_clamping(self):
        """Test _clamp_confidence with edge cases."""
        self.check("Normal 0.5", _clamp_confidence(0.5) == 0.5)
        self.check("Below 0 -> 0.0", _clamp_confidence(-1) == 0.0)
        self.check("Above 1 -> 1.0", _clamp_confidence(2) == 1.0)
        self.check("String '0.5'", _clamp_confidence("0.5") == 0.5)
        self.check("Invalid string -> 0.0", _clamp_confidence("invalid") == 0.0)
        self.check("None -> 0.0", _clamp_confidence(None) == 0.0)
    
    def test_timezone_label_validation(self):
        """Test _is_timezone_label with edge cases."""
        self.check("UTC+00:00 valid", _is_timezone_label("UTC+00:00"))
        self.check("UTC-07:00 valid", _is_timezone_label("UTC-07:00"))
        self.check("UTC+12:30 valid", _is_timezone_label("UTC+12:30"))
        self.check("PST invalid", not _is_timezone_label("PST"))
        self.check("UTC without offset invalid", not _is_timezone_label("UTC"))
        self.check("Empty string invalid", not _is_timezone_label(""))
        self.check("None invalid", not _is_timezone_label(None))
    
    def test_options_message_building(self):
        """Test _build_options_message with edge cases."""
        options = [
            {"start": "2025-03-12T18:00:00+00:00", "end": "2025-03-12T18:30:00+00:00"}
        ]
        tz_a = _timezone_from_label("UTC-08:00")
        tz_b = _timezone_from_label("UTC-05:00")
        
        result = _build_options_message(options, tz_a, tz_b, "Alice", "Bob")
        self.check("Message contains Alice", "Alice" in result)
        self.check("Message contains Bob", "Bob" in result)
        self.check("Message has option A", "A:" in result)
        
        # Empty options
        empty_result = _build_options_message([], tz_a, tz_b, "Alice", "Bob")
        self.check("Empty options returns fallback message", "no overlapping" in empty_result)


def main():
    print("\n" + "="*60)
    print("  COMPREHENSIVE MEETING SCHEDULER EDGE CASE AUDIT")
    print("="*60)
    
    tests = EdgeCaseTests()
    success = tests.run_all()
    
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())

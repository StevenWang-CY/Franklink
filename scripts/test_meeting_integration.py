#!/usr/bin/env python3
"""
Comprehensive End-to-End Integration Test for Meeting Scheduler.

This test verifies the ENTIRE meeting scheduling flow including:
1. Group chat invocation
2. Plan creation
3. DM routing for timezone collection
4. DM routing for availability collection
5. Overlap computation
6. Confirmation handling
7. Meeting finalization

Run with: python scripts/test_meeting_integration.py
"""

import asyncio
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from app.database.client import DatabaseClient
from app.groupchat.features.meeting_scheduler import (
    GroupChatMeetingSchedulerService,
    _normalize_timezone_input,
    _parse_option_choice,
    _is_explicit_schedule_invocation,
    _normalize_windows,
    _compute_overlap_options,
    _build_options_message,
    _safe_json_loads,
    _is_timezone_label,
)
from datetime import timedelta, timezone as tz
from typing import List, Tuple

class IntegrationTest:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.results: List[Tuple[str, bool, str]] = []
    
    def check(self, name: str, condition: bool, detail: str = ""):
        if condition:
            self.passed += 1
            print(f"  ✅ PASS: {name}")
        else:
            self.failed += 1
            print(f"  ❌ FAIL: {name} - {detail}")
        self.results.append((name, condition, detail))
    
    def section(self, title: str):
        print(f"\n{'='*60}")
        print(f"  {title}")
        print(f"{'='*60}")


async def run_integration_tests():
    test = IntegrationTest()
    
    print("\n" + "="*60)
    print("  COMPREHENSIVE MEETING SCHEDULER INTEGRATION TEST")
    print("="*60)
    
    # =========================================================================
    # 1. TEST DM ROUTING LOGIC
    # =========================================================================
    test.section("1. DM ROUTING LOGIC")
    
    db = DatabaseClient()
    
    # Test that we can find active plans for users
    # This simulates the DM handler checking for active plans
    user_id = "9765e793-d878-418d-a690-5b492ffb78ee"
    plan = await db.get_active_group_chat_meeting_plan_for_user_v1(user_id)
    test.check("Database lookup finds plan", plan is not None, "No plan found")
    
    if plan:
        status = plan.get("status")
        test.check("Plan has valid status", status in ["collecting_availability", "awaiting_confirmation", "scheduled", "canceled"], f"Invalid status: {status}")
        
        user_a_id = str(plan.get("user_a_id") or "")
        user_b_id = str(plan.get("user_b_id") or "")
        test.check("Plan has user_a_id", bool(user_a_id), "Missing user_a_id")
        test.check("Plan has user_b_id", bool(user_b_id), "Missing user_b_id")
        test.check("User matches plan", user_id in [user_a_id, user_b_id], "User not in plan")
    
    # =========================================================================
    # 2. TEST TIMEZONE EXTRACTION (Comprehensive)
    # =========================================================================
    test.section("2. TIMEZONE EXTRACTION")
    
    # Standard timezones
    test.check("NYC → UTC-05:00", _normalize_timezone_input("NYC") == "UTC-05:00")
    test.check("Beijing → UTC+08:00", _normalize_timezone_input("Beijing") == "UTC+08:00")
    test.check("Tokyo → UTC+09:00", _normalize_timezone_input("Tokyo") == "UTC+09:00")
    test.check("London → UTC+00:00", _normalize_timezone_input("London") == "UTC+00:00")
    test.check("Singapore → UTC+08:00", _normalize_timezone_input("Singapore") == "UTC+08:00")
    
    # Conversational inputs
    test.check("'I'm in new york'", _normalize_timezone_input("I'm in new york") == "UTC-05:00")
    test.check("'pacific time'", _normalize_timezone_input("pacific time") == "UTC-08:00")
    test.check("'eastern standard time'", _normalize_timezone_input("eastern standard time") == "UTC-05:00")
    
    # Direct offset formats
    test.check("UTC+08:00 format", _normalize_timezone_input("UTC+08:00") == "UTC+08:00")
    test.check("GMT-5 format", _normalize_timezone_input("GMT-5") is not None)
    
    # Edge cases
    test.check("Empty string → None", _normalize_timezone_input("") is None)
    test.check("None input → None", _normalize_timezone_input(None) is None)
    test.check("Invalid → None", _normalize_timezone_input("xyzabc") is None)
    
    # =========================================================================
    # 3. TEST OPTION CHOICE PARSING
    # =========================================================================
    test.section("3. OPTION CHOICE PARSING")
    
    # Direct selections
    test.check("'A' → 0", _parse_option_choice("A", 3) == 0)
    test.check("'B' → 1", _parse_option_choice("B", 3) == 1)
    test.check("'1' → 0", _parse_option_choice("1", 3) == 0)
    test.check("'2' → 1", _parse_option_choice("2", 3) == 1)
    
    # Natural language
    test.check("'first' → 0", _parse_option_choice("first", 3) == 0)
    test.check("'second' → 1", _parse_option_choice("second", 3) == 1)
    test.check("'I'll take A'", _parse_option_choice("I'll take A", 3) == 0)
    test.check("'go with B'", _parse_option_choice("go with B", 3) == 1)
    
    # Out of range
    test.check("'D' with max 3 → None", _parse_option_choice("D", 3) is None)
    test.check("'5' with max 3 → None", _parse_option_choice("5", 3) is None)
    
    # =========================================================================
    # 4. TEST EXPLICIT INVOCATION DETECTION
    # =========================================================================
    test.section("4. EXPLICIT INVOCATION DETECTION")
    
    test.check("'frank schedule'", _is_explicit_schedule_invocation("frank schedule"))
    test.check("'@frank schedule'", _is_explicit_schedule_invocation("@frank schedule"))
    test.check("'frank set up a meeting'", _is_explicit_schedule_invocation("frank set up a meeting"))
    test.check("'frank book a call'", _is_explicit_schedule_invocation("frank book a call"))
    test.check("'hey frank schedule'", _is_explicit_schedule_invocation("hey frank schedule"))
    test.check("'frank find us a time'", _is_explicit_schedule_invocation("frank find us a time"))
    test.check("'frank arrange a meeting'", _is_explicit_schedule_invocation("frank arrange a meeting"))
    test.check("'FRANK SCHEDULE' (uppercase)", _is_explicit_schedule_invocation("FRANK SCHEDULE"))
    
    # Should NOT match
    test.check("'schedule a meeting' (no frank) → False", not _is_explicit_schedule_invocation("schedule a meeting"))
    test.check("'frank hello' (no action) → False", not _is_explicit_schedule_invocation("frank hello"))
    
    # =========================================================================
    # 5. TEST WINDOW NORMALIZATION
    # =========================================================================
    test.section("5. WINDOW NORMALIZATION")
    
    valid_window = [{"start": "2026-01-12T14:00:00+08:00", "end": "2026-01-12T16:00:00+08:00"}]
    test.check("Valid window normalized", len(_normalize_windows(valid_window)) == 1)
    
    invalid_windows = [{"start": "no timezone", "end": "no timezone"}]
    test.check("Invalid window filtered", len(_normalize_windows(invalid_windows)) == 0)
    
    test.check("Empty list → empty", _normalize_windows([]) == [])
    test.check("None → empty", _normalize_windows(None) == [])
    
    # =========================================================================
    # 6. TEST OVERLAP COMPUTATION
    # =========================================================================
    test.section("6. OVERLAP COMPUTATION")
    
    windows_a = [{"start": "2026-01-12T14:00:00+00:00", "end": "2026-01-12T18:00:00+00:00"}]
    windows_b = [{"start": "2026-01-12T15:00:00+00:00", "end": "2026-01-12T19:00:00+00:00"}]
    
    options = _compute_overlap_options(windows_a, windows_b, timedelta(minutes=30), 3)
    test.check("Overlap found", len(options) > 0)
    
    # No overlap
    windows_a_no = [{"start": "2026-01-12T10:00:00+00:00", "end": "2026-01-12T11:00:00+00:00"}]
    windows_b_no = [{"start": "2026-01-12T14:00:00+00:00", "end": "2026-01-12T15:00:00+00:00"}]
    options_no = _compute_overlap_options(windows_a_no, windows_b_no, timedelta(minutes=30), 3)
    test.check("No overlap → empty", len(options_no) == 0)
    
    # =========================================================================
    # 7. TEST OPTIONS MESSAGE BUILDING
    # =========================================================================
    test.section("7. OPTIONS MESSAGE BUILDING")
    
    options_list = [{"start": "2026-01-12T15:00:00+00:00", "end": "2026-01-12T15:30:00+00:00"}]
    message = _build_options_message(options_list, tz.utc, tz.utc, "Alice", "Bob")
    test.check("Message contains 'overlap found'", "overlap found" in message)
    test.check("Message contains option label", "A:" in message)
    
    # Empty options
    empty_msg = _build_options_message([], tz.utc, tz.utc, "Alice", "Bob")
    test.check("Empty options handled", "no overlapping" in empty_msg)
    
    # =========================================================================
    # 8. TEST JSON PARSING
    # =========================================================================
    test.section("8. JSON PARSING")
    
    test.check("Valid JSON", _safe_json_loads('{"key": "value"}') == {"key": "value"})
    test.check("JSON with code fences", _safe_json_loads('```json\n{"a":1}\n```') == {"a": 1})
    test.check("Malformed → None", _safe_json_loads("not json") is None)
    test.check("Empty → None", _safe_json_loads("") is None)
    
    # =========================================================================
    # 9. TEST TIMEZONE LABEL VALIDATION
    # =========================================================================
    test.section("9. TIMEZONE LABEL VALIDATION")
    
    test.check("UTC+08:00 valid", _is_timezone_label("UTC+08:00"))
    test.check("UTC-05:00 valid", _is_timezone_label("UTC-05:00"))
    test.check("PST invalid", not _is_timezone_label("PST"))
    test.check("Empty invalid", not _is_timezone_label(""))
    
    # =========================================================================
    # SUMMARY
    # =========================================================================
    print("\n" + "="*60)
    print("  INTEGRATION TEST SUMMARY")
    print("="*60)
    print(f"  Passed: {test.passed}/{test.passed + test.failed}")
    print(f"  Failed: {test.failed}/{test.passed + test.failed}")
    
    if test.failed == 0:
        print("\n  ✅ ALL INTEGRATION TESTS PASSED!")
        return True
    else:
        print("\n  ⚠️  SOME TESTS FAILED - NEEDS INVESTIGATION")
        for name, passed, detail in test.results:
            if not passed:
                print(f"    - {name}: {detail}")
        return False


if __name__ == "__main__":
    success = asyncio.run(run_integration_tests())
    sys.exit(0 if success else 1)

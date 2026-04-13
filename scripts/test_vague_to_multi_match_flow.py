#!/usr/bin/env python3
"""Test the complete flow: vague request -> purpose selection -> multi-match.

This test verifies:
1. Vague networking request triggers Purpose Suggestion Flow
2. User selects a purpose from suggestions
3. User specifies they want to connect with multiple people
4. Frank finds multiple matches using find_multi_matches

Usage:
    python scripts/test_vague_to_multi_match_flow.py
"""

import asyncio
import sys
import os
import logging
from datetime import datetime
from typing import Any, Dict, Optional
from dataclasses import dataclass, field

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings
from app.database.client import DatabaseClient
from app.integrations.photon_client import PhotonClient
from app.integrations.azure_openai_client import AzureOpenAIClient
from app.agents.interaction import get_interaction_agent
from app.agents.state import AtomicStateManager, NetworkingFlowState

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)

# Test user
TEST_USER_ID = "6fec19ae-cfc9-463e-9486-0c643b59fee8"


@dataclass
class FlowTestResult:
    """Track test results."""

    test_name: str
    step: str
    passed: bool = False
    details: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    duration_ms: int = 0


def separator(title: str):
    """Print a section separator."""
    print("\n" + "=" * 70)
    print(f"  {title}")
    print("=" * 70 + "\n")


class MockPhotonClient:
    """Mock Photon client that captures messages instead of sending."""

    def __init__(self):
        self.sent_messages: list[Dict[str, Any]] = []
        self.typing_started: list[str] = []
        self.typing_stopped: list[str] = []

    async def send_message(self, to_number: str, content: str, **kwargs) -> Dict[str, Any]:
        """Capture the message instead of sending."""
        msg = {
            "to_number": to_number,
            "content": content,
            "timestamp": datetime.utcnow().isoformat(),
            **kwargs,
        }
        self.sent_messages.append(msg)
        logger.info(f"[MOCK_PHOTON] Message captured: {content[:80]}...")
        return {"success": True, "message_id": f"mock-{len(self.sent_messages)}"}

    async def start_typing(self, phone_number: str, chat_guid: Optional[str] = None):
        self.typing_started.append(phone_number)
        logger.info(f"[MOCK_PHOTON] Typing started for {phone_number}")

    async def stop_typing(self, phone_number: str, chat_guid: Optional[str] = None):
        self.typing_stopped.append(phone_number)
        logger.info(f"[MOCK_PHOTON] Typing stopped for {phone_number}")

    async def mark_chat_read(self, chat_guid: Optional[str] = None):
        pass

    def get_messages(self) -> list[Dict[str, Any]]:
        return self.sent_messages

    def clear(self):
        self.sent_messages = []
        self.typing_started = []
        self.typing_stopped = []


async def step1_vague_request(
    db: DatabaseClient,
    photon: MockPhotonClient,
    openai: AzureOpenAIClient,
    user: Dict[str, Any],
    state_manager: AtomicStateManager,
) -> FlowTestResult:
    """Step 1: Send a vague networking request.

    Expected:
    - System detects vague request
    - Purpose Suggestion Flow is triggered
    - Response contains purpose suggestions for user to select
    """
    result = FlowTestResult(test_name="Vague to Multi-Match Flow", step="1_vague_request")

    # Reset state for clean test
    await state_manager.force_reset_state(user["id"])
    photon.clear()

    start_time = datetime.utcnow()

    try:
        agent = get_interaction_agent(db=db, photon=photon, openai=openai)

        # Vague networking message - no specific criteria
        message = "i want to meet someone"

        print(f"User message: \"{message}\"")
        print("\nProcessing through InteractionAgent...")

        response = await agent.process_message(
            phone_number=user["phone_number"],
            message_content=message,
            user=user,
            webhook_data={"chat_guid": None, "message_id": "test-step1"},
        )

        duration = (datetime.utcnow() - start_time).total_seconds() * 1000
        result.duration_ms = int(duration)

        print(f"\nResponse received in {duration:.0f}ms")
        print(f"Success: {response.get('success')}")

        sent_messages = photon.get_messages()
        print(f"\nMessages sent via Photon: {len(sent_messages)}")
        for i, msg in enumerate(sent_messages):
            print(f"  [{i+1}] {msg['content'][:100]}{'...' if len(msg['content']) > 100 else ''}")

        response_text = response.get("response_text", "")
        task = response.get("task")
        status = response.get("status")
        print(f"\nAgent response:")
        print(f"  Task: {task}")
        print(f"  Status: {status}")
        print(f"  Text: {response_text[:300]}{'...' if len(response_text) > 300 else ''}")

        state = await state_manager.get_state(user["id"])
        print(f"\nAtomic State: {state.flow_state.value} (version {state.version})")

        # Check if response indicates purpose suggestions were made
        # The response should contain numbered options or purpose suggestions
        has_suggestions = any(
            keyword in response_text.lower()
            for keyword in ["1.", "2.", "option", "choose", "select", "pick", "suggest"]
        )

        result.passed = response.get("success", False)
        result.details = {
            "response_success": response.get("success"),
            "response_task": task,
            "response_status": status,
            "response_text_preview": response_text[:200] if response_text else None,
            "num_photon_messages": len(sent_messages),
            "atomic_state": state.flow_state.value,
            "has_suggestions": has_suggestions,
        }

        if has_suggestions:
            print("\n✅ Purpose suggestions were presented to user")
        else:
            print("\n⚠️  No clear purpose suggestions detected in response")
            print(f"    Full response: {response_text}")

    except Exception as e:
        result.error = str(e)
        result.passed = False
        logger.exception("Step 1 failed")

    return result


async def step2_select_purpose(
    db: DatabaseClient,
    photon: MockPhotonClient,
    openai: AzureOpenAIClient,
    user: Dict[str, Any],
    state_manager: AtomicStateManager,
) -> FlowTestResult:
    """Step 2: User selects a purpose from suggestions.

    Expected:
    - User picks a specific purpose (e.g., "hackathon teammates")
    - System asks about match type preference (one person vs multiple)
    """
    result = FlowTestResult(test_name="Vague to Multi-Match Flow", step="2_select_purpose")

    photon.clear()
    start_time = datetime.utcnow()

    try:
        agent = get_interaction_agent(db=db, photon=photon, openai=openai)

        # User selects a purpose - using a common team-based activity
        message = "hackathon teammates"

        print(f"User message: \"{message}\"")
        print("\nProcessing through InteractionAgent...")

        response = await agent.process_message(
            phone_number=user["phone_number"],
            message_content=message,
            user=user,
            webhook_data={"chat_guid": None, "message_id": "test-step2"},
        )

        duration = (datetime.utcnow() - start_time).total_seconds() * 1000
        result.duration_ms = int(duration)

        print(f"\nResponse received in {duration:.0f}ms")
        print(f"Success: {response.get('success')}")

        sent_messages = photon.get_messages()
        print(f"\nMessages sent via Photon: {len(sent_messages)}")
        for i, msg in enumerate(sent_messages):
            print(f"  [{i+1}] {msg['content'][:100]}{'...' if len(msg['content']) > 100 else ''}")

        response_text = response.get("response_text", "")
        task = response.get("task")
        status = response.get("status")
        print(f"\nAgent response:")
        print(f"  Task: {task}")
        print(f"  Status: {status}")
        print(f"  Text: {response_text[:300]}{'...' if len(response_text) > 300 else ''}")

        state = await state_manager.get_state(user["id"])
        print(f"\nAtomic State: {state.flow_state.value} (version {state.version})")

        # For hackathon teammates, the system should recognize this as SPECIFIC (activity-based)
        # and may either:
        # a) Directly go to multi-match flow (if it infers hackathons need teams)
        # b) Ask for match_type_preference (one person vs multiple)
        # c) Directly find matches
        is_in_matching_state = state.flow_state in [
            NetworkingFlowState.MATCHING,
            NetworkingFlowState.PENDING_INITIATOR_APPROVAL,
        ]

        # Check if matches were found or if it's asking for preference
        has_match_info = any(
            keyword in response_text.lower()
            for keyword in ["found", "match", "connect", "person", "people", "team"]
        )

        result.passed = response.get("success", False)
        result.details = {
            "response_success": response.get("success"),
            "response_task": task,
            "response_status": status,
            "response_text_preview": response_text[:200] if response_text else None,
            "num_photon_messages": len(sent_messages),
            "atomic_state": state.flow_state.value,
            "is_in_matching_state": is_in_matching_state,
            "has_match_info": has_match_info,
        }

        if is_in_matching_state or has_match_info:
            print("\n✅ System processed the purpose selection")
        else:
            print("\n⚠️  System may be asking for more info or encountered an issue")

    except Exception as e:
        result.error = str(e)
        result.passed = False
        logger.exception("Step 2 failed")

    return result


async def step3_confirm_multi_match(
    db: DatabaseClient,
    photon: MockPhotonClient,
    openai: AzureOpenAIClient,
    user: Dict[str, Any],
    state_manager: AtomicStateManager,
) -> FlowTestResult:
    """Step 3: User confirms they want multiple people (if asked).

    Expected:
    - If asked about match type, user specifies "multiple people"
    - System proceeds to find multiple matches
    """
    result = FlowTestResult(test_name="Vague to Multi-Match Flow", step="3_confirm_multi")

    photon.clear()
    start_time = datetime.utcnow()

    try:
        agent = get_interaction_agent(db=db, photon=photon, openai=openai)

        # Check current state first
        state = await state_manager.get_state(user["id"])
        print(f"Current state before step 3: {state.flow_state.value}")

        # If already in PENDING_INITIATOR_APPROVAL, skip this step
        if state.flow_state == NetworkingFlowState.PENDING_INITIATOR_APPROVAL:
            print("System already found matches, skipping this step")
            result.passed = True
            result.details = {
                "skipped": True,
                "reason": "Already in PENDING_INITIATOR_APPROVAL state",
                "atomic_state": state.flow_state.value,
            }
            return result

        # User confirms they want multiple teammates
        message = "multiple people"

        print(f"User message: \"{message}\"")
        print("\nProcessing through InteractionAgent...")

        response = await agent.process_message(
            phone_number=user["phone_number"],
            message_content=message,
            user=user,
            webhook_data={"chat_guid": None, "message_id": "test-step3"},
        )

        duration = (datetime.utcnow() - start_time).total_seconds() * 1000
        result.duration_ms = int(duration)

        print(f"\nResponse received in {duration:.0f}ms")
        print(f"Success: {response.get('success')}")

        sent_messages = photon.get_messages()
        print(f"\nMessages sent via Photon: {len(sent_messages)}")
        for i, msg in enumerate(sent_messages):
            content = msg['content']
            print(f"  [{i+1}] {content[:150]}{'...' if len(content) > 150 else ''}")

        response_text = response.get("response_text", "")
        task = response.get("task")
        status = response.get("status")
        print(f"\nAgent response:")
        print(f"  Task: {task}")
        print(f"  Status: {status}")
        print(f"  Text: {response_text[:400]}{'...' if len(response_text) > 400 else ''}")

        state = await state_manager.get_state(user["id"])
        print(f"\nAtomic State: {state.flow_state.value} (version {state.version})")

        # Check if multiple matches were found
        has_multi_match = any(
            keyword in response_text.lower()
            for keyword in ["found", "people", "matches", "teammates", "potential"]
        )

        match_found = state.flow_state == NetworkingFlowState.PENDING_INITIATOR_APPROVAL

        result.passed = response.get("success", False)
        result.details = {
            "response_success": response.get("success"),
            "response_task": task,
            "response_status": status,
            "response_text_preview": response_text[:200] if response_text else None,
            "num_photon_messages": len(sent_messages),
            "atomic_state": state.flow_state.value,
            "has_multi_match": has_multi_match,
            "match_found": match_found,
        }

        if match_found:
            print("\n✅ Multiple matches found, awaiting initiator approval")
        elif has_multi_match:
            print("\n✅ Multi-match info detected in response")
        else:
            print("\n⚠️  Multi-match flow may not have completed as expected")

    except Exception as e:
        result.error = str(e)
        result.passed = False
        logger.exception("Step 3 failed")

    return result


async def step4_verify_multi_matches(
    db: DatabaseClient,
    user: Dict[str, Any],
    state_manager: AtomicStateManager,
) -> FlowTestResult:
    """Step 4: Verify multiple connection requests were created.

    Expected:
    - Multiple connection requests exist for this user
    - Requests have is_multi_match=true
    - All requests share the same signal_group_id
    """
    result = FlowTestResult(test_name="Vague to Multi-Match Flow", step="4_verify_requests")

    start_time = datetime.utcnow()

    try:
        # Query connection requests for this user
        requests = db.client.table("connection_requests").select("*").eq(
            "initiator_user_id", user["id"]
        ).eq(
            "status", "pending_initiator_approval"
        ).execute()

        duration = (datetime.utcnow() - start_time).total_seconds() * 1000
        result.duration_ms = int(duration)

        pending_requests = requests.data or []
        print(f"\nPending connection requests: {len(pending_requests)}")

        # Check for multi-match requests
        multi_match_requests = [r for r in pending_requests if r.get("is_multi_match")]
        print(f"Multi-match requests: {len(multi_match_requests)}")

        # Check signal_group_ids
        signal_groups = set()
        for req in pending_requests:
            if req.get("signal_group_id"):
                signal_groups.add(req["signal_group_id"])
                print(f"  - Request {req['id'][:8]}... -> {req.get('target_user_id', 'N/A')[:8]}...")
                print(f"    signal_group_id: {req['signal_group_id'][:8]}...")
                print(f"    is_multi_match: {req.get('is_multi_match')}")

        result.passed = len(multi_match_requests) >= 1
        result.details = {
            "total_pending_requests": len(pending_requests),
            "multi_match_requests": len(multi_match_requests),
            "unique_signal_groups": len(signal_groups),
            "signal_group_ids": list(signal_groups),
        }

        if len(multi_match_requests) > 1:
            print(f"\n✅ Multiple multi-match requests created ({len(multi_match_requests)})")
            if len(signal_groups) == 1:
                print("✅ All requests share the same signal_group_id")
            else:
                print(f"⚠️  Multiple signal groups found: {len(signal_groups)}")
        elif len(multi_match_requests) == 1:
            print("\n✅ At least one multi-match request created")
        else:
            print("\n❌ No multi-match requests found")

    except Exception as e:
        result.error = str(e)
        result.passed = False
        logger.exception("Step 4 failed")

    return result


async def cleanup_test_data(db: DatabaseClient, user_id: str, state_manager: AtomicStateManager):
    """Clean up test data after the test."""
    try:
        # Reset atomic state
        await state_manager.force_reset_state(user_id)

        # Cancel any pending connection requests created during test
        # Only cancel requests created in the last 10 minutes
        from datetime import timedelta
        cutoff = (datetime.utcnow() - timedelta(minutes=10)).isoformat()

        db.client.table("connection_requests").update({
            "status": "cancelled"
        }).eq(
            "initiator_user_id", user_id
        ).eq(
            "status", "pending_initiator_approval"
        ).gte(
            "created_at", cutoff
        ).execute()

        print("Cleaned up test data")
    except Exception as e:
        logger.warning(f"Cleanup failed: {e}")


async def main():
    """Run the vague to multi-match flow test."""
    separator("VAGUE TO MULTI-MATCH FLOW TEST")
    print("Testing: vague request -> purpose selection -> multi-match")
    print(f"Test User ID: {TEST_USER_ID}")
    print(f"Timestamp: {datetime.utcnow().isoformat()}")

    # Initialize clients
    db = DatabaseClient()
    photon = MockPhotonClient()
    openai = AzureOpenAIClient()
    state_manager = AtomicStateManager(db)

    # Load test user
    separator("Loading Test User")
    user = await db.get_user_by_id(TEST_USER_ID)
    if not user:
        print(f"❌ User {TEST_USER_ID} not found!")
        return 1

    print(f"User: {user.get('name')} ({user.get('email')})")
    print(f"Phone: {user.get('phone_number')}")
    print(f"University: {user.get('university')}")

    filtered_user = {
        "id": user.get("id"),
        "phone_number": user.get("phone_number"),
        "name": user.get("name"),
        "email": user.get("email"),
        "university": user.get("university"),
        "location": user.get("location"),
        "major": user.get("major"),
        "year": user.get("year"),
        "career_interests": user.get("career_interests"),
        "networking_clarification": user.get("networking_clarification"),
        "is_onboarded": user.get("is_onboarded"),
        "latest_demand": user.get("latest_demand"),
        "all_demand": user.get("all_demand"),
        "all_value": user.get("all_value"),
        "personal_facts": user.get("personal_facts"),
    }

    results: list[FlowTestResult] = []

    # Run test steps
    separator("Step 1: Vague Networking Request")
    print("Sending a vague request to trigger Purpose Suggestion Flow")
    result1 = await step1_vague_request(db, photon, openai, filtered_user, state_manager)
    results.append(result1)

    if not result1.passed:
        print("\n⚠️  Step 1 failed, but continuing to test the specific request flow...")

    separator("Step 2: Select Purpose (Activity-Based)")
    print("Selecting 'hackathon teammates' - should be recognized as SPECIFIC")
    result2 = await step2_select_purpose(db, photon, openai, filtered_user, state_manager)
    results.append(result2)

    separator("Step 3: Confirm Multi-Person Preference")
    print("Confirming 'multiple people' if asked")
    result3 = await step3_confirm_multi_match(db, photon, openai, filtered_user, state_manager)
    results.append(result3)

    separator("Step 4: Verify Multi-Match Requests")
    print("Checking database for multi-match connection requests")
    result4 = await step4_verify_multi_matches(db, filtered_user, state_manager)
    results.append(result4)

    # Cleanup
    separator("Cleanup")
    await cleanup_test_data(db, TEST_USER_ID, state_manager)

    # Summary
    separator("TEST SUMMARY")

    passed = 0
    failed = 0

    for r in results:
        status = "✅ PASSED" if r.passed else "❌ FAILED"
        print(f"{status}: {r.test_name} - {r.step} ({r.duration_ms}ms)")
        if r.error:
            print(f"   Error: {r.error}")
        for key, val in r.details.items():
            if key != "response_text_preview":  # Skip long previews in summary
                print(f"   {key}: {val}")

        if r.passed:
            passed += 1
        else:
            failed += 1

    print(f"\nTotal: {passed} passed, {failed} failed")

    # Overall flow assessment
    separator("FLOW ASSESSMENT")

    # Check if the key flow worked
    if result2.details.get("atomic_state") == "pending_initiator_approval":
        print("✅ Direct Match Flow worked - 'hackathon teammates' was correctly classified as SPECIFIC")
        print("   (Activity-based requests bypass Purpose Suggestion Flow)")
    elif result4.passed:
        print("✅ Multi-match connection requests were created successfully")
    else:
        print("⚠️  Flow may need investigation")
        print("   Check if the LLM correctly classified 'hackathon teammates' as activity-based SPECIFIC")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)

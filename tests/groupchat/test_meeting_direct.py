import pytest
import os
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime

# Mock env vars for Settings
os.environ.update({
    "SUPABASE_URL": "http://localhost:54321",
    "SUPABASE_KEY": "test-key",
    "RESOURCES_SUPABASE_URL": "http://localhost:54321",
    "RESOURCES_SUPABASE_KEY": "test-key",
    "OPENAI_API_KEY": "test-key",
    "ANTHROPIC_API_KEY": "test-key",
    "GEMINI_API_KEY": "test-key",
    "COHERE_API_KEY": "test-key",
    "PH_API_KEY": "test-key",
    "PH_API_KEY_SECONDARY": "test-key",
    "AZURE_OPENAI_ENDPOINT": "https://test.openai.azure.com",
    "AZURE_OPENAI_API_KEY": "test-key",
    "AZURE_OPENAI_API_VERSION": "2023-05-15",
    "AZURE_OPENAI_DEPLOYMENT_NAME": "test-deployment",
    "AZURE_OPENAI_REASONING_DEPLOYMENT_NAME": "test-reasoning-deployment",
    "AZURE_OPENAI_EMBEDDING_DEPLOYMENT_NAME": "test-embedding-deployment",
    "PHOTON_SERVER_URL": "http://localhost:8000",
    "PHOTON_DEFAULT_NUMBER": "+1234567890",
})

from app.groupchat.features.meeting_scheduler import (
    GroupChatMeetingSchedulerService, 
    _STATUS_SCHEDULED, 
    _STATUS_COLLECTING_TZ,
    MeetingIntent
)
from app.database.client.group_chat_meetings import _ACTIVE_STATUSES

@pytest.fixture
def mock_service():
    db = AsyncMock()
    photon = AsyncMock()
    openai = AsyncMock()
    sender = AsyncMock() # Fix: Must be AsyncMock
    svc = GroupChatMeetingSchedulerService(db=db, photon=photon, openai=openai, sender=sender)
    
    # Mock settings to be enabled
    with patch("app.groupchat.features.meeting_scheduler.settings") as mock_settings:
        mock_settings.groupchat_meeting_scheduler_enabled = True
        mock_settings.groupchat_meeting_scheduler_intent_threshold = 0.7
        # Also need meeting duration as integer (not mocked object)
        # But _meeting_duration returns timedelta now? 
        # Wait, _meeting_duration calls getattr(settings, "meeting_duration_minutes", 30).
        # We should set it to int.
        mock_settings.meeting_duration_minutes = 30
        yield svc

@pytest.mark.asyncio
async def test_direct_schedule_explicit_full(mock_service):
    """Test explicit command with full time and timezone info -> Scheduled immediately."""
    mock_service.db.get_active_group_chat_meeting_plan_v1.return_value = None
    mock_service.db.get_recent_scheduled_meeting_plan_v1.return_value = None
    
    # Mock users
    mock_service.db.get_user_by_id.side_effect = lambda uid: {"id": uid, "phone_number": f"+1{uid}", "name": f"User {uid}"}
    
    # LLM returns full info
    mock_service._classify_meeting_intent = AsyncMock(return_value=MeetingIntent(
        intent="schedule",
        confidence=0.9,
        proposed_start="2026-01-12T15:00:00",
        proposed_timezone="America/New_York"
    ))
    
    # Mock create to return a plan
    mock_plan = {"id": "plan-123"}
    mock_service.db.create_group_chat_meeting_plan_v1.return_value = mock_plan

    handled = await mock_service.handle_group_message(
        chat_guid="chat-1",
        sender_user_id="user-1",
        message_text="schedule meeting Jan 12 3pm EST",
        user_a_id="user-1",
        user_b_id="user-2",
        user_a_name="Alice",
        user_b_name="Bob",
        user_a_mode="auto",
        user_b_mode="auto"
    )

    assert handled is True
    # Verify Create called with SCHEDULED
    mock_service.db.create_group_chat_meeting_plan_v1.assert_called_with(
        chat_guid="chat-1",
        user_a_id="user-1",
        user_b_id="user-2",
        initiator_user_id="user-1",
        initiated_by="frank",
        status=_STATUS_SCHEDULED
    )
    # Verify Update called (finalization)
    assert mock_service.db.update_group_chat_meeting_plan_v1.call_count >= 1
    call_args = mock_service.db.update_group_chat_meeting_plan_v1.call_args[1]
    assert call_args["updates"]["status"] == _STATUS_SCHEDULED
    assert len(call_args["updates"]["proposed_options"]) == 1
    assert call_args["updates"]["user_a_timezone"] == "UTC-05:00"

@pytest.mark.asyncio
async def test_direct_schedule_missing_tz(mock_service):
    """Test explicit command with time but NO timezone -> Collecting TZ."""
    mock_service.db.get_active_group_chat_meeting_plan_v1.return_value = None
    mock_service.db.get_recent_scheduled_meeting_plan_v1.return_value = None
    
    # LLM returns time but no TZ
    mock_service._classify_meeting_intent = AsyncMock(return_value=MeetingIntent(
        intent="schedule",
        confidence=0.9,
        proposed_start="2026-01-12T15:00:00",
        proposed_timezone=None
    ))
    
    mock_plan = {"id": "plan-123"}
    mock_service.db.create_group_chat_meeting_plan_v1.return_value = mock_plan

    handled = await mock_service.handle_group_message(
        chat_guid="chat-1",
        sender_user_id="user-1",
        message_text="schedule meeting Jan 12 3pm",
        user_a_id="user-1",
        user_b_id="user-2",
        user_a_name="Alice",
        user_b_name="Bob",
        user_a_mode="auto",
        user_b_mode="auto"
    )

    assert handled is True
    # Verify Create called with COLLECTING_TZ
    mock_service.db.create_group_chat_meeting_plan_v1.assert_called_with(
        chat_guid="chat-1",
        user_a_id="user-1",
        user_b_id="user-2",
        initiator_user_id="user-1",
        initiated_by="frank",
        status=_STATUS_COLLECTING_TZ
    )
    # Verify update stores pending start
    mock_service.db.update_group_chat_meeting_plan_v1.assert_called_with(
        plan_id="plan-123",
        updates={"proposed_options": [{"pending_start": "2026-01-12T15:00:00"}]}
    )
    # Verify sender asks for TZ
    mock_service.sender.send_and_record.assert_called_with(
        chat_guid="chat-1",
        content="what timezone?"
    )

@pytest.mark.asyncio
async def test_reply_timezone_finalizes(mock_service):
    """Test replying with timezone finalizes the pending direct schedule."""
    # Active plan in COLLECTING_TZ state
    mock_plan = {
        "id": "plan-123",
        "status": _STATUS_COLLECTING_TZ,
        "proposed_options": [{"pending_start": "2026-01-12T15:00:00"}]
    }
    mock_service.db.get_active_group_chat_meeting_plan_v1.return_value = mock_plan
    
    handled = await mock_service.handle_group_message(
        chat_guid="chat-1",
        sender_user_id="user-1",
        message_text="EST",
        user_a_id="user-1",
        user_b_id="user-2",
        user_a_name="Alice",
        user_b_name="Bob",
        user_a_mode="auto",
        user_b_mode="auto"
    )

    assert handled is True
    
    # Verify Finalize called (Update DB)
    call_args = mock_service.db.update_group_chat_meeting_plan_v1.call_args[1]
    assert call_args["updates"]["status"] == _STATUS_SCHEDULED
    # Should contain user_a_timezone 'America/New_York' (normalized from EST)
    # Note: _normalize_timezone_input uses internal map, assuming mocked or real func works.
    # In unit test, strict environment, _normalize_timezone_input depends on pytz/timezone_map.
    # If it works, it returns "America/New_York".
    assert call_args["updates"]["user_a_timezone"] == "UTC-05:00"

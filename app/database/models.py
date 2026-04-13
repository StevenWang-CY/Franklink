"""Database models using Pydantic."""

from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
from uuid import UUID, uuid4
from enum import Enum


class MessageType(str, Enum):
    """Message type enumeration."""
    USER = "user"
    BOT = "bot"


class ActiveResourceStatus(str, Enum):
    """User active resource interaction status (DEPRECATED - use InteractionType instead)."""
    RECOMMENDED = "recommended"
    INTERESTED = "interested"
    NOT_INTERESTED = "not_interested"
    ATTENDED = "attended"
    SHARED = "shared"  # User shared this resource

# Keep alias for backward compatibility
OpportunityStatus = ActiveResourceStatus


class ReminderType(str, Enum):
    """Reminder type enumeration."""
    TWENTY_FOUR_HOURS = "24h_before"
    TWO_HOURS = "2h_before"
    FOLLOW_UP = "follow_up"


class ReminderStatus(str, Enum):
    """Reminder status enumeration."""
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


class ConfirmationStatus(str, Enum):
    """Pending opportunity confirmation status."""
    PENDING = "pending"
    CONFIRMED = "confirmed"
    DECLINED = "declined"
    EXPIRED = "expired"


class User(BaseModel):
    """User profile model."""
    id: UUID = Field(default_factory=uuid4)
    phone_number: str
    name: Optional[str] = None
    email: Optional[str] = None
    demand_history: List[Dict[str, Any]] = Field(default_factory=list)
    value_history: List[Dict[str, Any]] = Field(default_factory=list)
    latest_demand: Optional[str] = None
    all_demand: Optional[str] = None
    all_value: Optional[str] = None
    intro_fee_cents: Optional[int] = None
    university: Optional[str] = None
    location: Optional[str] = None
    major: Optional[str] = None
    year: Optional[int] = None
    career_interests: List[str] = Field(default_factory=list)
    needs: List[Any] = Field(default_factory=list)  # Agent-inferred career needs/context
    personal_facts: Dict[str, Any] = Field(default_factory=dict)  # Flexible storage for ALL personal information
    networking_clarification: Optional[Dict[str, Any]] = None
    is_onboarded: bool = False
    metadata: Dict[str, Any] = Field(default_factory=dict)

    # LinkedIn Profile Integration Fields
    linkedin_url: Optional[str] = None
    linkedin_data: Optional[Dict[str, Any]] = None
    grade_level: Optional[str] = None  # 'freshman', 'sophomore', 'junior', 'senior', 'graduate', 'alumni'
    linkedin_scraped_at: Optional[datetime] = None
    linkedin_scrape_status: Optional[str] = "pending"  # 'pending', 'success', 'failed', 'skipped'
    linkedin_scrape_error: Optional[Dict[str, Any]] = None

    # Cold Email & Matching Fields
    offer_status: str = "N/A"  # "N/A" or "Yes - [Company Name]"

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    class Config:
        json_encoders = {
            UUID: str,
            datetime: lambda v: v.isoformat()
        }


class Conversation(BaseModel):
    """Conversation message model."""
    id: UUID = Field(default_factory=uuid4)
    user_id: UUID
    message_type: MessageType
    content: str
    metadata: Dict[str, Any] = Field(default_factory=dict)
    topics: List[str] = Field(default_factory=list)  # Extracted conversation topics
    topic_scores: Dict[str, float] = Field(default_factory=dict)  # Topic confidence scores
    created_at: datetime = Field(default_factory=datetime.utcnow)

    class Config:
        json_encoders = {
            UUID: str,
            datetime: lambda v: v.isoformat()
        }


class ActiveResource(BaseModel):
    """Active resource model (time-sensitive opportunities like jobs and events)."""
    id: UUID = Field(default_factory=uuid4)
    title: str
    description: Optional[str] = None
    organization: Optional[str] = None
    location: Optional[str] = None
    event_date: Optional[datetime] = None
    deadline: Optional[datetime] = None
    tags: List[str] = Field(default_factory=list)
    source: Optional[str] = None
    source_url: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.utcnow)

    class Config:
        json_encoders = {
            UUID: str,
            datetime: lambda v: v.isoformat()
        }

# Keep alias for backward compatibility
Opportunity = ActiveResource


class UserActiveResource(BaseModel):
    """User-active resource interaction model (DEPRECATED - use UserResourceInteraction instead)."""
    id: UUID = Field(default_factory=uuid4)
    user_id: UUID
    active_resource_id: UUID  # Renamed from active_resource_id
    status: ActiveResourceStatus
    feedback: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    class Config:
        json_encoders = {
            UUID: str,
            datetime: lambda v: v.isoformat()
        }

# Keep alias for backward compatibility
UserOpportunity = UserActiveResource


class Reminder(BaseModel):
    """Reminder model."""
    id: UUID = Field(default_factory=uuid4)
    user_id: UUID
    active_resource_id: UUID  # Renamed from active_resource_id
    reminder_time: datetime
    reminder_type: ReminderType
    status: ReminderStatus = ReminderStatus.PENDING
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.utcnow)

    class Config:
        json_encoders = {
            UUID: str,
            datetime: lambda v: v.isoformat()
        }


class UserProfileUpdate(BaseModel):
    """Model for updating user profile."""
    name: Optional[str] = None
    demand_history: Optional[List[Dict[str, Any]]] = None
    value_history: Optional[List[Dict[str, Any]]] = None
    latest_demand: Optional[str] = None
    all_demand: Optional[str] = None
    all_value: Optional[str] = None
    intro_fee_cents: Optional[int] = None
    university: Optional[str] = None
    location: Optional[str] = None
    major: Optional[str] = None
    year: Optional[int] = None
    career_interests: Optional[List[str]] = None
    is_onboarded: Optional[bool] = None
    networking_clarification: Optional[Dict[str, Any]] = None
    metadata: Optional[Dict[str, Any]] = None


class ConversationContext(BaseModel):
    """Model for conversation context."""
    user_id: UUID
    recent_messages: List[Conversation]
    current_intent: Optional[str] = None
    session_metadata: Dict[str, Any] = Field(default_factory=dict)

    class Config:
        json_encoders = {
            UUID: str,
            datetime: lambda v: v.isoformat()
        }


class ActiveResourceMatch(BaseModel):
    """Model for active resource matching results."""
    active_resource: ActiveResource
    match_score: float = Field(ge=0.0, le=1.0)
    match_reasons: List[str] = Field(default_factory=list)
    relevance_explanation: Optional[str] = None

    class Config:
        json_encoders = {
            UUID: str,
            datetime: lambda v: v.isoformat()
        }

# Keep alias for backward compatibility
OpportunityMatch = ActiveResourceMatch


class PendingSharedResource(BaseModel):
    """Model for pending shared resource awaiting user confirmation."""
    id: UUID = Field(default_factory=uuid4)
    user_id: UUID
    original_message: str
    extracted_url: str
    extracted_title: Optional[str] = None
    extracted_organization: Optional[str] = None
    extracted_description: Optional[str] = None
    extraction_metadata: Dict[str, Any] = Field(default_factory=dict)
    confirmation_status: ConfirmationStatus = ConfirmationStatus.PENDING
    confirmation_message_id: Optional[UUID] = None
    expires_at: datetime
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    class Config:
        json_encoders = {
            UUID: str,
            datetime: lambda v: v.isoformat()
        }

# Keep alias for backward compatibility
PendingSharedOpportunity = PendingSharedResource


class OutreachType(str, Enum):
    """Types of proactive outreach Frank can initiate."""
    NEW_OPPORTUNITY = "new_opportunity"
    DEADLINE_REMINDER = "deadline_reminder"
    FOLLOW_UP = "follow_up"


class ProactiveOutreach(BaseModel):
    """Model for tracking proactive messages Frank initiates."""
    id: UUID = Field(default_factory=uuid4)
    user_id: UUID
    outreach_type: OutreachType
    active_resource_id: Optional[UUID] = None  # Renamed from active_resource_id
    message_sent: str
    sent_at: datetime = Field(default_factory=datetime.utcnow)
    user_responded: bool = False
    responded_at: Optional[datetime] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.utcnow)

    # New fields for delivery tracking
    delivery_status: str = "success"  # "success", "failed", "pending"
    error_details: Optional[Dict[str, Any]] = None
    delivery_verified_at: Optional[datetime] = None

    class Config:
        json_encoders = {
            UUID: str,
            datetime: lambda v: v.isoformat()
        }


class UserEngagementStatus(BaseModel):
    """Model for tracking user engagement to prevent spam."""
    id: UUID = Field(default_factory=uuid4)
    user_id: UUID
    consecutive_no_responses: int = 0
    last_proactive_outreach: Optional[datetime] = None
    last_user_message: Optional[datetime] = None
    is_spam_protected: bool = False
    total_proactive_sent: int = 0
    total_responses: int = 0
    engagement_rate: float = 0.0  # Percentage 0-100
    proactive_messages_this_week: int = 0
    week_reset_at: datetime = Field(default_factory=datetime.utcnow)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    class Config:
        json_encoders = {
            UUID: str,
            datetime: lambda v: v.isoformat()
        }


class ConnectionStatus(str, Enum):
    """Cold email connection status enumeration."""
    DRAFT = "draft"
    APPROVED = "approved"
    SENT = "sent"
    RESPONDED = "responded"
    DECLINED = "declined"
    CANCELLED = "cancelled"


class ResponseStatus(str, Enum):
    """Cold email response status enumeration."""
    PENDING = "pending"
    REPLIED = "replied"
    BOUNCED = "bounced"
    UNSUBSCRIBED = "unsubscribed"


class ColdEmailMatch(BaseModel):
    """Model for cold email connection requests and tracking."""
    id: UUID = Field(default_factory=uuid4)
    user_id: UUID
    target_user_id: UUID

    # Email content and status
    email_draft: str
    email_subject: str
    connection_status: ConnectionStatus = ConnectionStatus.DRAFT

    # Matching metadata
    match_score: Optional[float] = Field(None, ge=0.0, le=1.0)
    matching_reasons: List[str] = Field(default_factory=list)

    # Email sending metadata
    email_sent_at: Optional[datetime] = None
    gmail_message_id: Optional[str] = None

    # Response tracking
    response_received_at: Optional[datetime] = None
    response_status: ResponseStatus = ResponseStatus.PENDING
    response_content: Optional[str] = None

    # Revision tracking
    revision_count: int = 0
    revision_history: List[Dict[str, Any]] = Field(default_factory=list)

    # Analytics and metadata
    metadata: Dict[str, Any] = Field(default_factory=dict)

    # Timestamps
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    class Config:
        json_encoders = {
            UUID: str,
            datetime: lambda v: v.isoformat()
        }


class UserMatch(BaseModel):
    """Model for user matching results (used in matching algorithm)."""
    user: User
    match_score: float = Field(ge=0.0, le=1.0)
    matching_reasons: List[str] = Field(default_factory=list)
    has_offer: bool = False
    offer_company: Optional[str] = None

    class Config:
        json_encoders = {
            UUID: str,
            datetime: lambda v: v.isoformat()
        }


class ConnectionRequestStatus(str, Enum):
    """Connection request status enumeration for handshake flow."""
    PENDING_INITIATOR_APPROVAL = "pending_initiator_approval"
    PENDING_TARGET_APPROVAL = "pending_target_approval"
    TARGET_DECLINED = "target_declined"
    TARGET_ACCEPTED = "target_accepted"
    GROUP_CREATED = "group_created"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class ConnectionRequest(BaseModel):
    """Model for connection requests between users (handshake flow)."""
    id: UUID = Field(default_factory=uuid4)
    initiator_user_id: UUID
    target_user_id: UUID
    match_score: Optional[float] = Field(None, ge=0.0, le=1.0)
    matching_reasons: List[str] = Field(default_factory=list)
    llm_introduction: Optional[str] = None
    llm_concern: Optional[str] = None
    status: ConnectionRequestStatus = ConnectionRequestStatus.PENDING_INITIATOR_APPROVAL
    target_notified_at: Optional[datetime] = None
    target_responded_at: Optional[datetime] = None
    group_chat_guid: Optional[str] = None
    excluded_candidates: List[UUID] = Field(default_factory=list)
    expires_at: datetime = Field(default_factory=lambda: datetime.utcnow() + timedelta(days=3))
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    class Config:
        json_encoders = {
            UUID: str,
            datetime: lambda v: v.isoformat()
        }


class GroupChatMode(str, Enum):
    """User mode in a group chat."""
    ACTIVE = "active"
    QUIET = "quiet"
    MUTED = "muted"


class GroupChat(BaseModel):
    """Model for Frank-managed group chats."""
    id: UUID = Field(default_factory=uuid4)
    chat_guid: str
    user_a_id: UUID
    user_b_id: UUID
    user_a_mode: GroupChatMode = GroupChatMode.ACTIVE
    user_b_mode: GroupChatMode = GroupChatMode.ACTIVE
    connection_request_id: Optional[UUID] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)

    class Config:
        json_encoders = {
            UUID: str,
            datetime: lambda v: v.isoformat()
        }

"""State schemas for Frank's graphs (onboarding, recommendation, networking, general)."""

from typing import TypedDict, Literal, Optional, List, Dict, Any, Annotated
from datetime import datetime
from operator import add


# Onboarding stages
# Note: stage is persisted in user_profile.personal_facts["onboarding_stage"].
OnboardingStage = Literal[
    "value_eval",
    "needs_eval",
    "rejected",
    "name",
    "school",
    "career_interest",
    "email_connect",
    "complete",
]

# Intent types
IntentType = Literal["general", "recommendation", "onboarding", "networking", "update"]

# Waiting states
WaitingFor = Literal[
    "user_input",
    "career_interest",
    "school",
    "email_connect",
    "email_confirmation",  # networking draft confirmation (legacy)
    "initiator_confirmation",  # waiting for User A to confirm match
    "target_response",  # waiting for User B to accept/decline
    "networking_clarification",  # waiting for networking demand clarification
    None,
]

# Subscription (kept for future payment use)
PricingTier = Literal["free", "premium", "enterprise"]
SubscriptionStatus = Literal["active", "canceled", "past_due", "trialing", "incomplete"]


class UserProfileState(TypedDict, total=False):
    """User profile information - persisted across sessions."""

    phone_number: str
    user_id: str
    name: Optional[str]
    email: Optional[str]
    linkedin_url: Optional[str]
    demand_history: List[Dict[str, str]]
    value_history: List[Dict[str, str]]
    latest_demand: Optional[str]
    all_demand: Optional[str]
    all_value: Optional[str]
    intro_fee_cents: Optional[int]
    university: Optional[str]
    major: Optional[str]
    year: Optional[str]
    career_interests: List[str]
    career_goals: Optional[str]
    needs: List[Any]
    personal_facts: Dict[str, Any]
    networking_clarification: Optional[Dict[str, Any]]
    networking_limitation: Optional[Dict[str, Any]]
    is_onboarded: bool
    onboarding_stage: OnboardingStage
    subscription_tier: PricingTier
    subscription_status: SubscriptionStatus
    stripe_customer_id: Optional[str]
    stripe_subscription_id: Optional[str]
    subscription_started_at: Optional[str]
    subscription_ends_at: Optional[str]
    engagement_rate: float
    consecutive_no_responses: int
    total_interactions: int
    last_check_in: Optional[str]
    created_at: str
    last_interaction: str


class ConversationContext(TypedDict, total=False):
    recent_messages: Annotated[List[Dict[str, Any]], add]
    current_topics: List[str]
    conversation_summary: Optional[str]
    recent_facts: List[Dict[str, Any]]


class PendingAction(TypedDict, total=False):
    action_type: Literal["onboarding_step", "email_confirmation"]
    data: Dict[str, Any]
    expires_at: Optional[str]
    attempts: int


class MessageState(TypedDict, total=False):
    content: str
    from_number: str
    message_id: Optional[str]
    timestamp: str
    chat_guid: Optional[str]
    intent: Optional[IntentType]
    extracted_entities: Dict[str, Any]
    extracted_urls: List[str]
    metadata: Dict[str, Any]


class ResponseState(TypedDict, total=False):
    response_text: Optional[str]
    send_style: Literal["normal", "invisible", "loud", "gentle", "celebration"]
    sent: bool


class TaskItem(TypedDict, total=False):
    intent: IntentType
    task: str
    task_id: Optional[str]
    source: Optional[str]


class TaskResult(TypedDict, total=False):
    intent: IntentType
    task: Optional[str]  # Task identifier (e.g., "onboarding", "networking")
    response_text: Optional[str]
    resource_urls: List[Dict[str, Any]]
    outbound_messages: List[str]
    waiting_for: WaitingFor


class GraphState(TypedDict, total=False):
    user_profile: UserProfileState
    conversation_context: ConversationContext
    current_message: MessageState
    pending_action: Optional[PendingAction]
    waiting_for: WaitingFor
    response: ResponseState
    should_continue: bool
    next_graph: Optional[str]  # DEPRECATED: kept for backward compatibility
    temp_data: Dict[str, Any]
    errors: Annotated[List[str], add]
    graph_metadata: Dict[str, Any]
    task_queue: List[TaskItem]
    active_task: Optional[TaskItem]
    task_results: List[TaskResult]
    original_message: Optional[str]


class OnboardingState(GraphState):
    """State specific to onboarding graph."""
    onboarding_stage: OnboardingStage


class RecommendationState(GraphState):
    """State specific to recommendation graph."""

    parsed_query: Optional[Dict[str, Any]]
    query_filters: Dict[str, Any]
    search_results: List[Dict[str, Any]]
    ranked_results: List[Dict[str, Any]]
    filtered_results: List[Dict[str, Any]]
    recommendations: List[Dict[str, Any]]
    recommended_resource_ids: List[str]


class NetworkingState(GraphState):
    """State specific to networking graph (handshake-based group chat creation)."""

    # Match state
    selected_match: Optional[Dict[str, Any]]
    match_score: Optional[float]
    matching_reasons: Optional[List[str]]
    llm_introduction: Optional[str]
    match_concern: Optional[str]

    # Connection request state
    connection_request_id: Optional[str]
    excluded_candidates: List[str]

    # Target user info (when processing target response)
    pending_request: Optional[Dict[str, Any]]

    # Flow control
    initiator_confirmed: bool
    target_accepted: Optional[bool]

    # Group chat state
    group_chat_guid: Optional[str]


# Helpers
def create_initial_user_profile(phone_number: str, user_id: str) -> UserProfileState:
    return UserProfileState(
        phone_number=phone_number,
        user_id=user_id,
        name=None,
        email=None,
        linkedin_url=None,
        demand_history=[],
        value_history=[],
        latest_demand=None,
        all_demand=None,
        all_value=None,
        intro_fee_cents=None,
        university=None,
        major=None,
        year=None,
        career_interests=[],
        needs=[],
        career_goals=None,
        personal_facts={},
        networking_clarification=None,
        networking_limitation=None,
        is_onboarded=False,
        onboarding_stage="name",
        subscription_tier="free",
        subscription_status="active",
        stripe_customer_id=None,
        stripe_subscription_id=None,
        subscription_started_at=None,
        subscription_ends_at=None,
        engagement_rate=0.0,
        consecutive_no_responses=0,
        total_interactions=0,
        last_check_in=None,
        created_at=datetime.utcnow().isoformat(),
        last_interaction=datetime.utcnow().isoformat(),
    )


def create_initial_conversation_context() -> ConversationContext:
    return ConversationContext(
        recent_messages=[],
        current_topics=[],
        conversation_summary=None,
        recent_facts=[],
    )


def create_message_state(
    content: str,
    from_number: str,
    message_id: Optional[str] = None,
    media_url: Optional[str] = None,
    chat_guid: Optional[str] = None,
) -> MessageState:
    return MessageState(
        content=content,
        from_number=from_number,
        message_id=message_id,
        chat_guid=chat_guid,
        timestamp=datetime.utcnow().isoformat(),
        intent=None,
        extracted_entities={},
        extracted_urls=[],
        metadata={"media_url": media_url} if media_url else {},
    )


def create_initial_graph_state(
    phone_number: str,
    user_id: str,
    message_content: str,
    media_url: Optional[str] = None,
    chat_guid: Optional[str] = None,
    message_id: Optional[str] = None,
) -> GraphState:
    return GraphState(
        user_profile=create_initial_user_profile(phone_number, user_id),
        conversation_context=create_initial_conversation_context(),
        current_message=create_message_state(
            message_content,
            phone_number,
            message_id=message_id,
            media_url=media_url,
            chat_guid=chat_guid,
        ),
        pending_action=None,
        waiting_for=None,
        response=ResponseState(
            response_text=None,
            send_style="normal",
            sent=False,
        ),
        should_continue=True,
        next_graph=None,
        temp_data={},
        errors=[],
        task_queue=[],
        active_task=None,
        task_results=[],
        original_message=message_content,
        graph_metadata={
            "started_at": datetime.utcnow().isoformat(),
            "graph_version": "1.0.0",
        },
    )

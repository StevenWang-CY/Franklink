from __future__ import annotations

from .connection_requests import _ConnectionRequestMethods
from .conversations import _ConversationMethods
from .core import _DatabaseClientCore
from .daily_email_jobs import _DailyEmailJobMethods
from .embeddings import _EmbeddingMethods
from .group_chat import _GroupChatMethods
from .group_chat_followup import _GroupChatFollowupMethods
from .group_chat_summary import _GroupChatSummaryMethods
from .proactive_outreach_jobs import _ProactiveOutreachJobMethods
from .proactive_outreach_tracking import _ProactiveOutreachTrackingMethods
from .user_emails import _UserEmailMethods
from .user_email_signals import _UserEmailSignalMethods
from .user_email_highlights import _UserEmailHighlightMethods
from .user_handle_links import _UserHandleLinkMethods
from .user_locations import _UserLocationMethods
from .user_profiles import _UserProfileMethods
from .users import _UserMethods


class DatabaseClient(
    _DatabaseClientCore,
    _UserMethods,
    _UserProfileMethods,
    _UserLocationMethods,
    _UserHandleLinkMethods,
    _UserEmailMethods,
    _UserEmailHighlightMethods,
    _UserEmailSignalMethods,
    _ConversationMethods,
    _ConnectionRequestMethods,
    _GroupChatMethods,
    _GroupChatFollowupMethods,
    _GroupChatSummaryMethods,
    _DailyEmailJobMethods,
    _ProactiveOutreachJobMethods,
    _ProactiveOutreachTrackingMethods,
    _EmbeddingMethods,
):
    """Client for interacting with Supabase database."""

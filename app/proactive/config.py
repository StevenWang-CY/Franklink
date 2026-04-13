"""Configuration constants for proactive workers."""

# Daily Email Worker
DAILY_EMAIL_RUN_HOUR_UTC = 17  # 5 PM UTC
DAILY_EMAIL_WORKER_MAX_JOBS = 10
DAILY_EMAIL_WORKER_STALE_MINUTES = 30
DAILY_EMAIL_WORKER_MAX_ATTEMPTS = 5
DAILY_EMAIL_WORKER_POLL_SECONDS = 300  # 5 minutes

# Proactive Outreach Worker
PROACTIVE_OUTREACH_RUN_HOUR_UTC = 18  # 6 PM UTC
PROACTIVE_OUTREACH_WORKER_MAX_JOBS = 5
PROACTIVE_OUTREACH_WORKER_STALE_MINUTES = 30
PROACTIVE_OUTREACH_WORKER_MAX_ATTEMPTS = 5
PROACTIVE_OUTREACH_WORKER_POLL_SECONDS = 300  # 5 minutes
PROACTIVE_OUTREACH_COOLDOWN_DAYS = 7

# Proactive signal extraction limits:
# - Proactive outreach extracts multiple networking opportunities ("signals") from Zep
# - Uses ONLY THE FIRST ONE that finds a match (subsequent are fallback)
# Note: Interactive networking (CASE A Purpose Suggestion Flow) uses suggest_connection_purposes()
PROACTIVE_OUTREACH_MAX_SIGNALS = 3

# Multi-match settings
PROACTIVE_MULTI_MATCH_THRESHOLD = 2  # Number of acceptances needed to create group
PROACTIVE_MULTI_MATCH_MAX_TARGETS = 5  # Maximum targets for multi-match

# Location Update Worker
LOCATION_UPDATE_WORKER_POLL_SECONDS = 300  # 5 minutes

# Exponential backoff (in seconds)
BACKOFF_BASE = 1800  # 30 minutes
BACKOFF_CAP = 14400  # 4 hours


def compute_backoff_seconds(attempts: int) -> int:
    """Compute exponential backoff seconds based on attempt count."""
    try:
        n = int(attempts or 0)
    except Exception:
        n = 0
    return min(BACKOFF_CAP, int(BACKOFF_BASE * (2 ** max(0, n))))

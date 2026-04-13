# Email Extraction Function

A standalone, production-ready function for extracting user emails from Gmail via Composio and storing them to the `user_emails` database table.

## Location

```
app/agents/tools/email_extraction.py
```

## Function Signature

```python
async def extract_and_store_emails(
    *,
    user_id: str,
    connected_account_id: Optional[str] = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    raw_query: Optional[str] = None,
    include_sent: bool = True,
    max_received: int = 100,
    max_sent: int = 50,
    timeout_seconds: float = 60.0,
) -> EmailExtractionResult:
```

## Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `user_id` | `str` | **required** | User ID for Composio lookup and database storage |
| `connected_account_id` | `Optional[str]` | `None` | Pre-resolved Composio account ID (skips lookup if provided) |
| `start_date` | `Optional[datetime]` | `None` | Start of date range (inclusive) |
| `end_date` | `Optional[datetime]` | `None` | End of date range (exclusive) |
| `raw_query` | `Optional[str]` | `None` | Raw Gmail query override (takes precedence over dates) |
| `include_sent` | `bool` | `True` | Whether to fetch sent emails |
| `max_received` | `int` | `100` | Maximum received emails to fetch |
| `max_sent` | `int` | `50` | Maximum sent emails to fetch |
| `timeout_seconds` | `float` | `60.0` | Total timeout for fetch operations |

## Return Type

```python
@dataclass
class EmailExtractionResult:
    success: bool                          # Whether extraction completed successfully
    total_fetched: int = 0                 # Total emails fetched from Gmail
    total_stored: int = 0                  # Total new emails stored to database
    duplicates_skipped: int = 0            # Emails skipped (already in database)
    sensitive_filtered: int = 0            # Emails filtered for PII content
    received_count: int = 0                # Received emails stored
    sent_count: int = 0                    # Sent emails stored
    error: Optional[str] = None            # Human-readable error message
    error_code: Optional[str] = None       # Machine-readable error code
    metadata: Dict[str, Any] = field(...)  # Additional info (queries_used, duration_ms)
```

## Time Period Options

The function supports multiple ways to specify the time period:

### Option 1: Datetime objects (recommended for programmatic use)

```python
from datetime import datetime, timedelta

# Last 30 days
result = await extract_and_store_emails(
    user_id="user-123",
    start_date=datetime.now() - timedelta(days=30),
)

# Specific date range
result = await extract_and_store_emails(
    user_id="user-123",
    start_date=datetime(2024, 1, 1),
    end_date=datetime(2024, 6, 1),
)

# Before a specific date (up to 90 days back)
result = await extract_and_store_emails(
    user_id="user-123",
    end_date=datetime(2024, 3, 15),
)
```

### Option 2: Raw Gmail query (maximum flexibility)

```python
# Relative time
result = await extract_and_store_emails(
    user_id="user-123",
    raw_query="newer_than:14d",
)

# Date range with Gmail syntax
result = await extract_and_store_emails(
    user_id="user-123",
    raw_query="after:2024/01/01 before:2024/06/01",
)

# With labels or other filters
result = await extract_and_store_emails(
    user_id="user-123",
    raw_query="label:important newer_than:7d",
)

# Specific sender
result = await extract_and_store_emails(
    user_id="user-123",
    raw_query="from:recruiter@company.com newer_than:30d",
)
```

### Default behavior

If no time period is specified, defaults to `newer_than:90d` (last 90 days).

## Usage Examples

### Basic extraction (last 90 days, inbox + sent)

```python
from app.agents.tools.email_extraction import extract_and_store_emails

result = await extract_and_store_emails(user_id="user-123")

if result.success:
    print(f"Stored {result.total_stored} new emails")
    print(f"Skipped {result.duplicates_skipped} duplicates")
else:
    print(f"Error: {result.error} (code: {result.error_code})")
```

### Inbox only (no sent emails)

```python
result = await extract_and_store_emails(
    user_id="user-123",
    include_sent=False,
    max_received=50,
)
```

### With pre-resolved Composio account (faster)

```python
from app.integrations.composio_client import ComposioClient

composio = ComposioClient()
account_id = await composio.get_connected_account_id(user_id="user-123")

result = await extract_and_store_emails(
    user_id="user-123",
    connected_account_id=account_id,  # Skip lookup
    max_received=25,
    max_sent=10,
)
```

### Custom timeout for slow connections

```python
result = await extract_and_store_emails(
    user_id="user-123",
    timeout_seconds=120.0,  # 2 minute timeout
)
```

### Checking results

```python
result = await extract_and_store_emails(user_id="user-123")

# Check success
if not result.success:
    if result.error_code == "NO_CONNECTED_ACCOUNT":
        # User needs to connect Gmail first
        pass
    elif result.error_code in ("FETCH_TIMEOUT", "COMPOSIO_UNAVAILABLE"):
        # Transient error - can retry
        pass
    else:
        # Other error
        pass

# Access metadata
print(f"Queries used: {result.metadata.get('queries_used')}")
print(f"Duration: {result.metadata.get('duration_ms')}ms")
```

## Error Codes

| Code | Retryable | Description |
|------|-----------|-------------|
| `MISSING_USER_ID` | No | Required parameter missing or empty |
| `INVALID_DATE_RANGE` | No | start_date >= end_date |
| `COMPOSIO_UNAVAILABLE` | Yes | Composio service unavailable |
| `CONNECTION_LOOKUP_FAILED` | Yes | Failed to resolve Composio account |
| `CONNECTION_LOOKUP_TIMEOUT` | Yes | Timeout during account lookup |
| `NO_CONNECTED_ACCOUNT` | No | User hasn't connected Gmail |
| `FETCH_TIMEOUT` | Yes | Email fetch timed out |
| `STORE_FAILED` | Yes | Database storage error |

### Retry logic example

```python
from app.agents.tools.email_extraction import extract_and_store_emails

RETRYABLE_CODES = {
    "COMPOSIO_UNAVAILABLE",
    "CONNECTION_LOOKUP_FAILED",
    "CONNECTION_LOOKUP_TIMEOUT",
    "FETCH_TIMEOUT",
    "STORE_FAILED",
}

async def extract_with_retry(user_id: str, max_retries: int = 3):
    for attempt in range(max_retries):
        result = await extract_and_store_emails(user_id=user_id)

        if result.success:
            return result

        if result.error_code not in RETRYABLE_CODES:
            return result  # Non-retryable error

        # Exponential backoff
        await asyncio.sleep(2 ** attempt)

    return result
```

## Gmail Query Syntax Reference

The `raw_query` parameter accepts standard Gmail search operators:

| Operator | Example | Description |
|----------|---------|-------------|
| `newer_than:Xd` | `newer_than:7d` | Emails from last X days |
| `older_than:Xd` | `older_than:30d` | Emails older than X days |
| `after:YYYY/MM/DD` | `after:2024/01/01` | Emails after date (inclusive) |
| `before:YYYY/MM/DD` | `before:2024/06/01` | Emails before date (exclusive) |
| `from:` | `from:example@gmail.com` | From specific sender |
| `to:` | `to:me@example.com` | To specific recipient |
| `subject:` | `subject:interview` | Subject contains word |
| `label:` | `label:important` | Has specific label |
| `has:attachment` | `has:attachment` | Has attachments |
| `is:unread` | `is:unread` | Unread emails only |

Operators can be combined: `from:recruiter@company.com newer_than:14d has:attachment`

## Database Storage

Emails are stored in the `user_emails` table with the following fields:

| Field | Type | Description |
|-------|------|-------------|
| `id` | UUID | Auto-generated primary key |
| `user_id` | UUID | User foreign key |
| `message_id` | TEXT | Gmail message ID (for deduplication) |
| `sender` | TEXT | Full sender (name + email) |
| `sender_domain` | TEXT | Extracted domain |
| `subject` | TEXT | Email subject |
| `body` | TEXT | Email body (PII scrubbed, max 500 chars) |
| `snippet` | TEXT | Email preview |
| `received_at` | TIMESTAMPTZ | When email was received |
| `fetched_at` | TIMESTAMPTZ | When we fetched it |
| `is_sensitive` | BOOLEAN | PII flag |
| `is_sent` | BOOLEAN | True if sent by user |

### Deduplication

- Emails are deduplicated by `message_id` both in-memory and against existing database records
- Running extraction multiple times is safe - duplicates are automatically skipped
- The `duplicates_skipped` count in the result shows how many were skipped

### PII Handling

- Sensitive content (OTP codes, SSN, medical records, etc.) is automatically filtered
- Email bodies are scrubbed of PII patterns (emails, URLs, phone numbers, API keys)
- Bodies are truncated to 500 characters

## Integration with Existing Code

This function is designed to work alongside the existing email infrastructure:

```python
# Existing email context (for onboarding)
from app.agents.tools.onboarding.email_context import ensure_email_signals

# New standalone extraction (for scheduled jobs, API endpoints, etc.)
from app.agents.tools.email_extraction import extract_and_store_emails
```

Use `extract_and_store_emails` when you need:
- Fine-grained control over time periods
- Specific email counts
- Raw Gmail query flexibility
- Direct database storage without signal processing

Use `ensure_email_signals` when you need:
- Onboarding flow integration
- Automatic refresh logic
- Signal processing for AI prompts

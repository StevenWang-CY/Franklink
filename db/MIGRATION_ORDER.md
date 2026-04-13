# Proactive Signal Outreach - Database Migrations

## Prerequisites (run these first if not already applied)

These tables must exist before running the feature migrations:

1. **`user_email_demands.sql`** - Creates the original demands table
2. **`proactive_outreach_tracking.sql`** - Creates outreach tracking table
3. **`connection_requests`** table (should already exist from core setup)
4. **`users`** table (should already exist from core setup)

## Feature Migrations (run in order)

### Migration 1: `rename_demands_to_signals.sql`
**Purpose:** Rename `user_email_demands` table to `user_email_signals`

Changes:
- Renames table: `user_email_demands` → `user_email_signals`
- Renames columns: `demand_text` → `signal_text`, `demand_rank` → `signal_rank`
- Adds columns: `match_type` (single/multi), `max_matches`
- Renames indexes
- Creates new RPC functions with signal naming

### Migration 2: `connection_requests_multi_match.sql`
**Purpose:** Add multi-match tracking to connection_requests

Changes:
- Adds columns: `signal_group_id`, `signal_id`, `is_multi_match`, `multi_match_threshold`, `multi_match_chat_guid`
- Creates index for signal_group_id
- Creates RPC functions:
  - `check_multi_match_ready_v1` - Check if threshold met
  - `get_signal_group_requests_v1` - Get all requests in group
  - `update_multi_match_chat_guid_v1` - Set chat GUID for group
  - `get_accepted_multi_match_requests_v1` - Get accepted requests with user details

### Migration 3: `group_chat_participants.sql`
**Purpose:** Create table for N-person group chats

Changes:
- Creates `group_chat_participants` table
- Creates indexes for chat and user lookup
- Creates RPC functions:
  - `add_group_chat_participant_v1`
  - `get_group_chat_participants_v1`
  - `update_participant_mode_v1`
  - `get_user_group_chats_v1`
  - `get_group_chat_initiator_v1`

### Migration 4: `proactive_outreach_tracking_signal_rename.sql`
**Purpose:** Rename columns in proactive_outreach_tracking

Changes:
- Renames columns: `demand_id` → `signal_id`, `demand_text_hash` → `signal_text`
- Renames index
- Updates RPC functions with new column names
- Creates `get_recent_outreach_texts_v1` for semantic comparison

## Quick Reference - Run Commands

```sql
-- Step 1: Prerequisites (if not already run)
-- Run: user_email_demands.sql
-- Run: proactive_outreach_tracking.sql

-- Step 2: Feature migrations (in order)
-- Run: rename_demands_to_signals.sql
-- Run: connection_requests_multi_match.sql
-- Run: group_chat_participants.sql
-- Run: proactive_outreach_tracking_signal_rename.sql
```

## How the Multi-Match Storage Works

### Data Flow

```
Signal extracted from email
    ↓
Stored in user_email_signals (with match_type='multi')
    ↓
For each match found, create connection_request with:
    - signal_id → points to user_email_signals.id
    - signal_group_id → shared UUID linking all requests from this signal
    - is_multi_match = true
    - multi_match_threshold = 2 (configurable)
    ↓
Each target has their own status in connection_requests:
    - pending_initiator_approval (waiting for initiator to confirm)
    - pending_target_approval (waiting for target to accept/decline)
    - target_accepted / target_declined
    - group_created (final state when chat created)
    ↓
When accepted_count >= threshold:
    - Create multi-person group chat
    - Store chat_guid in multi_match_chat_guid
    - Add participants to group_chat_participants
    - Mark requests as group_created
```

### Key Queries

```sql
-- Check if multi-match is ready for group creation
SELECT * FROM check_multi_match_ready_v1('signal-group-uuid');
-- Returns: ready, accepted_count, threshold, accepted_request_ids, chat_guid

-- Get all requests in a signal group
SELECT * FROM get_signal_group_requests_v1('signal-group-uuid');

-- Get accepted requests with user details (for group creation)
SELECT * FROM get_accepted_multi_match_requests_v1('signal-group-uuid');

-- Get participants in a group chat
SELECT * FROM get_group_chat_participants_v1('iMessage;+;chat123');
```

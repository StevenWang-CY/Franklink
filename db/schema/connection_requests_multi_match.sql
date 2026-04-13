-- Migration: Add multi-match support to connection_requests
-- Purpose: Enable tracking of multiple connection requests linked by a signal group
--
-- PREREQUISITES: connection_requests table must exist
-- RUN ORDER: This is migration #2 of 4 for proactive signal outreach feature
--
-- This adds columns to track:
-- - signal_group_id: Links multiple requests from same multi-match signal
-- - signal_id: References the signal that triggered the match
-- - is_multi_match: Boolean flag for multi-person requests
-- - multi_match_threshold: Number of acceptances needed to create group
-- - multi_match_chat_guid: The group chat once created

-- =============================================================================
-- Step 1: Add columns for multi-match tracking
-- =============================================================================
ALTER TABLE connection_requests ADD COLUMN IF NOT EXISTS signal_group_id uuid;
ALTER TABLE connection_requests ADD COLUMN IF NOT EXISTS signal_id uuid;
ALTER TABLE connection_requests ADD COLUMN IF NOT EXISTS is_multi_match boolean DEFAULT false;
ALTER TABLE connection_requests ADD COLUMN IF NOT EXISTS multi_match_threshold int DEFAULT 2;
ALTER TABLE connection_requests ADD COLUMN IF NOT EXISTS multi_match_chat_guid text;

-- =============================================================================
-- Step 2: Create index for finding requests by signal group
-- =============================================================================
CREATE INDEX IF NOT EXISTS connection_requests_signal_group_idx
    ON connection_requests(signal_group_id)
    WHERE signal_group_id IS NOT NULL;

-- =============================================================================
-- Step 3: RPC function to check if multi-match threshold is met
-- =============================================================================
CREATE OR REPLACE FUNCTION check_multi_match_ready_v1(p_signal_group_id uuid)
RETURNS TABLE(
    ready boolean,
    accepted_count int,
    threshold int,
    accepted_request_ids uuid[],
    chat_guid text
)
LANGUAGE plpgsql
STABLE
AS $$
BEGIN
    RETURN QUERY
    SELECT
        (COUNT(*) FILTER (WHERE cr.status = 'target_accepted'))::int >=
            COALESCE(MAX(cr.multi_match_threshold), 2),
        (COUNT(*) FILTER (WHERE cr.status = 'target_accepted'))::int,
        COALESCE(MAX(cr.multi_match_threshold), 2)::int,
        ARRAY_AGG(cr.id) FILTER (WHERE cr.status = 'target_accepted'),
        MAX(cr.multi_match_chat_guid)
    FROM connection_requests cr
    WHERE cr.signal_group_id = p_signal_group_id;
END;
$$;

-- =============================================================================
-- Step 4: RPC function to get all requests in a signal group
-- =============================================================================
CREATE OR REPLACE FUNCTION get_signal_group_requests_v1(p_signal_group_id uuid)
RETURNS SETOF connection_requests
LANGUAGE sql
STABLE
AS $$
    SELECT *
    FROM connection_requests
    WHERE signal_group_id = p_signal_group_id
    ORDER BY created_at ASC;
$$;

-- =============================================================================
-- Step 5: RPC function to update multi-match chat GUID for all requests in group
-- =============================================================================
CREATE OR REPLACE FUNCTION update_multi_match_chat_guid_v1(
    p_signal_group_id uuid,
    p_chat_guid text
)
RETURNS int
LANGUAGE plpgsql
AS $$
DECLARE
    updated_count int;
BEGIN
    UPDATE connection_requests
    SET
        multi_match_chat_guid = p_chat_guid,
        updated_at = now()
    WHERE signal_group_id = p_signal_group_id;

    GET DIAGNOSTICS updated_count = ROW_COUNT;
    RETURN updated_count;
END;
$$;

-- =============================================================================
-- Step 6: RPC function to get accepted requests with user details for group creation
-- =============================================================================
CREATE OR REPLACE FUNCTION get_accepted_multi_match_requests_v1(p_signal_group_id uuid)
RETURNS TABLE(
    request_id uuid,
    initiator_user_id uuid,
    target_user_id uuid,
    target_name text,
    target_phone text,
    match_score float,
    matching_reasons text[]
)
LANGUAGE sql
STABLE
AS $$
    SELECT
        cr.id as request_id,
        cr.initiator_user_id,
        cr.target_user_id,
        u.name as target_name,
        u.phone_number as target_phone,
        cr.match_score,
        cr.matching_reasons
    FROM connection_requests cr
    JOIN users u ON u.id = cr.target_user_id
    WHERE cr.signal_group_id = p_signal_group_id
      AND cr.status = 'target_accepted'
    ORDER BY cr.created_at ASC;
$$;

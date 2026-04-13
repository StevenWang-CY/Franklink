-- Migration: Rename demand columns to signal in proactive_outreach_tracking
-- Purpose: Align with signal terminology and switch from hash to text storage
--
-- PREREQUISITES: Run proactive_outreach_tracking.sql first if table doesn't exist
--                Run rename_demands_to_signals.sql first (this migration depends on it)
-- RUN ORDER: This is migration #4 of 4 for proactive signal outreach feature
--
-- NOTE: After rename_demands_to_signals.sql runs, the FK in this table still points
-- to the renamed table (user_email_signals) but the column is still called demand_id.
-- This migration renames the columns for consistency.

-- =============================================================================
-- Step 1: Rename columns
-- =============================================================================
ALTER TABLE proactive_outreach_tracking RENAME COLUMN demand_id TO signal_id;
ALTER TABLE proactive_outreach_tracking RENAME COLUMN demand_text_hash TO signal_text;

-- =============================================================================
-- Step 2: Update indexes (rename for consistency)
-- =============================================================================
ALTER INDEX IF EXISTS proactive_outreach_user_demand_hash_idx
    RENAME TO proactive_outreach_user_signal_text_idx;

-- =============================================================================
-- Step 3: Update the create function to use new column names
-- =============================================================================
DROP FUNCTION IF EXISTS create_proactive_outreach_tracking_v1(uuid, uuid, text, uuid, uuid, text, text);

CREATE OR REPLACE FUNCTION create_proactive_outreach_tracking_v1(
    p_user_id uuid,
    p_signal_id uuid,
    p_signal_text text,
    p_target_user_id uuid,
    p_connection_request_id uuid,
    p_outreach_type text DEFAULT 'email_derived',
    p_message_sent text DEFAULT NULL
)
RETURNS proactive_outreach_tracking
LANGUAGE plpgsql
AS $$
DECLARE
    out_row proactive_outreach_tracking;
BEGIN
    INSERT INTO proactive_outreach_tracking (
        user_id,
        signal_id,
        signal_text,
        target_user_id,
        connection_request_id,
        outreach_type,
        message_sent,
        outcome,
        reached_out_at,
        created_at
    )
    VALUES (
        p_user_id,
        p_signal_id,
        p_signal_text,
        p_target_user_id,
        p_connection_request_id,
        COALESCE(p_outreach_type, 'email_derived'),
        p_message_sent,
        'pending',
        now(),
        now()
    )
    RETURNING * INTO out_row;

    RETURN out_row;
END;
$$;

-- =============================================================================
-- Step 4: Update the recent outreach lookup function
-- =============================================================================
DROP FUNCTION IF EXISTS get_recent_outreach_by_demand_hash_v1(uuid, text, timestamptz);

-- New function that returns signal_text for semantic comparison
CREATE OR REPLACE FUNCTION get_recent_outreach_texts_v1(
    p_user_id uuid,
    p_since timestamptz
)
RETURNS TABLE(
    id uuid,
    signal_text text,
    target_user_id uuid,
    reached_out_at timestamptz
)
LANGUAGE sql
STABLE
AS $$
    SELECT
        id,
        signal_text,
        target_user_id,
        reached_out_at
    FROM proactive_outreach_tracking
    WHERE user_id = p_user_id
      AND reached_out_at >= p_since
    ORDER BY reached_out_at DESC;
$$;

-- =============================================================================
-- Step 5: Update target lookup function name for consistency
-- =============================================================================
-- Keep the existing function but ensure it works with new schema
CREATE OR REPLACE FUNCTION get_recent_outreach_by_target_v1(
    p_user_id uuid,
    p_target_user_id uuid,
    p_since timestamptz
)
RETURNS proactive_outreach_tracking
LANGUAGE sql
STABLE
AS $$
    SELECT *
    FROM proactive_outreach_tracking
    WHERE user_id = p_user_id
      AND target_user_id = p_target_user_id
      AND reached_out_at >= p_since
    ORDER BY reached_out_at DESC
    LIMIT 1;
$$;

-- Migration: Rename user_email_demands to user_email_signals
-- Purpose: Change terminology from "demand" to "signal" for proactive outreach
-- Also adds match_type column for LLM single vs multi classification
--
-- PREREQUISITES: Run user_email_demands.sql first if table doesn't exist
-- RUN ORDER: This is migration #1 of 4 for proactive signal outreach feature
--
-- NOTE: proactive_outreach_tracking has FK to this table (demand_id → user_email_demands.id)
-- PostgreSQL automatically updates FK references when table is renamed.

-- =============================================================================
-- Step 1: Rename the table (FK references auto-update in PostgreSQL)
-- =============================================================================
ALTER TABLE user_email_demands RENAME TO user_email_signals;

-- =============================================================================
-- Step 2: Rename columns
-- =============================================================================
ALTER TABLE user_email_signals RENAME COLUMN demand_text TO signal_text;
ALTER TABLE user_email_signals RENAME COLUMN demand_rank TO signal_rank;

-- =============================================================================
-- Step 3: Add new columns for multi-match support
-- =============================================================================
ALTER TABLE user_email_signals ADD COLUMN IF NOT EXISTS match_type text DEFAULT 'single'
    CHECK (match_type IN ('single', 'multi'));
ALTER TABLE user_email_signals ADD COLUMN IF NOT EXISTS max_matches int DEFAULT 1;

-- =============================================================================
-- Step 4: Rename indexes
-- =============================================================================
ALTER INDEX IF EXISTS user_email_demands_user_status_idx RENAME TO user_email_signals_user_status_idx;
ALTER INDEX IF EXISTS user_email_demands_expires_idx RENAME TO user_email_signals_expires_idx;
ALTER INDEX IF EXISTS user_email_demands_user_rank_active_idx RENAME TO user_email_signals_user_rank_active_idx;

-- =============================================================================
-- Step 5: Update RPC functions with new naming
-- =============================================================================

-- Drop old functions (if they exist)
DROP FUNCTION IF EXISTS upsert_user_email_demands_v1(uuid, jsonb);
DROP FUNCTION IF EXISTS get_active_user_email_demands_v1(uuid);
DROP FUNCTION IF EXISTS update_demand_status_v1(uuid, text);

-- Create new function: upsert_user_email_signals_v1
CREATE OR REPLACE FUNCTION upsert_user_email_signals_v1(
    p_user_id uuid,
    p_signals jsonb  -- Array of {signal_text, signal_rank, urgency_score, relevance_score, source_intent_event_ids, extraction_reasoning, match_type, max_matches}
)
RETURNS int
LANGUAGE plpgsql
AS $$
DECLARE
    signal_record jsonb;
    upserted_count int := 0;
BEGIN
    -- First, expire any existing active signals for this user
    UPDATE user_email_signals
    SET
        status = 'expired',
        updated_at = now()
    WHERE user_id = p_user_id
      AND status = 'active';

    -- Insert new signals
    FOR signal_record IN SELECT * FROM jsonb_array_elements(p_signals)
    LOOP
        INSERT INTO user_email_signals (
            user_id,
            signal_text,
            signal_rank,
            urgency_score,
            relevance_score,
            source_intent_event_ids,
            extraction_reasoning,
            match_type,
            max_matches,
            status,
            extracted_at,
            expires_at,
            created_at,
            updated_at
        )
        VALUES (
            p_user_id,
            signal_record->>'signal_text',
            (signal_record->>'signal_rank')::int,
            COALESCE((signal_record->>'urgency_score')::float, 0.5),
            COALESCE((signal_record->>'relevance_score')::float, 0.5),
            COALESCE(
                (SELECT array_agg(x::uuid) FROM jsonb_array_elements_text(signal_record->'source_intent_event_ids') x),
                '{}'::uuid[]
            ),
            signal_record->>'extraction_reasoning',
            COALESCE(signal_record->>'match_type', 'single'),
            COALESCE((signal_record->>'max_matches')::int, 1),
            'active',
            now(),
            now() + interval '7 days',
            now(),
            now()
        );
        upserted_count := upserted_count + 1;
    END LOOP;

    RETURN upserted_count;
END;
$$;

-- Create new function: get_active_user_email_signals_v1
CREATE OR REPLACE FUNCTION get_active_user_email_signals_v1(
    p_user_id uuid
)
RETURNS SETOF user_email_signals
LANGUAGE sql
STABLE
AS $$
    SELECT *
    FROM user_email_signals
    WHERE user_id = p_user_id
      AND status = 'active'
      AND expires_at > now()
    ORDER BY signal_rank ASC;
$$;

-- Create new function: update_signal_status_v1
CREATE OR REPLACE FUNCTION update_signal_status_v1(
    p_signal_id uuid,
    p_status text
)
RETURNS user_email_signals
LANGUAGE plpgsql
AS $$
DECLARE
    out_row user_email_signals;
BEGIN
    UPDATE user_email_signals
    SET
        status = p_status,
        updated_at = now()
    WHERE id = p_signal_id
    RETURNING * INTO out_row;

    RETURN out_row;
END;
$$;

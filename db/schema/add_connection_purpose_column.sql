-- Migration: Add connection_purpose column to connection_requests
-- Purpose: Store the initiator's goal/purpose for connecting (used for group naming)
--
-- PREREQUISITES: connection_requests table must exist
-- RUN ORDER: Run after connection_requests_multi_match.sql
--
-- This adds a column to track the initiator's connection purpose,
-- which is used to name the group chat (e.g., "Algo Trading Study Group")

-- =============================================================================
-- Step 1: Add connection_purpose column
-- =============================================================================
ALTER TABLE connection_requests ADD COLUMN IF NOT EXISTS connection_purpose text;

-- =============================================================================
-- Step 2: Create index for finding requests by purpose (optional, for analytics)
-- =============================================================================
CREATE INDEX IF NOT EXISTS connection_requests_purpose_idx
    ON connection_requests(connection_purpose)
    WHERE connection_purpose IS NOT NULL;

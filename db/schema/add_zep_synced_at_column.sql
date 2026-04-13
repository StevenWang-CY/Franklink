-- Migration: Add zep_synced_at column for tracking Zep graph sync status
-- Purpose: Enable incremental sync - only sync emails that haven't been synced yet
-- Run this migration via Supabase dashboard or CLI

-- Add zep_synced_at column to track when each email was synced to Zep
ALTER TABLE user_emails ADD COLUMN IF NOT EXISTS zep_synced_at timestamptz;

-- Index for efficiently finding unsynced emails
CREATE INDEX IF NOT EXISTS idx_user_emails_zep_unsynced
  ON user_emails(user_id, zep_synced_at)
  WHERE zep_synced_at IS NULL;

-- Index for finding emails synced in a time range (for debugging/monitoring)
CREATE INDEX IF NOT EXISTS idx_user_emails_zep_synced_at
  ON user_emails(user_id, zep_synced_at DESC)
  WHERE zep_synced_at IS NOT NULL;

-- Comment for documentation
COMMENT ON COLUMN user_emails.zep_synced_at IS 'Timestamp when this email was synced to Zep knowledge graph. NULL means not yet synced.';

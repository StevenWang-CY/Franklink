-- Migration: Cleanup connection_requests table
-- Purpose:
--   1. Add CASCADE delete to group_chats foreign key so deleting connection_requests doesn't fail
--   2. Remove legacy target_offers and initiator_offers columns
-- Run this migration via Supabase dashboard SQL editor

-- Step 1: Drop the existing foreign key constraint on group_chats
ALTER TABLE group_chats
DROP CONSTRAINT IF EXISTS group_chats_connection_request_id_fkey;

-- Step 2: Re-add the foreign key with ON DELETE CASCADE
ALTER TABLE group_chats
ADD CONSTRAINT group_chats_connection_request_id_fkey
FOREIGN KEY (connection_request_id)
REFERENCES connection_requests(id)
ON DELETE CASCADE;

-- Step 3: Remove legacy offer columns from connection_requests
-- These fields are no longer used - matching_reasons is used instead
ALTER TABLE connection_requests
DROP COLUMN IF EXISTS target_offers;

ALTER TABLE connection_requests
DROP COLUMN IF EXISTS initiator_offers;

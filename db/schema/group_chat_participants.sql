-- Migration: Create group_chat_participants table
-- Purpose: Support N-person group chats (beyond the current 2-person limitation)
--
-- PREREQUISITES: users table and connection_requests table must exist
-- RUN ORDER: This is migration #3 of 4 for proactive signal outreach feature
--
-- This table tracks participants in multi-person group chats:
-- - chat_guid: The iMessage/Photon group chat identifier
-- - user_id: Participant's user ID
-- - role: 'initiator' or 'member'
-- - mode: 'active', 'quiet', or 'muted'
-- - connection_request_id: Links back to the connection request that added them

-- =============================================================================
-- Table: group_chat_participants
-- =============================================================================
CREATE TABLE IF NOT EXISTS group_chat_participants (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    chat_guid text NOT NULL,
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role text DEFAULT 'member' CHECK (role IN ('member', 'initiator')),
    mode text DEFAULT 'active' CHECK (mode IN ('active', 'quiet', 'muted')),
    joined_at timestamptz NOT NULL DEFAULT now(),
    connection_request_id uuid REFERENCES connection_requests(id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now(),

    -- Each user can only be in a group chat once
    UNIQUE(chat_guid, user_id)
);

-- =============================================================================
-- Indexes
-- =============================================================================
CREATE INDEX IF NOT EXISTS group_chat_participants_chat_idx
    ON group_chat_participants(chat_guid);

CREATE INDEX IF NOT EXISTS group_chat_participants_user_idx
    ON group_chat_participants(user_id);

CREATE INDEX IF NOT EXISTS group_chat_participants_role_idx
    ON group_chat_participants(chat_guid, role)
    WHERE role = 'initiator';

-- =============================================================================
-- RPC: add_group_chat_participant_v1
-- Add a participant to a group chat
-- =============================================================================
CREATE OR REPLACE FUNCTION add_group_chat_participant_v1(
    p_chat_guid text,
    p_user_id uuid,
    p_role text DEFAULT 'member',
    p_connection_request_id uuid DEFAULT NULL
)
RETURNS group_chat_participants
LANGUAGE plpgsql
AS $$
DECLARE
    out_row group_chat_participants;
BEGIN
    INSERT INTO group_chat_participants (
        chat_guid,
        user_id,
        role,
        mode,
        joined_at,
        connection_request_id,
        created_at
    )
    VALUES (
        p_chat_guid,
        p_user_id,
        COALESCE(p_role, 'member'),
        'active',
        now(),
        p_connection_request_id,
        now()
    )
    ON CONFLICT (chat_guid, user_id) DO UPDATE
    SET
        role = EXCLUDED.role,
        connection_request_id = COALESCE(EXCLUDED.connection_request_id, group_chat_participants.connection_request_id)
    RETURNING * INTO out_row;

    RETURN out_row;
END;
$$;

-- =============================================================================
-- RPC: get_group_chat_participants_v1
-- Get all participants in a group chat
-- =============================================================================
CREATE OR REPLACE FUNCTION get_group_chat_participants_v1(p_chat_guid text)
RETURNS TABLE(
    participant_id uuid,
    user_id uuid,
    user_name text,
    user_phone text,
    role text,
    mode text,
    joined_at timestamptz
)
LANGUAGE sql
STABLE
AS $$
    SELECT
        gcp.id as participant_id,
        gcp.user_id,
        u.name as user_name,
        u.phone_number as user_phone,
        gcp.role,
        gcp.mode,
        gcp.joined_at
    FROM group_chat_participants gcp
    JOIN users u ON u.id = gcp.user_id
    WHERE gcp.chat_guid = p_chat_guid
    ORDER BY gcp.joined_at ASC;
$$;

-- =============================================================================
-- RPC: update_participant_mode_v1
-- Update a participant's mode (active, quiet, muted)
-- =============================================================================
CREATE OR REPLACE FUNCTION update_participant_mode_v1(
    p_chat_guid text,
    p_user_id uuid,
    p_mode text
)
RETURNS group_chat_participants
LANGUAGE plpgsql
AS $$
DECLARE
    out_row group_chat_participants;
BEGIN
    UPDATE group_chat_participants
    SET mode = p_mode
    WHERE chat_guid = p_chat_guid
      AND user_id = p_user_id
    RETURNING * INTO out_row;

    RETURN out_row;
END;
$$;

-- =============================================================================
-- RPC: get_user_group_chats_v1
-- Get all group chats a user is participating in
-- =============================================================================
CREATE OR REPLACE FUNCTION get_user_group_chats_v1(p_user_id uuid)
RETURNS TABLE(
    chat_guid text,
    role text,
    mode text,
    joined_at timestamptz,
    participant_count bigint
)
LANGUAGE sql
STABLE
AS $$
    SELECT
        gcp.chat_guid,
        gcp.role,
        gcp.mode,
        gcp.joined_at,
        (SELECT COUNT(*) FROM group_chat_participants WHERE chat_guid = gcp.chat_guid) as participant_count
    FROM group_chat_participants gcp
    WHERE gcp.user_id = p_user_id
    ORDER BY gcp.joined_at DESC;
$$;

-- =============================================================================
-- RPC: get_group_chat_initiator_v1
-- Get the initiator of a group chat
-- =============================================================================
CREATE OR REPLACE FUNCTION get_group_chat_initiator_v1(p_chat_guid text)
RETURNS TABLE(
    user_id uuid,
    user_name text,
    user_phone text
)
LANGUAGE sql
STABLE
AS $$
    SELECT
        u.id as user_id,
        u.name as user_name,
        u.phone_number as user_phone
    FROM group_chat_participants gcp
    JOIN users u ON u.id = gcp.user_id
    WHERE gcp.chat_guid = p_chat_guid
      AND gcp.role = 'initiator'
    LIMIT 1;
$$;

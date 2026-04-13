-- User handle links table: stores manually linked Find My handles for users.
-- Allows users to claim unrecognized Find My handles (e.g., iCloud emails)
-- so the location worker can match their location going forward.

CREATE TABLE IF NOT EXISTS user_handle_links (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    handle TEXT NOT NULL,
    handle_type TEXT NOT NULL DEFAULT 'findmy',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(user_id, handle)
);

CREATE INDEX IF NOT EXISTS idx_user_handle_links_handle ON user_handle_links(handle);
CREATE INDEX IF NOT EXISTS idx_user_handle_links_user_id ON user_handle_links(user_id);

ALTER TABLE user_handle_links ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Service role can manage user_handle_links" ON user_handle_links
    FOR ALL USING (true) WITH CHECK (true);

-- User locations table: stores the latest Find My location for each user.
-- One row per user, upserted by the location-update-worker every hour.

CREATE TABLE IF NOT EXISTS user_locations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE UNIQUE,
    latitude DOUBLE PRECISION NOT NULL,
    longitude DOUBLE PRECISION NOT NULL,
    long_address TEXT,
    short_address TEXT,
    findmy_handle TEXT NOT NULL,
    findmy_status TEXT,
    findmy_last_updated TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_user_locations_user_id ON user_locations(user_id);
CREATE INDEX IF NOT EXISTS idx_user_locations_coords ON user_locations(latitude, longitude);

ALTER TABLE user_locations ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Service role can manage user_locations" ON user_locations FOR ALL USING (true) WITH CHECK (true);

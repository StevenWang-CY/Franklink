-- User Profiles Table
-- Stores holistic user understanding synthesized from Zep knowledge graph
-- Used to enhance matching algorithm with deeper user insights

-- Enable vector extension (should already exist)
CREATE EXTENSION IF NOT EXISTS vector;

-- Create user_profiles table
CREATE TABLE IF NOT EXISTS user_profiles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES users(id) ON DELETE CASCADE UNIQUE NOT NULL,

    -- Inferred Traits
    personality_summary TEXT,
    communication_style TEXT,
    work_patterns TEXT,

    -- Latent Needs
    latent_needs JSONB DEFAULT '[]'::jsonb,
    unspoken_gaps TEXT,

    -- Relationship Potential
    ideal_relationship_types JSONB DEFAULT '[]'::jsonb,
    relationship_strengths TEXT,
    relationship_risks TEXT,

    -- Life Trajectory
    trajectory_summary TEXT,
    core_motivations JSONB DEFAULT '[]'::jsonb,
    career_stage TEXT CHECK (career_stage IN ('early_explorer', 'skill_builder', 'career_changer', 'established', NULL)),

    -- Composite
    holistic_summary TEXT,
    holistic_embedding vector(1536),

    -- Metadata
    computed_at TIMESTAMPTZ,
    zep_facts_count INTEGER DEFAULT 0,
    confidence_score FLOAT CHECK (confidence_score >= 0 AND confidence_score <= 1),

    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Index for fast user lookup
CREATE INDEX IF NOT EXISTS idx_user_profiles_user_id ON user_profiles(user_id);

-- Index for holistic embedding similarity search
CREATE INDEX IF NOT EXISTS idx_user_profiles_holistic_embedding ON user_profiles
    USING ivfflat (holistic_embedding vector_cosine_ops) WITH (lists = 100);

-- Index for finding stale profiles
CREATE INDEX IF NOT EXISTS idx_user_profiles_computed_at ON user_profiles(computed_at);

-- RPC function to match users by holistic profile embedding
CREATE OR REPLACE FUNCTION match_users_by_profile(
    query_embedding vector(1536),
    exclude_user_id uuid,
    match_threshold float DEFAULT 0.35,
    match_count int DEFAULT 15
)
RETURNS TABLE (
    id uuid,
    name text,
    phone_number text,
    university text,
    major text,
    year text,
    location text,
    career_interests text[],
    all_demand text,
    all_value text,
    context_summary text,
    needs jsonb,
    linkedin_data jsonb,
    holistic_summary text,
    latent_needs jsonb,
    ideal_relationship_types jsonb,
    career_stage text,
    similarity float
)
LANGUAGE plpgsql
AS $$
BEGIN
    RETURN QUERY
    SELECT
        u.id,
        u.name,
        u.phone_number,
        u.university,
        u.major,
        u.year::text,
        u.location,
        u.career_interests,
        u.all_demand,
        u.all_value,
        u.context_summary,
        u.needs,
        u.linkedin_data,
        up.holistic_summary,
        up.latent_needs,
        up.ideal_relationship_types,
        up.career_stage,
        (1 - (up.holistic_embedding <=> query_embedding))::float as similarity
    FROM users u
    INNER JOIN user_profiles up ON u.id = up.user_id
    WHERE
        u.id != exclude_user_id
        AND u.is_onboarded = true
        AND u.phone_number IS NOT NULL
        AND up.holistic_embedding IS NOT NULL
        AND (1 - (up.holistic_embedding <=> query_embedding)) >= match_threshold
    ORDER BY similarity DESC
    LIMIT match_count;
END;
$$;

-- Grant execute permissions
GRANT EXECUTE ON FUNCTION match_users_by_profile(vector, uuid, float, int) TO authenticated;
GRANT EXECUTE ON FUNCTION match_users_by_profile(vector, uuid, float, int) TO service_role;

-- Function to get users needing profile synthesis
CREATE OR REPLACE FUNCTION get_users_needing_profile_synthesis(
    stale_days int DEFAULT 7,
    batch_limit int DEFAULT 50
)
RETURNS TABLE (
    user_id uuid,
    reason text
)
LANGUAGE plpgsql
AS $$
BEGIN
    -- Use parameterized interval to prevent SQL injection
    -- Wrap in subquery to ensure LIMIT applies to combined result
    RETURN QUERY
    SELECT * FROM (
        -- Users with no profile
        SELECT u.id as user_id, 'no_profile'::text as reason
        FROM users u
        LEFT JOIN user_profiles up ON u.id = up.user_id
        WHERE
            u.is_onboarded = true
            AND up.id IS NULL
        UNION ALL
        -- Users with stale profiles
        SELECT up.user_id, 'stale_profile'::text as reason
        FROM user_profiles up
        INNER JOIN users u ON u.id = up.user_id
        WHERE
            u.is_onboarded = true
            AND up.computed_at < NOW() - (stale_days * INTERVAL '1 day')
    ) combined
    LIMIT batch_limit;
END;
$$;

-- Grant execute permissions
GRANT EXECUTE ON FUNCTION get_users_needing_profile_synthesis(int, int) TO authenticated;
GRANT EXECUTE ON FUNCTION get_users_needing_profile_synthesis(int, int) TO service_role;

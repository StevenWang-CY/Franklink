-- Drop existing function if it exists
DROP FUNCTION IF EXISTS match_users_by_career_interest(vector, text, uuid, float, int);
DROP FUNCTION IF EXISTS match_users_by_career_interest(uuid, text, int);

-- Create the RPC function with correct parameter names matching Python code
CREATE OR REPLACE FUNCTION match_users_by_career_interest(
    query_embedding vector(1536),
    university_filter text,
    exclude_user_id uuid,
    match_threshold float DEFAULT 0.4,
    match_count int DEFAULT 20
)
RETURNS TABLE (
    id uuid,
    name text,
    phone_number text,
    university text,
    career_interests text[],
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
        u.career_interests,
        (1 - (u.career_interest_embedding <=> query_embedding))::float as similarity
    FROM users u
    WHERE
        u.id != exclude_user_id
        AND u.university = university_filter
        AND u.career_interest_embedding IS NOT NULL
        AND u.is_onboarded = true
        AND (1 - (u.career_interest_embedding <=> query_embedding)) >= match_threshold
    ORDER BY similarity DESC
    LIMIT match_count;
END;
$$;

-- Grant execute permission to authenticated and service roles
GRANT EXECUTE ON FUNCTION match_users_by_career_interest(vector, text, uuid, float, int) TO authenticated;
GRANT EXECUTE ON FUNCTION match_users_by_career_interest(vector, text, uuid, float, int) TO service_role;

-- IMPORTANT: Run this to backfill embeddings for existing users who don't have them
-- This is a one-time operation you should run manually for each user without embeddings
-- Example (replace with actual user IDs):
--
-- SELECT id, name, university, career_interests
-- FROM users
-- WHERE career_interest_embedding IS NULL
--   AND is_onboarded = true
--   AND career_interests IS NOT NULL
--   AND array_length(career_interests, 1) > 0;

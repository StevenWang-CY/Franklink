-- Adaptive Match RPC Functions
-- Supports multi-signal candidate generation for networking matches

-- Enable vector extension (required for embeddings)
CREATE EXTENSION IF NOT EXISTS vector;

-- Add context embedding column for synthesized background context
ALTER TABLE users ADD COLUMN IF NOT EXISTS context_embedding vector(1536);

-- Add context_summary column to store the text used to generate context_embedding
ALTER TABLE users ADD COLUMN IF NOT EXISTS context_summary text;

-- Index for fast context similarity search
CREATE INDEX IF NOT EXISTS idx_users_context_embedding ON users
USING ivfflat (context_embedding vector_cosine_ops) WITH (lists = 100);

-- Comprehensive user matching function that returns rich candidate data
-- Supports matching by value, demand, context, or career_interest embedding types
DROP FUNCTION IF EXISTS match_users_comprehensive(vector, text, uuid, float, int);

CREATE OR REPLACE FUNCTION match_users_comprehensive(
    query_embedding vector(1536),
    embedding_type text,  -- 'value', 'demand', 'context', or 'career_interest'
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
        CASE embedding_type
            WHEN 'value' THEN (1 - (u.value_embedding <=> query_embedding))::float
            WHEN 'demand' THEN (1 - (u.demand_embedding <=> query_embedding))::float
            WHEN 'context' THEN (1 - (u.context_embedding <=> query_embedding))::float
            WHEN 'career_interest' THEN (1 - (u.career_interest_embedding <=> query_embedding))::float
            ELSE 0.0
        END as similarity
    FROM users u
    WHERE
        u.id != exclude_user_id
        AND u.is_onboarded = true
        AND u.phone_number IS NOT NULL
        -- Check that the relevant embedding exists based on type
        AND (
            (embedding_type = 'value' AND u.value_embedding IS NOT NULL)
            OR (embedding_type = 'demand' AND u.demand_embedding IS NOT NULL)
            OR (embedding_type = 'context' AND u.context_embedding IS NOT NULL)
            OR (embedding_type = 'career_interest' AND u.career_interest_embedding IS NOT NULL)
        )
        -- Apply threshold filter
        AND (
            (embedding_type = 'value' AND (1 - (u.value_embedding <=> query_embedding)) >= match_threshold)
            OR (embedding_type = 'demand' AND (1 - (u.demand_embedding <=> query_embedding)) >= match_threshold)
            OR (embedding_type = 'context' AND (1 - (u.context_embedding <=> query_embedding)) >= match_threshold)
            OR (embedding_type = 'career_interest' AND (1 - (u.career_interest_embedding <=> query_embedding)) >= match_threshold)
        )
    ORDER BY similarity DESC
    LIMIT match_count;
END;
$$;

-- Grant execute permissions
GRANT EXECUTE ON FUNCTION match_users_comprehensive(vector, text, uuid, float, int) TO authenticated;
GRANT EXECUTE ON FUNCTION match_users_comprehensive(vector, text, uuid, float, int) TO service_role;

-- Enable vector extension (required for embeddings)
CREATE EXTENSION IF NOT EXISTS vector;

-- Columns for value-exchange matching
ALTER TABLE users ADD COLUMN IF NOT EXISTS demand text;
ALTER TABLE users ADD COLUMN IF NOT EXISTS value text;
ALTER TABLE users ADD COLUMN IF NOT EXISTS demand_embedding vector(1536);
ALTER TABLE users ADD COLUMN IF NOT EXISTS value_embedding vector(1536);

-- Indexes for fast vector search (tune lists based on dataset size)
CREATE INDEX IF NOT EXISTS idx_users_demand_embedding ON users
USING ivfflat (demand_embedding vector_cosine_ops) WITH (lists = 100);

CREATE INDEX IF NOT EXISTS idx_users_value_embedding ON users
USING ivfflat (value_embedding vector_cosine_ops) WITH (lists = 100);

-- Match users by value (candidate value matches initiator demand)
DROP FUNCTION IF EXISTS match_users_by_value(vector, uuid, float, int);

CREATE OR REPLACE FUNCTION match_users_by_value(
    query_embedding vector(1536),
    exclude_user_id uuid,
    match_threshold float DEFAULT 0.45,
    match_count int DEFAULT 20
)
RETURNS TABLE (
    id uuid,
    name text,
    phone_number text,
    university text,
    major text,
    year text,
    demand text,
    value text,
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
        u.demand,
        u.value,
        (1 - (u.value_embedding <=> query_embedding))::float as similarity
    FROM users u
    WHERE
        u.id != exclude_user_id
        AND u.value_embedding IS NOT NULL
        AND u.is_onboarded = true
        AND u.phone_number IS NOT NULL
        AND u.demand IS NOT NULL
        AND u.value IS NOT NULL
        AND length(trim(u.demand)) > 0
        AND length(trim(u.value)) > 0
        AND (1 - (u.value_embedding <=> query_embedding)) >= match_threshold
    ORDER BY similarity DESC
    LIMIT match_count;
END;
$$;

-- Match users by demand (candidate demand matches initiator value)
DROP FUNCTION IF EXISTS match_users_by_demand(vector, uuid, float, int);

CREATE OR REPLACE FUNCTION match_users_by_demand(
    query_embedding vector(1536),
    exclude_user_id uuid,
    match_threshold float DEFAULT 0.45,
    match_count int DEFAULT 20
)
RETURNS TABLE (
    id uuid,
    name text,
    phone_number text,
    university text,
    major text,
    year text,
    demand text,
    value text,
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
        u.demand,
        u.value,
        (1 - (u.demand_embedding <=> query_embedding))::float as similarity
    FROM users u
    WHERE
        u.id != exclude_user_id
        AND u.demand_embedding IS NOT NULL
        AND u.is_onboarded = true
        AND u.phone_number IS NOT NULL
        AND u.demand IS NOT NULL
        AND u.value IS NOT NULL
        AND length(trim(u.demand)) > 0
        AND length(trim(u.value)) > 0
        AND (1 - (u.demand_embedding <=> query_embedding)) >= match_threshold
    ORDER BY similarity DESC
    LIMIT match_count;
END;
$$;

GRANT EXECUTE ON FUNCTION match_users_by_value(vector, uuid, float, int) TO authenticated;
GRANT EXECUTE ON FUNCTION match_users_by_value(vector, uuid, float, int) TO service_role;
GRANT EXECUTE ON FUNCTION match_users_by_demand(vector, uuid, float, int) TO authenticated;
GRANT EXECUTE ON FUNCTION match_users_by_demand(vector, uuid, float, int) TO service_role;

-- Backfill checklist (run in batches)
-- SELECT id, name, demand, value
-- FROM users
-- WHERE is_onboarded = true
--   AND (demand_embedding IS NULL OR value_embedding IS NULL)
--   AND demand IS NOT NULL AND value IS NOT NULL;

-- User Email Demands Table
-- Stores ranked networking demands extracted from user email activity

-- =============================================================================
-- Table: user_email_demands
-- =============================================================================
create table if not exists user_email_demands (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references users(id) on delete cascade,

    -- Demand details
    demand_text text not null,
    demand_rank int not null check (demand_rank between 1 and 3),
    urgency_score float not null default 0.5,
    relevance_score float not null default 0.5,

    -- Source tracking (links to intent events that contributed)
    source_intent_event_ids uuid[] not null default '{}'::uuid[],

    -- LLM reasoning for the extraction
    extraction_reasoning text,

    -- Status: active (can be used), matched (connection made), expired, dismissed
    status text not null default 'active'
        check (status in ('active', 'matched', 'expired', 'dismissed')),

    -- Timestamps
    extracted_at timestamptz not null default now(),
    expires_at timestamptz not null default (now() + interval '7 days'),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

-- Index for finding active demands by user
create index if not exists user_email_demands_user_status_idx
    on user_email_demands (user_id, status);

-- Index for finding expiring demands
create index if not exists user_email_demands_expires_idx
    on user_email_demands (expires_at)
    where status = 'active';

-- Unique constraint: only one active demand per rank per user
create unique index if not exists user_email_demands_user_rank_active_idx
    on user_email_demands (user_id, demand_rank)
    where status = 'active';

-- =============================================================================
-- RPC: upsert_user_email_demands_v1
-- Insert or update demands for a user, replacing any existing active demands
-- =============================================================================
create or replace function upsert_user_email_demands_v1(
    p_user_id uuid,
    p_demands jsonb  -- Array of {demand_text, demand_rank, urgency_score, relevance_score, source_intent_event_ids, extraction_reasoning}
)
returns int
language plpgsql
as $$
declare
    demand_record jsonb;
    upserted_count int := 0;
begin
    -- First, expire any existing active demands for this user
    update user_email_demands
    set
        status = 'expired',
        updated_at = now()
    where user_id = p_user_id
      and status = 'active';

    -- Insert new demands
    for demand_record in select * from jsonb_array_elements(p_demands)
    loop
        insert into user_email_demands (
            user_id,
            demand_text,
            demand_rank,
            urgency_score,
            relevance_score,
            source_intent_event_ids,
            extraction_reasoning,
            status,
            extracted_at,
            expires_at,
            created_at,
            updated_at
        )
        values (
            p_user_id,
            demand_record->>'demand_text',
            (demand_record->>'demand_rank')::int,
            coalesce((demand_record->>'urgency_score')::float, 0.5),
            coalesce((demand_record->>'relevance_score')::float, 0.5),
            coalesce(
                (select array_agg(x::uuid) from jsonb_array_elements_text(demand_record->'source_intent_event_ids') x),
                '{}'::uuid[]
            ),
            demand_record->>'extraction_reasoning',
            'active',
            now(),
            now() + interval '7 days',
            now(),
            now()
        );
        upserted_count := upserted_count + 1;
    end loop;

    return upserted_count;
end;
$$;

-- =============================================================================
-- RPC: get_active_user_email_demands_v1
-- Get all active demands for a user, ordered by rank
-- =============================================================================
create or replace function get_active_user_email_demands_v1(
    p_user_id uuid
)
returns setof user_email_demands
language sql
stable
as $$
    select *
    from user_email_demands
    where user_id = p_user_id
      and status = 'active'
      and expires_at > now()
    order by demand_rank asc;
$$;

-- =============================================================================
-- RPC: update_demand_status_v1
-- Update the status of a specific demand
-- =============================================================================
create or replace function update_demand_status_v1(
    p_demand_id uuid,
    p_status text
)
returns user_email_demands
language plpgsql
as $$
declare
    out_row user_email_demands;
begin
    update user_email_demands
    set
        status = p_status,
        updated_at = now()
    where id = p_demand_id
    returning * into out_row;

    return out_row;
end;
$$;

-- Proactive Outreach Tracking Table
-- Tracks what has been proactively reached out about to avoid duplicates

-- =============================================================================
-- Table: proactive_outreach_tracking
-- =============================================================================
create table if not exists proactive_outreach_tracking (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references users(id) on delete cascade,

    -- What was reached out about
    demand_id uuid references user_email_demands(id) on delete set null,
    demand_text_hash text not null,  -- SHA256 of normalized demand text for duplicate detection
    target_user_id uuid references users(id) on delete set null,
    connection_request_id uuid references connection_requests(id) on delete set null,

    -- Outreach details
    outreach_type text not null default 'email_derived'
        check (outreach_type in ('email_derived', 'manual', 'scheduled')),
    message_sent text,

    -- Outcome tracking
    outcome text not null default 'pending'
        check (outcome in ('pending', 'confirmed', 'declined', 'no_response', 'expired')),

    -- Timestamps
    reached_out_at timestamptz not null default now(),
    response_at timestamptz,
    created_at timestamptz not null default now()
);

-- Index for checking recent outreach by demand hash (duplicate detection)
create index if not exists proactive_outreach_user_demand_hash_idx
    on proactive_outreach_tracking (user_id, demand_text_hash, reached_out_at desc);

-- Index for checking recent outreach by target user (avoid suggesting same person)
create index if not exists proactive_outreach_user_target_idx
    on proactive_outreach_tracking (user_id, target_user_id, reached_out_at desc)
    where target_user_id is not null;

-- Index for finding pending outreach to update outcome
create index if not exists proactive_outreach_pending_idx
    on proactive_outreach_tracking (user_id, outcome, reached_out_at desc)
    where outcome = 'pending';

-- =============================================================================
-- RPC: create_proactive_outreach_tracking_v1
-- Record a new proactive outreach
-- =============================================================================
create or replace function create_proactive_outreach_tracking_v1(
    p_user_id uuid,
    p_demand_id uuid,
    p_demand_text_hash text,
    p_target_user_id uuid,
    p_connection_request_id uuid,
    p_outreach_type text default 'email_derived',
    p_message_sent text default null
)
returns proactive_outreach_tracking
language plpgsql
as $$
declare
    out_row proactive_outreach_tracking;
begin
    insert into proactive_outreach_tracking (
        user_id,
        demand_id,
        demand_text_hash,
        target_user_id,
        connection_request_id,
        outreach_type,
        message_sent,
        outcome,
        reached_out_at,
        created_at
    )
    values (
        p_user_id,
        p_demand_id,
        p_demand_text_hash,
        p_target_user_id,
        p_connection_request_id,
        coalesce(p_outreach_type, 'email_derived'),
        p_message_sent,
        'pending',
        now(),
        now()
    )
    returning * into out_row;

    return out_row;
end;
$$;

-- =============================================================================
-- RPC: get_recent_outreach_by_demand_hash_v1
-- Check if we've reached out about a similar demand recently
-- =============================================================================
create or replace function get_recent_outreach_by_demand_hash_v1(
    p_user_id uuid,
    p_demand_text_hash text,
    p_since timestamptz
)
returns proactive_outreach_tracking
language sql
stable
as $$
    select *
    from proactive_outreach_tracking
    where user_id = p_user_id
      and demand_text_hash = p_demand_text_hash
      and reached_out_at >= p_since
    order by reached_out_at desc
    limit 1;
$$;

-- =============================================================================
-- RPC: get_recent_outreach_by_target_v1
-- Check if we've suggested this target user recently
-- =============================================================================
create or replace function get_recent_outreach_by_target_v1(
    p_user_id uuid,
    p_target_user_id uuid,
    p_since timestamptz
)
returns proactive_outreach_tracking
language sql
stable
as $$
    select *
    from proactive_outreach_tracking
    where user_id = p_user_id
      and target_user_id = p_target_user_id
      and reached_out_at >= p_since
    order by reached_out_at desc
    limit 1;
$$;

-- =============================================================================
-- RPC: update_outreach_outcome_v1
-- Update the outcome of an outreach
-- =============================================================================
create or replace function update_outreach_outcome_v1(
    p_outreach_id uuid,
    p_outcome text
)
returns proactive_outreach_tracking
language plpgsql
as $$
declare
    out_row proactive_outreach_tracking;
begin
    update proactive_outreach_tracking
    set
        outcome = p_outcome,
        response_at = now()
    where id = p_outreach_id
    returning * into out_row;

    return out_row;
end;
$$;

-- =============================================================================
-- RPC: update_outreach_outcome_by_connection_request_v1
-- Update outcome by connection request ID (for when user responds)
-- =============================================================================
create or replace function update_outreach_outcome_by_connection_request_v1(
    p_connection_request_id uuid,
    p_outcome text
)
returns proactive_outreach_tracking
language plpgsql
as $$
declare
    out_row proactive_outreach_tracking;
begin
    update proactive_outreach_tracking
    set
        outcome = p_outcome,
        response_at = now()
    where connection_request_id = p_connection_request_id
    returning * into out_row;

    return out_row;
end;
$$;

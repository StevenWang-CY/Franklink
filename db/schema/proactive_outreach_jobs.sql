-- Proactive Outreach Jobs Table and RPC Functions
-- This table tracks scheduled proactive outreach jobs for each user

-- =============================================================================
-- Table: proactive_outreach_jobs
-- =============================================================================
create table if not exists proactive_outreach_jobs (
    user_id uuid primary key references users(id) on delete cascade,
    status text not null check (status in ('queued', 'running', 'done', 'failed', 'skipped')),

    -- Last successful outreach stats
    last_outreach_at timestamptz,
    last_outreach_demand_id uuid references user_email_demands(id) on delete set null,
    last_outreach_connection_request_id uuid references connection_requests(id) on delete set null,

    -- Next scheduled run
    run_after timestamptz not null,

    -- Retry tracking
    attempts int not null default 0,
    last_error text,
    last_skip_reason text,  -- Why job was skipped (no_demands, no_match, user_opted_out, etc.)

    -- Worker claim (for multi-instance safety)
    claimed_by text,
    claimed_at timestamptz,

    updated_at timestamptz not null default now()
);

-- Index for finding due jobs
create index if not exists proactive_outreach_jobs_due_idx
    on proactive_outreach_jobs (status, run_after)
    where status in ('queued', 'running');

-- =============================================================================
-- RPC: schedule_proactive_outreach_job_v1
-- Schedule or reschedule a proactive outreach job for a user
-- =============================================================================
create or replace function schedule_proactive_outreach_job_v1(
    p_user_id uuid,
    p_run_after timestamptz default null
)
returns proactive_outreach_jobs
language plpgsql
as $$
declare
    out_row proactive_outreach_jobs;
    default_run_after timestamptz;
begin
    -- Default: next 6 PM UTC
    default_run_after := coalesce(
        p_run_after,
        date_trunc('day', now() at time zone 'UTC') + interval '18 hours'
    );
    -- If already past 6 PM UTC today, schedule for tomorrow
    if default_run_after <= now() then
        default_run_after := default_run_after + interval '1 day';
    end if;

    insert into proactive_outreach_jobs (user_id, status, run_after, updated_at)
    values (p_user_id, 'queued', default_run_after, now())
    on conflict (user_id) do update
    set
        -- Don't interrupt a running job
        status = case
            when proactive_outreach_jobs.status = 'running' then 'running'
            else 'queued'
        end,
        run_after = case
            when proactive_outreach_jobs.status = 'running' then proactive_outreach_jobs.run_after
            else excluded.run_after
        end,
        updated_at = now()
    returning * into out_row;

    return out_row;
end;
$$;

-- =============================================================================
-- RPC: claim_proactive_outreach_jobs_v1
-- Claim up to max_jobs that are due for processing
-- Uses FOR UPDATE SKIP LOCKED for multi-instance safety
-- =============================================================================
create or replace function claim_proactive_outreach_jobs_v1(
    p_worker_id text,
    p_max_jobs int default 5,
    p_stale_after interval default '30 minutes'
)
returns setof proactive_outreach_jobs
language plpgsql
as $$
begin
    return query
    with candidates as (
        select user_id
        from proactive_outreach_jobs
        where (
            -- Job is queued and due
            (status = 'queued' and run_after <= now())
            -- Or job is stale (claimed but not completed)
            or (status = 'running' and claimed_at <= now() - p_stale_after)
        )
        order by run_after asc
        for update skip locked
        limit p_max_jobs
    )
    update proactive_outreach_jobs j
    set
        status = 'running',
        claimed_by = p_worker_id,
        claimed_at = now(),
        updated_at = now()
    from candidates c
    where j.user_id = c.user_id
    returning j.*;
end;
$$;

-- =============================================================================
-- RPC: complete_proactive_outreach_job_v1
-- Mark a job as complete (outreach was sent) and schedule next run
-- =============================================================================
create or replace function complete_proactive_outreach_job_v1(
    p_user_id uuid,
    p_worker_id text,
    p_demand_id uuid default null,
    p_connection_request_id uuid default null
)
returns proactive_outreach_jobs
language plpgsql
as $$
declare
    out_row proactive_outreach_jobs;
    next_run timestamptz;
begin
    -- Schedule next run for tomorrow at 6 PM UTC
    next_run := date_trunc('day', now() at time zone 'UTC') + interval '1 day' + interval '18 hours';

    update proactive_outreach_jobs j
    set
        status = 'done',
        last_outreach_at = now(),
        last_outreach_demand_id = p_demand_id,
        last_outreach_connection_request_id = p_connection_request_id,
        run_after = next_run,
        attempts = 0,
        last_error = null,
        last_skip_reason = null,
        claimed_by = null,
        claimed_at = null,
        updated_at = now()
    where j.user_id = p_user_id
      and j.claimed_by = p_worker_id
    returning * into out_row;

    return out_row;
end;
$$;

-- =============================================================================
-- RPC: skip_proactive_outreach_job_v1
-- Mark a job as skipped (no outreach sent) and schedule next run
-- =============================================================================
create or replace function skip_proactive_outreach_job_v1(
    p_user_id uuid,
    p_worker_id text,
    p_skip_reason text
)
returns proactive_outreach_jobs
language plpgsql
as $$
declare
    out_row proactive_outreach_jobs;
    next_run timestamptz;
begin
    -- Schedule next run for tomorrow at 6 PM UTC
    next_run := date_trunc('day', now() at time zone 'UTC') + interval '1 day' + interval '18 hours';

    update proactive_outreach_jobs j
    set
        status = 'skipped',
        last_skip_reason = p_skip_reason,
        run_after = next_run,
        attempts = 0,
        last_error = null,
        claimed_by = null,
        claimed_at = null,
        updated_at = now()
    where j.user_id = p_user_id
      and j.claimed_by = p_worker_id
    returning * into out_row;

    return out_row;
end;
$$;

-- =============================================================================
-- RPC: fail_proactive_outreach_job_v1
-- Mark a job as failed with exponential backoff
-- =============================================================================
create or replace function fail_proactive_outreach_job_v1(
    p_user_id uuid,
    p_worker_id text,
    p_error text,
    p_backoff_seconds int default 1800,  -- 30 minutes default
    p_max_attempts int default 5
)
returns proactive_outreach_jobs
language plpgsql
as $$
declare
    out_row proactive_outreach_jobs;
begin
    update proactive_outreach_jobs j
    set
        attempts = j.attempts + 1,
        last_error = left(coalesce(p_error, ''), 2000),
        status = case
            when (j.attempts + 1) >= p_max_attempts then 'failed'
            else 'queued'
        end,
        run_after = greatest(j.run_after, now() + (p_backoff_seconds || ' seconds')::interval),
        claimed_by = null,
        claimed_at = null,
        updated_at = now()
    where j.user_id = p_user_id
      and j.claimed_by = p_worker_id
    returning * into out_row;

    return out_row;
end;
$$;

-- =============================================================================
-- RPC: release_proactive_outreach_job_v1
-- Release a claimed job back to queued status
-- =============================================================================
create or replace function release_proactive_outreach_job_v1(
    p_user_id uuid,
    p_worker_id text
)
returns void
language plpgsql
as $$
begin
    update proactive_outreach_jobs j
    set
        status = 'queued',
        claimed_by = null,
        claimed_at = null,
        updated_at = now()
    where j.user_id = p_user_id
      and j.claimed_by = p_worker_id;
end;
$$;

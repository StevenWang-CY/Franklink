-- Daily Email Jobs Table and RPC Functions
-- This table tracks scheduled daily email extraction jobs for each user

-- =============================================================================
-- Table: daily_email_jobs
-- =============================================================================
create table if not exists daily_email_jobs (
    user_id uuid primary key references users(id) on delete cascade,
    status text not null check (status in ('queued', 'running', 'done', 'failed')),

    -- Last successful run stats
    last_run_at timestamptz,
    last_run_emails_fetched int default 0,
    last_run_highlights_created int default 0,

    -- Next scheduled run
    run_after timestamptz not null,

    -- Retry tracking
    attempts int not null default 0,
    last_error text,

    -- Worker claim (for multi-instance safety)
    claimed_by text,
    claimed_at timestamptz,

    updated_at timestamptz not null default now()
);

-- Index for finding due jobs
create index if not exists daily_email_jobs_due_idx
    on daily_email_jobs (status, run_after)
    where status in ('queued', 'running');

-- =============================================================================
-- RPC: schedule_daily_email_job_v1
-- Schedule or reschedule a daily email job for a user
-- =============================================================================
create or replace function schedule_daily_email_job_v1(
    p_user_id uuid,
    p_run_after timestamptz default null
)
returns daily_email_jobs
language plpgsql
as $$
declare
    out_row daily_email_jobs;
    default_run_after timestamptz;
begin
    -- Default: next 5 PM UTC
    default_run_after := coalesce(
        p_run_after,
        date_trunc('day', now() at time zone 'UTC') + interval '17 hours'
    );
    -- If already past 5 PM UTC today, schedule for tomorrow
    if default_run_after <= now() then
        default_run_after := default_run_after + interval '1 day';
    end if;

    insert into daily_email_jobs (user_id, status, run_after, updated_at)
    values (p_user_id, 'queued', default_run_after, now())
    on conflict (user_id) do update
    set
        -- Don't interrupt a running job
        status = case
            when daily_email_jobs.status = 'running' then 'running'
            else 'queued'
        end,
        run_after = case
            when daily_email_jobs.status = 'running' then daily_email_jobs.run_after
            else excluded.run_after
        end,
        updated_at = now()
    returning * into out_row;

    return out_row;
end;
$$;

-- =============================================================================
-- RPC: claim_daily_email_jobs_v1
-- Claim up to max_jobs that are due for processing
-- Uses FOR UPDATE SKIP LOCKED for multi-instance safety
-- =============================================================================
create or replace function claim_daily_email_jobs_v1(
    p_worker_id text,
    p_max_jobs int default 10,
    p_stale_after interval default '30 minutes'
)
returns setof daily_email_jobs
language plpgsql
as $$
begin
    return query
    with candidates as (
        select user_id
        from daily_email_jobs
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
    update daily_email_jobs j
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
-- RPC: complete_daily_email_job_v1
-- Mark a job as complete and schedule next run
-- =============================================================================
create or replace function complete_daily_email_job_v1(
    p_user_id uuid,
    p_worker_id text,
    p_emails_fetched int,
    p_highlights_created int
)
returns daily_email_jobs
language plpgsql
as $$
declare
    out_row daily_email_jobs;
    next_run timestamptz;
begin
    -- Schedule next run for tomorrow at 5 PM UTC
    next_run := date_trunc('day', now() at time zone 'UTC') + interval '1 day' + interval '17 hours';

    update daily_email_jobs j
    set
        status = 'done',
        last_run_at = now(),
        last_run_emails_fetched = p_emails_fetched,
        last_run_highlights_created = p_highlights_created,
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
-- RPC: fail_daily_email_job_v1
-- Mark a job as failed with exponential backoff
-- =============================================================================
create or replace function fail_daily_email_job_v1(
    p_user_id uuid,
    p_worker_id text,
    p_error text,
    p_backoff_seconds int default 1800,  -- 30 minutes default
    p_max_attempts int default 5
)
returns daily_email_jobs
language plpgsql
as $$
declare
    out_row daily_email_jobs;
begin
    update daily_email_jobs j
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
-- RPC: release_daily_email_job_v1
-- Release a claimed job back to queued status
-- =============================================================================
create or replace function release_daily_email_job_v1(
    p_user_id uuid,
    p_worker_id text
)
returns void
language plpgsql
as $$
begin
    update daily_email_jobs j
    set
        status = 'queued',
        claimed_by = null,
        claimed_at = null,
        updated_at = now()
    where j.user_id = p_user_id
      and j.claimed_by = p_worker_id;
end;
$$;

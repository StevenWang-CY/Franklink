-- Group Chat Follow-up (v1)
--
-- One row per chat follow-up job:
--   - group_chat_followup_jobs_v1
-- Required RPC helpers:
--   - schedule_group_chat_followup_job_v1
--   - claim_group_chat_followup_jobs_v1
--   - complete_group_chat_followup_job_v1
--   - fail_group_chat_followup_job_v1

create table if not exists group_chat_followup_jobs_v1 (
  chat_guid text primary key,
  status text not null check (status in ('queued', 'running', 'done', 'failed')),

  last_user_message_at timestamptz not null,
  last_user_event_id text not null,
  run_after timestamptz not null,

  last_nudge_at timestamptz,
  last_nudge_event_id text,

  attempts int not null default 0,
  last_error text,

  claimed_by text,
  claimed_at timestamptz,
  updated_at timestamptz not null default now()
);

create index if not exists group_chat_followup_jobs_due_idx
  on group_chat_followup_jobs_v1 (status, run_after);

-- Debounced scheduling: called on inbound user messages.
create or replace function schedule_group_chat_followup_job_v1(
  p_chat_guid text,
  p_last_user_message_at timestamptz,
  p_last_user_event_id text,
  p_inactivity_window interval default '24 hours'
)
returns group_chat_followup_jobs_v1
language plpgsql
as $$
declare
  out_row group_chat_followup_jobs_v1;
begin
  insert into group_chat_followup_jobs_v1 (
    chat_guid,
    status,
    last_user_message_at,
    last_user_event_id,
    run_after,
    attempts,
    last_error,
    updated_at
  ) values (
    p_chat_guid,
    'queued',
    p_last_user_message_at,
    p_last_user_event_id,
    p_last_user_message_at + p_inactivity_window,
    0,
    null,
    now()
  )
  on conflict (chat_guid) do update
  set
    last_user_message_at = case
      when excluded.last_user_message_at >= group_chat_followup_jobs_v1.last_user_message_at then excluded.last_user_message_at
      else group_chat_followup_jobs_v1.last_user_message_at
    end,
    last_user_event_id = case
      when excluded.last_user_message_at >= group_chat_followup_jobs_v1.last_user_message_at then excluded.last_user_event_id
      else group_chat_followup_jobs_v1.last_user_event_id
    end,
    run_after = case
      when excluded.last_user_message_at >= group_chat_followup_jobs_v1.last_user_message_at
        then excluded.last_user_message_at + p_inactivity_window
      else group_chat_followup_jobs_v1.run_after
    end,
    attempts = case
      when excluded.last_user_message_at >= group_chat_followup_jobs_v1.last_user_message_at then 0
      else group_chat_followup_jobs_v1.attempts
    end,
    last_error = case
      when excluded.last_user_message_at >= group_chat_followup_jobs_v1.last_user_message_at then null
      else group_chat_followup_jobs_v1.last_error
    end,
    last_nudge_at = case
      when excluded.last_user_message_at >= group_chat_followup_jobs_v1.last_user_message_at then null
      else group_chat_followup_jobs_v1.last_nudge_at
    end,
    last_nudge_event_id = case
      when excluded.last_user_message_at >= group_chat_followup_jobs_v1.last_user_message_at then null
      else group_chat_followup_jobs_v1.last_nudge_event_id
    end,
    status = case when group_chat_followup_jobs_v1.status = 'running' then 'running' else 'queued' end,
    updated_at = now()
  returning * into out_row;

  return out_row;
end;
$$;

-- Multi-instance safe claiming: FOR UPDATE SKIP LOCKED.
create or replace function claim_group_chat_followup_jobs_v1(
  p_worker_id text,
  p_max_jobs int default 5,
  p_stale_after interval default '20 minutes'
)
returns setof group_chat_followup_jobs_v1
language plpgsql
as $$
begin
  return query
  with candidates as (
    select chat_guid
    from group_chat_followup_jobs_v1
    where (
      (status = 'queued' and run_after <= now() and (claimed_at is null or claimed_at <= now() - p_stale_after))
      or (status = 'running' and claimed_at <= now() - p_stale_after)
    )
    order by run_after asc
    for update skip locked
    limit greatest(1, least(coalesce(p_max_jobs, 5), 50))
  )
  update group_chat_followup_jobs_v1 j
  set
    status = 'running',
    claimed_by = p_worker_id,
    claimed_at = now(),
    updated_at = now()
  from candidates c
  where j.chat_guid = c.chat_guid
  returning j.*;
end;
$$;

-- Finish a job safely: if new messages arrived while running, release back to queued.
create or replace function complete_group_chat_followup_job_v1(
  p_chat_guid text,
  p_worker_id text,
  p_expected_last_user_event_id text,
  p_nudge_sent_at timestamptz,
  p_nudge_event_id text
)
returns group_chat_followup_jobs_v1
language plpgsql
as $$
declare
  out_row group_chat_followup_jobs_v1;
begin
  update group_chat_followup_jobs_v1 j
  set
    status = case when j.last_user_event_id = p_expected_last_user_event_id then 'done' else 'queued' end,
    attempts = case when j.last_user_event_id = p_expected_last_user_event_id then 0 else j.attempts end,
    last_error = case when j.last_user_event_id = p_expected_last_user_event_id then null else j.last_error end,
    last_nudge_at = case when j.last_user_event_id = p_expected_last_user_event_id then p_nudge_sent_at else j.last_nudge_at end,
    last_nudge_event_id = case when j.last_user_event_id = p_expected_last_user_event_id then p_nudge_event_id else j.last_nudge_event_id end,
    claimed_by = null,
    claimed_at = null,
    updated_at = now()
  where j.chat_guid = p_chat_guid and j.claimed_by = p_worker_id
  returning * into out_row;

  return out_row;
end;
$$;

-- Record a failure with backoff. If the job anchor changed while running, do not overwrite it.
create or replace function fail_group_chat_followup_job_v1(
  p_chat_guid text,
  p_worker_id text,
  p_expected_last_user_event_id text,
  p_error text,
  p_backoff interval default '60 seconds',
  p_max_attempts int default 6
)
returns group_chat_followup_jobs_v1
language plpgsql
as $$
declare
  out_row group_chat_followup_jobs_v1;
begin
  update group_chat_followup_jobs_v1 j
  set
    attempts = case
      when j.last_user_event_id = p_expected_last_user_event_id then j.attempts + 1
      else j.attempts
    end,
    last_error = case
      when j.last_user_event_id = p_expected_last_user_event_id then left(coalesce(p_error, ''), 1800)
      else j.last_error
    end,
    status = case
      when j.last_user_event_id = p_expected_last_user_event_id and (j.attempts + 1) >= p_max_attempts then 'failed'
      else 'queued'
    end,
    run_after = case
      when j.last_user_event_id = p_expected_last_user_event_id then greatest(j.run_after, now() + p_backoff)
      else j.run_after
    end,
    claimed_by = null,
    claimed_at = null,
    updated_at = now()
  where j.chat_guid = p_chat_guid and j.claimed_by = p_worker_id
  returning * into out_row;

  return out_row;
end;
$$;

-- Group Chat Summary (v1)
--
-- One row per chat summary memory:
--   - group_chat_summary_memory_v1
-- One row per chat raw transcript tail:
--   - group_chat_raw_memory_v1
-- Debounced "next run" job row per chat:
--   - group_chat_summary_jobs
-- Required RPC helpers for correctness:
--   - schedule_group_chat_summary_job_v1
--   - claim_group_chat_summary_jobs_v1
--   - append_group_chat_raw_message_v1
--   - get_group_chat_raw_messages_window_v1
--   - prune_group_chat_raw_memory_before_v1
--   - ingest_group_chat_user_message_and_schedule_summary_v1
--   - append_group_chat_summary_memory_segment_v1
--   - complete_group_chat_summary_job_v1
--   - fail_group_chat_summary_job_v1

create table if not exists group_chat_summary_memory_v1 (
  chat_guid text primary key,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),

  last_segment_end_at timestamptz,

  segment_end_at timestamptz[] not null default '{}'::timestamptz[],
  last_user_message_at timestamptz[] not null default '{}'::timestamptz[],
  last_user_event_id text[] not null default '{}'::text[],
  summary_md text[] not null default '{}'::text[],

  constraint group_chat_summary_memory_v1_len_check check (
    coalesce(array_length(segment_end_at, 1), 0) = coalesce(array_length(last_user_message_at, 1), 0)
    and coalesce(array_length(segment_end_at, 1), 0) = coalesce(array_length(last_user_event_id, 1), 0)
    and coalesce(array_length(segment_end_at, 1), 0) = coalesce(array_length(summary_md, 1), 0)
  )
);

create index if not exists group_chat_summary_memory_v1_last_segment_end_at_idx
  on group_chat_summary_memory_v1 (last_segment_end_at);

-- One row per chat raw transcript tail (bounded + prunable).
create table if not exists group_chat_raw_memory_v1 (
  chat_guid text primary key,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),

  last_pruned_before timestamptz,
  last_event_at timestamptz,

  event_id text[] not null default '{}'::text[],
  message_id text[] not null default '{}'::text[],
  role text[] not null default '{}'::text[],
  sender_user_id uuid[] not null default '{}'::uuid[],
  sender_handle text[] not null default '{}'::text[],
  sent_at timestamptz[] not null default '{}'::timestamptz[],
  content text[] not null default '{}'::text[],
  media_url text[] not null default '{}'::text[],
  msg_type text[] not null default '{}'::text[],

  constraint group_chat_raw_memory_v1_len_check check (
    coalesce(array_length(event_id, 1), 0) = coalesce(array_length(message_id, 1), 0)
    and coalesce(array_length(event_id, 1), 0) = coalesce(array_length(role, 1), 0)
    and coalesce(array_length(event_id, 1), 0) = coalesce(array_length(sender_user_id, 1), 0)
    and coalesce(array_length(event_id, 1), 0) = coalesce(array_length(sender_handle, 1), 0)
    and coalesce(array_length(event_id, 1), 0) = coalesce(array_length(sent_at, 1), 0)
    and coalesce(array_length(event_id, 1), 0) = coalesce(array_length(content, 1), 0)
    and coalesce(array_length(event_id, 1), 0) = coalesce(array_length(media_url, 1), 0)
    and coalesce(array_length(event_id, 1), 0) = coalesce(array_length(msg_type, 1), 0)
  )
);

create index if not exists group_chat_raw_memory_v1_last_event_at_idx
  on group_chat_raw_memory_v1 (last_event_at);

-- Atomic idempotent append into the one-row-per-chat raw memory table.
create or replace function append_group_chat_raw_message_v1(
  p_chat_guid text,
  p_event_id text,
  p_message_id text,
  p_role text,
  p_sender_user_id uuid,
  p_sender_handle text,
  p_sent_at timestamptz,
  p_content text,
  p_media_url text,
  p_msg_type text default '',
  p_keep_last_n int default 800
)
returns group_chat_raw_memory_v1
language plpgsql
as $$
declare
  out_row group_chat_raw_memory_v1;
  n int;
  keep_n int;
begin
  keep_n := greatest(1, least(coalesce(p_keep_last_n, 800), 4000));

  insert into group_chat_raw_memory_v1 (
    chat_guid,
    updated_at,
    last_event_at,
    event_id,
    message_id,
    role,
    sender_user_id,
    sender_handle,
    sent_at,
    content,
    media_url,
    msg_type
  ) values (
    p_chat_guid,
    now(),
    p_sent_at,
    array[p_event_id],
    array[coalesce(p_message_id, '')],
    array[coalesce(p_role, '')],
    array[p_sender_user_id],
    array[coalesce(p_sender_handle, '')],
    array[p_sent_at],
    array[coalesce(p_content, '')],
    array[coalesce(p_media_url, '')],
    array[coalesce(p_msg_type, '')]
  )
  on conflict (chat_guid) do update
  set
    updated_at = now(),
    last_event_at = greatest(group_chat_raw_memory_v1.last_event_at, p_sent_at),

    -- Idempotency: if we've already stored this event_id, no-op.
    event_id = case
      when p_event_id = any(group_chat_raw_memory_v1.event_id)
        then group_chat_raw_memory_v1.event_id
      else array_append(group_chat_raw_memory_v1.event_id, p_event_id)
    end,
    message_id = case
      when p_event_id = any(group_chat_raw_memory_v1.event_id)
        then group_chat_raw_memory_v1.message_id
      else array_append(group_chat_raw_memory_v1.message_id, coalesce(p_message_id, ''))
    end,
    role = case
      when p_event_id = any(group_chat_raw_memory_v1.event_id)
        then group_chat_raw_memory_v1.role
      else array_append(group_chat_raw_memory_v1.role, coalesce(p_role, ''))
    end,
    sender_user_id = case
      when p_event_id = any(group_chat_raw_memory_v1.event_id)
        then group_chat_raw_memory_v1.sender_user_id
      else array_append(group_chat_raw_memory_v1.sender_user_id, p_sender_user_id)
    end,
    sender_handle = case
      when p_event_id = any(group_chat_raw_memory_v1.event_id)
        then group_chat_raw_memory_v1.sender_handle
      else array_append(group_chat_raw_memory_v1.sender_handle, coalesce(p_sender_handle, ''))
    end,
    sent_at = case
      when p_event_id = any(group_chat_raw_memory_v1.event_id)
        then group_chat_raw_memory_v1.sent_at
      else array_append(group_chat_raw_memory_v1.sent_at, p_sent_at)
    end,
    content = case
      when p_event_id = any(group_chat_raw_memory_v1.event_id)
        then group_chat_raw_memory_v1.content
      else array_append(group_chat_raw_memory_v1.content, coalesce(p_content, ''))
    end,
    media_url = case
      when p_event_id = any(group_chat_raw_memory_v1.event_id)
        then group_chat_raw_memory_v1.media_url
      else array_append(group_chat_raw_memory_v1.media_url, coalesce(p_media_url, ''))
    end,
    msg_type = case
      when p_event_id = any(group_chat_raw_memory_v1.event_id)
        then group_chat_raw_memory_v1.msg_type
      else array_append(group_chat_raw_memory_v1.msg_type, coalesce(p_msg_type, ''))
    end
  returning * into out_row;

  -- Bound row size (keep last N messages).
  n := coalesce(array_length(out_row.event_id, 1), 0);
  if n > keep_n then
    update group_chat_raw_memory_v1 r
    set
      event_id = r.event_id[(n - keep_n + 1):n],
      message_id = r.message_id[(n - keep_n + 1):n],
      role = r.role[(n - keep_n + 1):n],
      sender_user_id = r.sender_user_id[(n - keep_n + 1):n],
      sender_handle = r.sender_handle[(n - keep_n + 1):n],
      sent_at = r.sent_at[(n - keep_n + 1):n],
      content = r.content[(n - keep_n + 1):n],
      media_url = r.media_url[(n - keep_n + 1):n],
      msg_type = r.msg_type[(n - keep_n + 1):n],
      updated_at = now()
    where r.chat_guid = p_chat_guid
    returning * into out_row;
  end if;

  return out_row;
end;
$$;

-- Fetch a transcript window as rows (for workers/features).
create or replace function get_group_chat_raw_messages_window_v1(
  p_chat_guid text,
  p_start_at timestamptz default null,
  p_end_at timestamptz default null,
  p_limit int default 200
)
returns table (
  msg_index int,
  event_id text,
  message_id text,
  role text,
  sender_user_id uuid,
  sender_handle text,
  sent_at timestamptz,
  content text,
  media_url text,
  msg_type text
)
language sql
as $$
  select *
  from (
    select
      u.msg_index::int,
      u.event_id,
      u.message_id,
      u.role,
      u.sender_user_id,
      u.sender_handle,
      u.sent_at,
      u.content,
      u.media_url,
      u.msg_type
    from group_chat_raw_memory_v1 r
    cross join lateral unnest(
      r.event_id,
      r.message_id,
      r.role,
      r.sender_user_id,
      r.sender_handle,
      r.sent_at,
      r.content,
      r.media_url,
      r.msg_type
    ) with ordinality as u(
      event_id,
      message_id,
      role,
      sender_user_id,
      sender_handle,
      sent_at,
      content,
      media_url,
      msg_type,
      msg_index
    )
    where r.chat_guid = p_chat_guid
      and (p_start_at is null or u.sent_at >= p_start_at)
      and (p_end_at is null or u.sent_at < p_end_at)
    order by u.sent_at desc, u.msg_index desc
    limit greatest(1, least(coalesce(p_limit, 200), 1200))
  ) t
  order by t.sent_at asc, t.msg_index asc;
$$;

-- Prune raw memory strictly before a boundary (keep a small overlap tail).
create or replace function prune_group_chat_raw_memory_before_v1(
  p_chat_guid text,
  p_before timestamptz,
  p_keep_tail int default 40
)
returns group_chat_raw_memory_v1
language plpgsql
as $$
declare
  out_row group_chat_raw_memory_v1;
  keep_tail int;
  keep_from_idx int;
  n int;
begin
  keep_tail := greatest(0, least(coalesce(p_keep_tail, 40), 400));

  -- Find the first index whose sent_at >= p_before.
  select min(u.idx) into keep_from_idx
  from group_chat_raw_memory_v1 r
  cross join lateral unnest(r.sent_at) with ordinality as u(ts, idx)
  where r.chat_guid = p_chat_guid and u.ts >= p_before;

  if keep_from_idx is null then
    select coalesce(array_length(r.event_id, 1), 0) into n
    from group_chat_raw_memory_v1 r
    where r.chat_guid = p_chat_guid;
    keep_from_idx := greatest(1, n - keep_tail + 1);
  else
    keep_from_idx := greatest(1, keep_from_idx - keep_tail);
  end if;

  update group_chat_raw_memory_v1 r
  set
    event_id = r.event_id[keep_from_idx:],
    message_id = r.message_id[keep_from_idx:],
    role = r.role[keep_from_idx:],
    sender_user_id = r.sender_user_id[keep_from_idx:],
    sender_handle = r.sender_handle[keep_from_idx:],
    sent_at = r.sent_at[keep_from_idx:],
    content = r.content[keep_from_idx:],
    media_url = r.media_url[keep_from_idx:],
    msg_type = r.msg_type[keep_from_idx:],
    last_pruned_before = p_before,
    updated_at = now()
  where r.chat_guid = p_chat_guid
  returning * into out_row;

  return out_row;
end;
$$;

create table if not exists group_chat_summary_jobs (
  chat_guid text primary key,
  status text not null check (status in ('queued', 'running', 'done', 'failed')),

  last_user_message_at timestamptz not null,
  last_user_event_id text not null,
  run_after timestamptz not null,

  attempts int not null default 0,
  last_error text,

  claimed_by text,
  claimed_at timestamptz,
  updated_at timestamptz not null default now()
);

create index if not exists group_chat_summary_jobs_due_idx
  on group_chat_summary_jobs (status, run_after);

-- Debounced scheduling: called on every inbound user message.
create or replace function schedule_group_chat_summary_job_v1(
  p_chat_guid text,
  p_last_user_message_at timestamptz,
  p_last_user_event_id text,
  p_inactivity_window interval default '5 minutes'
)
returns group_chat_summary_jobs
language plpgsql
as $$
declare
  out_row group_chat_summary_jobs;
begin
  insert into group_chat_summary_jobs (
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
    -- Out-of-order safety: only move the "last user" anchor forward.
    last_user_message_at = case
      when excluded.last_user_message_at >= group_chat_summary_jobs.last_user_message_at then excluded.last_user_message_at
      else group_chat_summary_jobs.last_user_message_at
    end,
    last_user_event_id = case
      when excluded.last_user_message_at >= group_chat_summary_jobs.last_user_message_at then excluded.last_user_event_id
      else group_chat_summary_jobs.last_user_event_id
    end,
    -- IMPORTANT: new user activity should clear any previous backoff/old run_after.
    -- Only preserve run_after when the incoming message is out-of-order (older than current anchor).
    run_after = case
      when excluded.last_user_message_at >= group_chat_summary_jobs.last_user_message_at
        then excluded.last_user_message_at + p_inactivity_window
      else group_chat_summary_jobs.run_after
    end,

    -- New user activity clears previous errors/backoff.
    attempts = case
      when excluded.last_user_message_at >= group_chat_summary_jobs.last_user_message_at then 0
      else group_chat_summary_jobs.attempts
    end,
    last_error = case
      when excluded.last_user_message_at >= group_chat_summary_jobs.last_user_message_at then null
      else group_chat_summary_jobs.last_error
    end,

    -- Preserve running so another worker can't claim concurrently.
    status = case when group_chat_summary_jobs.status = 'running' then 'running' else 'queued' end,
    updated_at = now()
  returning * into out_row;

  return out_row;
end;
$$;

-- Ingest inbound user message (append raw) + schedule summary in a single DB transaction.
create or replace function ingest_group_chat_user_message_and_schedule_summary_v1(
  p_chat_guid text,
  p_event_id text,
  p_message_id text,
  p_sender_user_id uuid,
  p_sender_handle text,
  p_sent_at timestamptz,
  p_content text,
  p_media_url text,
  p_inactivity_window interval default '5 minutes',
  p_keep_last_n int default 800
)
returns group_chat_summary_jobs
language plpgsql
as $$
declare
  job_row group_chat_summary_jobs;
begin
  perform append_group_chat_raw_message_v1(
    p_chat_guid,
    p_event_id,
    p_message_id,
    'user',
    p_sender_user_id,
    p_sender_handle,
    p_sent_at,
    p_content,
    p_media_url,
    'user_message',
    p_keep_last_n
  );

  job_row := schedule_group_chat_summary_job_v1(
    p_chat_guid,
    p_sent_at,
    p_event_id,
    p_inactivity_window
  );

  return job_row;
end;
$$;

-- Multi-instance safe claiming: FOR UPDATE SKIP LOCKED.
create or replace function claim_group_chat_summary_jobs_v1(
  p_worker_id text,
  p_max_jobs int default 5,
  p_stale_after interval default '20 minutes'
)
returns setof group_chat_summary_jobs
language plpgsql
as $$
begin
  return query
  with candidates as (
    select chat_guid
    from group_chat_summary_jobs
    where (
      (status = 'queued' and run_after <= now() and (claimed_at is null or claimed_at <= now() - p_stale_after))
      or (status = 'running' and claimed_at <= now() - p_stale_after)
    )
    order by run_after asc
    for update skip locked
    limit p_max_jobs
  )
  update group_chat_summary_jobs j
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

-- Atomic append into the one-row-per-chat summary memory table.
create or replace function append_group_chat_summary_memory_segment_v1(
  p_chat_guid text,
  p_last_user_event_id text,
  p_last_user_message_at timestamptz,
  p_segment_end_at timestamptz,
  p_summary_md text
)
returns group_chat_summary_memory_v1
language plpgsql
as $$
declare
  out_row group_chat_summary_memory_v1;
begin
  insert into group_chat_summary_memory_v1 (
    chat_guid,
    updated_at,
    last_segment_end_at,
    segment_end_at,
    last_user_message_at,
    last_user_event_id,
    summary_md
  ) values (
    p_chat_guid,
    now(),
    p_segment_end_at,
    array[p_segment_end_at],
    array[p_last_user_message_at],
    array[p_last_user_event_id],
    array[p_summary_md]
  )
  on conflict (chat_guid) do update
  set
    updated_at = now(),

    -- Idempotency: if we've already stored this segment, no-op.
    segment_end_at = case
      when p_last_user_event_id = any(group_chat_summary_memory_v1.last_user_event_id)
        then group_chat_summary_memory_v1.segment_end_at
      else array_append(group_chat_summary_memory_v1.segment_end_at, p_segment_end_at)
    end,
    last_user_message_at = case
      when p_last_user_event_id = any(group_chat_summary_memory_v1.last_user_event_id)
        then group_chat_summary_memory_v1.last_user_message_at
      else array_append(group_chat_summary_memory_v1.last_user_message_at, p_last_user_message_at)
    end,
    last_user_event_id = case
      when p_last_user_event_id = any(group_chat_summary_memory_v1.last_user_event_id)
        then group_chat_summary_memory_v1.last_user_event_id
      else array_append(group_chat_summary_memory_v1.last_user_event_id, p_last_user_event_id)
    end,
    summary_md = case
      when p_last_user_event_id = any(group_chat_summary_memory_v1.last_user_event_id)
        then group_chat_summary_memory_v1.summary_md
      else array_append(group_chat_summary_memory_v1.summary_md, p_summary_md)
    end,

    last_segment_end_at = case
      when group_chat_summary_memory_v1.last_segment_end_at is null then p_segment_end_at
      else greatest(group_chat_summary_memory_v1.last_segment_end_at, p_segment_end_at)
    end
  returning * into out_row;

  return out_row;
end;
$$;

-- Finish a job safely: if new messages arrived while running, release back to queued.
create or replace function complete_group_chat_summary_job_v1(
  p_chat_guid text,
  p_worker_id text,
  p_expected_last_user_event_id text
)
returns group_chat_summary_jobs
language plpgsql
as $$
declare
  out_row group_chat_summary_jobs;
begin
  update group_chat_summary_jobs j
  set
    status = case when j.last_user_event_id = p_expected_last_user_event_id then 'done' else 'queued' end,
    attempts = case when j.last_user_event_id = p_expected_last_user_event_id then 0 else j.attempts end,
    last_error = case when j.last_user_event_id = p_expected_last_user_event_id then null else j.last_error end,
    claimed_by = null,
    claimed_at = null,
    updated_at = now()
  where j.chat_guid = p_chat_guid and j.claimed_by = p_worker_id
  returning * into out_row;

  return out_row;
end;
$$;

-- Record a failure with backoff. If the job anchor changed while running, do not overwrite it.
create or replace function fail_group_chat_summary_job_v1(
  p_chat_guid text,
  p_worker_id text,
  p_expected_last_user_event_id text,
  p_error text,
  p_backoff interval default '60 seconds',
  p_max_attempts int default 6
)
returns group_chat_summary_jobs
language plpgsql
as $$
declare
  out_row group_chat_summary_jobs;
begin
  update group_chat_summary_jobs j
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

-- Optional: query segments as rows for debugging/research.
create or replace view group_chat_summary_segments_v1 as
select
  m.chat_guid,
  u.segment_index,
  u.segment_end_at,
  u.last_user_message_at,
  u.last_user_event_id,
  u.summary_md
from group_chat_summary_memory_v1 m
cross join lateral unnest(
  m.segment_end_at,
  m.last_user_message_at,
  m.last_user_event_id,
  m.summary_md
) with ordinality as u(
  segment_end_at,
  last_user_message_at,
  last_user_event_id,
  summary_md,
  segment_index
);

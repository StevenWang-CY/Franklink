-- Migration: Create task_state table for task execution history
-- Purpose: Store completed task states to provide context for future interactions
-- Run this migration via Supabase dashboard or CLI

create table if not exists task_state (
  id uuid primary key default gen_random_uuid(),

  -- User reference
  user_id uuid not null references users(id) on delete cascade,

  -- Task identification
  task_name text not null,                -- e.g., "networking", "update"

  -- Concise task summary (for context, not full replay)
  instruction text,                       -- What the user wanted (refined instruction)
  outcome text,                           -- Brief outcome summary
  status text not null,                   -- "complete" or "failed"

  -- Key data points (compact JSON)
  key_data jsonb default '{}'::jsonb,     -- Important results (e.g., match found, field updated)

  -- Timestamps
  created_at timestamptz not null default now()
);

-- Index for efficient user queries (most recent first)
create index if not exists idx_task_state_user_id_created on task_state(user_id, created_at desc);

-- Index for task name filtering
create index if not exists idx_task_state_task_name on task_state(task_name);

-- RLS policies (backend-only access via service role)
alter table task_state enable row level security;

create policy "Service role can manage all task states"
  on task_state for all
  using (true)
  with check (true);

-- Optional: Auto-cleanup old states (keep last 30 days)
-- Uncomment if you want automatic cleanup
-- create or replace function cleanup_old_task_states()
-- returns trigger as $$
-- begin
--   delete from task_state
--   where created_at < now() - interval '30 days';
--   return null;
-- end;
-- $$ language plpgsql;

-- create trigger trigger_cleanup_old_task_states
--   after insert on task_state
--   execute function cleanup_old_task_states();

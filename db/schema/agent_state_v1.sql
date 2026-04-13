-- Migration: Create agent_state table for task persistence
-- Purpose: Store agent state for task resumption in the new Task + Tool architecture
-- Run this migration via Supabase dashboard or CLI

create table if not exists agent_state (
  thread_id text primary key,

  -- State storage
  state jsonb not null,                 -- Serialized ExecutionMemory + task context
  task_name text,                       -- Task name for filtering/debugging

  -- Timestamps
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

-- Indexes for efficient queries
create index if not exists idx_agent_state_task_name on agent_state(task_name);
create index if not exists idx_agent_state_updated_at on agent_state(updated_at);

-- RLS policies (backend-only access via service role)
alter table agent_state enable row level security;

create policy "Service role can manage all agent states"
  on agent_state for all
  using (true)
  with check (true);

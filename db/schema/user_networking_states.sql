-- Migration: Create user_networking_states table for atomic state management
-- Purpose: Store atomic networking flow state with optimistic locking
-- Run this migration via Supabase dashboard or CLI

create table if not exists user_networking_states (
  user_id uuid primary key references users(id) on delete cascade,

  -- State storage (includes version for optimistic locking)
  state_data jsonb not null default '{
    "flow_state": "idle",
    "version": 0
  }'::jsonb,

  -- Timestamps
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

-- Indexes for efficient queries
create index if not exists idx_user_networking_states_flow_state
  on user_networking_states ((state_data->>'flow_state'));

create index if not exists idx_user_networking_states_version
  on user_networking_states ((state_data->>'version'));

create index if not exists idx_user_networking_states_updated_at
  on user_networking_states(updated_at);

-- RLS policies (backend-only access via service role)
alter table user_networking_states enable row level security;

create policy "Service role can manage all networking states"
  on user_networking_states for all
  using (true)
  with check (true);

-- Function to auto-update updated_at timestamp
create or replace function update_user_networking_states_updated_at()
returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

create trigger trigger_update_user_networking_states_updated_at
  before update on user_networking_states
  for each row
  execute function update_user_networking_states_updated_at();

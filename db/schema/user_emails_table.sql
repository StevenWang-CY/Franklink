-- Migration: Create user_emails table for persistent email storage
-- Purpose: Store emails fetched from Composio to avoid re-fetching on every stage
-- Run this migration via Supabase dashboard or CLI

create table if not exists user_emails (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references users(id) on delete cascade,

  -- Email identifiers
  message_id text,              -- Gmail message ID for deduplication

  -- Email content
  sender text not null,         -- Full sender (name + email)
  sender_domain text,           -- Extracted domain for filtering
  subject text,
  body text,                    -- Full body (not truncated like email_signals)
  snippet text,                 -- Short preview

  -- Metadata
  received_at timestamptz,      -- When email was received
  fetched_at timestamptz not null default now(),  -- When we fetched it from Composio
  is_sensitive boolean default false,  -- PII flag (OTP, bank, medical)
  is_sent boolean default false,       -- True if user sent this email (vs received)

  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

-- Indexes for efficient queries
create index if not exists idx_user_emails_user_id on user_emails(user_id);
create index if not exists idx_user_emails_fetched_at on user_emails(user_id, fetched_at desc);
create unique index if not exists idx_user_emails_message_id on user_emails(user_id, message_id);
create index if not exists idx_user_emails_sent on user_emails(user_id, is_sent, fetched_at desc);

-- Migration for existing tables: Add is_sent column if it doesn't exist
-- Run this separately if table already exists:
-- ALTER TABLE user_emails ADD COLUMN IF NOT EXISTS is_sent boolean default false;
-- CREATE INDEX IF NOT EXISTS idx_user_emails_sent ON user_emails(user_id, is_sent, fetched_at desc);

-- RLS policies
alter table user_emails enable row level security;

create policy "Users can view their own emails"
  on user_emails for select
  using (auth.uid() = user_id);

create policy "Service role can manage all emails"
  on user_emails for all
  using (true)
  with check (true);

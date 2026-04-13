-- Add proactive_preference column to users table
-- This allows users to opt-out of proactive networking suggestions

alter table users add column if not exists proactive_preference boolean default true;

-- Add comment for documentation
comment on column users.proactive_preference is 'Whether user wants to receive proactive networking suggestions. true=opted in (default), false=opted out';

-- Add onboarding summary history fields to users table
alter table users add column if not exists value_history jsonb default '[]'::jsonb;
alter table users add column if not exists demand_history jsonb default '[]'::jsonb;

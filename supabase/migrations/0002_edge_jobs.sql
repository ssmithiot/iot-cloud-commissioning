-- Cloud-to-edge job framework schema.
-- Review before applying to a live Supabase project.

do $$
begin
    if not exists (select 1 from pg_type where typname = 'edge_job_status') then
        create type public.edge_job_status as enum ('queued', 'claimed', 'completed', 'failed');
    end if;
end $$;

create table if not exists public.edge_jobs (
    id uuid primary key default gen_random_uuid(),
    job_id text not null unique,
    gateway_id text not null,
    job_type text not null,
    status public.edge_job_status not null default 'queued',
    request_json jsonb not null default '{}'::jsonb,
    result_json jsonb,
    error_message text,
    created_at timestamptz not null default now(),
    claimed_at timestamptz,
    completed_at timestamptz,
    created_by uuid,
    metadata jsonb not null default '{}'::jsonb
);

create index if not exists idx_edge_jobs_job_id on public.edge_jobs(job_id);
create index if not exists idx_edge_jobs_gateway_id on public.edge_jobs(gateway_id);
create index if not exists idx_edge_jobs_gateway_status_created on public.edge_jobs(gateway_id, status, created_at);
create index if not exists idx_edge_jobs_status on public.edge_jobs(status);
create index if not exists idx_edge_jobs_job_type on public.edge_jobs(job_type);
create index if not exists idx_edge_jobs_created_at on public.edge_jobs(created_at desc);

alter table public.edge_jobs enable row level security;

comment on table public.edge_jobs is 'Cloud-to-edge jobs. Edge gateways claim and report jobs through the cloud API, not direct database access.';
comment on column public.edge_jobs.status is 'Stable status values: queued, claimed, completed, failed.';
comment on column public.edge_jobs.request_json is 'Job request payload. Use jsonb for handler-specific data.';
comment on column public.edge_jobs.result_json is 'Job result payload returned by the edge agent.';

-- TODO: Add a transactional claim RPC or Edge Function before high concurrency production use.
-- TODO: Add RLS policies for portal users after Supabase Auth and memberships are active.
-- No broad public policies are created in this MVP.

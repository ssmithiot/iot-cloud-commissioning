-- Durable requests created by the Gateway Registry update controls.

create table if not exists public.gateway_update_requests (
    id uuid primary key default gen_random_uuid(),
    gateway_id text not null references public.edge_nodes(gateway_id) on delete cascade,
    requested_by text,
    status text not null default 'queued',
    requested_at timestamptz not null default now(),
    started_at timestamptz,
    completed_at timestamptz,
    error_message text
);

create index if not exists idx_gateway_update_requests_gateway_id
    on public.gateway_update_requests(gateway_id);
create index if not exists idx_gateway_update_requests_status
    on public.gateway_update_requests(status);

alter table public.gateway_update_requests enable row level security;

comment on table public.gateway_update_requests is 'Cloud-to-local updater requests for edge application updates.';

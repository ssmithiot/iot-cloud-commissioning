-- Core cloud schema for IOT Cloud Commissioning.
-- Review before applying to a live Supabase project.

create extension if not exists pgcrypto;

create table if not exists public.organizations (
    id uuid primary key default gen_random_uuid(),
    name text not null unique,
    slug text unique,
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists public.sites (
    id uuid primary key default gen_random_uuid(),
    organization_id uuid references public.organizations(id) on delete set null,
    site_id text not null unique,
    name text not null,
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists public.edge_nodes (
    id uuid primary key default gen_random_uuid(),
    site_id text not null references public.sites(site_id) on delete restrict,
    gateway_id text not null unique,
    hostname text not null,
    lan_ip text,
    bacnet_port integer not null default 47814,
    agent_version text not null,
    ui_version text not null,
    sqlite_db_ok boolean not null default false,
    queued_upload_count integer not null default 0,
    latest_status text not null default 'unknown',
    latest_heartbeat_at timestamptz,
    last_seen_at timestamptz,
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists public.edge_heartbeats (
    id uuid primary key default gen_random_uuid(),
    edge_node_id uuid references public.edge_nodes(id) on delete cascade,
    gateway_id text not null,
    site_id text not null,
    hostname text not null,
    lan_ip text,
    bacnet_port integer not null,
    agent_version text not null,
    ui_version text not null,
    sqlite_db_ok boolean not null,
    queued_upload_count integer not null,
    timestamp_utc timestamptz not null,
    received_at timestamptz not null default now(),
    payload jsonb not null default '{}'::jsonb
);

create index if not exists idx_sites_organization_id on public.sites(organization_id);
create index if not exists idx_sites_site_id on public.sites(site_id);
create index if not exists idx_edge_nodes_gateway_id on public.edge_nodes(gateway_id);
create index if not exists idx_edge_nodes_site_id on public.edge_nodes(site_id);
create index if not exists idx_edge_nodes_last_seen_at on public.edge_nodes(last_seen_at desc);
create index if not exists idx_edge_nodes_latest_status on public.edge_nodes(latest_status);
create index if not exists idx_edge_heartbeats_gateway_id on public.edge_heartbeats(gateway_id);
create index if not exists idx_edge_heartbeats_site_id on public.edge_heartbeats(site_id);
create index if not exists idx_edge_heartbeats_received_at on public.edge_heartbeats(received_at desc);
create index if not exists idx_edge_heartbeats_timestamp_utc on public.edge_heartbeats(timestamp_utc desc);

alter table public.organizations enable row level security;
alter table public.sites enable row level security;
alter table public.edge_nodes enable row level security;
alter table public.edge_heartbeats enable row level security;

comment on table public.organizations is 'Tenant organizations. RLS policies will be added when Supabase Auth is wired.';
comment on table public.sites is 'Customer sites. Server-side API access only for MVP.';
comment on table public.edge_nodes is 'Registered edge gateways and latest status.';
comment on table public.edge_heartbeats is 'Heartbeat history from edge gateways.';

comment on column public.edge_nodes.gateway_id is 'Stable gateway identifier used by edge agents and cloud API.';
comment on column public.edge_heartbeats.payload is 'Flexible future heartbeat payload extension.';

-- TODO: Add RLS policies after Supabase Auth, profiles, and memberships are active.
-- No broad public policies are created in this MVP.

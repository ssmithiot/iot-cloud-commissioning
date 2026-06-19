-- Future feature placeholders for files, trends, samples, and BACnet records.
-- Review before applying to a live Supabase project.

create table if not exists public.report_files (
    id uuid primary key default gen_random_uuid(),
    organization_id uuid references public.organizations(id) on delete set null,
    site_uuid uuid references public.sites(id) on delete set null,
    edge_node_id uuid references public.edge_nodes(id) on delete set null,
    storage_bucket text,
    storage_path text not null,
    file_name text not null,
    content_type text,
    size_bytes bigint,
    metadata jsonb not null default '{}'::jsonb,
    created_by uuid references public.profiles(id) on delete set null,
    created_at timestamptz not null default now()
);

create table if not exists public.trend_upload_batches (
    id uuid primary key default gen_random_uuid(),
    gateway_id text not null,
    edge_node_id uuid references public.edge_nodes(id) on delete set null,
    status text not null default 'received',
    sample_count integer not null default 0,
    payload_metadata jsonb not null default '{}'::jsonb,
    received_at timestamptz not null default now(),
    processed_at timestamptz
);

create table if not exists public.point_samples (
    id uuid primary key default gen_random_uuid(),
    gateway_id text not null,
    edge_node_id uuid references public.edge_nodes(id) on delete set null,
    point_key text not null,
    sample_value jsonb,
    sample_time_utc timestamptz not null,
    upload_batch_id uuid references public.trend_upload_batches(id) on delete set null,
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now()
);

create table if not exists public.bacnet_devices (
    id uuid primary key default gen_random_uuid(),
    gateway_id text not null,
    edge_node_id uuid references public.edge_nodes(id) on delete set null,
    device_id integer not null,
    mac text,
    network integer,
    sadr text,
    apdu integer,
    metadata jsonb not null default '{}'::jsonb,
    discovered_at timestamptz not null default now(),
    last_seen_at timestamptz,
    unique (gateway_id, device_id)
);

create index if not exists idx_report_files_org_created on public.report_files(organization_id, created_at desc);
create index if not exists idx_report_files_site_created on public.report_files(site_uuid, created_at desc);
create index if not exists idx_trend_batches_gateway_received on public.trend_upload_batches(gateway_id, received_at desc);
create index if not exists idx_trend_batches_status on public.trend_upload_batches(status);
create index if not exists idx_point_samples_gateway_time on public.point_samples(gateway_id, sample_time_utc desc);
create index if not exists idx_point_samples_point_time on public.point_samples(point_key, sample_time_utc desc);
create index if not exists idx_bacnet_devices_gateway on public.bacnet_devices(gateway_id);
create index if not exists idx_bacnet_devices_device_id on public.bacnet_devices(device_id);
create index if not exists idx_bacnet_devices_last_seen on public.bacnet_devices(last_seen_at desc);

alter table public.report_files enable row level security;
alter table public.trend_upload_batches enable row level security;
alter table public.point_samples enable row level security;
alter table public.bacnet_devices enable row level security;

comment on table public.report_files is 'Future metadata for Supabase Storage files and generated reports.';
comment on table public.trend_upload_batches is 'Future trend upload batch tracking from edge gateways.';
comment on table public.point_samples is 'Future cloud point samples. Edge SQLite remains the local runtime store.';
comment on table public.bacnet_devices is 'Future discovered BACnet device records summarized from edge-local discovery.';

-- TODO: Add RLS policies once portal roles, storage layout, and trend access rules are finalized.
-- No broad public policies are created in this MVP.

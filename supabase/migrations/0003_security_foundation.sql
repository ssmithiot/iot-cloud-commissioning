-- Security foundation for future Supabase Auth and portal access.
-- Review before applying to a live Supabase project.

create table if not exists public.profiles (
    id uuid primary key,
    email text,
    display_name text,
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists public.organization_memberships (
    id uuid primary key default gen_random_uuid(),
    organization_id uuid not null references public.organizations(id) on delete cascade,
    profile_id uuid not null references public.profiles(id) on delete cascade,
    role text not null,
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (organization_id, profile_id)
);

create table if not exists public.site_permissions (
    id uuid primary key default gen_random_uuid(),
    site_uuid uuid not null references public.sites(id) on delete cascade,
    profile_id uuid not null references public.profiles(id) on delete cascade,
    permission text not null,
    created_at timestamptz not null default now(),
    unique (site_uuid, profile_id, permission)
);

create table if not exists public.edge_node_permissions (
    id uuid primary key default gen_random_uuid(),
    edge_node_id uuid not null references public.edge_nodes(id) on delete cascade,
    profile_id uuid not null references public.profiles(id) on delete cascade,
    permission text not null,
    created_at timestamptz not null default now(),
    unique (edge_node_id, profile_id, permission)
);

create table if not exists public.audit_events (
    id uuid primary key default gen_random_uuid(),
    organization_id uuid references public.organizations(id) on delete set null,
    site_uuid uuid references public.sites(id) on delete set null,
    edge_node_id uuid references public.edge_nodes(id) on delete set null,
    actor_profile_id uuid references public.profiles(id) on delete set null,
    actor_type text not null default 'system',
    event_type text not null,
    event_data jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now()
);

create index if not exists idx_profiles_email on public.profiles(email);
create index if not exists idx_memberships_org on public.organization_memberships(organization_id);
create index if not exists idx_memberships_profile on public.organization_memberships(profile_id);
create index if not exists idx_site_permissions_site on public.site_permissions(site_uuid);
create index if not exists idx_site_permissions_profile on public.site_permissions(profile_id);
create index if not exists idx_edge_node_permissions_node on public.edge_node_permissions(edge_node_id);
create index if not exists idx_edge_node_permissions_profile on public.edge_node_permissions(profile_id);
create index if not exists idx_audit_events_org_created on public.audit_events(organization_id, created_at desc);
create index if not exists idx_audit_events_site_created on public.audit_events(site_uuid, created_at desc);
create index if not exists idx_audit_events_edge_created on public.audit_events(edge_node_id, created_at desc);
create index if not exists idx_audit_events_type_created on public.audit_events(event_type, created_at desc);

alter table public.profiles enable row level security;
alter table public.organization_memberships enable row level security;
alter table public.site_permissions enable row level security;
alter table public.edge_node_permissions enable row level security;
alter table public.audit_events enable row level security;

comment on table public.profiles is 'Future Supabase Auth profile extension. Profile id should map to auth.users.id when Auth is wired.';
comment on table public.organization_memberships is 'Future organization membership and roles.';
comment on table public.site_permissions is 'Future site-level permissions for portal access.';
comment on table public.edge_node_permissions is 'Future edge-node-specific permissions.';
comment on table public.audit_events is 'Security and workflow audit events.';

-- TODO: Add auth.uid()-scoped policies after the portal auth model is implemented.
-- No broad public policies are created in this MVP.

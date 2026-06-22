-- Gateway API credential metadata.
-- Review before applying to a live Supabase project.

do $$
begin
    if exists (
        select 1
        from public.edge_nodes
        where gateway_id is null
    ) then
        raise exception 'public.edge_nodes.gateway_id must be populated before gateway_credentials can be created';
    end if;
end $$;

alter table public.edge_nodes
    alter column gateway_id set not null;

do $$
declare
    gateway_id_attnum smallint;
begin
    select attnum
    into gateway_id_attnum
    from pg_attribute
    where attrelid = 'public.edge_nodes'::regclass
      and attname = 'gateway_id'
      and not attisdropped;

    if not exists (
        select 1
        from pg_constraint
        where conrelid = 'public.edge_nodes'::regclass
          and contype = 'u'
          and conkey = array[gateway_id_attnum]::smallint[]
    ) then
        alter table public.edge_nodes
            add constraint uq_edge_nodes_gateway_id unique (gateway_id);
    end if;
end $$;

create table if not exists public.gateway_credentials (
    id uuid primary key default gen_random_uuid(),
    gateway_id text not null references public.edge_nodes(gateway_id) on delete cascade,
    token_prefix text not null unique,
    token_hash text not null,
    scopes jsonb not null default '[]'::jsonb,
    label text,
    last_used_at timestamptz,
    expires_at timestamptz,
    revoked_at timestamptz,
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists idx_gateway_credentials_gateway_id on public.gateway_credentials(gateway_id);
create index if not exists idx_gateway_credentials_token_prefix on public.gateway_credentials(token_prefix);
create index if not exists idx_gateway_credentials_revoked_at on public.gateway_credentials(revoked_at);

alter table public.gateway_credentials enable row level security;

comment on table public.gateway_credentials is 'Server-side gateway credential metadata keyed by the edge gateway text identifier.';
comment on column public.gateway_credentials.gateway_id is 'Stable text gateway identifier referencing public.edge_nodes(gateway_id).';
comment on column public.gateway_credentials.token_prefix is 'Non-secret token prefix used to select a credential before hash comparison.';
comment on column public.gateway_credentials.token_hash is 'HMAC-SHA256 credential verifier. Raw credentials are never stored.';
comment on column public.gateway_credentials.scopes is 'Future gateway API authorization scopes.';

-- TODO: Add credential issuance, rotation, and verification endpoints in the cloud API.
-- No broad public policies are created in this MVP.

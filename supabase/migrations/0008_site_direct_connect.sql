-- MVP-014 site Direct Connect metadata.
-- Safe for Supabase SQL Editor: idempotent, preserves existing data, no destructive changes.

alter table public.sites
    add column if not exists external_ip text,
    add column if not exists address text,
    add column if not exists store_hours_mf text,
    add column if not exists store_hours_sat text,
    add column if not exists store_hours_sun text,
    add column if not exists cradlepoint_ip text,
    add column if not exists direct_connect_host text,
    add column if not exists direct_connect_port integer default 5002,
    add column if not exists gateway_ui_port integer default 5000,
    add column if not exists store_hours_monday_friday text,
    add column if not exists store_hours_saturday text,
    add column if not exists store_hours_sunday text,
    add column if not exists network_status_notes text;

update public.sites
set
    direct_connect_port = coalesce(direct_connect_port, 5002),
    gateway_ui_port = coalesce(gateway_ui_port, 5000),
    direct_connect_host = coalesce(nullif(direct_connect_host, ''), nullif(cradlepoint_ip, ''), nullif(external_ip, '')),
    store_hours_monday_friday = coalesce(nullif(store_hours_monday_friday, ''), nullif(store_hours_mf, '')),
    store_hours_saturday = coalesce(nullif(store_hours_saturday, ''), nullif(store_hours_sat, '')),
    store_hours_sunday = coalesce(nullif(store_hours_sunday, ''), nullif(store_hours_sun, ''))
where true;

comment on column public.sites.cradlepoint_ip is 'Cradlepoint or cellular WAN host/IP for Direct Connect metadata.';
comment on column public.sites.direct_connect_host is 'Validated host used to build Direct Connect URLs; no scheme or path.';
comment on column public.sites.direct_connect_port is 'External Direct Connect port, default 5002.';
comment on column public.sites.gateway_ui_port is 'Informational gateway-local UI port, default 5000.';
comment on column public.sites.external_ip is 'Optional older external IP metadata retained for compatibility.';
comment on column public.sites.address is 'Operator-facing site address.';
comment on column public.sites.store_hours_mf is 'Legacy Monday-Friday store hours field retained for compatibility.';
comment on column public.sites.store_hours_sat is 'Legacy Saturday store hours field retained for compatibility.';
comment on column public.sites.store_hours_sun is 'Legacy Sunday store hours field retained for compatibility.';
comment on column public.sites.store_hours_monday_friday is 'Operator-facing Monday-Friday store hours.';
comment on column public.sites.store_hours_saturday is 'Operator-facing Saturday store hours.';
comment on column public.sites.store_hours_sunday is 'Operator-facing Sunday store hours.';
comment on column public.sites.network_status_notes is 'Operator-facing site/network status notes, e.g. related boxes online.';

-- Verification queries:
-- select site_id, name, direct_connect_host, direct_connect_port, gateway_ui_port
-- from public.sites
-- order by site_id;
--
-- select column_name, data_type
-- from information_schema.columns
-- where table_schema = 'public'
--   and table_name = 'sites'
--   and column_name in (
--     'cradlepoint_ip',
--     'external_ip',
--     'address',
--     'direct_connect_host',
--     'direct_connect_port',
--     'gateway_ui_port',
--     'store_hours_mf',
--     'store_hours_sat',
--     'store_hours_sun',
--     'store_hours_monday_friday',
--     'store_hours_saturday',
--     'store_hours_sunday',
--     'network_status_notes'
--   )
-- order by column_name;

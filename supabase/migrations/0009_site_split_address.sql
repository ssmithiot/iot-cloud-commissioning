-- MVP-014 site split address fields.
-- Safe for Supabase SQL Editor: idempotent, preserves existing data, no destructive changes.

alter table public.sites
    add column if not exists address text,
    add column if not exists address_street text,
    add column if not exists address_city text,
    add column if not exists address_state text,
    add column if not exists address_postal_code text;

comment on column public.sites.address is 'Legacy/free-form site address retained for compatibility.';
comment on column public.sites.address_street is 'Operator-facing site street address.';
comment on column public.sites.address_city is 'Operator-facing site city.';
comment on column public.sites.address_state is 'Operator-facing site state or province.';
comment on column public.sites.address_postal_code is 'Operator-facing site ZIP or postal code.';

-- Verification queries:
-- select site_id, name, address, address_street, address_city, address_state, address_postal_code
-- from public.sites
-- order by site_id;
--
-- select column_name, data_type
-- from information_schema.columns
-- where table_schema = 'public'
--   and table_name = 'sites'
--   and column_name in (
--     'address',
--     'address_street',
--     'address_city',
--     'address_state',
--     'address_postal_code'
--   )
-- order by column_name;

-- Store operator-facing site metadata used by the cloud UI.

alter table public.sites
    add column if not exists external_ip text,
    add column if not exists address text,
    add column if not exists store_hours_mf text,
    add column if not exists store_hours_sat text,
    add column if not exists store_hours_sun text;

comment on column public.sites.external_ip is 'Optional customer/router external IP retained as site metadata; gateway configuration uses outbound tunnels, not port forwarding.';
comment on column public.sites.address is 'Operator-facing site street address.';
comment on column public.sites.store_hours_mf is 'Operator-facing Monday-Friday store hours.';
comment on column public.sites.store_hours_sat is 'Operator-facing Saturday store hours.';
comment on column public.sites.store_hours_sun is 'Operator-facing Sunday store hours.';

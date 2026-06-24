-- Allow cloud BACnet jobs to yield while the local commissioning UI owns BACnet.

do $$
begin
    if exists (select 1 from pg_type where typname = 'edge_job_status')
       and not exists (
           select 1
           from pg_enum e
           join pg_type t on t.oid = e.enumtypid
           where t.typname = 'edge_job_status'
             and e.enumlabel = 'deferred'
       ) then
        alter type public.edge_job_status add value 'deferred';
    end if;
end $$;

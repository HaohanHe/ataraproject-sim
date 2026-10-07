-- Optional extra entry next to the online competition: an unscored evaluation phase organizers can offer.
-- Organizers name it by hand in private.observer_extra_phase (no row by default). While that phase is active,
-- open and projects-enabled, the RPC also answers extra_phase_id: to everyone once it is unrestricted, and only
-- to (non-banned) members of its access team while access_team_id is set. Everyone else gets exactly the
-- answer as before.
create table private.observer_extra_phase (
  id boolean primary key default true check(id),
  phase_id uuid references public.phases(id),
  updated_at timestamptz not null default now()
);
revoke all on private.observer_extra_phase from public,anon,authenticated;

create or replace function public.current_competition()
returns jsonb language sql stable security definer set search_path=public,pg_temp as $$
  with m as (select m.mode,coalesce(m.phase_id,(select id from public.phases
      where slug=case when m.mode='practice' then 'practice' else 'online' end)) phase_id
    from private.observer_site_mode m where m.id),
  practice as (select p.id from public.phases p join public.observer_phase_settings s on s.phase_id=p.id
    where p.slug='practice-projects' and p.is_active and s.projects_enabled and s.access_team_id is null
      and (p.starts_at is null or now()>=p.starts_at) and (p.ends_at is null or now()<p.ends_at)),
  extra as (select p.id from private.observer_extra_phase x join public.phases p on p.id=x.phase_id
      join public.observer_phase_settings s on s.phase_id=p.id
    where x.id and p.is_active and s.projects_enabled
      and (s.access_team_id is null or exists(select 1 from public.profiles u where u.id=auth.uid()
        and not u.is_banned and u.team_id=s.access_team_id))
      and (p.starts_at is null or now()>=p.starts_at) and (p.ends_at is null or now()<p.ends_at))
  select jsonb_build_object('mode',m.mode,'phase_id',m.phase_id)
    || coalesce((select case when m.mode='practice' then jsonb_build_object('project_phase_id',p.id)
        else jsonb_build_object('practice_phase_id',p.id)
          || case when exists(select 1 from public.phases o where o.id=m.phase_id and o.ends_at is not null and now()>=o.ends_at)
             then jsonb_build_object('project_phase_id',p.id) else '{}'::jsonb end end
      from practice p),'{}'::jsonb)
    || coalesce((select jsonb_build_object('extra_phase_id',e.id) from extra e),'{}'::jsonb)
  from m
$$;

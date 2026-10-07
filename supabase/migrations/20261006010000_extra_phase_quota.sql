-- The extra phase has its own evaluations and does not raise the daily version-preparation limit;
-- daily evaluation limits may go above 100 (an extra phase can be effectively unlimited).
-- Applied in production on 2026-10-05.
alter table public.observer_phase_settings drop constraint if exists observer_phase_settings_daily_batches_check,
  add constraint observer_phase_settings_daily_batches_check check (daily_batches between 1 and 10000);

CREATE OR REPLACE FUNCTION private.observer_preparation_quota(p_team uuid)
 RETURNS TABLE(daily_limit integer, used integer, remaining integer, window_start timestamp with time zone)
 LANGUAGE sql
 STABLE SECURITY DEFINER
 SET search_path TO 'public', 'pg_temp'
AS $function$
  with phases as (
    select s.phase_id, s.daily_batches from public.observer_phase_settings s join public.phases p on p.id=s.phase_id
    where s.projects_enabled and p.is_active and (p.ends_at is null or now() < p.ends_at) and not s.sealed
      and (s.access_team_id is null or s.access_team_id = p_team)
      -- The extra phase (its own evaluations, never a quota for version preparations) does not raise this limit.
      and s.phase_id is distinct from nullif(public.current_competition()->>'extra_phase_id','')::uuid
  ), q as (
    select coalesce((select max(daily_batches) from phases), 10)::integer as daily_limit,
      greatest(date_trunc('day', now() at time zone 'UTC') at time zone 'UTC',
        (select max(private.observer_quota_window_start(phase_id)) from phases)) as window_start
  )
  select q.daily_limit, u.used, greatest(0, q.daily_limit - u.used), q.window_start
  from q cross join lateral (select count(*)::integer as used from public.observer_revisions r
    join public.observer_projects p on p.id=r.project_id where p.team_id=p_team and r.created_at >= q.window_start) u
$function$
;

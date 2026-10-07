-- Two cheap read paths for public pages (database load, 2026-10-05).

-- 1. Task-card file lists without listing the 'scenarios' bucket.
-- Listing the bucket as anon/authenticated runs storage.search under the storage.objects policies,
-- which evaluates them for every candidate row (seconds per folder under load). This returns the same
-- names the bucket listing would: the direct files of <slug>/config, <slug>/public and <slug>/truth
-- (as '<folder>/<file>'), current versions only, filtered by exactly the SELECT policies on
-- storage.objects for the 'scenarios' bucket: one of the permissive ones ("scenario files read",
-- "released scenario files read") and the restrictive "formal source files remain private".
-- The storage policies stay in place for downloads. If they change, change this function with them
-- (tests/test_formal_source_privacy.py and tests/test_configure_v4_phases.py compare the two).
create or replace function public.observer_card_files(p_slug text)
returns setof text
language sql stable security definer
set search_path = public, pg_temp
as $$
  select substr(o.name, length(p_slug) + 2)
  from storage.objects o
  where p_slug ~ '^[A-Za-z0-9_.-]+$'
    and o.bucket_id = 'scenarios'
    and o.name collate "C" >= p_slug || '/' and o.name collate "C" < p_slug || '0'
    and split_part(o.name, '/', 1) = p_slug and o.name ~ '^[^/]+/(config|public|truth)/[^/]+$'
    and to_jsonb(o) ->> 'archived_at' is null
    and coalesce((to_jsonb(o) ->> 'is_delete_marker')::boolean, false) = false
    -- permissive: "scenario files read" or "released scenario files read"
    and (public.is_admin() or exists (
          select 1 from public.scenarios s
          where s.slug = (storage.foldername(o.name))[1] and s.is_active
            and ((storage.foldername(o.name))[2] = 'config'
              or storage.filename(o.name) = any (array['night_calendar.csv', 'slots.csv', 'targets.csv', 'tile_windows.csv',
                'observation_requests.csv', 'observation_request_tiles.csv', 'scenario_manifest.json', 'calendar_metadata.json',
                'catalog_metadata.json', 'observation_request_metadata.json', 'score_config.json'])
              or (s.tiles_public and storage.filename(o.name) = 'tiles.csv')
              or (s.weather_public and storage.filename(o.name) = any (array['weather.csv', 'weather_metadata.json']))
              or (s.forecasts_public and storage.filename(o.name) = 'weather_forecasts.csv')
              or (s.events_public and storage.filename(o.name) = 'weather_events.csv')))
        or public.observer_released_scenario_file(o.name))
    -- restrictive: "formal source files remain private"
    and (public.is_admin() or not public.observer_formal_source((storage.foldername(o.name))[1])
        or public.observer_released_scenario_file(o.name))
  order by 1
$$;
revoke all on function public.observer_card_files(text) from public;
grant execute on function public.observer_card_files(text) to anon, authenticated, service_role;

-- 2. A short shared cache for observer_card_board.
-- Its result depends only on (phase, card, limit) for every non-admin caller who can see the phase,
-- and changes when an evaluation is scored, yet every open leaderboard tab recomputes it each minute.
-- The computation moves unchanged to private.observer_card_board_live; public.observer_card_board
-- serves non-admin callers from private.observer_card_board_cache for up to 45 s (setting
-- observer.card_board_cache_seconds; 0 disables). Admins, phases the caller cannot see and unknown
-- cards always compute live. A change of the phase's board mode, activity, layout, sealing, repeat
-- count or access team invalidates the entry at once.
create table if not exists private.observer_card_board_cache (
  phase_id uuid not null,
  scenario_slug text not null,
  row_limit integer not null,
  context text not null,
  result jsonb not null,
  computed_at timestamptz not null,
  primary key (phase_id, scenario_slug, row_limit)
);
revoke all on private.observer_card_board_cache from public, anon, authenticated;

do $$
begin
  if to_regprocedure('private.observer_card_board_live(uuid,text,integer)') is null then
    alter function public.observer_card_board(uuid, text, integer) rename to observer_card_board_live;
    alter function public.observer_card_board_live(uuid, text, integer) set schema private;
  end if;
end $$;
revoke all on function private.observer_card_board_live(uuid, text, integer) from public, anon, authenticated;

create or replace function public.observer_card_board(p_phase uuid, p_scenario_slug text default null, p_limit integer default 100)
returns jsonb
language plpgsql volatile security definer
set search_path = public, pg_temp
as $$
declare
  v_ttl integer := coalesce(nullif(current_setting('observer.card_board_cache_seconds', true), '')::integer, 45);
  v_limit integer := greatest(1, least(coalesce(p_limit, 100), 1000));
  v_slug text := coalesce(p_scenario_slug, '');
  v_context text;
  v_result jsonb;
begin
  if v_ttl <= 0 or public.is_admin() or not public.observer_phase_visible(p_phase) then
    return private.observer_card_board_live(p_phase, p_scenario_slug, p_limit);
  end if;
  select md5(row(p.leaderboard_mode, p.is_active, s.board_layout, s.sealed, s.repeat_runs, s.access_team_id)::text)
    into v_context
    from public.phases p left join public.observer_phase_settings s on s.phase_id = p.id where p.id = p_phase;
  if v_context is null or (p_scenario_slug is not null and not exists (
      select 1 from public.phase_scenarios ps join public.scenarios sc on sc.id = ps.scenario_id
      where ps.phase_id = p_phase and sc.slug = p_scenario_slug)) then
    return private.observer_card_board_live(p_phase, p_scenario_slug, p_limit);
  end if;

  select c.result into v_result from private.observer_card_board_cache c
    where c.phase_id = p_phase and c.scenario_slug = v_slug and c.row_limit = v_limit and c.context = v_context
      and c.computed_at > clock_timestamp() - make_interval(secs => v_ttl);
  if found then return v_result; end if;

  v_result := private.observer_card_board_live(p_phase, p_scenario_slug, p_limit);
  if current_setting('transaction_read_only') = 'off' then
    insert into private.observer_card_board_cache as c (phase_id, scenario_slug, row_limit, context, result, computed_at)
      values (p_phase, v_slug, v_limit, v_context, v_result, clock_timestamp())
      on conflict (phase_id, scenario_slug, row_limit)
      do update set context = excluded.context, result = excluded.result, computed_at = excluded.computed_at;
    delete from private.observer_card_board_cache where computed_at < clock_timestamp() - interval '1 hour';
  end if;
  return v_result;
end $$;
revoke all on function public.observer_card_board(uuid, text, integer) from public;
grant execute on function public.observer_card_board(uuid, text, integer) to anon, authenticated, service_role;

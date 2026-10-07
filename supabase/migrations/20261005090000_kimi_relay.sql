-- Temporary Kimi relay for DEVELOPMENT (owner decision 2026-10-05).
--
-- Contestants on the leaderboard may call the organizer's Kimi coding key from their own
-- machines (debugging, local runs, coding helpers) through the edge function kimi-relay,
-- authenticated with their existing personal API token (s26_…, cli_tokens). It is not meant
-- for evaluations: limited, temporary, may stop at any time; finals use each team's own key.
-- The key is only a function secret (KIMI_RELAY_KEY); it never reaches the database or a page.
--
-- Everything is one config row, editable without a redeploy:
--   update private.kimi_relay_config set enabled = false;                       -- kill switch
--   update private.kimi_relay_config set daily_requests = 300, daily_tokens = 3000000;
--   update private.kimi_relay_config set eligibility = jsonb_set(eligibility, '{phases}',
--     '["049d6029-343d-4d16-80d5-94b56b350301","249c223b-2ec8-4f0a-924e-7af3c4e3eb44"]');  -- + practice board
-- Eligibility: a team with a scored evaluation (purposes, default formal) in one of the phases
-- (default: the online phase = on the leaderboard), or a hidden (organizer test) team when
-- allow_hidden, or a team listed in "teams". Checked on every request, so newly ranked teams
-- get access automatically. Usage rows hold counters only, never prompt or answer content.
-- Additive: three new tables and new functions; nothing existing changes.

create table if not exists private.kimi_relay_config (
  id boolean primary key default true check (id),
  enabled boolean not null default false,
  daily_requests integer not null default 200,      -- per team per UTC day
  daily_tokens bigint not null default 2000000,     -- per team per UTC day (prompt + completion)
  max_concurrent integer not null default 2,        -- per team
  max_tokens integer not null default 8192,         -- clamp on max_tokens / max_completion_tokens
  eligibility jsonb not null default jsonb_build_object(
    'phases', jsonb_build_array('049d6029-343d-4d16-80d5-94b56b350301'),
    'purposes', jsonb_build_array('formal'),
    'allow_hidden', true,
    'teams', '[]'::jsonb),
  block_edge_runtime boolean not null default true, -- refuse Deno edge clients (the platform's own model proxy)
  note text not null default '',
  updated_at timestamptz not null default now()
);
insert into private.kimi_relay_config (id) values (true) on conflict (id) do nothing;

create table if not exists private.kimi_relay_usage (
  id bigserial primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  team_id uuid not null references public.teams(id) on delete cascade,
  token_id uuid,
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  stream boolean not null default false,
  status text not null default 'running',            -- running, ok, upstream_<code>, failed
  http_status integer,
  prompt_tokens integer not null default 0,
  completion_tokens integer not null default 0,
  total_tokens integer not null default 0,
  key_slot smallint                                  -- which pool key served it (1..n), never the key
);
alter table private.kimi_relay_usage add column if not exists key_slot smallint;
create index if not exists kimi_relay_usage_team_day on private.kimi_relay_usage(team_id, started_at);
create index if not exists kimi_relay_usage_running on private.kimi_relay_usage(team_id) where finished_at is null;
revoke all on private.kimi_relay_config, private.kimi_relay_usage from public, anon, authenticated;
revoke all on sequence private.kimi_relay_usage_id_seq from public, anon, authenticated;

-- Whether a team may use the relay under the current eligibility rule.
create or replace function private.kimi_relay_eligible(p_team uuid)
returns boolean language sql stable security definer set search_path = public, pg_temp as $$
  select coalesce((
    select (coalesce((c.eligibility ->> 'allow_hidden')::boolean, false) and t.is_hidden)
        or coalesce(c.eligibility -> 'teams', '[]'::jsonb) ? p_team::text
        or (not t.is_hidden and exists (
          select 1 from public.observer_batches b
           where b.team_id = p_team and b.status = 'scored'
             and b.phase_id::text in (select jsonb_array_elements_text(coalesce(c.eligibility -> 'phases', '[]'::jsonb)))
             and b.purpose in (select jsonb_array_elements_text(coalesce(c.eligibility -> 'purposes', '["formal"]'::jsonb)))))
      from private.kimi_relay_config c, public.teams t where c.id and t.id = p_team), false)
$$;
revoke all on function private.kimi_relay_eligible(uuid) from public, anon, authenticated;

-- Today's usage of a team (UTC day). Running requests count as requests; tokens only when finished.
create or replace function private.kimi_relay_today(p_team uuid)
returns jsonb language sql stable security definer set search_path = public, pg_temp as $$
  select jsonb_build_object(
    'requests', count(*),
    'tokens', coalesce(sum(u.total_tokens), 0),
    'running', count(*) filter (where u.finished_at is null and u.started_at > now() - interval '10 minutes'))
  from private.kimi_relay_usage u
  where u.team_id = p_team and u.started_at >= date_trunc('day', now() at time zone 'UTC') at time zone 'UTC'
    and u.status <> 'no_key'   -- requests refused because every organizer key was cooling down do not count
$$;
revoke all on function private.kimi_relay_today(uuid) from public, anon, authenticated;

-- Edge function only: token check (the CLI token rules: switch, ban, per-minute rate), then the
-- relay switch, team, eligibility, daily and concurrency limits; records a running usage row.
create or replace function public.kimi_relay_begin(p_hash text, p_stream boolean default false)
returns jsonb language plpgsql volatile security definer set search_path = public, pg_temp as $$
declare
  v_identity jsonb;
  v_user uuid;
  v_team uuid;
  c private.kimi_relay_config;
  v_today jsonb;
  v_id bigint;
begin
  v_identity := public.cli_token_authenticate(p_hash);
  v_user := (v_identity ->> 'user_id')::uuid;
  select * into c from private.kimi_relay_config where id;
  if not found or not c.enabled then raise exception 'relay_disabled'; end if;
  select p.team_id into v_team from public.profiles p where p.id = v_user;
  if v_team is null then raise exception 'team_required'; end if;
  if not private.kimi_relay_eligible(v_team) then raise exception 'not_eligible'; end if;
  perform pg_advisory_xact_lock(hashtextextended('kimi_relay:' || v_team::text, 0));
  v_today := private.kimi_relay_today(v_team);
  if (v_today ->> 'requests')::int >= c.daily_requests then raise exception 'daily_requests_exhausted'; end if;
  if (v_today ->> 'tokens')::bigint >= c.daily_tokens then raise exception 'daily_tokens_exhausted'; end if;
  if (v_today ->> 'running')::int >= c.max_concurrent then raise exception 'too_many_concurrent'; end if;
  insert into private.kimi_relay_usage (user_id, team_id, token_id, stream)
  values (v_user, v_team, (v_identity ->> 'token_id')::uuid, coalesce(p_stream, false))
  returning id into v_id;
  return jsonb_build_object('usage_id', v_id, 'max_tokens', c.max_tokens, 'block_edge_runtime', c.block_edge_runtime);
end $$;

create or replace function public.kimi_relay_finish(p_id bigint, p_status text, p_http integer,
  p_prompt integer, p_completion integer, p_total integer)
returns void language sql volatile security definer set search_path = public, pg_temp as $$
  update private.kimi_relay_usage set finished_at = now(), status = left(coalesce(p_status, 'failed'), 40),
    http_status = p_http, prompt_tokens = greatest(coalesce(p_prompt, 0), 0),
    completion_tokens = greatest(coalesce(p_completion, 0), 0),
    total_tokens = greatest(coalesce(p_total, coalesce(p_prompt, 0) + coalesce(p_completion, 0)), 0)
  where id = p_id and finished_at is null
$$;

-- Edge function only: the relay switch and token check for GET /v1/models (no usage row).
create or replace function public.kimi_relay_check(p_hash text)
returns boolean language plpgsql volatile security definer set search_path = public, pg_temp as $$
declare v_user uuid; v_team uuid;
begin
  v_user := (public.cli_token_authenticate(p_hash) ->> 'user_id')::uuid;
  if not coalesce((select enabled from private.kimi_relay_config where id), false) then raise exception 'relay_disabled'; end if;
  select p.team_id into v_team from public.profiles p where p.id = v_user;
  if v_team is null then raise exception 'team_required'; end if;
  if not private.kimi_relay_eligible(v_team) then raise exception 'not_eligible'; end if;
  return true;
end $$;

revoke all on function public.kimi_relay_begin(text, boolean), public.kimi_relay_finish(bigint, text, integer, integer, integer, integer),
  public.kimi_relay_check(text) from public, anon, authenticated;
grant execute on function public.kimi_relay_begin(text, boolean), public.kimi_relay_finish(bigint, text, integer, integer, integer, integer),
  public.kimi_relay_check(text) to service_role;

-- Key pool: the keys are function secrets KIMI_RELAY_KEY, KIMI_RELAY_KEY_2, … (slot 1, 2, …); only
-- the slot number and its health live here. A key that answers with a quota/limit/auth error
-- cools down (weekly limit or auth: 24 h; plain 429: minutes) and the request is retried once on
-- another key. Put a key back early: update private.kimi_relay_keys set cooling_until = null where slot = 2;
create table if not exists private.kimi_relay_keys (
  slot smallint primary key check (slot between 1 and 50),
  cooling_until timestamptz,
  last_status integer,
  last_error text,
  last_error_at timestamptz,
  last_used_at timestamptz,
  uses bigint not null default 0
);
revoke all on private.kimi_relay_keys from public, anon, authenticated;

-- Least recently used healthy key among the configured slots (null when all are cooling down);
-- recorded on the usage row.
create or replace function public.kimi_relay_pick_key(p_usage bigint, p_slots smallint[], p_exclude smallint[] default '{}')
returns smallint language plpgsql volatile security definer set search_path = public, pg_temp as $$
declare v_slot smallint;
begin
  insert into private.kimi_relay_keys (slot) select unnest(p_slots) on conflict (slot) do nothing;
  select k.slot into v_slot from private.kimi_relay_keys k
   where k.slot = any(p_slots) and not (k.slot = any(coalesce(p_exclude, '{}')))
     and (k.cooling_until is null or k.cooling_until <= now())
   order by k.last_used_at nulls first, k.slot limit 1 for update skip locked;
  if v_slot is null then return null; end if;
  update private.kimi_relay_keys set last_used_at = now(), uses = uses + 1 where slot = v_slot;
  update private.kimi_relay_usage set key_slot = v_slot where id = p_usage;
  return v_slot;
end $$;

create or replace function public.kimi_relay_key_failed(p_slot smallint, p_status integer, p_error text, p_cool_seconds integer)
returns void language sql volatile security definer set search_path = public, pg_temp as $$
  update private.kimi_relay_keys set last_status = p_status, last_error = left(coalesce(p_error, ''), 80), last_error_at = now(),
    cooling_until = case when coalesce(p_cool_seconds, 0) > 0 then now() + make_interval(secs => p_cool_seconds) else cooling_until end
  where slot = p_slot
$$;
revoke all on function public.kimi_relay_pick_key(bigint, smallint[], smallint[]), public.kimi_relay_key_failed(smallint, integer, text, integer)
  from public, anon, authenticated;
grant execute on function public.kimi_relay_pick_key(bigint, smallint[], smallint[]), public.kimi_relay_key_failed(smallint, integer, text, integer)
  to service_role;

-- Profile page: switch, eligibility, limits and the team's remaining quota today.
create or replace function public.my_kimi_relay()
returns jsonb language plpgsql stable security definer set search_path = public, pg_temp as $$
declare
  v_user uuid := auth.uid();
  v_team uuid;
  c private.kimi_relay_config;
  v_today jsonb;
begin
  if v_user is null then return null; end if;
  select * into c from private.kimi_relay_config where id;
  select p.team_id into v_team from public.profiles p where p.id = v_user;
  v_today := case when v_team is null then null else private.kimi_relay_today(v_team) end;
  return jsonb_build_object(
    'enabled', coalesce(c.enabled, false),
    'has_team', v_team is not null,
    'eligible', v_team is not null and private.kimi_relay_eligible(v_team),
    'daily_requests', c.daily_requests, 'daily_tokens', c.daily_tokens,
    'max_concurrent', c.max_concurrent, 'max_tokens', c.max_tokens,
    'used_requests', coalesce((v_today ->> 'requests')::int, 0),
    'used_tokens', coalesce((v_today ->> 'tokens')::bigint, 0));
end $$;
revoke all on function public.my_kimi_relay() from public, anon;
grant execute on function public.my_kimi_relay() to authenticated;

-- Organizer view: the config and per-team usage for the last p_days UTC days (counters only).
create or replace function public.admin_kimi_relay(p_days integer default 1)
returns jsonb language plpgsql stable security definer set search_path = public, pg_temp as $$
begin
  if not public.is_admin() then raise exception 'forbidden'; end if;
  return jsonb_build_object(
    'config', (select to_jsonb(c) - 'id' from private.kimi_relay_config c where c.id),
    'teams', coalesce((select jsonb_agg(r order by r.requests desc) from (
      select t.id as team_id, t.name as team_name, t.is_hidden,
        count(*) as requests,
        count(*) filter (where u.status = 'ok') as ok,
        count(*) filter (where u.status <> 'ok' and u.finished_at is not null) as errors,
        count(distinct u.user_id) as users,
        coalesce(sum(u.total_tokens), 0) as tokens,
        max(u.started_at) as last_at
      from private.kimi_relay_usage u join public.teams t on t.id = u.team_id
      where u.started_at >= date_trunc('day', now() at time zone 'UTC') at time zone 'UTC'
                            - make_interval(days => greatest(coalesce(p_days, 1), 1) - 1)
      group by t.id, t.name, t.is_hidden) r), '[]'::jsonb),
    'keys', coalesce((select jsonb_agg(jsonb_build_object('slot', k.slot,
        'state', case when k.cooling_until > now() then 'cooling' else 'healthy' end,
        'cooling_until', k.cooling_until, 'last_status', k.last_status, 'last_error', k.last_error,
        'last_error_at', k.last_error_at, 'last_used_at', k.last_used_at, 'uses', k.uses,
        'ok_today', (select count(*) from private.kimi_relay_usage u where u.key_slot = k.slot and u.status = 'ok'
          and u.started_at >= date_trunc('day', now() at time zone 'UTC') at time zone 'UTC')) order by k.slot)
      from private.kimi_relay_keys k), '[]'::jsonb));
end $$;

create or replace function public.admin_kimi_relay_set_enabled(p_enabled boolean)
returns boolean language plpgsql volatile security definer set search_path = public, pg_temp as $$
begin
  if not public.is_admin() then raise exception 'forbidden'; end if;
  update private.kimi_relay_config set enabled = coalesce(p_enabled, false), updated_at = now() where id;
  insert into public.audit_log (user_id, action, detail)
  values (auth.uid(), 'kimi_relay_enabled', jsonb_build_object('enabled', coalesce(p_enabled, false)));
  return coalesce(p_enabled, false);
end $$;
revoke all on function public.admin_kimi_relay(integer), public.admin_kimi_relay_set_enabled(boolean) from public, anon;
grant execute on function public.admin_kimi_relay(integer), public.admin_kimi_relay_set_enabled(boolean) to authenticated;

notify pgrst, 'reload schema';

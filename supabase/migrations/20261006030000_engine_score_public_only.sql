-- Engine and score jobs never burn private GitHub Actions minutes (2026-10-06). Applied in production.
--
-- Private runner organizations (observer-control, private) have 2000 included minutes a month;
-- the public repositories (observer-public) are free and unmetered. Four organizations hit the
-- wall: their jobs were refused by GitHub, and every other organization was hours away.
--
-- 1. The global public monthly cap no longer stops the free public pool (it had been reached, so
--    every engine/score job fell back to private minutes).
-- 2. private.observer_public_pool.engine_score_private (default true here for existing test
--    setups; false in production): when false, an engine/score job the public pool can run
--    (phase switched on, sealed rules, no randomized private instance, no split execute partner)
--    is moved to a public repository by observer_pending_jobs itself, or waits queued for a free
--    public slot. It is never dispatched to a private repository. A queued job, or one dispatched
--    to a private repository more than two minutes ago and never claimed, is moved. Jobs the pool
--    cannot run (preparation, phases not switched on) keep the private path.
--    Escape hatch: update private.observer_public_pool set engine_score_private=true;
-- 3. The legacy candidate check excludes only split execute runs, not requeued runs.
-- 4. Conservative private-minute accounting: per job ceil(claim->finish minutes)+1 (GitHub rounds
--    every job up and bills setup before the claim), running jobs count as running so far (capped);
--    with a fresh billing sync (scripts/sync-runner-minutes.py, private repositories only) the
--    effective minutes are billed + estimate growth since the sync.

alter table private.observer_public_pool drop constraint if exists observer_public_pool_monthly_minute_cap_check;
alter table private.observer_public_pool add constraint observer_public_pool_monthly_minute_cap_check
  check (monthly_minute_cap >= 0 and monthly_minute_cap <= 100000000);

alter table private.observer_public_pool add column if not exists engine_score_private boolean not null default true;
-- Can this job run in a public repository at all (ignoring capacity and earlier returns)?
create or replace function private.observer_public_eligible(p_job uuid)
 returns boolean language plpgsql stable security definer set search_path to 'public','pg_temp' as $function$
declare p private.observer_public_pool; v private.observer_jobs; v_phase uuid; v_user uuid; v_sealed boolean;
begin
  select * into p from private.observer_public_pool where id;
  if not found or p.mode='off' then return false; end if;
  select * into v from private.observer_jobs where id=p_job;
  if not found or v.runner<>'github-hosted' or v.status not in ('queued','dispatched') or v.claimed_at is not null
    or v.run_id is null or v.expires_at<=now() or v.kind not in ('engine','score') then return false; end if;
  if v.kind='score' then
    select c.sealed,b.user_id into v_sealed,v_user from public.observer_runs r join public.observer_batches b on b.id=r.batch_id
      join public.observer_phase_settings c on c.phase_id=b.phase_id where r.id=v.run_id;
    if not p.score_jobs and not coalesce(v_user=any(p.drill_users),false) then return false; end if;
    return not (coalesce(v_sealed,true) and not p.sealed_transfer_verified);
  end if;
  -- split execute/engine runs and randomized private instances stay private
  if exists(select 1 from private.observer_jobs o where o.run_id=v.run_id and o.kind='execute')
    or exists(select 1 from private.observer_scenario_instances i where i.run_id=v.run_id) then return false; end if;
  select b.phase_id,b.user_id,c.sealed into v_phase,v_user,v_sealed
    from public.observer_runs r join public.observer_batches b on b.id=r.batch_id
    join public.observer_phase_settings c on c.phase_id=b.phase_id
    where r.id=v.run_id and b.mode='project' and c.colocated;
  if v_phase is null or not exists(select 1 from private.observer_public_pool_phases f
    where f.phase_id=v_phase and f.enabled) then return false; end if;
  if v_sealed and not p.sealed_transfer_verified then return false; end if;
  if v_user=any(p.drill_users) then return true; end if;
  return p.mode in ('overflow','primary');
end $function$;

-- Engine/score job that must not use private minutes.
create or replace function private.observer_public_only(p_job uuid)
 returns boolean language sql stable security definer set search_path to 'public','pg_temp' as $function$
  select coalesce((select not p.engine_score_private from private.observer_public_pool p where p.id),false)
    and private.observer_public_eligible(p_job)
$function$;

-- Move one public-only job to the least busy healthy public repository with a free slot.
-- Returns false (job untouched, it waits) when no public slot is free.
create or replace function private.observer_public_move(p_job uuid)
 returns boolean language plpgsql security definer set search_path to 'public','pg_temp' as $function$
declare t private.observer_public_targets; v private.observer_jobs; p private.observer_public_pool;
begin
  select * into p from private.observer_public_pool where id;
  select * into v from private.observer_jobs where id=p_job for update skip locked;
  if not found or not private.observer_public_only(p_job) then return false; end if;
  if (select count(*) from private.observer_jobs j where j.runner='public-hosted'
      and j.status in ('queued','dispatched','claimed') and j.expires_at>now())>=p.max_active then return false; end if;
  t:=private.observer_public_free_target();
  if t.organization is null then return false; end if;
  update private.observer_jobs set home_organization=organization,home_dispatch_count=dispatch_count,
    public_moved_at=now(),organization=t.organization,repository_id=t.repository_id,organization_id=t.organization_id,
    workflow_sha=t.approved_sha,runner='public-hosted',status='queued',error='',public_run_id=null,
    dispatch_count=0,last_dispatch_at=null,public_declined_at=null,
    expires_at=greatest(expires_at,now()+interval '30 minutes')
    where id=p_job;
  return true;
end $function$;

-- Legacy candidate (used only when engine_score_private=true): only split execute runs are excluded,
-- not runs that were requeued (their earlier score job no longer blocks the pool).
CREATE OR REPLACE FUNCTION private.observer_public_candidate(p_job uuid, p_require_trigger boolean DEFAULT true)
 RETURNS boolean
 LANGUAGE plpgsql
 STABLE SECURITY DEFINER
 SET search_path TO 'public', 'pg_temp'
AS $function$
declare p private.observer_public_pool; v private.observer_jobs; v_phase uuid; v_user uuid; v_sealed boolean; v_team uuid;
  v_load record;
begin
  select * into p from private.observer_public_pool where id;
  if not found or p.mode='off' then return false; end if;
  select * into v from private.observer_jobs where id=p_job;
  if found and v.kind='score' then
    if v.runner<>'github-hosted' or v.status<>'queued' or v.run_id is null
      or v.expires_at<=now() or v.public_declined_at is not null then return false; end if;
    select c.sealed,b.user_id into v_sealed,v_user from public.observer_runs r join public.observer_batches b on b.id=r.batch_id
      join public.observer_phase_settings c on c.phase_id=b.phase_id where r.id=v.run_id;
    if not p.score_jobs and not coalesce(v_user=any(p.drill_users),false) then return false; end if;
    if coalesce(v_sealed,true) and not p.sealed_transfer_verified then return false; end if;
    if (select count(*) from private.observer_jobs j where j.runner='public-hosted'
        and j.status in ('queued','dispatched','claimed') and j.expires_at>now())>=p.max_active
      or private.observer_public_month_minutes()>=p.monthly_minute_cap then return false; end if;
    return (private.observer_public_free_target()).organization is not null;
  end if;
  if not found or v.kind<>'engine' or v.runner<>'github-hosted' or v.status<>'queued' or v.run_id is null
    or v.expires_at<=now() or v.public_declined_at is not null then return false; end if;
  if exists(select 1 from private.observer_jobs o where o.run_id=v.run_id and o.kind='execute')
    or exists(select 1 from private.observer_scenario_instances i where i.run_id=v.run_id) then return false; end if;
  select b.phase_id,b.user_id,b.team_id,c.sealed into v_phase,v_user,v_team,v_sealed
    from public.observer_runs r join public.observer_batches b on b.id=r.batch_id
    join public.observer_phase_settings c on c.phase_id=b.phase_id
    where r.id=v.run_id and b.mode='project' and c.colocated;
  if v_phase is null or not exists(select 1 from private.observer_public_pool_phases f
    where f.phase_id=v_phase and f.enabled) then return false; end if;
  if v_sealed and not p.sealed_transfer_verified then return false; end if;
  if (select count(*) from private.observer_jobs j where j.runner='public-hosted'
      and j.status in ('queued','dispatched','claimed') and j.expires_at>now())>=p.max_active
    or private.observer_public_month_minutes()>=p.monthly_minute_cap then return false; end if;
  if (private.observer_public_free_target()).organization is null then return false; end if;
  if v_user=any(p.drill_users) then return true; end if;
  if p.mode not in ('overflow','primary') then return false; end if;
  if p.mode='primary' and private.observer_public_rollout(v_team,p.rollout_percent) then return true; end if;
  if v_sealed then return true; end if;
  if not coalesce(p_require_trigger,true) then return true; end if;
  select * into v_load from public.observer_organizations_by_load() o where o.organization=v.organization;
  return v_load.organization is null or v_load.over_limit or not v_load.healthy
    or v_load.month_minutes>=v_load.monthly_minute_limit*p.overflow_ratio or v_load.active_jobs>=p.busy_jobs;
end $function$;

CREATE OR REPLACE FUNCTION public.observer_pending_jobs(p_limit integer DEFAULT 10)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public', 'pg_temp'
AS $function$
declare v_result jsonb; v_slots boolean; v_over boolean; v_private boolean; v_job record; v_moved integer:=0;
begin
  v_private:=coalesce((select engine_score_private from private.observer_public_pool where id),true);
  if not v_private then
    -- Public-only engine/score jobs (queued, or dispatched to a private repository more than two
    -- minutes ago and never claimed) move to a public repository; without a free slot they wait.
    perform pg_advisory_xact_lock(hashtext('observer-public-pool'));
    for v_job in select j.id from private.observer_jobs j
      where j.runner='github-hosted' and j.kind in ('engine','score') and j.claimed_at is null and j.expires_at>now()
        and (j.status='queued' or (j.status='dispatched'
          and (j.last_dispatch_at is null or j.last_dispatch_at<now()-interval '2 minutes')))
      order by j.created_at limit 40
    loop
      if private.observer_public_move(v_job.id) then v_moved:=v_moved+1; end if;
    end loop;
  end if;
  v_slots:=private.observer_fallback_slots()>0;
  v_over:=false;
  if v_slots then
    v_over:=not exists(select 1 from public.observer_organizations_by_load() o where not o.over_limit);
  end if;
  -- Reserve a dispatch attempt before calling GitHub. An ambiguous network result
  -- may be retried after two minutes; only one resulting machine can claim the job.
  with picked as (
    select j.id,
      case when j.runner='github-hosted' and j.status='dispatched' and j.dispatch_count>=3 then 'stalled'
        when v_over and j.runner='github-hosted' and j.status='queued' then 'over_limit' end as fallback
    from private.observer_jobs j join private.observer_installations i on i.organization=j.organization
    where j.status in ('queued','dispatched') and i.enabled and j.expires_at>now() and (
      (j.dispatch_count<3 and (j.last_dispatch_at is null or j.last_dispatch_at<now()-interval '2 minutes')
        -- A self-hosted run accepted by GitHub waits for a free runner;
        -- re-dispatching it would only queue duplicates on the fallback.
        and not (j.runner in ('self-hosted','public-hosted') and j.status='dispatched'))
      or (v_slots and j.runner='github-hosted' and j.status='dispatched' and j.dispatch_count>=3
        and j.last_dispatch_at<now()-interval '10 minutes'))
      -- public-only engine/score jobs never go to a private repository: they wait for a public slot
      and (v_private or j.runner<>'github-hosted' or j.kind not in ('engine','score') or j.claimed_at is not null
        or not private.observer_public_only(j.id))
    order by j.created_at for update of j skip locked limit greatest(1,least(coalesce(p_limit,10),20))
  ), reserved as (
    update private.observer_jobs j set dispatch_count=dispatch_count+1,last_dispatch_at=now()
    from picked p where j.id=p.id
    returning j.id,j.kind,j.organization,j.workflow_sha,j.encrypted_nonce,j.runner,p.fallback,j.status
  )
  select coalesce(jsonb_agg(jsonb_build_object('id',r.id,'kind',r.kind,'organization',r.organization,
    'workflow_sha',r.workflow_sha,'encrypted_nonce',r.encrypted_nonce,'runner',r.runner,'fallback',r.fallback,
    'public',case when v_private and r.runner='github-hosted' and r.kind in ('engine','score') and r.status='queued'
      then private.observer_public_candidate(r.id) end)),'[]') into v_result from reserved r;
  return v_result;
end $function$;

-- The health mover must not shuffle public-only jobs between private organizations (it also
-- re-places the owner); they go to the public pool instead.
CREATE OR REPLACE FUNCTION private.observer_health_move(p_job uuid, p_reason text)
 RETURNS text
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public', 'pg_temp'
AS $function$
declare v private.observer_jobs; v_target text;
begin
  select * into v from private.observer_jobs where id=p_job for update skip locked;
  if not found or v.runner<>'github-hosted' or v.kind='prepare' or v.status not in ('queued','dispatched')
    or v.claimed_at is not null or v.expires_at<=now() then return null; end if;
  if private.observer_public_only(p_job) then return null; end if;
  v_target:=private.observer_healthy_organization(v.organization);
  if v_target is null then return null; end if;
  begin
    perform public.observer_failover_job(p_job,v_target);
  exception when others then
    return null;
  end;
  -- The job gets a fresh claim window; the team keeps the time it waited.
  update private.observer_jobs set status='queued',dispatch_count=0,last_dispatch_at=null,
    expires_at=greatest(expires_at,now()+interval '30 minutes') where id=p_job;
  update private.observer_sessions s set expires_at=s.expires_at+least(now()-v.created_at,interval '30 minutes')
    where s.expires_at>now() and s.deadline_at is null and s.run_id=v.run_id;
  insert into private.observer_private_events(organization,job_id,kind,detail)
    values(v.organization,p_job,'moved',left(p_reason||' -> '||v_target,200));
  return v_target;
end $function$;

create or replace function private.observer_org_month_estimate(p_org text, p_finished_only boolean default false)
 returns double precision language sql stable security definer set search_path to 'public','pg_temp' as $function$
  select coalesce(sum(least(ceil(extract(epoch from coalesce(j.finished_at,now())-j.claimed_at)/60),
      case when j.finished_at is null then 75 else 1e9 end)+1),0)::double precision
  from private.observer_jobs j where j.organization=p_org and j.runner='github-hosted' and j.claimed_at is not null
    and (j.finished_at is null and j.status='claimed' and not p_finished_only or j.finished_at>=date_trunc('month',now()))
$function$;

CREATE OR REPLACE FUNCTION public.observer_set_github_minutes(p_minutes jsonb)
 RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'public', 'pg_temp'
AS $function$
declare k text; m double precision; n integer:=0;
begin
  if p_minutes is null or jsonb_typeof(p_minutes)<>'object' then raise exception 'invalid_minutes'; end if;
  for k in select jsonb_object_keys(p_minutes) loop
    m:=(p_minutes->>k)::double precision;
    if m is null or m<0 or m>1000000 then raise exception 'invalid_minutes'; end if;
    update private.observer_installations set github_minutes=m,
      github_minutes_estimate=private.observer_org_month_estimate(k,true),github_minutes_at=now()
      where organization=k;
    if found then n:=n+1; end if;
  end loop;
  return n;
end $function$;

CREATE OR REPLACE FUNCTION private.observer_effective_minutes(i private.observer_installations, p_estimate double precision)
 RETURNS double precision LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO 'public', 'pg_temp'
AS $function$
  select case when private.observer_health_mode()='on' and i.github_minutes is not null
      and i.github_minutes_at>=date_trunc('month',now())
    then case when i.github_minutes_at>now()-interval '3 hours'
      then i.github_minutes+greatest(p_estimate-coalesce(i.github_minutes_estimate,p_estimate),0)
      else greatest(p_estimate,i.github_minutes+greatest(p_estimate-coalesce(i.github_minutes_estimate,p_estimate),0)) end
    else p_estimate end
$function$;

CREATE OR REPLACE FUNCTION public.observer_organizations_by_load()
 RETURNS TABLE(organization text, approved_sha text, active_jobs bigint, week_seconds double precision, month_minutes double precision, monthly_minute_limit integer, dispatch_failures bigint, placements bigint, over_limit boolean, healthy boolean, health text)
 LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO 'public', 'pg_temp'
AS $function$
  select y.organization, y.approved_sha, y.active_jobs, y.week_seconds, y.month_minutes, y.monthly_minute_limit,
    y.dispatch_failures, y.placements, y.over_limit,
    y.health='ok' as healthy, y.health
  from (
    select x.*, x.month_minutes>=x.monthly_minute_limit as over_limit,
      case when private.observer_health_mode()<>'on' then 'ok'
        when not x.routing then 'switched_off'
        when x.cooldown_until>now() then 'cooldown'
        when x.month_minutes>=x.monthly_minute_limit then 'minute_limit'
        else 'ok' end as health
    from (
      select i.organization, i.approved_sha, i.routing, i.cooldown_until,
        (select count(*) from private.observer_jobs j where j.organization=i.organization and j.runner='github-hosted'
          and j.status in ('queued','dispatched','claimed') and j.expires_at>now()) as active_jobs,
        (select coalesce(sum(extract(epoch from j.finished_at-j.claimed_at)),0)::double precision
          from private.observer_jobs j where j.organization=i.organization and j.runner='github-hosted'
            and j.claimed_at is not null and j.finished_at is not null
            and j.finished_at>now()-interval '7 days') as week_seconds,
        private.observer_effective_minutes(i,private.observer_org_month_estimate(i.organization)) as month_minutes,
        i.monthly_minute_limit,
        (select count(*) from private.observer_dispatch_failures f where f.organization=i.organization
          and f.failed_at>now()-interval '30 minutes') as dispatch_failures,
        (select count(*) from private.observer_placements p where p.organization=i.organization) as placements
      from private.observer_installations i where i.enabled) x) y
  order by y.health<>'ok', y.over_limit, y.dispatch_failures>0, y.active_jobs, y.week_seconds,
    case when y.over_limit then y.month_minutes end,
    y.placements, substring(y.organization from '[0-9]+$')::integer
$function$;

-- Production (2026-10-06): update private.observer_public_pool set engine_score_private=false;
--   select public.observer_set_public_pool(p_monthly_minute_cap=>100000000, p_max_active=>260);

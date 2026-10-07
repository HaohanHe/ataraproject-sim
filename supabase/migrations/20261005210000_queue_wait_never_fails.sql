-- Waiting for a runner never fails an evaluation, and scarce runners go to ranked teams first
-- (2026-10-05). Formal evaluations now run more cards, so at peak demand exceeds the runner
-- capacity and evaluations wait (possibly for hours). Waiting is fine; failing because of the
-- wait is not. Based on the live definitions.
--
-- 1. A job that no runner has claimed yet keeps its claim window for 12 hours (was 30 minutes):
--    an evaluation waiting in a runner queue is no longer expired, requeued and dispatched again
--    (which put it at the back of the queue each time). Existing unclaimed jobs get the same window.
-- 2. public.observer_claim_job: the session window (runtime + 30 minutes) starts when a runner
--    claims the job, not when the evaluation was handed to the runner queue.
-- 3. public.observer_reconcile_sessions: a session never expires while its job still waits for a
--    runner; a queued run the dispatcher never took expires 12 hours (was 30 minutes) after it
--    could start (the safety net for a truly broken run).
-- 4. public.observer_pending_runs / observer_pending_preparations: the scheduling retry budget
--    (park with an incident) needs 8 attempts, not only 2 hours since the first lease, so a run
--    that waited long and then hit one platform retry is retried instead of parked.
-- 5. public.observer_pending_runs: while runners are scarce (private.observer_queue_config
--    .max_waiting_jobs unclaimed evaluation jobs already wait), no further runs are handed out;
--    runs are handed out by team tier, FIFO within a tier:
--      1 scored formal evaluation in the online phase, 2 scored evaluation on the practice
--      board, 3 scored entry on the CSV debug board, 4 everyone else
--    (private.observer_queue_priority_teams can place a team in a tier explicitly).
--    Runs already leased, starting or running are not affected.
-- Function replacements, one column default and two small config tables; nothing running changes.

alter table private.observer_jobs alter column expires_at set default now()+interval '12 hours';

update private.observer_jobs set expires_at=greatest(expires_at,created_at+interval '12 hours')
  where status in ('queued','dispatched') and claimed_at is null and expires_at>now();

create table if not exists private.observer_queue_config(
  id boolean primary key default true check(id),
  -- null: no capacity gate (every queued run is handed out at once, as before).
  max_waiting_jobs integer check(max_waiting_jobs is null or max_waiting_jobs>=0),
  online_phase uuid,
  practice_phase uuid,
  csv_phase uuid,
  updated_at timestamptz not null default now());
insert into private.observer_queue_config(id,max_waiting_jobs,online_phase,practice_phase,csv_phase)
  values(true,50,'049d6029-343d-4d16-80d5-94b56b350301','249c223b-2ec8-4f0a-924e-7af3c4e3eb44',
    '88908c68-ebb1-4475-b793-a437c857c932')
  on conflict do nothing;
revoke all on private.observer_queue_config from public,anon,authenticated;

create table if not exists private.observer_queue_priority_teams(
  team_id uuid primary key references public.teams(id) on delete cascade,
  tier integer not null default 1 check(tier between 1 and 4),
  note text not null default '',
  added_at timestamptz not null default now());
revoke all on private.observer_queue_priority_teams from public,anon,authenticated;

-- Dispatch tier of a team (1 highest .. 4); a team takes its highest tier.
create or replace function private.observer_queue_tier(p_team uuid)
returns integer language sql stable security definer set search_path=public,pg_temp as $$
  select least(
    coalesce((select x.tier from private.observer_queue_priority_teams x where x.team_id=p_team),4),
    case when t.is_hidden then 4
      when exists(select 1 from public.observer_batches b where b.team_id=p_team and b.phase_id=c.online_phase
        and b.status='scored' and b.purpose='formal') then 1
      when exists(select 1 from public.observer_batches b where b.team_id=p_team and b.phase_id=c.practice_phase
        and b.status='scored' and b.purpose='formal') then 2
      when exists(select 1 from public.submissions s where s.team_id=p_team and s.phase_id=c.csv_phase
        and s.status='scored' and not s.is_excluded
        and exists(select 1 from public.evaluations e where e.submission_id=s.id and e.score is not null)) then 3
      else 4 end)
  from public.teams t left join private.observer_queue_config c on c.id where t.id=p_team
$$;
revoke all on function private.observer_queue_tier(uuid) from public,anon,authenticated;

create or replace function public.observer_claim_job(p_job uuid, p_nonce text, p_github_run text, p_attempt text, p_repository text, p_owner text, p_sha text)
returns text language plpgsql security definer set search_path=public,pg_temp as $$
declare v private.observer_jobs; v_run uuid; v_seconds integer;
begin
  select j.* into v from private.observer_jobs j join private.observer_installations i on i.organization=j.organization
    where j.id=p_job and i.enabled for update of j;
  if not found or v.expires_at<=clock_timestamp() or v.status not in ('queued','dispatched','claimed') then
    raise exception 'job_unavailable'; end if;
  if v.runner='public-hosted' then
    if p_nonce is not null or v.public_run_id is null or p_github_run is distinct from v.public_run_id
      or not exists(select 1 from private.observer_public_targets t where t.organization=v.organization
        and t.repository_id=v.repository_id) then raise exception 'job_identity_mismatch'; end if;
  elsif p_nonce is null or v.nonce_hash is distinct from sha256(convert_to(p_nonce,'UTF8')) then
    raise exception 'job_identity_mismatch';
  end if;
  if p_repository is distinct from v.repository_id or p_owner is distinct from v.organization_id
    or p_sha is distinct from v.workflow_sha or p_github_run is null or p_github_run !~ '^[0-9]+$'
    or p_attempt is null or p_attempt !~ '^[0-9]+$' then raise exception 'job_identity_mismatch'; end if;
  if v.status='claimed' then
    if v.github_run_id is distinct from p_github_run or v.github_run_attempt is distinct from p_attempt then
      raise exception 'job_already_claimed'; end if;
    return v.encrypted_input;
  end if;
  update private.observer_jobs set status='claimed',github_run_id=p_github_run,github_run_attempt=p_attempt,
    claimed_at=now(),expires_at=now()+interval '6 hours' where id=p_job;
  -- The session window starts when a runner takes the evaluation, not when it was queued.
  if v.kind in ('engine','execute','prepare') then
    v_run:=coalesce(v.run_id,(select p.model_run_id from private.observer_preparations p where p.revision_id=v.revision_id));
    select case when b.purpose='adaptation' then 1800 when b.purpose='preview' then least(c.runtime_seconds,300)
        else c.runtime_seconds end into v_seconds
      from public.observer_runs r join public.observer_batches b on b.id=r.batch_id
      join public.observer_phase_settings c on c.phase_id=b.phase_id where r.id=v_run;
    if v_seconds is not null then
      update private.observer_sessions set expires_at=greatest(expires_at,now()+make_interval(secs=>v_seconds+1800))
        where run_id=v_run and deadline_at is null;
    end if;
  end if;
  return v.encrypted_input;
end $$;

create or replace function public.observer_reconcile_sessions()
returns integer language plpgsql security definer set search_path=public,pg_temp as $$
declare v record; n integer:=0;
begin
  for v in select id from private.observer_model_calls where status='reserved'
    and created_at<now()-interval '5 minutes' order by created_at limit 100
  loop
    perform public.observer_settle_model(v.id,null);
    n:=n+1;
  end loop;
  -- Queued/never-dispatched runs and missing job callbacks must not lock a team
  -- out forever. Expiration is an infrastructure failure, never a scored zero.
  -- Waiting for a runner never expires a run: a queued run expires only when it has been
  -- able to start for 12 hours and the dispatcher never took it (waiting for an earlier
  -- evaluation of its self-check set, or an earlier scenario of a local session, is not
  -- stuck; a run the dispatcher is retrying (it has a lease) follows the lease budget in
  -- observer_pending_runs: parked with an incident, never failed), and a session never
  -- expires while one of its jobs still waits for a runner (the window starts at claim).
  for v in select r.id,r.batch_id from public.observer_runs r
    left join private.observer_sessions s on s.run_id=r.id
    where (r.status='queued' and not exists(select 1 from private.observer_run_leases l where l.run_id=r.id)
        and private.observer_run_startable_since(r.id)<now()-interval '12 hours')
      or (r.status in ('starting','ready','running') and s.expires_at<=now()
        and not exists(select 1 from private.observer_jobs j where j.run_id=r.id
          and j.status in ('queued','dispatched') and j.expires_at>now())
        and not exists(select 1 from private.observer_preparations p join private.observer_jobs j
          on j.revision_id=p.revision_id and j.kind='prepare'
          where p.model_run_id=r.id and j.status in ('queued','dispatched') and j.expires_at>now()))
    order by r.created_at limit 100
  loop
    update public.observer_runs set status='failed',error='evaluation_expired',finished_at=now()
      where id=v.id and status in ('queued','starting','ready','running');
    perform private.observer_finalize_batch(v.batch_id);
    n:=n+1;
  end loop;
  return n;
end $$;

create or replace function public.observer_pending_runs(p_limit integer default 5)
returns jsonb language plpgsql security definer set search_path=public,pg_temp as $$
declare r record; result jsonb:='[]'; v_lease uuid; v_minutes integer; v_old private.observer_run_leases;
  v_cap integer:=greatest(1,least(coalesce(p_limit,5),10)); v_count integer:=0; v_group text; v_max integer;
begin
  -- Scheduling leases past the retry budget are parked, never failed: the run
  -- stays 'queued' for the contestant and an incident pages the organizers.
  -- Waiting for a runner does not use the budget: it needs repeated attempts too.
  for r in select run.id,run.batch_id,l.attempts,l.first_leased_at from public.observer_runs run
    join private.observer_run_leases l on l.run_id=run.id
    where run.status='queued' and l.paused_at is null and l.expires_at<=now()
      and now()-l.first_leased_at>interval '2 hours' and l.attempts>=8
    for update of run skip locked limit 100
  loop
    update private.observer_run_leases set paused_at=now() where run_id=r.id;
    perform private.observer_raise_incident('run',r.id,'run_schedule_retry_budget_exhausted',
      jsonb_build_object('attempts',r.attempts,'batch_id',r.batch_id));
  end loop;
  -- Capacity gate: while enough evaluation jobs already wait for a runner, further runs wait
  -- here (queued, by tier) instead of in the runner queues.
  select q.max_waiting_jobs into v_max from private.observer_queue_config q where q.id;
  if v_max is not null then
    v_cap:=least(v_cap,greatest(0,v_max-(select count(*) from private.observer_jobs j
      where j.status in ('queued','dispatched') and j.kind in ('engine','execute') and j.expires_at>now())::integer));
    if v_cap=0 then return result; end if;
  end if;
  -- Serialize reservations, including first insert, on the public run row. A
  -- crashed scheduler leaves a short lease, never a half-open session.
  for r in select run.id,b.user_id,b.mode,
      case when b.purpose='preview' then least(c.runtime_seconds,300) else c.runtime_seconds end as runtime_seconds,
      s.storage_path,s.digest as scenario_digest,
      m.archive_ref,m.digest as materialized_digest,rev.manifest,
      -- Repeated evaluations that must start together: the same card of one team's evaluations.
      case when c.repeat_runs>1 and b.purpose='formal' then b.team_id::text||'/'||run.scenario_id::text end as start_group
    from public.observer_runs run join public.observer_batches b on b.id=run.batch_id
    join public.observer_phase_settings c on c.phase_id=b.phase_id
    join private.observer_scenario_bundles s on s.scenario_id=run.scenario_id
    join public.profiles p on p.id=b.user_id and not p.is_banned and p.team_id=b.team_id
    left join public.observer_revisions rev on rev.id=b.revision_id
    left join private.observer_materializations m on m.revision_id=rev.id
    left join private.observer_run_leases l on l.run_id=run.id
    where run.status='queued' and b.purpose in ('formal','preview') and b.status in ('queued','running')
      and ((b.mode='local' and c.local_sessions_enabled) or
        (b.mode='project' and c.projects_enabled and m.revision_id is not null and
          ((b.purpose='formal' and rev.status='approved') or (b.purpose='preview' and rev.status='preparing'))))
      -- A local user starts one scenario at a time. Do not spend hosted engine
      -- minutes waiting for the other scenarios while their first CLI is busy.
      and (b.mode<>'local' or not exists(select 1 from public.observer_runs other
        where other.batch_id=run.batch_id and other.id<>run.id and
          (other.status in ('starting','ready','running') or
            (other.status='queued' and (other.created_at,other.id)<(run.created_at,run.id)))))
      and (l.run_id is null or (l.expires_at<=now() and l.paused_at is null))
      -- The evaluations of one self-check set run one after another; a team's other evaluations
      -- run side by side (up to max_active_evaluations, enforced on creation). The hidden final's
      -- repeats start together (start_group).
      and (b.purpose<>'formal' or c.repeat_runs>1 or b.repeat_group is null or not exists(select 1 from public.observer_batches earlier
        where earlier.repeat_group=b.repeat_group and earlier.purpose='formal'
          and earlier.id<>b.id and earlier.status in ('queued','running')
          and (earlier.created_at,earlier.id)<(b.created_at,b.id)))
    -- Ranked teams first (private.observer_queue_tier), FIFO within a tier.
    order by private.observer_queue_tier(b.team_id),run.created_at,run.id for update of run skip locked limit 40
  loop
    -- Up to the cap, but a start group (created adjacently) is never split between passes.
    exit when v_count>=v_cap and (r.start_group is null or r.start_group is distinct from v_group);
    -- The join snapshot may predate a concurrently committed first lease even
    -- though this row lock was acquired afterwards. Re-read under the lock.
    select * into v_old from private.observer_run_leases where run_id=r.id;
    if found and (v_old.expires_at>now() or v_old.paused_at is not null) then continue; end if;
    v_lease:=gen_random_uuid();
    v_minutes:=case when found then (least(20,2^greatest(v_old.attempts,1)))::int else 2 end;
    insert into private.observer_run_leases(run_id,lease,expires_at)
      values(r.id,v_lease,now()+(v_minutes||' minutes')::interval)
      on conflict(run_id) do update set lease=excluded.lease,expires_at=excluded.expires_at,
        attempts=private.observer_run_leases.attempts+1;
    result:=result || jsonb_build_array((to_jsonb(r)-'start_group')||jsonb_build_object('lease',v_lease));
    v_count:=v_count+1; v_group:=r.start_group;
  end loop;
  return result;
end $$;

create or replace function public.observer_pending_preparations(p_limit integer default 3)
returns jsonb language plpgsql security definer set search_path=public,pg_temp as $$
declare r record;c private.observer_preparation_config;l private.observer_preparations;result jsonb:='[]';v_gameplay text;v_minutes integer;
begin
  select x.* into c from private.observer_preparation_config x
    join public.scenarios s on s.id=x.scenario_id
    join public.observer_phase_settings f on f.phase_id=x.phase_id
    where x.enabled and f.projects_enabled and s.is_active and s.weather_public and s.events_public and s.forecasts_public;
  if not found then return result; end if;
  v_gameplay:=case when exists(select 1 from public.scenarios where id=c.scenario_id and contract='v4-score-v1') then 'v4' else 'v3' end;
  for r in select rev.id,p.owner_id,p.team_id,rev.source_kind,rev.source_location,rev.submitted_commit
    from public.observer_revisions rev join public.observer_projects p on p.id=rev.project_id
    join public.profiles u on u.id=p.owner_id and u.team_id=p.team_id and not u.is_banned
    left join private.observer_preparations prep on prep.revision_id=rev.id
    -- A parked row stays out of this scan entirely: no new lease until an
    -- organizer clears it via observer_admin_requeue.
    where rev.status='queued' and (prep.revision_id is null or (prep.expires_at<=now() and prep.paused_at is null))
    order by coalesce(prep.attempts,0),rev.created_at,rev.id for update of rev skip locked limit greatest(1,least(coalesce(p_limit,3),10))
  loop
    select * into l from private.observer_preparations where revision_id=r.id;
    if found and l.expires_at>now() then continue; end if;
    -- Waiting for a runner does not use the budget: it needs repeated attempts too.
    if found and now()-l.first_leased_at>interval '2 hours' and l.attempts>=8 then
      update private.observer_preparations set paused_at=now() where revision_id=r.id;
      perform private.observer_raise_incident('revision',r.id,'preparation_retry_budget_exhausted',
        jsonb_build_object('attempts',l.attempts,'team_id',r.team_id,'owner_id',r.owner_id));
      continue;
    end if;
    v_minutes:=case when found then (least(20,2^greatest(l.attempts,1)))::int else 2 end;
    insert into private.observer_preparations(revision_id,lease,expires_at,phase_id,scenario_id,model)
      values(r.id,gen_random_uuid(),now()+(v_minutes||' minutes')::interval,c.phase_id,c.scenario_id,c.model)
      on conflict(revision_id) do update set lease=excluded.lease,expires_at=excluded.expires_at,
        attempts=private.observer_preparations.attempts+1 returning * into l;
    result:=result || jsonb_build_array(to_jsonb(r)||jsonb_build_object('lease',l.lease,'model_run_id',l.model_run_id,'model',l.model,
      'gameplay',v_gameplay));
  end loop;
  return result;
end $$;

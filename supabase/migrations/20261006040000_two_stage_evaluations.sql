-- Two-stage evaluations (online competition): every project evaluation in a phase that has both cards A-D and
-- the added cards A1-D1 (and is not sealed) runs A-D first; when all four finished, the A1-D1 cards of the same
-- evaluation start (observer_pending_runs). Halves a team's concurrent model calls; the super board always uses
-- one evaluation for all 8 cards. Evaluations created earlier (staged=false) run as before.
-- * A failed stage 1 fails the evaluation as before and cancels its queued stage 2 (error 'stage1_failed').
-- * The A-D board counts a staged evaluation once its A-D are complete, while A1-D1 still run.
-- * Super board: 20% of the A-D sum + 80% of the A1-D1 sum of each team's latest complete 8-card evaluation.
-- Quota, refunds, the hidden final and other phases are unchanged.
alter table public.observer_batches add column if not exists staged boolean not null default false;

CREATE OR REPLACE FUNCTION private.observer_phase_staged(p_phase uuid)
 RETURNS boolean
 LANGUAGE sql
 STABLE SECURITY DEFINER
 SET search_path TO 'public', 'pg_temp'
AS $function$
  select coalesce((select not coalesce(s.sealed,false) from public.observer_phase_settings s where s.phase_id=p_phase),false)
    and exists(select 1 from public.phase_scenarios ps join public.scenarios sc on sc.id=ps.scenario_id
      where ps.phase_id=p_phase and private.observer_extra_card(sc.slug))
    and exists(select 1 from public.phase_scenarios ps join public.scenarios sc on sc.id=ps.scenario_id
      where ps.phase_id=p_phase and not private.observer_extra_card(sc.slug))
$function$;
revoke all on function private.observer_phase_staged(uuid) from public,anon,authenticated;

CREATE OR REPLACE FUNCTION private.observer_stage2_waiting(p_run uuid)
 RETURNS boolean
 LANGUAGE sql
 STABLE SECURITY DEFINER
 SET search_path TO 'public', 'pg_temp'
AS $function$
  -- An A1-D1 card of a staged evaluation whose A-D cards have not all finished.
  select exists(select 1 from public.observer_runs r join public.observer_batches b on b.id=r.batch_id
      join public.scenarios s on s.id=r.scenario_id
    where r.id=p_run and b.staged and private.observer_extra_card(s.slug)
      and exists(select 1 from public.observer_runs o join public.scenarios os on os.id=o.scenario_id
        where o.batch_id=r.batch_id and not private.observer_extra_card(os.slug)
          and o.status not in ('scored','failed','cancelled')))
$function$;
revoke all on function private.observer_stage2_waiting(uuid) from public,anon,authenticated;

CREATE OR REPLACE FUNCTION public.observer_create_batch(p_phase uuid, p_revision uuid DEFAULT NULL::uuid, p_confirm_repeat boolean DEFAULT false, p_no_model boolean DEFAULT false)
 RETURNS uuid
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public', 'pg_temp'
AS $function$
declare v_team uuid; v_config public.observer_phase_settings; v_phase public.phases; v_id uuid;
begin
  perform private.assert_not_banned();
  select team_id into v_team from public.profiles where id=auth.uid();
  if v_team is null then raise exception 'team_required'; end if;
  perform 1 from public.teams where id=v_team for update;
  select * into v_phase from public.phases where id=p_phase;
  select * into v_config from public.observer_phase_settings where phase_id=p_phase;
  if not found or not v_phase.is_active or (v_phase.starts_at is not null and now()<v_phase.starts_at)
     or (v_phase.ends_at is not null and now()>=v_phase.ends_at) then raise exception 'phase_closed'; end if;
  if coalesce(p_no_model,false) and (p_revision is null or coalesce(v_config.sealed,false)) then
    raise exception 'no_model_not_available'; end if;
  if p_revision is null then
    if not v_config.local_sessions_enabled then raise exception 'local_sessions_disabled'; end if;
  else
    if not v_config.projects_enabled then raise exception 'projects_not_enabled'; end if;
    if exists(select 1 from public.observer_revisions r join public.observer_projects p on p.id=r.project_id
      where r.id=p_revision and p.team_id=v_team and r.archived_at is not null) then raise exception 'revision_withdrawn'; end if;
    if not exists(select 1 from public.observer_revisions r join public.observer_projects p on p.id=r.project_id
      where r.id=p_revision and r.status='approved' and p.team_id=v_team) then raise exception 'revision_not_approved'; end if;
  end if;
  if private.observer_batches_used(v_team,p_phase) >= v_config.daily_batches then
    raise exception 'daily_limit';
  end if;
  -- Up to max_active_evaluations of the team's evaluations in flight (a self-check set counts once);
  -- the organizers' sealed final evaluations do not count.
  if private.observer_active_evaluations(v_team) >= coalesce(v_config.max_active_evaluations,4) then
    raise exception 'batch_already_active';
  end if;
  if not exists(select 1 from public.phase_scenarios where phase_id=p_phase) then raise exception 'no_scenarios'; end if;
  -- Another evaluation of the same version uses another daily evaluation; the
  -- team must ask for it explicitly. A refunded (platform-failed) try does not count.
  if p_revision is not null and not coalesce(p_confirm_repeat,false) and exists(select 1 from public.observer_batches
    where revision_id=p_revision and phase_id=p_phase and purpose='formal' and not quota_refunded) then
    raise exception 'revision_already_evaluated';
  end if;
  -- Two stages (online competition): A-D first, then A1-D1 of the same evaluation (observer_pending_runs).
  insert into public.observer_batches(team_id,user_id,phase_id,revision_id,mode,model_disabled,staged)
    values(v_team,auth.uid(),p_phase,p_revision,case when p_revision is null then 'local' else 'project' end,
      coalesce(p_no_model,false),p_revision is not null and private.observer_phase_staged(p_phase))
    returning id into v_id;
  insert into public.observer_runs(batch_id,scenario_id)
    select v_id,scenario_id from public.phase_scenarios where phase_id=p_phase;
  return v_id;
end $function$;

CREATE OR REPLACE FUNCTION public.observer_create_repeat_batches(p_phase uuid, p_revision uuid, p_confirm_repeat boolean DEFAULT false, p_no_model boolean DEFAULT false)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public', 'pg_temp'
AS $function$
declare v_team uuid; v_config public.observer_phase_settings; v_runs integer:=private.observer_self_check_runs();
  v_group uuid:=gen_random_uuid(); v_first uuid; v_id uuid; v_ids jsonb; i integer;
begin
  perform private.assert_not_banned();
  select team_id into v_team from public.profiles where id=auth.uid();
  if v_team is null then raise exception 'team_required'; end if;
  if p_revision is null then raise exception 'revision_not_approved'; end if;
  perform 1 from public.teams where id=v_team for update;
  select * into v_config from public.observer_phase_settings where phase_id=p_phase;
  -- Checked before the first evaluation is created: all three or none.
  if found and private.observer_batches_used(v_team,p_phase)+v_runs > v_config.daily_batches then
    raise exception 'repeat_daily_limit';
  end if;
  -- At most one self-check set in flight per team.
  if exists(select 1 from public.observer_batches where team_id=v_team and purpose='formal' and repeat_group is not null
    and status in ('queued','running')) then raise exception 'repeat_already_active'; end if;
  -- Every other check (phase open, confirmed version, evaluations in flight, repeat confirmation) as usual.
  v_first:=public.observer_create_batch(p_phase,p_revision,p_confirm_repeat,coalesce(p_no_model,false));
  update public.observer_batches set repeat_group=v_group,repeat_runs=v_runs where id=v_first;
  v_ids:=jsonb_build_array(v_first);
  for i in 2..v_runs loop
    insert into public.observer_batches(team_id,user_id,phase_id,revision_id,mode,repeat_group,repeat_runs,created_at,
        model_disabled,staged)
      values(v_team,auth.uid(),p_phase,p_revision,'project',v_group,v_runs,clock_timestamp(),coalesce(p_no_model,false),
        private.observer_phase_staged(p_phase))
      returning id into v_id;
    insert into public.observer_runs(batch_id,scenario_id,created_at)
      select v_id,scenario_id,clock_timestamp() from public.phase_scenarios where phase_id=p_phase order by scenario_id;
    v_ids:=v_ids||jsonb_build_array(v_id);
  end loop;
  perform private.audit('observer.repeat_evaluation',jsonb_build_object('phase_id',p_phase,'revision_id',p_revision,
    'repeat_group',v_group,'batches',v_ids,'model_disabled',coalesce(p_no_model,false)));
  return jsonb_build_object('repeat_group',v_group,'batch_ids',v_ids);
end $function$;

CREATE OR REPLACE FUNCTION public.observer_pending_runs(p_limit integer DEFAULT 5)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public', 'pg_temp'
AS $function$
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
      -- Two stages: an A1-D1 card of a staged evaluation starts only once all its A-D cards finished.
      and not (b.staged and private.observer_stage2_waiting(run.id))
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
end $function$;

CREATE OR REPLACE FUNCTION private.observer_run_startable_since(p_run uuid)
 RETURNS timestamp with time zone
 LANGUAGE sql
 STABLE SECURITY DEFINER
 SET search_path TO 'public', 'pg_temp'
AS $function$
  select case
    when b.staged and private.observer_stage2_waiting(r.id) then null
    when b.repeat_group is not null and exists(select 1 from public.observer_batches e
      where e.repeat_group=b.repeat_group and e.id<>b.id and e.status in ('queued','running')
        and (e.created_at,e.id)<(b.created_at,b.id)) then null
    when b.mode='local' and exists(select 1 from public.observer_runs o where o.batch_id=r.batch_id and o.id<>r.id
        and o.status in ('queued','starting','ready','running') and (o.created_at,o.id)<(r.created_at,r.id)) then null
    else greatest(r.created_at,
      coalesce((select max(e.finished_at) from public.observer_batches e where b.repeat_group is not null
        and e.repeat_group=b.repeat_group and e.id<>b.id and (e.created_at,e.id)<(b.created_at,b.id)),r.created_at),
      coalesce((select max(o.finished_at) from public.observer_runs o where b.mode='local' and o.batch_id=r.batch_id
        and o.id<>r.id and (o.created_at,o.id)<(r.created_at,r.id)),r.created_at),
      -- Stage 2 (A1-D1 of a staged evaluation) can start once stage 1 (A-D) finished.
      coalesce((select max(o.finished_at) from public.observer_runs o join public.scenarios os on os.id=o.scenario_id
        where b.staged and o.batch_id=r.batch_id and not private.observer_extra_card(os.slug)
          and private.observer_extra_card((select s.slug from public.scenarios s where s.id=r.scenario_id))),r.created_at))
  end
  from public.observer_runs r join public.observer_batches b on b.id=r.batch_id where r.id=p_run
$function$;

CREATE OR REPLACE FUNCTION private.observer_finalize_batch(p_batch uuid)
 RETURNS void
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public', 'pg_temp'
AS $function$
begin
  perform 1 from public.observer_batches where id=p_batch for update;
  if exists(select 1 from public.observer_runs r where r.batch_id=p_batch and private.observer_run_fails_batch(r.id)) then
    -- Decided once, when the batch fails: later runs of a failed batch no longer count.
    update public.observer_batches set status='failed',finished_at=now(),
      quota_refunded=not exists(select 1 from public.observer_runs r where r.batch_id=p_batch
        and private.observer_participant_failure(r.id))
      where id=p_batch and status in ('queued','running');
    -- Two stages: a failed stage 1 (A-D) never starts stage 2 (A1-D1); requeue_platform_failures
    -- queues these again if it reopens the evaluation.
    update public.observer_runs r set status='cancelled',error='stage1_failed',finished_at=now()
      from public.observer_batches b, public.scenarios s
      where b.id=p_batch and b.staged and b.status='failed' and r.batch_id=p_batch and s.id=r.scenario_id
        and r.status='queued' and private.observer_extra_card(s.slug);
  elsif exists(select 1 from public.observer_runs where batch_id=p_batch)
    and not exists(select 1 from public.observer_runs where batch_id=p_batch and status not in ('scored','failed','cancelled')) then
    -- Only scored cards, (hidden final) the team's own failed cards and failed added cards are
    -- left (a failed or cancelled A-D card failed the batch above); those count as 0.
    update public.observer_batches set status='scored',score=private.observer_batch_score(p_batch),finished_at=now()
      where id=p_batch and status in ('queued','running');
  end if;
end $function$;

CREATE OR REPLACE FUNCTION private.observer_card_board_live(p_phase uuid, p_scenario_slug text DEFAULT NULL::text, p_limit integer DEFAULT 100)
 RETURNS jsonb
 LANGUAGE plpgsql
 STABLE SECURITY DEFINER
 SET search_path TO 'public', 'pg_temp'
AS $function$
declare
  v_layout text; v_mode text; v_named boolean; v_cards jsonb:='[]'; v_extra jsonb:='[]'; v_card uuid; v_rows jsonb;
  v_limit integer:=greatest(1,least(coalesce(p_limit,100),1000));
  v_zero boolean:=private.observer_failed_cards_score_zero(p_phase);
  v_repeat integer; v_final boolean:=private.observer_final_phase(p_phase);
begin
  select coalesce(s.board_layout,'overall'),p.leaderboard_mode,greatest(1,coalesce(s.repeat_runs,1)) into v_layout,v_mode,v_repeat
    from public.phases p left join public.observer_phase_settings s on s.phase_id=p.id where p.id=p_phase;
  if v_layout is null or not public.observer_phase_visible(p_phase) then
    return jsonb_build_object('layout',coalesce(v_layout,'overall'),'cards','[]'::jsonb,'scenario',null,'rows','[]'::jsonb);
  end if;
  if v_layout='overall' then
    return jsonb_build_object('layout','overall','cards','[]'::jsonb,'scenario',null,
      'rows',public.observer_board(p_phase,v_limit));
  end if;

  select coalesce(bool_and(public.is_admin() or v_mode='published' or public.observer_scenario_listed(sc.id)),false),
         coalesce(jsonb_agg(jsonb_build_object('slug',sc.slug,'name',sc.name) order by sc.slug),'[]'::jsonb)
    into v_named,v_cards
    from public.phase_scenarios ps join public.scenarios sc on sc.id=ps.scenario_id
    where ps.phase_id=p_phase and not private.observer_extra_card(sc.slug);
  if not v_named then v_cards:='[]'; end if;
  -- The added cards A1-D1 (named only where listed): their tabs are the super board's.
  select case when bool_and(public.is_admin() or v_mode='published' or public.observer_scenario_listed(sc.id))
           then jsonb_agg(jsonb_build_object('slug',sc.slug,'name',sc.name) order by sc.slug) end
    into v_extra
    from public.phase_scenarios ps join public.scenarios sc on sc.id=ps.scenario_id
    where ps.phase_id=p_phase and private.observer_extra_card(sc.slug);
  v_extra:=coalesce(v_extra,'[]'::jsonb);

  if p_scenario_slug is not null and v_extra @> jsonb_build_array(jsonb_build_object('slug',p_scenario_slug)) then
    return jsonb_build_object('layout',v_layout,'cards',v_cards,'extra_cards',v_extra,'scenario',p_scenario_slug,
      'rows',public.observer_super_board(p_phase,p_scenario_slug,v_limit)->'rows');
  end if;
  if p_scenario_slug is not null then
    select sc.id into v_card from public.phase_scenarios ps join public.scenarios sc on sc.id=ps.scenario_id
      where ps.phase_id=p_phase and sc.slug=p_scenario_slug and not private.observer_extra_card(sc.slug);
    if v_card is null or not v_named then
      return jsonb_build_object('layout',v_layout,'cards',v_cards,'extra_cards',v_extra,'scenario',null,'rows','[]'::jsonb);
    end if;
  end if;

  -- Cards A-D only: the added cards never enter this board's score, completeness or columns.
  with cards as (select ps.scenario_id from public.phase_scenarios ps join public.scenarios sc on sc.id=ps.scenario_id
      where ps.phase_id=p_phase and not private.observer_extra_card(sc.slug)),
  eligible as (
    select b.id,b.team_id,t.name as team_name,b.revision_id,b.created_at,b.finished_at,avg(coalesce(r.score,0)) as overall
    from public.observer_batches b join public.teams t on t.id=b.team_id join public.phases p on p.id=b.phase_id
    join public.observer_runs r on r.batch_id=b.id and r.scenario_id in (select scenario_id from cards)
      and (r.status='scored' or (v_zero and private.observer_participant_failure(r.id)))
    -- A staged evaluation (A-D, then A1-D1) counts here once its A-D are complete, while A1-D1 still run.
    where b.phase_id=p_phase and b.purpose='formal' and (b.status='scored' or (b.staged and b.status in ('queued','running')))
      and b.superseded_at is null and (p.is_active or public.is_admin())
      and (public.is_admin() or (not t.is_hidden and p.leaderboard_mode in ('live','published')))
    group by b.id,t.name
    having count(*)=(select count(*) from cards) and count(*)>0
  ),
  -- One batch per team: its best one; with repeat_runs > 1 its first repeat_runs complete ones, averaged.
  best as (
    select x.* from (select distinct on(e.team_id) e.id,e.team_id,e.team_name,e.created_at,e.finished_at,e.overall,
        array[e.id] as batch_ids,e.overall as low,e.overall as high
      from eligible e order by e.team_id,e.overall desc,e.created_at) x where v_repeat=1
    union all
    select (array_agg(o.id order by o.created_at,o.id))[1],o.team_id,min(o.team_name),min(o.created_at),max(o.finished_at),
      avg(o.overall),array_agg(o.id order by o.created_at,o.id),min(o.overall),max(o.overall)
    from (select e.*,row_number() over(partition by e.team_id order by e.created_at,e.id) as n from eligible e) o
    where v_repeat>1 and o.n<=v_repeat
    group by o.team_id having count(*)=v_repeat
  ),
  ranked as (
    select b.*,rank() over(order by b.overall desc) as overall_rank,
      (select count(*) from public.observer_batches x where x.phase_id=p_phase and x.team_id=b.team_id
        and x.purpose='formal' and x.status='scored') as submission_count
    from best b
  )
  select coalesce(jsonb_agg(to_jsonb(rows) order by rows.rank,rows.scored_at,rows.team_name),'[]'::jsonb) into v_rows from (
    select * from (
      select
        case when v_card is null then k.overall_rank else rank() over(order by coalesce(r.score,0) desc) end as rank,
        k.team_id,k.team_name,
        (select p.github from public.teams tt join public.profiles p on p.id=tt.leader_id where tt.id=k.team_id) as leader_github,(select p.avatar_url from public.teams tt join public.profiles p on p.id=tt.leader_id where tt.id=k.team_id) as leader_avatar_url,
        case when v_card is null then k.overall else coalesce(r.score,0) end as total_score,
        k.overall as overall_score,k.overall_rank,k.finished_at as scored_at,
        k.id as observer_batch_id,'observer'::text as kind,
        case when v_card is null then null else p_scenario_slug end as scenario_slug,
        k.submission_count,
        case when v_repeat>1 then cardinality(k.batch_ids) end as averaged_runs,
        -- Lowest and highest of the averaged evaluations: overall, this card, or per card on the overall tab.
        case when v_repeat>1 then case when v_card is null then jsonb_build_array(k.low,k.high)
          else jsonb_build_array(r.low,r.high) end end as score_range,
        case when v_repeat>1 and v_card is null then (
          select case when v_named then jsonb_object_agg(c.slug,jsonb_build_array(c.low,c.high)) end from (
            select sc.slug,min(coalesce(rr.score,0)) as low,max(coalesce(rr.score,0)) as high
            from public.observer_runs rr join public.scenarios sc on sc.id=rr.scenario_id
            where rr.batch_id=any(k.batch_ids) and rr.scenario_id in (select scenario_id from cards) group by sc.slug) c
        ) end as card_ranges,
        case when v_card is null then (
          select case when v_named then jsonb_object_agg(c.slug,c.score) end from (
            select sc.slug,avg(coalesce(rr.score,0)) as score
            from public.observer_runs rr join public.scenarios sc on sc.id=rr.scenario_id
            where rr.batch_id=any(k.batch_ids) and rr.scenario_id in (select scenario_id from cards) group by sc.slug) c
        ) end as card_scores,
        case when v_card is null then (
          select case when v_named then coalesce(jsonb_agg(c.slug order by c.slug),'[]'::jsonb) end from (
            select distinct sc.slug from public.observer_runs rr join public.scenarios sc on sc.id=rr.scenario_id
            where rr.batch_id=any(k.batch_ids) and rr.scenario_id in (select scenario_id from cards) and rr.status<>'scored') c
        ) end as unfinished_cards,
        case when v_card is null then null else r.unfinished end as unfinished,
        case when v_final then (
          select jsonb_build_object('chosen',f.chosen is not null,'score',(
            select case when v_card is null then e.overall else (select coalesce(rr.score,0) from public.observer_runs rr
                where rr.batch_id=e.id and rr.scenario_id=v_card) end
              from eligible e where e.team_id=k.team_id and e.revision_id=f.chosen
              order by e.overall desc,e.created_at limit 1))
          from (select (private.observer_final_version_state(k.team_id,p_phase)->>'chosen_revision_id')::uuid as chosen) f
        ) end as final_version,
        agg.calibrated,agg.raw_total_score,agg.base_science,agg.program_bonus,agg.request_reward,agg.report_reward,
        agg.coverage_bonus,agg.coverage_evenness,agg.penalty_total,agg.completed_tiles,agg.required_missing,
        agg.targets_observed,agg.components,
        case when v_card is null then null else r.termination_reason end as termination_reason
      from ranked k
      left join lateral (
        select avg(coalesce(x.score,0)) as score,bool_or(x.status<>'scored') as unfinished,
          min(coalesce(x.score,0)) as low,max(coalesce(x.score,0)) as high,
          case when count(*)=1 then max(case when x.status='scored' and x.score_summary->>'termination_reason' ~ '^[a-z_]{1,64}$'
            then x.score_summary->>'termination_reason' end) end as termination_reason
        from public.observer_runs x where x.batch_id=any(k.batch_ids) and x.scenario_id=v_card
      ) r on v_card is not null
      cross join lateral (
        select bool_and(x.score_summary ? 'calibration') filter (where x.status='scored') as calibrated,
          avg(case when x.status='scored' then coalesce((parts.raw->>'total')::double precision,x.score) else 0 end) as raw_total_score,
          avg(coalesce((parts.raw->>'base_science')::double precision,0)) as base_science,
          avg(coalesce((parts.raw->>'program_bonus')::double precision,0)) as program_bonus,
          avg(coalesce((parts.raw->>'request_reward')::double precision,0)) as request_reward,
          avg(coalesce((parts.raw->>'report_reward')::double precision,0)) as report_reward,
          avg(coalesce((parts.raw->>'coverage_bonus')::double precision,0)) as coverage_bonus,
          avg(coalesce((parts.raw->>'coverage_evenness')::double precision,0)) as coverage_evenness,
          avg(coalesce(case when jsonb_typeof(parts.raw->'penalties')='object' then
            (select sum(value::double precision) from jsonb_each_text(parts.raw->'penalties')) end,0)) as penalty_total,
          avg((x.score_summary->>'completed_tiles')::double precision) filter (where x.status='scored') as completed_tiles,
          avg(private.observer_summary_count(x.score_summary,'required_missing')) filter (where x.status='scored') as required_missing,
          avg(private.observer_summary_count(x.score_summary,'targets_observed')) filter (where x.status='scored') as targets_observed,
          (select coalesce(jsonb_object_agg(c.key,c.value),'{}'::jsonb) from (
            select e.key,case when v_zero then sum(e.value::text::double precision)/greatest(1,(select count(*) from public.observer_runs z where z.batch_id=any(k.batch_ids)
                  and (case when v_card is null then z.scenario_id in (select scenario_id from cards) else z.scenario_id=v_card end)))
                else avg(e.value::text::double precision) end as value
              from public.observer_runs y cross join lateral jsonb_each(private.observer_score_components(y.score_summary)) e
              where y.batch_id=any(k.batch_ids) and y.status='scored'
                and (case when v_card is null then y.scenario_id in (select scenario_id from cards) else y.scenario_id=v_card end)
              group by e.key) c) as components
        from public.observer_runs x
        -- A voided (rejected) or failed card contributes 0, never its old summary.
        cross join lateral (select case when x.status='scored' then coalesce(x.score_summary->'raw_score',x.score_summary->'score') end as raw) parts
        where x.batch_id=any(k.batch_ids) and (case when v_card is null then x.scenario_id in (select scenario_id from cards) else x.scenario_id=v_card end)
      ) agg
    ) all_rows order by rank,scored_at,team_name limit v_limit
  ) rows;

  return jsonb_build_object('layout',v_layout,'cards',v_cards,'extra_cards',v_extra,
    'scenario',case when v_card is null then null else p_scenario_slug end,'rows',v_rows);
end $function$;

CREATE OR REPLACE FUNCTION public.observer_super_board(p_phase uuid, p_scenario_slug text DEFAULT NULL::text, p_limit integer DEFAULT 100)
 RETURNS jsonb
 LANGUAGE plpgsql
 STABLE SECURITY DEFINER
 SET search_path TO 'public', 'pg_temp'
AS $function$
declare
  v_layout text; v_mode text; v_named boolean; v_cards jsonb:='[]'; v_card uuid; v_rows jsonb;
  v_limit integer:=greatest(1,least(coalesce(p_limit,100),1000));
  v_zero boolean:=private.observer_failed_cards_score_zero(p_phase);
  v_none jsonb:=jsonb_build_object('layout','super','cards','[]'::jsonb,'scenario',null,'rows','[]'::jsonb);
begin
  select coalesce(s.board_layout,'overall'),p.leaderboard_mode into v_layout,v_mode
    from public.phases p left join public.observer_phase_settings s on s.phase_id=p.id where p.id=p_phase;
  if v_layout is null or v_layout='overall' or not public.observer_phase_visible(p_phase)
     or not (public.is_admin() or v_mode in ('live','published'))
     or not exists(select 1 from public.phase_scenarios ps join public.scenarios sc on sc.id=ps.scenario_id
                   where ps.phase_id=p_phase and private.observer_extra_card(sc.slug)) then
    return v_none;
  end if;

  select coalesce(bool_and(public.is_admin() or v_mode='published' or public.observer_scenario_listed(sc.id)),false),
         coalesce(jsonb_agg(jsonb_build_object('slug',sc.slug,'name',sc.name) order by sc.slug),'[]'::jsonb)
    into v_named,v_cards
    from public.phase_scenarios ps join public.scenarios sc on sc.id=ps.scenario_id where ps.phase_id=p_phase;
  if not v_named then v_cards:='[]'; end if;

  if p_scenario_slug is not null then
    select sc.id into v_card from public.phase_scenarios ps join public.scenarios sc on sc.id=ps.scenario_id
      where ps.phase_id=p_phase and sc.slug=p_scenario_slug;
    if v_card is null or not v_named then
      return jsonb_build_object('layout','super','cards',v_cards,'scenario',null,'rows','[]'::jsonb);
    end if;
  end if;

  with cards as (select scenario_id from public.phase_scenarios where phase_id=p_phase),
  eligible as (
    select b.id,b.team_id,t.name as team_name,b.created_at,b.finished_at,sum(coalesce(r.score,0)*(select case when private.observer_extra_card(sc.slug) then 0.8 else 0.2 end from public.scenarios sc where sc.id=r.scenario_id)) as total
    from public.observer_batches b join public.teams t on t.id=b.team_id join public.phases p on p.id=b.phase_id
    join public.observer_runs r on r.batch_id=b.id and r.scenario_id in (select scenario_id from cards)
      and (r.status='scored' or (v_zero and private.observer_participant_failure(r.id)))
    where b.phase_id=p_phase and b.purpose='formal' and b.status='scored' and b.superseded_at is null
      and (p.is_active or public.is_admin())
      and (public.is_admin() or (not t.is_hidden and p.leaderboard_mode in ('live','published')))
    group by b.id,t.name
    having count(*)=(select count(*) from cards) and count(*)>0
  ),
  best as (select distinct on(e.team_id) e.* from eligible e order by e.team_id,e.created_at desc),
  ranked as (
    select b.*,rank() over(order by b.total desc) as overall_rank,
      (select count(*) from public.observer_batches x where x.phase_id=p_phase and x.team_id=b.team_id
        and x.purpose='formal' and x.status='scored') as submission_count
    from best b
  )
  select coalesce(jsonb_agg(to_jsonb(rows) order by rows.rank,rows.scored_at,rows.team_name),'[]'::jsonb) into v_rows from (
    select * from (
      select
        case when v_card is null then k.overall_rank else rank() over(order by coalesce(r.score,0) desc) end as rank,
        k.team_id,k.team_name,
        (select p.github from public.teams tt join public.profiles p on p.id=tt.leader_id where tt.id=k.team_id) as leader_github,
        (select p.avatar_url from public.teams tt join public.profiles p on p.id=tt.leader_id where tt.id=k.team_id) as leader_avatar_url,
        case when v_card is null then k.total else coalesce(r.score,0) end as total_score,
        k.total as overall_score,k.overall_rank,k.finished_at as scored_at,
        k.id as observer_batch_id,'observer'::text as kind,
        case when v_card is null then null else p_scenario_slug end as scenario_slug,
        k.submission_count,
        case when v_card is null then (
          select case when v_named then jsonb_object_agg(sc.slug,coalesce(rr.score,0)) end
            from public.observer_runs rr join public.scenarios sc on sc.id=rr.scenario_id
            where rr.batch_id=k.id and rr.scenario_id in (select scenario_id from cards)
        ) end as card_scores,
        case when v_card is null then (
          select case when v_named then coalesce(jsonb_agg(sc.slug order by sc.slug),'[]'::jsonb) end
            from public.observer_runs rr join public.scenarios sc on sc.id=rr.scenario_id
            where rr.batch_id=k.id and rr.scenario_id in (select scenario_id from cards) and rr.status<>'scored'
        ) end as unfinished_cards,
        case when v_card is null then null else r.status<>'scored' end as unfinished,
        -- Card tab: that run's own numbers (a card that did not score shows none).
        case when v_card is not null and r.status='scored' then private.observer_score_components(r.score_summary) end as components,
        case when v_card is not null and r.status='scored' then (r.score_summary->>'completed_tiles')::double precision end as completed_tiles,
        case when v_card is not null and r.status='scored' then private.observer_summary_count(r.score_summary,'required_missing') end as required_missing,
        case when v_card is not null and r.status='scored' then private.observer_summary_count(r.score_summary,'targets_observed') end as targets_observed,
        case when v_card is not null and r.status='scored' and r.score_summary->>'termination_reason' ~ '^[a-z_]{1,64}$'
          then r.score_summary->>'termination_reason' end as termination_reason
      from ranked k
      left join public.observer_runs r on v_card is not null and r.batch_id=k.id and r.scenario_id=v_card
    ) all_rows order by rank,scored_at,team_name limit v_limit
  ) rows;

  return jsonb_build_object('layout','super','cards',v_cards,
    'scenario',case when v_card is null then null else p_scenario_slug end,'rows',v_rows);
end $function$
;
revoke all on function public.observer_super_board(uuid,text,integer) from public;
grant execute on function public.observer_super_board(uuid,text,integer) to anon,authenticated,service_role;

notify pgrst,'reload schema';

-- A team cancels its own queued evaluation (2026-10-05). Based on the live definitions; adds one
-- function and changes nothing that runs.
--
-- public.observer_cancel_batch(p_batch): a member of the evaluation's team (or an organizer) cancels an
-- evaluation (formal purpose: practice, online and every other phase) while none of its cards has
-- started: every run is still 'queued' and none has a job that is queued, dispatched to or claimed by a
-- runner. A run the dispatcher has leased but not yet scheduled counts as not started. The runs and the
-- evaluation become 'cancelled', the evaluation is refunded (quota_refunded: it does not count toward
-- the day's evaluations) and, never scored, it appears on no board.
--
-- Self-check sets ("evaluate 3 times"): cancelling one member cancels every member of its set that has
-- not started (members already running or finished are kept and count as usual). The set's members run
-- one after another, so this cancels the rest of the set.
--
-- Race safety: the run rows are locked FOR UPDATE, the same lock the dispatcher takes
-- (observer_pending_runs: FOR UPDATE SKIP LOCKED; observer_schedule_run / observer_open_session:
-- FOR UPDATE), before the evaluation row (the order observer_open_session and finalize use), and every
-- check is repeated under the locks. A cancelled run is never leased again (pending runs are 'queued'
-- only), and a lease taken just before is refused by observer_schedule_run (run_lease_invalid) before a
-- session or job exists. Once observer_schedule_run has committed, the run is 'starting' and cancelling
-- is refused. No job is ever terminated: an evaluation with a live job is not cancellable.
--
-- Errors: evaluation_not_found (unknown, another team's, or not a formal evaluation),
-- evaluation_not_cancellable (an organizers' sealed evaluation), evaluation_finished (scored or failed),
-- evaluation_started (a card has started). Cancelling an already cancelled evaluation returns it again.
-- Result: {batch_id, status:'cancelled', cancelled:[ids cancelled by this call]}.

create or replace function public.observer_cancel_batch(p_batch uuid)
returns jsonb language plpgsql security definer set search_path=public,pg_temp as $$
declare v public.observer_batches; v_admin boolean; v_ids uuid[]; v_done uuid[]:='{}'; v_id uuid;
begin
  perform private.assert_not_banned();
  v_admin:=public.is_admin();
  select * into v from public.observer_batches where id=p_batch;
  if not found or v.purpose<>'formal' or not (v_admin or public.observer_team(v.team_id)) then
    raise exception 'evaluation_not_found'; end if;
  if not v_admin and exists(select 1 from public.observer_phase_settings s where s.phase_id=v.phase_id and s.sealed) then
    raise exception 'evaluation_not_cancellable'; end if;
  -- This evaluation and, in a self-check set, the set's other unfinished members.
  select array_agg(b.id order by b.id) into v_ids from public.observer_batches b
    where b.id=p_batch or (v.repeat_group is not null and b.repeat_group=v.repeat_group
      and b.purpose='formal' and b.status in ('queued','running'));
  -- Runs first (the dispatcher's lock), then the evaluations, each in id order.
  perform 1 from public.observer_runs where batch_id=any(v_ids) order by id for update;
  perform 1 from public.observer_batches where id=any(v_ids) order by id for update;
  select * into v from public.observer_batches where id=p_batch;
  if v.status='cancelled' then
    return jsonb_build_object('batch_id',p_batch,'status','cancelled','cancelled','[]'::jsonb); end if;
  if v.status not in ('queued','running') then raise exception 'evaluation_finished'; end if;
  foreach v_id in array v_ids loop
    -- Not started: unfinished, every card still queued and no card with a live job.
    if exists(select 1 from public.observer_batches b where b.id=v_id and b.status in ('queued','running'))
      and not exists(select 1 from public.observer_runs r where r.batch_id=v_id and r.status<>'queued')
      and not exists(select 1 from private.observer_jobs j join public.observer_runs r on r.id=j.run_id
        where r.batch_id=v_id and j.status in ('queued','dispatched','claimed'))
    then
      update public.observer_runs set status='cancelled',finished_at=now() where batch_id=v_id and status='queued';
      delete from private.observer_run_leases l using public.observer_runs r where r.id=l.run_id and r.batch_id=v_id;
      update public.observer_batches set status='cancelled',finished_at=now(),quota_refunded=true where id=v_id;
      v_done:=v_done||v_id;
    elsif v_id=p_batch then
      raise exception 'evaluation_started';
    end if;
  end loop;
  perform private.audit('observer.cancel_batch',jsonb_build_object('batch_id',p_batch,'team_id',v.team_id,
    'repeat_group',v.repeat_group,'cancelled',to_jsonb(v_done),'by_admin',v_admin and not public.observer_team(v.team_id)));
  return jsonb_build_object('batch_id',p_batch,'status','cancelled','cancelled',to_jsonb(v_done));
end $$;
revoke all on function public.observer_cancel_batch(uuid) from public,anon;
grant execute on function public.observer_cancel_batch(uuid) to authenticated,service_role;

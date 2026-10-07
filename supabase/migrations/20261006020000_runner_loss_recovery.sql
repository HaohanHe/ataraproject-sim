-- Recovery from runners that never pick a job up or go silent mid-run (GitHub outage, 2026-10-05). Applied in production.
-- 1. A job no runner has claimed within an hour expires; its run goes back to the queue (observer_reconcile_jobs).
alter table private.observer_jobs alter column expires_at set default (now() + interval '60 minutes');
-- 2. A started run whose session passed its deadline runs again automatically, up to twice, before it expires (refunded).
CREATE OR REPLACE FUNCTION public.observer_reconcile_sessions()
 RETURNS integer
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public', 'pg_temp'
AS $function$
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
  for v in select r.id,r.batch_id,r.status from public.observer_runs r
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
    -- A run that had started but whose runner went silent (session past its deadline) runs again
    -- automatically, up to twice; only then does it expire (refunded, as before).
    if v.status<>'queued' and (select count(*) from private.observer_jobs j where j.run_id=v.id
        and j.error='session_expired_rerun')<2 then
      update private.observer_jobs set status='failed',error='session_expired_rerun',finished_at=now()
        where run_id=v.id and status in ('queued','dispatched','claimed');
      update public.observer_runs set status='queued',error='',started_at=null
        where id=v.id and status in ('starting','ready','running');
      n:=n+1;
      continue;
    end if;
    update public.observer_runs set status='failed',error='evaluation_expired',finished_at=now()
      where id=v.id and status in ('queued','starting','ready','running');
    perform private.observer_finalize_batch(v.batch_id);
    n:=n+1;
  end loop;
  return n;
end $function$
;

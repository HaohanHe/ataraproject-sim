-- A platform-side failure (failed engine job, expired job lease, admin requeue)
-- puts a run back to 'queued' but leaves the session row of the failed attempt.
-- The next scheduling pass then hit observer_sessions_pkey in
-- observer_open_session (and observer_local_credentials_pkey for local runs),
-- so the run stayed 'queued' forever with lease error 'schedule_unavailable'.
--
-- Reopening a queued run's session now replaces the stale attempt: its
-- protocol messages are dropped (the engine restarts at sequence 1), the
-- capabilities rotate (the old job's tokens stop working) and the counters
-- reset. Model-call rows of the old attempt are kept for accounting.

CREATE OR REPLACE FUNCTION public.observer_open_session(p_run uuid, p_participant_token text, p_engine_token text)
 RETURNS void
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public', 'pg_temp'
AS $function$
declare v_run public.observer_runs; v_config public.observer_phase_settings; v_purpose text;
begin
  if p_participant_token is null or length(p_participant_token) < 40 or length(p_participant_token)>200
     or p_engine_token is null or length(p_engine_token)<40 or length(p_engine_token)>200
     or p_participant_token = p_engine_token then raise exception 'invalid_capability'; end if;
  select * into v_run from public.observer_runs where id=p_run for update;
  if not found or v_run.status <> 'queued' then raise exception 'run_not_queued'; end if;
  select c.* into v_config from public.observer_phase_settings c join public.observer_batches b on b.phase_id=c.phase_id
    where b.id=v_run.batch_id;
  select purpose into v_purpose from public.observer_batches where id=v_run.batch_id;
  if v_purpose='adaptation' then
    v_config.runtime_seconds:=1800;v_config.model_token_limit:=65536;v_config.model_call_limit:=1;v_config.model_concurrency:=1;
  elsif v_purpose='preview' then
    v_config.runtime_seconds:=least(v_config.runtime_seconds,300);
  end if;
  -- A requeued run: the previous attempt's protocol is discarded.
  delete from private.observer_messages where run_id=p_run;
  insert into private.observer_sessions(run_id,participant_hash,engine_hash,expires_at,token_limit,call_limit,concurrency_limit)
    values(p_run,sha256(convert_to(p_participant_token,'UTF8')),sha256(convert_to(p_engine_token,'UTF8')),
      now()+make_interval(secs=>v_config.runtime_seconds+1800),v_config.model_token_limit,
      v_config.model_call_limit,v_config.model_concurrency)
    on conflict(run_id) do update set participant_hash=excluded.participant_hash,engine_hash=excluded.engine_hash,
      expires_at=excluded.expires_at,token_limit=excluded.token_limit,call_limit=excluded.call_limit,
      concurrency_limit=excluded.concurrency_limit,deadline_at=null,ready_at=null,publication=null,
      next_sequence=1,tokens_used=0,tokens_reserved=0,calls_used=0,calls_active=0;
  update public.observer_runs set status='starting' where id=p_run;
  update public.observer_batches set status='running' where id=v_run.batch_id;
end $function$;

CREATE OR REPLACE FUNCTION public.observer_open_local_session(p_run uuid, p_participant_token text, p_engine_token text, p_encrypted text)
 RETURNS void
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public', 'pg_temp'
AS $function$
begin
  if not exists(select 1 from public.observer_runs r join public.observer_batches b on b.id=r.batch_id
    where r.id=p_run and b.mode='local') or p_encrypted is null or length(p_encrypted)<20 then
    raise exception 'invalid_local_session'; end if;
  perform public.observer_open_session(p_run,p_participant_token,p_engine_token);
  insert into private.observer_local_credentials(run_id,encrypted_credential) values(p_run,p_encrypted)
    on conflict(run_id) do update set encrypted_credential=excluded.encrypted_credential;
end $function$;

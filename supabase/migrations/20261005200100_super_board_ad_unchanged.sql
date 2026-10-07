-- Cards A1-D1 alongside A-D (owner-approved plan 2026-10-05): the existing online board, its
-- score and its rules stay exactly as they are when an evaluation runs 8 cards.
-- An added card is one whose slug matches private.observer_extra_card (v4-a1 .. v4-d1, any -vN
-- version; migration 20261005200000). In a phase without such a card nothing below changes
-- behaviour: every rule reduces to the previous one.
--
-- 1. private.observer_batch_score: an evaluation's score (observer_batches.score, the headline
--    score everywhere) is the mean over its cards A-D only; a batch without any A-D card keeps
--    the mean over all its cards as before.
-- 2. private.observer_run_fails_batch / private.observer_finalize_batch: a failed or cancelled
--    added card no longer fails the evaluation, so an evaluation is scored once A-D are scored
--    and every card has finished (the "A-D complete" rule is unchanged). Such an evaluation is
--    on the A-D board but not on the super board (which needs all 8 cards). A failure of any A-D
--    card fails the evaluation exactly as before (refund rules unchanged).
-- 3. private.observer_settle_score_check: the score check recomputes the A-D mean; a voided
--    trace of an added card leaves the evaluation scored (A-D only), off the super board.
-- 4. public.observer_card_board: cards, completeness, mean, columns and card tabs use A-D only,
--    so 4-card evaluations made before the switch keep counting and ranks do not move. It also
--    returns extra_cards (A1-D1 where named); a card tab of an added card returns that card's
--    super-board rows (public.observer_super_board), which also serves the CLI's --card.
-- 5. public.observer_baseline_rows: the official examples' averages also use A-D only.

create or replace function private.observer_batch_score(p_batch uuid)
returns double precision language sql stable security definer set search_path=public,pg_temp as $$
  select coalesce(
    (select avg(coalesce(r.score,0)) from public.observer_runs r join public.scenarios s on s.id=r.scenario_id
      where r.batch_id=p_batch and not private.observer_extra_card(s.slug)),
    (select avg(coalesce(r.score,0)) from public.observer_runs r where r.batch_id=p_batch))
$$;
revoke all on function private.observer_batch_score(uuid) from public,anon,authenticated;

-- True when the run failed and, in the hidden final, does not count as 0; never for an added card.
create or replace function private.observer_run_fails_batch(p_run uuid)
returns boolean language sql stable security definer set search_path=public,pg_temp as $$
  select exists(select 1 from public.observer_runs r join public.observer_batches b on b.id=r.batch_id
      join public.scenarios s on s.id=r.scenario_id
    where r.id=p_run and r.status in ('failed','cancelled')
      and not (private.observer_failed_cards_score_zero(b.phase_id) and private.observer_participant_failure(r.id))
      and not private.observer_extra_card(s.slug))
$$;
revoke all on function private.observer_run_fails_batch(uuid) from public,anon,authenticated;

create or replace function private.observer_finalize_batch(p_batch uuid)
returns void language plpgsql security definer set search_path=public,pg_temp as $$
begin
  perform 1 from public.observer_batches where id=p_batch for update;
  if exists(select 1 from public.observer_runs r where r.batch_id=p_batch and private.observer_run_fails_batch(r.id)) then
    -- Decided once, when the batch fails: later runs of a failed batch no longer count.
    update public.observer_batches set status='failed',finished_at=now(),
      quota_refunded=not exists(select 1 from public.observer_runs r where r.batch_id=p_batch
        and private.observer_participant_failure(r.id))
      where id=p_batch and status in ('queued','running');
  elsif exists(select 1 from public.observer_runs where batch_id=p_batch)
    and not exists(select 1 from public.observer_runs where batch_id=p_batch and status not in ('scored','failed','cancelled')) then
    -- Only scored cards, (hidden final) the team's own failed cards and failed added cards are
    -- left (a failed or cancelled A-D card failed the batch above); those count as 0.
    update public.observer_batches set status='scored',score=private.observer_batch_score(p_batch),finished_at=now()
      where id=p_batch and status in ('queued','running');
  end if;
end $$;
revoke all on function private.observer_finalize_batch(uuid) from public,anon,authenticated;

create or replace function private.observer_settle_score_check(p_job uuid,p_outcome text,p_recomputed jsonb)
returns void language plpgsql security definer set search_path=public,pg_temp as $$
declare j private.observer_jobs; r public.observer_runs; b public.observer_batches; v_total double precision;
  v_outcome text:=p_outcome;
begin
  select * into j from private.observer_jobs where id=p_job and kind='score';
  if not found then raise exception 'job_unavailable'; end if;
  select * into r from public.observer_runs where id=j.run_id for update;
  if r.status<>'scored' or r.score_check is distinct from 'pending' then return; end if;
  if v_outcome='scored' then
    if jsonb_typeof(p_recomputed) is distinct from 'object' or p_recomputed->>'run_id' is distinct from r.id::text
      or jsonb_typeof(p_recomputed->'score') is distinct from 'object' or octet_length(p_recomputed::text)>65536 then
      raise exception 'invalid_job_result'; end if;
    -- The job proved its trace against this digest; re-check the binding so a
    -- receipt can never pair a score with a different trace.
    if p_recomputed->>'decisions_digest' is distinct from r.decisions_digest then
      raise exception 'invalid_job_result'; end if;
    v_total:=(p_recomputed->'score'->>'total')::double precision;
    if v_total is null or v_total in ('NaN'::double precision,'Infinity'::double precision,'-Infinity'::double precision) then
      raise exception 'invalid_job_result'; end if;
    v_outcome:=case when abs(v_total-r.score)<=1e-6*greatest(1,abs(v_total)) then 'verified' else 'corrected' end;
  end if;
  if v_outcome not in ('verified','corrected','rejected','unverified') then raise exception 'invalid_job_result'; end if;
  -- Switched off while the job ran: record the evidence, change nothing.
  if v_outcome in ('corrected','rejected') and not coalesce((select h.rescore from private.observer_hardening h where h.id),false) then
    insert into private.observer_score_checks(run_id,job_id,outcome,reported_score,recomputed)
      values(r.id,j.id,'unverified',r.score,p_recomputed) on conflict(run_id) do nothing;
    update public.observer_runs set score_check='unverified' where id=r.id;
    return;
  end if;
  insert into private.observer_score_checks(run_id,job_id,outcome,reported_score,reported_summary,recomputed)
    values(r.id,j.id,v_outcome,r.score,case when v_outcome in ('corrected','rejected') then r.score_summary end,p_recomputed)
    on conflict(run_id) do nothing;
  if v_outcome in ('verified','unverified') then
    update public.observer_runs set score_check=v_outcome where id=r.id;
    return;
  end if;
  select * into b from public.observer_batches where id=r.batch_id for update;
  if v_outcome='corrected' then
    update public.observer_runs set score=v_total,score_check='corrected',
      score_summary=jsonb_set(r.score_summary,'{score}',p_recomputed->'score',true)
        ||jsonb_build_object('rescore',jsonb_build_object('reported_total',r.score,'recomputed_total',v_total))
      where id=r.id;
    if b.status='scored' then
      update public.observer_batches set score=private.observer_batch_score(b.id) where id=b.id;
    end if;
    perform private.audit('observer.score_corrected',jsonb_build_object('run_id',r.id,'job_id',j.id,
      'reported',r.score,'recomputed',v_total));
  else
    update public.observer_runs set status='failed',error='score_verification_failed',score=null,
      score_check='rejected' where id=r.id;
    -- A voided trace is the team's run, never a platform failure: no refund and
    -- no automatic retry. In the hidden final the card counts as 0.
    if b.status='scored' and private.observer_failed_cards_score_zero(b.phase_id) then
      update public.observer_batches set score=private.observer_batch_score(b.id) where id=b.id;
    elsif b.status='scored' and private.observer_extra_card((select s.slug from public.scenarios s where s.id=r.scenario_id)) then
      -- An added card (A1-D1) voided: the evaluation keeps its A-D score; it leaves the super board.
      null;
    elsif b.status='scored' then
      update public.observer_batches set status='failed',score=null,quota_refunded=false where id=b.id;
    else
      perform private.observer_finalize_batch(b.id);
    end if;
    perform private.audit('observer.score_rejected',jsonb_build_object('run_id',r.id,'job_id',j.id,
      'reported',r.score));
  end if;
end $$;
revoke all on function private.observer_settle_score_check(uuid,text,jsonb) from public,anon,authenticated;

create or replace function private.observer_card_board_live(p_phase uuid, p_scenario_slug text DEFAULT NULL::text, p_limit integer DEFAULT 100)
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
    where b.phase_id=p_phase and b.purpose='formal' and b.status='scored' and b.superseded_at is null and (p.is_active or public.is_admin())
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

create or replace function public.observer_baseline_rows(p_phase uuid)
returns jsonb language plpgsql stable security definer set search_path=public,pg_temp as $$
declare v_mode text; v_named boolean; v_zero boolean:=private.observer_failed_cards_score_zero(p_phase); v_rows jsonb;
begin
  select p.leaderboard_mode into v_mode from public.phases p where p.id=p_phase and (p.is_active or public.is_admin());
  if v_mode is null or not public.observer_phase_visible(p_phase)
     or not (public.is_admin() or v_mode in ('live','published')) then return '[]'::jsonb; end if;
  -- Card names (and so per-card numbers) only where observer_card_board names the cards.
  select coalesce(bool_and(public.is_admin() or v_mode='published' or public.observer_scenario_listed(ps.scenario_id)),false)
    into v_named from public.phase_scenarios ps join public.scenarios sc on sc.id=ps.scenario_id
    where ps.phase_id=p_phase and not private.observer_extra_card(sc.slug);

  with cards as (select ps.scenario_id,sc.slug from public.phase_scenarios ps join public.scenarios sc on sc.id=ps.scenario_id
      where ps.phase_id=p_phase and not private.observer_extra_card(sc.slug)),
  examples as (select t.id as team_id,e.name,e.grp from public.teams t
      join (values ('python','basic'),('typescript','basic'),('rust','basic'),
                   ('python-pro','pro'),('typescript-pro','pro'),('rust-pro','pro')) e(name,grp)
        on t.slug='official-example-'||e.name
      where t.is_hidden),
  batches as (
    select b.id,x.name,x.grp,b.finished_at from public.observer_batches b join examples x on x.team_id=b.team_id
      join public.observer_runs r on r.batch_id=b.id and r.scenario_id in (select scenario_id from cards)
        and (r.status='scored' or (v_zero and private.observer_participant_failure(r.id)))
    where b.phase_id=p_phase and b.purpose='formal' and b.status='scored' and b.superseded_at is null
    group by b.id,x.name,x.grp
    having count(*)=(select count(*) from cards) and count(*)>0),
  -- Per evaluation and card (a failed card counts 0, as on the board).
  card_runs as (select b.id,b.name,b.grp,c.slug,coalesce(r.score,0) as score from batches b
      join public.observer_runs r on r.batch_id=b.id join cards c on c.scenario_id=r.scenario_id),
  per_example as (
    select o.name,o.grp,avg(o.overall) as overall,count(*) as runs,max(o.finished_at) as updated_at,
      (select jsonb_object_agg(c.slug,c.score) from (select cr.slug,avg(cr.score) as score from card_runs cr
         where cr.name=o.name group by cr.slug) c) as card_scores
    from (select b.name,b.grp,b.finished_at,(select avg(cr.score) from card_runs cr where cr.id=b.id) as overall
          from batches b) o
    group by o.name,o.grp),
  per_group as (
    select e.grp,avg(e.overall) as overall,sum(e.runs)::int as runs,max(e.updated_at) as updated_at,
      (select jsonb_object_agg(k.key,k.score) from (select c.key,avg(c.value::text::double precision) as score
         from per_example x cross join lateral jsonb_each(x.card_scores) c where x.grp=e.grp group by c.key) k) as card_scores
    from per_example e group by e.grp having count(*)=3)
  select coalesce(jsonb_agg(jsonb_build_object('group',g.grp,'overall_score',g.overall,
      'card_scores',case when v_named then g.card_scores end,'runs',g.runs,'examples',3,'updated_at',g.updated_at)
      order by g.grp),'[]'::jsonb)
    into v_rows from per_group g;
  return v_rows;
end $$;
revoke all on function public.observer_baseline_rows(uuid) from public;
grant execute on function public.observer_baseline_rows(uuid) to anon,authenticated,service_role;

notify pgrst,'reload schema';

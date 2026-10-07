-- Super board (超级总榜) for a complete-project phase that also runs the added cards A1-D1.
-- Owner-approved plan 2026-10-05: an online evaluation may run 8 cards, A-D plus A1-D1.
-- The existing board (public.observer_card_board) keeps ranking the mean of A-D only
-- (migration 20261005200100); this board ranks the SUM of all the phase's cards of one
-- evaluation, counting only evaluations in which every card of the phase completed.
-- Each team appears once, with its highest total; teams without such an evaluation are not listed.
-- p_scenario_slug: one card of the phase, ranked by that card's score in the team's super-board
-- evaluation (the per-card boards A1-D1).
--
-- An added card is recognized by its slug only: v4-a1, v4-b1-v2, ... (private.observer_extra_card),
-- so card versions can be swapped without code changes. A phase without such a card has no super
-- board: every call returns no cards and no rows (the feature stays off until the cards are added).
-- Same visibility as observer_card_board (phase visible, live/published or admin, hidden teams only
-- for admins; card names only where they are listed). One evaluation per team: a phase with
-- repeat_runs > 1 is not averaged here.
-- Additive: two new functions; nothing existing changes.

create or replace function private.observer_extra_card(p_slug text)
returns boolean language sql immutable as $$
  select coalesce(p_slug ~ '^v4-[a-d]1(-v[0-9]+)?$', false)
$$;
revoke all on function private.observer_extra_card(text) from public,anon,authenticated;

create or replace function public.observer_super_board(p_phase uuid, p_scenario_slug text default null, p_limit integer default 100)
returns jsonb language plpgsql stable security definer set search_path=public,pg_temp as $$
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
    select b.id,b.team_id,t.name as team_name,b.created_at,b.finished_at,sum(coalesce(r.score,0)) as total
    from public.observer_batches b join public.teams t on t.id=b.team_id join public.phases p on p.id=b.phase_id
    join public.observer_runs r on r.batch_id=b.id and r.scenario_id in (select scenario_id from cards)
      and (r.status='scored' or (v_zero and private.observer_participant_failure(r.id)))
    where b.phase_id=p_phase and b.purpose='formal' and b.status='scored' and b.superseded_at is null
      and (p.is_active or public.is_admin())
      and (public.is_admin() or (not t.is_hidden and p.leaderboard_mode in ('live','published')))
    group by b.id,t.name
    having count(*)=(select count(*) from cards) and count(*)>0
  ),
  best as (select distinct on(e.team_id) e.* from eligible e order by e.team_id,e.total desc,e.created_at),
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
end $$;
revoke all on function public.observer_super_board(uuid,text,integer) from public;
grant execute on function public.observer_super_board(uuid,text,integer) to anon,authenticated,service_role;

notify pgrst,'reload schema';

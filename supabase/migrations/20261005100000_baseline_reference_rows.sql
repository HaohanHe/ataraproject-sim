-- Baseline reference rows for a complete-project board: the average scores of the official examples run unmodified
-- by six hidden organizer teams (slug official-example-<name>). Basic = python, typescript, rust; pro = the -pro ones.
-- Each example: the mean of its scored formal evaluations on the phase (the same complete batches observer_card_board
-- counts), overall and per card; each group: the mean of its three examples (only once all three have one).
-- Returns only aggregated numbers, the evaluations used and the last update: no team ids or names.
-- Read-only and additive: no table changes, no existing function changes.
create or replace function public.observer_baseline_rows(p_phase uuid)
returns jsonb language plpgsql stable security definer set search_path=public,pg_temp as $$
declare v_mode text; v_named boolean; v_zero boolean:=private.observer_failed_cards_score_zero(p_phase); v_rows jsonb;
begin
  select p.leaderboard_mode into v_mode from public.phases p where p.id=p_phase and (p.is_active or public.is_admin());
  if v_mode is null or not public.observer_phase_visible(p_phase)
     or not (public.is_admin() or v_mode in ('live','published')) then return '[]'::jsonb; end if;
  -- Card names (and so per-card numbers) only where observer_card_board names the cards.
  select coalesce(bool_and(public.is_admin() or v_mode='published' or public.observer_scenario_listed(ps.scenario_id)),false)
    into v_named from public.phase_scenarios ps where ps.phase_id=p_phase;

  with cards as (select ps.scenario_id,sc.slug from public.phase_scenarios ps join public.scenarios sc on sc.id=ps.scenario_id
      where ps.phase_id=p_phase),
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

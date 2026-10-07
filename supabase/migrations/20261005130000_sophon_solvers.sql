-- 解谜名单 / Solvers next to the extra (Overlook) phase's board: team display name and time only, in solve order.
-- private.sophon_solves was created by hand on the live database; recorded here with the same shape.
-- Rows are answered only where the extra phase is offered to the caller (current_competition answers
-- extra_phase_id), or to organizers; hidden teams only to organizers, as on the boards. No keys, runs or users.
create table if not exists private.sophon_solves (
  team_id uuid primary key,
  run_id uuid not null,
  user_id uuid,
  solved_at timestamptz not null default now(),
  key text not null
);
revoke all on private.sophon_solves from public,anon,authenticated;

create or replace function public.sophon_solvers()
returns table(team_name text, solved_at timestamptz) language sql stable security definer set search_path=public,pg_temp as $$
  select t.name, s.solved_at from private.sophon_solves s join public.teams t on t.id=s.team_id
  where (public.is_admin() or (not t.is_hidden and public.current_competition() ? 'extra_phase_id'))
  order by s.solved_at, t.name
$$;
revoke all on function public.sophon_solvers() from public;
grant execute on function public.sophon_solvers() to anon,authenticated,service_role;

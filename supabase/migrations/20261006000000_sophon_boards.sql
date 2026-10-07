-- Sophon tab: boards and finishers. Rows live in private tables written by the organizer-run 'sophon' function;
-- these functions only read them, and only where the extra phase is offered (same rule as sophon_solvers).
-- Recorded here with the same shape as on the live database.
create table if not exists private.sophon_g_solves (
  board text not null check (board in ('debug', 'auto')),
  team_id uuid not null, no integer not null,
  at timestamptz not null, run_id uuid, user_id uuid,
  primary key (board, team_id, no)
);

create table if not exists private.sophon_g_finishers (
  board text not null, team_id uuid not null, at timestamptz not null, evals integer not null, k integer not null,
  is_first boolean not null, text_en text not null, text_zh text not null,
  primary key (board, team_id)
);

revoke all on private.sophon_g_solves, private.sophon_g_finishers from public, anon, authenticated;

create or replace function private.sophon_g_visible(p_team uuid) returns boolean
language sql stable security definer set search_path = public, pg_temp as $$
  select public.is_admin() or (not t.is_hidden and public.current_competition() ? 'extra_phase_id')
  from public.teams t where t.id = p_team
$$;

-- Dynamic value: max(100, 500 + (100 - 500) / 20^2 * (s - 1)^2), s = visible solvers of this flag on this board.
create or replace function private.sophon_g_value(p_board text, p_no integer) returns integer
language sql stable security definer set search_path = public, pg_temp as $$
  select greatest(100, round(500 + (100 - 500) / 400.0 * power(greatest(count(*), 1) - 1, 2)))::int
  from private.sophon_g_solves s join public.teams t on t.id = s.team_id
  where s.board = p_board and s.no = p_no and not t.is_hidden
$$;

drop function if exists public.sophon_board();
create or replace function public.sophon_board()
returns table(board text, team_name text, flag text, solved_at timestamptz, points integer, blood smallint)
language sql stable security definer set search_path = public, pg_temp as $$
  with vis as (
    select s.* from private.sophon_g_solves s join public.teams t on t.id = s.team_id
    where (not t.is_hidden or public.is_admin()) and private.sophon_g_visible(s.team_id)),
  cols as (
    select board, no, 'F' || dense_rank() over (partition by board order by min(at), no) as label
    from vis group by board, no)
  select v.board, t.name, c.label, v.at, private.sophon_g_value(v.board, v.no),
    case when rank() over (partition by v.board, v.no order by v.at) <= 3 then (rank() over (partition by v.board, v.no order by v.at))::smallint end
  from vis v join cols c on c.board = v.board and c.no = v.no join public.teams t on t.id = v.team_id
  order by v.board, t.name, c.label
$$;
revoke all on function public.sophon_board() from public;
grant execute on function public.sophon_board() to anon, authenticated, service_role;

drop function if exists public.sophon_totals();
create or replace function public.sophon_totals()
returns table(team_name text, debug integer, auto integer, total integer, last_at timestamptz)
language sql stable security definer set search_path = public, pg_temp as $$
  select t.name,
    coalesce(sum(private.sophon_g_value(s.board, s.no)) filter (where s.board = 'debug'), 0)::int,
    coalesce(sum(private.sophon_g_value(s.board, s.no)) filter (where s.board = 'auto'), 0)::int,
    (coalesce(sum(private.sophon_g_value(s.board, s.no)) filter (where s.board = 'debug'), 0)
      + 2 * coalesce(sum(private.sophon_g_value(s.board, s.no)) filter (where s.board = 'auto'), 0))::int,
    max(s.at)
  from private.sophon_g_solves s join public.teams t on t.id = s.team_id
  where private.sophon_g_visible(s.team_id)
  group by t.name order by 4 desc, 5
$$;

drop function if exists public.sophon_finishers();
create or replace function public.sophon_finishers()
returns table(board text, team_name text, finished_at timestamptz, text_en text, text_zh text)
language sql stable security definer set search_path = public, pg_temp as $$
  select f.board, t.name, f.at, f.text_en, f.text_zh
  from private.sophon_g_finishers f join public.teams t on t.id = f.team_id
  where private.sophon_g_visible(f.team_id)
  order by f.at
$$;

revoke all on function public.sophon_totals(), public.sophon_finishers() from public;
grant execute on function public.sophon_totals(), public.sophon_finishers() to anon, authenticated, service_role;

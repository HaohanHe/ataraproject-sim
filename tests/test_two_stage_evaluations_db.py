"""Two-stage evaluations (migration 20261006040000): in a phase with cards A-D and A1-D1, a project evaluation
runs A-D first and its A1-D1 only once all A-D finished; a failed stage 1 cancels stage 2; the A-D board counts
the evaluation once A-D are complete; evaluations created before (staged=false) run all cards together."""
import secrets

import pytest
from psycopg.types.json import Jsonb

from test_card_board import board, card, v4_summary
from test_hidden_final import materialize
from test_project_database import database, identity, query, rpc, setup  # noqa: F401
from test_project_eval_ux import revision, team  # noqa: F401
from test_super_board_db import fail_run, live, super_board


@pytest.fixture
def staged(team):
    s = team; uri = s['uri']
    query(uri, 'delete from public.phase_scenarios where phase_id=%s', (s['phase'],))
    v = secrets.randbelow(10**6) + 2
    s['ad'] = {c: card(uri, s['phase'], f'v4-card-{c}-{str(s["phase"])[:8]}', f'Card {c.upper()}') for c in 'abcd'}
    s['x1'] = {c: card(uri, s['phase'], f'v4-{c}1-v{v}', f'Card {c.upper()}1') for c in 'abcd'}
    for sid in list(s['ad'].values()) + list(s['x1'].values()):
        query(uri, 'insert into private.observer_scenario_bundles values(%s,%s,%s)', (sid, f'{sid}/b.zip', 'd' * 64))
    query(uri, "update public.observer_phase_settings set daily_batches=10,max_active_evaluations=10,board_layout='cards_overall'"
               " where phase_id=%s", (s['phase'],))
    live(s)
    s['rev'] = revision(s); materialize(s, s['rev'])
    return s


def evaluate(s):
    return rpc(s['uri'], 'observer_create_batch', s['phase'], s['rev'], True, role='authenticated', user=s['user'])


def runs(s, b, group):
    return {r[0]: r[1] for r in query(s['uri'], 'select id,scenario_id from public.observer_runs where batch_id=%s', (b,))
            if r[1] in s[group].values()}


def dispatchable(s):
    """Run ids the dispatcher hands out now; their leases are dropped again."""
    got = set()
    for _ in range(10):
        page = [p['id'] for p in rpc(s['uri'], 'observer_pending_runs', 10)]
        if not page: break
        got |= set(page)
    query(s['uri'], 'delete from private.observer_run_leases')
    mine = {str(r[0]) for r in query(s['uri'], 'select r.id from public.observer_runs r join public.observer_batches b on b.id=r.batch_id'
                                    ' where b.team_id=%s', (s['team'],))}
    return got & mine


def score(s, run_ids, value):
    for run in run_ids:
        query(s['uri'], "update public.observer_runs set status='scored',score=%s,score_summary=%s,finished_at=now() where id=%s",
              (value, Jsonb(v4_summary(value)), run))


def finalize(s, b):
    query(s['uri'], 'select private.observer_finalize_batch(%s)', (b,))
    return query(s['uri'], 'select status,score,quota_refunded from public.observer_batches where id=%s', (b,))[0]


def test_stage_two_starts_after_stage_one(staged):
    s = staged; uri = s['uri']
    b = evaluate(s)
    assert query(uri, 'select staged from public.observer_batches where id=%s', (b,)) == [(True,)]
    ad, x1 = runs(s, b, 'ad'), runs(s, b, 'x1')
    assert len(ad) == 4 and len(x1) == 4                                    # one evaluation, all 8 cards
    assert query(uri, 'select private.observer_batches_used(%s,%s)', (s['team'], s['phase']))[0][0] == 1
    got = {str(r) for r in dispatchable(s)}
    assert got == {str(r) for r in ad}                                      # stage 1 only
    assert all(query(uri, 'select private.observer_run_startable_since(%s)', (r,))[0][0] is None for r in x1)
    score(s, list(ad)[:3], 10)
    assert {str(r) for r in dispatchable(s)} & {str(r) for r in x1} == set()
    score(s, list(ad)[3:], 10)
    assert finalize(s, b)[0] == 'queued'                                    # A1-D1 still to run
    assert {str(r) for r in dispatchable(s)} == {str(r) for r in x1}
    # The A-D board shows it as soon as A-D are complete; the super board waits for all 8.
    rows = board(uri, s['phase'])['rows']
    assert [(r['observer_batch_id'], r['total_score']) for r in rows] == [(str(b), 10)]
    assert super_board(uri, s['phase'])['rows'] == []
    score(s, x1, 50)
    assert finalize(s, b) == ('scored', 10.0, False)
    row, = super_board(uri, s['phase'])['rows']
    assert (row['observer_batch_id'], row['total_score']) == (str(b), 168)   # 0.2*40 + 0.8*200
    assert [r['observer_batch_id'] for r in board(uri, s['phase'])['rows']] == [str(b)]


def test_failed_stage_one_cancels_stage_two(staged):
    s = staged; uri = s['uri']
    b = evaluate(s)
    ad, x1 = runs(s, b, 'ad'), runs(s, b, 'x1')
    score(s, list(ad)[1:], 10)
    fail_run(s, b, list(ad.values())[0])                                     # the team's own failure
    assert query(uri, 'select status,quota_refunded from public.observer_batches where id=%s', (b,)) == [('failed', False)]
    assert {r[0] for r in query(uri, 'select status||\'/\'||error from public.observer_runs where id=any(%s)', (list(x1),))} \
        == {'cancelled/stage1_failed'}
    assert not dispatchable(s) & {str(r) for r in x1}
    assert board(uri, s['phase'])['rows'] == []


def test_earlier_and_local_evaluations_are_not_staged(staged):
    s = staged; uri = s['uri']
    b = evaluate(s)
    query(uri, 'update public.observer_batches set staged=false where id=%s', (b,))  # created before the switch
    assert {str(r) for r in dispatchable(s)} == {str(r) for r in runs(s, b, 'ad')} | {str(r) for r in runs(s, b, 'x1')}
    local = rpc(uri, 'observer_create_batch', s['phase'], None, role='authenticated', user=s['user'])
    assert query(uri, 'select staged from public.observer_batches where id=%s', (local,)) == [(False,)]


def test_phases_without_both_card_groups_are_not_staged(team):
    s = team; uri = s['uri']
    query(uri, 'update public.observer_phase_settings set daily_batches=10 where phase_id=%s', (s['phase'],))
    rev = revision(s)
    b = rpc(uri, 'observer_create_batch', s['phase'], rev, True, role='authenticated', user=s['user'])
    assert query(uri, 'select staged from public.observer_batches where id=%s', (b,)) == [(False,)]

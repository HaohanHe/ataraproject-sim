"""Cards A1-D1 alongside A-D (migrations 20261005200000/20261005200100): the A-D board, its score and its
completeness rule are unchanged when evaluations run 8 cards; the super board ranks the sum of all 8 cards of
evaluations that completed every card, 20% A-D + 80% A1-D1, latest per team (20261006040000); a phase without added cards shows nothing new."""
import secrets
import uuid

import pytest
from psycopg.types.json import Jsonb

from test_baseline_rows_db import EXAMPLES, baseline, example_team
from test_card_board import batch, board, card, v4_phase
from test_project_database import database, identity, query, rpc, setup  # noqa: F401
from test_project_eval_ux import ORG


def super_board(uri, phase, slug=None, role='anon', user=None):
    return rpc(uri, 'observer_super_board', phase, slug, 100, role=role, user=user)


def add_extra_cards(s):
    """Cards A1-D1 with a fresh version suffix (scenario slugs are unique across tests)."""
    version = secrets.randbelow(10**6) + 2
    return {c + '1': card(s['uri'], s['phase'], f'v4-{c}1-v{version}', f'Card {c.upper()}1') for c in 'abcd'}


def live(s):
    query(s['uri'], "update public.phases set leaderboard_mode='live',is_active=true where id=%s", (s['phase'],))


def fail_run(s, batch_id, scenario, code='project_operation_failed'):
    """The colocated engine reports a failure of one card (the team's own failure by default)."""
    uri = s['uri']
    query(uri, """insert into private.observer_installations
        (organization,organization_id,installation_id,repository_id,approved_sha,enabled)
        values(%s,'101',202,'303',%s,true) on conflict(organization) do update set enabled=true""", (ORG, 'a'*40))
    run = query(uri, 'select id from public.observer_runs where batch_id=%s and scenario_id=%s', (batch_id, scenario))[0][0]
    job, nonce = uuid.uuid4(), secrets.token_urlsafe(32)
    rpc(uri, 'observer_enqueue_job', job, 'engine', run, None, ORG, nonce, 'encrypted job payload', 'encrypted nonce')
    rpc(uri, 'observer_claim_job', job, nonce, '404', '1', '303', '101', 'a'*40)
    rpc(uri, 'observer_finish_job', job, '404', '1', {'diagnostics': {'stage': 'execute', 'code': code, 'log': ''}},
        'engine_job_failed')
    return run


def partial_batch(s, user, scores):
    """A batch whose listed cards score; the others are left running."""
    uri = s['uri']
    b = rpc(uri, 'observer_create_batch', s['phase'], None, role='authenticated', user=user)
    for run, scenario in query(uri, 'select id,scenario_id from public.observer_runs where batch_id=%s', (b,)):
        if scenario in scores:
            query(uri, "update public.observer_runs set status='scored',score=%s,score_summary=%s,finished_at=now() where id=%s",
                  (scores[scenario], Jsonb({'score': {'total': scores[scenario]}}), run))
        else:
            query(uri, "update public.observer_runs set status='running' where id=%s", (run,))
    query(uri, 'select private.observer_finalize_batch(%s)', (b,))
    return b


def state(uri, b):
    return query(uri, 'select status,score from public.observer_batches where id=%s', (b,))[0]


def test_extra_card_slugs(database):
    yes = ['v4-a1', 'v4-b1', 'v4-c1-v1', 'v4-d1-v2', 'v4-a1-v12']
    no = ['v4-a', 'v4-e1', 'v4-a2', 'v4-a1-x', 'v4-a1-v', 'v4-practice-alpha', 'xv4-a1', 'v4-a1-v1-old', None]
    for slug in yes + no:
        assert query(database, 'select private.observer_extra_card(%s)', (slug,))[0][0] is (slug in yes), slug


def test_a_phase_without_added_cards_is_unchanged(setup):
    s = setup; uri = s['uri']
    cards = v4_phase(s); live(s)
    b = batch(uri, s['phase'], s['user'], {sid: 10 * (i + 1) for i, sid in enumerate(cards.values())})
    assert state(uri, b) == ('scored', 25.0)
    result = board(uri, s['phase'])
    assert result['extra_cards'] == [] and len(result['cards']) == 4 and result['rows'][0]['total_score'] == 25
    assert super_board(uri, s['phase']) == {'layout': 'super', 'cards': [], 'scenario': None, 'rows': []}


def test_a_d_board_unchanged_and_super_board_sums_eight_cards(setup):
    s = setup; uri = s['uri']
    cards = v4_phase(s); live(s)
    slug = {k: query(uri, 'select slug from public.scenarios where id=%s', (v,))[0][0] for k, v in cards.items()}
    old_team = s['user']
    old = batch(uri, s['phase'], old_team, {sid: 50 for sid in cards.values()})       # before the switch: 4 cards
    before = board(uri, s['phase'])

    extra = add_extra_cards(s)
    for k, v in extra.items():
        slug[k] = query(uri, 'select slug from public.scenarios where id=%s', (v,))[0][0]
    # Switching on moves nothing: same rows, ranks, scores, cards; only the added cards are announced.
    after = board(uri, s['phase'])
    assert after['rows'] == before['rows'] and after['cards'] == before['cards']
    assert [c['slug'] for c in after['extra_cards']] == [slug[k] for k in ('a1', 'b1', 'c1', 'd1')]
    assert super_board(uri, s['phase'])['rows'] == []                                 # 4-card evaluations never enter

    full_user, full_team = identity(uri)
    full = batch(uri, s['phase'], full_user, {**{cards[c]: 40 for c in 'abcd'}, **{extra[c]: 100 for c in extra}})
    assert state(uri, full) == ('scored', 40.0)                                        # headline score: A-D mean
    rival_user, rival_team = identity(uri)
    rival = batch(uri, s['phase'], rival_user, {**{cards[c]: 45 for c in 'abcd'}, **{extra[c]: 10 for c in extra}})

    rows = board(uri, s['phase'])['rows']
    assert [(r['observer_batch_id'], r['rank'], r['total_score']) for r in rows] == \
        [(str(old), 1, 50), (str(rival), 2, 45), (str(full), 3, 40)]
    assert set(rows[2]['card_scores']) == {slug[c] for c in 'abcd'} and rows[2]['unfinished_cards'] == []
    # The A tab is the A-D board's, unchanged.
    a_tab = board(uri, s['phase'], slug['a'])['rows']
    assert [r['total_score'] for r in a_tab] == [50, 45, 40]

    sup = super_board(uri, s['phase'])
    assert [c['slug'] for c in sup['cards']] == sorted(slug.values())
    assert [(r['observer_batch_id'], r['rank'], r['total_score']) for r in sup['rows']] == \
        [(str(full), 1, 352), (str(rival), 2, 68)]
    assert sup['rows'][0]['card_scores'] == {**{slug[c]: 40 for c in 'abcd'}, **{slug[c]: 100 for c in extra}}
    # A1 tab, from the super board (the card board delegates, so the CLI's --card works too).
    a1 = board(uri, s['phase'], slug['a1'])
    assert a1['scenario'] == slug['a1'] and a1['cards'] == after['cards']
    assert [(r['team_id'], r['total_score'], r['overall_score']) for r in a1['rows']] == \
        [(str(full_team), 100, 352), (str(rival_team), 10, 68)]
    assert a1['rows'] == super_board(uri, s['phase'], slug['a1'])['rows']

    # A better A-D evaluation of the rival without A1-D1 complete: A-D board moves, super board keeps the latest complete 8-card evaluation.
    better = partial_batch(s, rival_user, {**{cards[c]: 90 for c in 'abcd'}, **{extra[c]: 1 for c in ('b1', 'c1', 'd1')}})
    assert state(uri, better)[0] in ('queued', 'running')
    fail_run(s, better, extra['a1'])                                                   # an added card fails
    assert state(uri, better) == ('scored', 90.0)                                      # the evaluation still counts on A-D
    assert board(uri, s['phase'])['rows'][0]['observer_batch_id'] == str(better)
    assert [r['observer_batch_id'] for r in super_board(uri, s['phase'])['rows']] == [str(full), str(rival)]

    # A failure of an A-D card fails the evaluation exactly as before.
    worse = partial_batch(s, rival_user, {**{cards[c]: 99 for c in 'bcd'}, **{extra[c]: 99 for c in extra}})
    fail_run(s, worse, cards['a'])
    assert state(uri, worse) == ('failed', None)

    # Hidden board: nothing for participants.
    query(uri, "update public.phases set leaderboard_mode='hidden' where id=%s", (s['phase'],))
    assert super_board(uri, s['phase'])['rows'] == [] and board(uri, s['phase'], slug['a1'])['rows'] == []


def test_voided_added_card_keeps_the_a_d_score(setup):
    s = setup; uri = s['uri']
    cards = v4_phase(s); live(s)
    extra = add_extra_cards(s)
    b = batch(uri, s['phase'], s['user'], {**{cards[c]: 20 for c in 'abcd'}, **{extra[c]: 30 for c in extra}})
    run = query(uri, 'select id from public.observer_runs where batch_id=%s and scenario_id=%s', (b, extra['b1']))[0][0]
    query(uri, """insert into private.observer_installations
        (organization,organization_id,installation_id,repository_id,approved_sha,enabled)
        values(%s,'101',202,'303',%s,true) on conflict(organization) do update set enabled=true""", (ORG, 'a'*40))
    job, nonce = uuid.uuid4(), secrets.token_urlsafe(32)
    rpc(uri, 'observer_enqueue_job', job, 'score', run, None, ORG, nonce, 'encrypted job payload', 'encrypted nonce')
    query(uri, "update public.observer_runs set score_check='pending' where id=%s", (run,))
    query(uri, "select private.observer_settle_score_check(%s,'rejected',null)", (job,))
    assert query(uri, 'select status from public.observer_runs where id=%s', (run,)) == [('failed',)]
    assert state(uri, b) == ('scored', 20.0)
    assert board(uri, s['phase'])['rows'][0]['total_score'] == 20
    assert super_board(uri, s['phase'])['rows'] == []


def test_baselines_use_a_d_only(setup):
    s = setup; uri = s['uri']
    cards = v4_phase(s); live(s)
    users = {n: example_team(uri, n) for n in EXAMPLES if n in ('python', 'typescript', 'rust')}
    batch(uri, s['phase'], users['python'][0], {sid: 30 for sid in cards.values()})   # before the switch
    extra = add_extra_cards(s)
    for name in ('typescript', 'rust'):
        batch(uri, s['phase'], users[name][0], {**{cards[c]: 30 for c in 'abcd'}, **{extra[c]: 1000 for c in extra}})
    basic = baseline(uri, s['phase'])['basic']
    assert basic['overall_score'] == pytest.approx(30) and basic['runs'] == 3 and len(basic['card_scores']) == 4

"""Baseline reference rows (migration 20261005100000): the official examples' averages, aggregates only, while the
six hidden organizer teams stay off the board for participants."""
import uuid

from test_card_board import batch, board, v4_phase
from test_project_database import database, identity, query, rpc, setup  # noqa: F401

EXAMPLES = {'python': 'basic', 'typescript': 'basic', 'rust': 'basic', 'python-pro': 'pro', 'typescript-pro': 'pro', 'rust-pro': 'pro'}


def example_team(uri, name):
    user, team = identity(uri)
    query(uri, "update public.teams set slug=%s,name=%s,is_hidden=true where id=%s",
          (f'official-example-{name}', f'官方示例 · {name} (主办方)', team))
    return user, team


def baseline(uri, phase, role='anon', user=None):
    return {r['group']: r for r in rpc(uri, 'observer_baseline_rows', phase, role=role, user=user)}


def test_baseline_rows(setup):
    s = setup; uri = s['uri']
    cards = v4_phase(s)
    query(uri, "update public.phases set leaderboard_mode='live',is_active=true where id=%s", (s['phase'],))
    slug = {k: query(uri, 'select slug from public.scenarios where id=%s', (v,))[0][0] for k, v in cards.items()}
    batch(uri, s['phase'], s['user'], {sid: 100 for sid in cards.values()})        # an ordinary participant team
    users = {n: example_team(uri, n) for n in EXAMPLES}
    # Basic: python two evaluations (A 10/30 -> 20), typescript 40, rust 60: card A mean 40.
    batch(uri, s['phase'], users['python'][0], {cards['a']: 10, cards['b']: 10, cards['c']: 10, cards['d']: 10})
    batch(uri, s['phase'], users['python'][0], {cards['a']: 30, cards['b']: 30, cards['c']: 30, cards['d']: 30})
    batch(uri, s['phase'], users['typescript'][0], {sid: 40 for sid in cards.values()})
    batch(uri, s['phase'], users['rust'][0], {sid: 60 for sid in cards.values()})
    # Pro: only two of the three examples have run so far, so no pro row yet.
    batch(uri, s['phase'], users['python-pro'][0], {sid: 80 for sid in cards.values()})
    batch(uri, s['phase'], users['typescript-pro'][0], {cards['a']: 50, cards['b']: 90, cards['c']: 90, cards['d']: 90})

    stranger, _ = identity(uri)
    for role, user in (('anon', None), ('authenticated', stranger), ('authenticated', s['user'])):
        rows = baseline(uri, s['phase'], role, user)
        assert set(rows) == {'basic'}
        b = rows['basic']
        assert abs(b['overall_score'] - 40) < 1e-9 and b['runs'] == 4 and b['examples'] == 3 and b['updated_at']
        assert {k: round(v, 6) for k, v in b['card_scores'].items()} == {slug[c]: 40 for c in 'abcd'}
        # Aggregates only: no team ids or names; the board itself has only the participant team, ranked 1.
        text = str(rows)
        assert all(str(t) not in text for _, t in users.values()) and '官方示例' not in text and 'official-example' not in text
        result = board(uri, s['phase'], role=role, user=user)
        assert [(r['team_id'], r['rank']) for r in result['rows']] == [(str(s['team']), 1)]

    batch(uri, s['phase'], users['rust-pro'][0], {cards['a']: 110, cards['b']: 90, cards['c']: 90, cards['d']: 90})
    pro = baseline(uri, s['phase'])['pro']
    assert abs(pro['overall_score'] - 85) < 1e-9 and pro['runs'] == 3
    assert abs(pro['card_scores'][slug['a']] - 80) < 1e-9

    # A team that merely takes the name but is not hidden, or a non-example hidden team, never counts.
    query(uri, "update public.teams set is_hidden=false where id=%s", (users['rust'][1],))
    assert 'basic' not in baseline(uri, s['phase'])
    query(uri, "update public.teams set is_hidden=true where id=%s", (users['rust'][1],))

    # Board not visible to participants: no baselines either.
    query(uri, "update public.phases set leaderboard_mode='hidden' where id=%s", (s['phase'],))
    assert baseline(uri, s['phase']) == {}
    assert baseline(uri, uuid.uuid4()) == {}

"""A team cancels its own queued evaluation (migration 20261005220000): only before any card started,
refunded, never scored, race-safe with the dispatcher; a self-check cancels its members that have not started."""
import secrets
import uuid

import psycopg
import pytest

from test_project_database import database, identity, query, rpc, setup  # noqa: F401
from test_project_eval_ux import ORG, revision, team  # noqa: F401


def cancel(s, batch, user=None):
    return query(s['uri'], 'select public.observer_cancel_batch(%s)', (batch,), role='authenticated',
                 user=user or s['user'])[0][0]


def evaluate(s, rev):
    return query(s['uri'], 'select public.observer_create_batch(%s,%s,%s)', (s['phase'], rev, True),
                 role='authenticated', user=s['user'])[0][0]


def self_check(s, rev):
    return query(s['uri'], 'select public.observer_create_repeat_batches(%s,%s,%s)', (s['phase'], rev, True),
                 role='authenticated', user=s['user'])[0][0]['batch_ids']


def used(s):
    return query(s['uri'], 'select private.observer_batches_used(%s,%s)', (s['team'], s['phase']))[0][0]


def state(s, batch):
    uri = s['uri']
    b = query(uri, 'select status,quota_refunded,score from public.observer_batches where id=%s', (batch,))[0]
    return b, sorted(r[0] for r in query(uri, 'select status from public.observer_runs where batch_id=%s', (batch,)))


def first_run(s, batch):
    return query(s['uri'], 'select id from public.observer_runs where batch_id=%s order by id limit 1', (batch,))[0][0]


def start(s, batch):
    """observer_schedule_run's effect: the session opens (run 'starting') and the engine job is queued."""
    run = first_run(s, batch)
    rpc(s['uri'], 'observer_open_session', run, secrets.token_urlsafe(32), secrets.token_urlsafe(32))
    rpc(s['uri'], 'observer_enqueue_job', uuid.uuid4(), 'engine', run, None, ORG, secrets.token_urlsafe(32),
        'encrypted job payload', 'encrypted nonce')
    return run


@pytest.fixture
def ready(team):
    s = team
    query(s['uri'], 'update public.observer_phase_settings set daily_batches=10,max_active_evaluations=10 where phase_id=%s',
          (s['phase'],))
    return s, revision(s)


def test_a_queued_evaluation_is_cancelled_refunded_and_never_scored(ready):
    s, rev = ready; uri = s['uri']
    batch = evaluate(s, rev)
    assert used(s) == 1
    got = cancel(s, batch)
    assert got == {'batch_id': str(batch), 'status': 'cancelled', 'cancelled': [str(batch)]}
    assert state(s, batch) == (('cancelled', True, None), ['cancelled'])
    assert used(s) == 0
    # Idempotent: a second call (another tab, a retried request) returns it again and changes nothing.
    assert cancel(s, batch) == {'batch_id': str(batch), 'status': 'cancelled', 'cancelled': []}
    # Never handed to a runner afterwards, and it no longer blocks evaluating the same version again.
    assert str(first_run(s, batch)) not in [p['id'] for p in rpc(uri, 'observer_pending_runs', 10)]
    query(uri, 'select public.observer_create_batch(%s,%s)', (s['phase'], rev), role='authenticated', user=s['user'])
    assert query(uri, "select count(*) from public.audit_log where action='observer.cancel_batch'")[0][0] >= 1


def test_only_the_team_or_an_organizer(ready):
    s, rev = ready; uri = s['uri']
    batch = evaluate(s, rev)
    outsider, _ = identity(uri)
    with pytest.raises(psycopg.Error, match='evaluation_not_found'):
        cancel(s, batch, user=outsider)
    with pytest.raises(psycopg.Error, match='evaluation_not_found'):
        cancel(s, uuid.uuid4())
    with pytest.raises(psycopg.Error, match='permission denied'):
        query(uri, 'select public.observer_cancel_batch(%s)', (batch,), role='anon')
    query(uri, 'update public.profiles set is_banned=true where id=%s', (s['user'],))
    try:
        with pytest.raises(psycopg.Error, match='banned'):
            cancel(s, batch)
    finally:
        query(uri, 'update public.profiles set is_banned=false where id=%s', (s['user'],))
    teammate, _ = identity(uri, team=s['team'])
    assert cancel(s, batch, user=teammate)['cancelled'] == [str(batch)]
    other = evaluate(s, rev)
    query(uri, 'update public.profiles set is_admin=true where id=%s', (outsider,))
    assert cancel(s, other, user=outsider)['cancelled'] == [str(other)]


def test_a_started_or_finished_evaluation_is_refused(ready):
    s, rev = ready; uri = s['uri']
    batch = evaluate(s, rev)
    start(s, batch)
    with pytest.raises(psycopg.Error, match='evaluation_started'):
        cancel(s, batch)
    assert state(s, batch)[0] == ('running', False, None)
    # A live job alone (claimed, dispatched or queued) also counts as started.
    other = evaluate(s, rev)
    rpc(uri, 'observer_enqueue_job', uuid.uuid4(), 'engine', first_run(s, other), None, ORG, secrets.token_urlsafe(32),
        'encrypted job payload', 'encrypted nonce')
    with pytest.raises(psycopg.Error, match='evaluation_started'):
        cancel(s, other)
    query(uri, "update public.observer_batches set status='failed',finished_at=now() where id=%s", (other,))
    with pytest.raises(psycopg.Error, match='evaluation_finished'):
        cancel(s, other)


def test_a_leased_run_is_cancelled_and_the_dispatcher_cannot_start_it(ready):
    s, rev = ready; uri = s['uri']
    batch = evaluate(s, rev)
    run, lease = first_run(s, batch), uuid.uuid4()
    # The dispatcher leased the run (observer_pending_runs) and is building its jobs.
    query(uri, "insert into private.observer_run_leases(run_id,lease,expires_at) values(%s,%s,now()+interval '2 minutes')",
          (run, lease))
    assert cancel(s, batch)['cancelled'] == [str(batch)]
    assert query(uri, 'select count(*) from private.observer_run_leases where run_id=%s', (run,)) == [(0,)]
    jobs = [{'id': str(uuid.uuid4()), 'kind': 'engine', 'nonce': secrets.token_urlsafe(32),
             'encrypted_input': 'x', 'encrypted_nonce': 'y'}]
    with pytest.raises(psycopg.Error, match='run_lease_invalid'):
        rpc(uri, 'observer_schedule_run', run, lease, ORG, secrets.token_urlsafe(32), secrets.token_urlsafe(32), None, jobs)
    assert query(uri, 'select count(*) from private.observer_jobs where run_id=%s', (run,)) == [(0,)]
    assert query(uri, 'select count(*) from private.observer_sessions where run_id=%s', (run,)) == [(0,)]


def test_waits_for_the_dispatchers_row_lock(ready):
    s, rev = ready; uri = s['uri']
    batch = evaluate(s, rev)
    run = first_run(s, batch)
    with psycopg.connect(uri) as dispatcher:
        # The dispatcher holds the run (as observer_schedule_run does) and starts it.
        dispatcher.execute('select 1 from public.observer_runs where id=%s for update', (run,))
        with psycopg.connect(uri) as waiter:
            waiter.execute("set statement_timeout='300ms'")
            waiter.execute("set local role authenticated")
            waiter.execute("select set_config('request.jwt.claims', %s, true)", ('{"role":"authenticated","sub":"%s"}' % s['user'],))
            with pytest.raises(psycopg.errors.QueryCanceled):
                waiter.execute('select public.observer_cancel_batch(%s)', (batch,))
        dispatcher.execute("update public.observer_runs set status='starting' where id=%s", (run,))
        dispatcher.execute("update public.observer_batches set status='running' where id=%s", (batch,))
    with pytest.raises(psycopg.Error, match='evaluation_started'):
        cancel(s, batch)


def test_a_self_check_cancels_its_members_that_have_not_started(ready):
    s, rev = ready
    first, second, third = self_check(s, rev)
    assert used(s) == 3
    start(s, first)
    with pytest.raises(psycopg.Error, match='evaluation_started'):
        cancel(s, first)
    got = cancel(s, third)
    assert sorted(got['cancelled']) == sorted([str(second), str(third)])
    assert state(s, first)[0][0] == 'running'
    assert state(s, second) == (('cancelled', True, None), ['cancelled'])
    assert used(s) == 1
    # A set none of whose members started is cancelled as a whole from any member.
    query(s['uri'], "update public.observer_batches set status='failed',finished_at=now() where id=%s", (first,))
    ids = self_check(s, rev)
    assert sorted(cancel(s, ids[0])['cancelled']) == sorted(str(i) for i in ids)
    assert used(s) == 1


def test_an_organizers_sealed_evaluation_is_not_the_teams_to_cancel(ready):
    s, rev = ready; uri = s['uri']
    batch = evaluate(s, rev)
    query(uri, 'update public.observer_phase_settings set sealed=true where phase_id=%s', (s['phase'],))
    try:
        with pytest.raises(psycopg.Error, match='evaluation_not_cancellable'):
            cancel(s, batch)
    finally:
        query(uri, 'update public.observer_phase_settings set sealed=false where phase_id=%s', (s['phase'],))

import assert from 'node:assert/strict'
import test from 'node:test'
import { ACTIVE_REFRESH_MS, IDLE_REFRESH_MS, inProgress, refreshDue } from '../src/lib/dashboardRefresh.ts'

const revision = (status: string, test?: string) => ({ status, public_test: test ? { status: test } : {} })

test('work in progress refreshes every 30 s, an idle dashboard every 60 s', () => {
  assert.equal(ACTIVE_REFRESH_MS, 30_000); assert.equal(IDLE_REFRESH_MS, 60_000)
  const idle = { batches: [{ status: 'scored' }, { status: 'failed' }], projects: [{ observer_revisions: [revision('approved', 'passed')] }] }
  const running = { ...idle, batches: [...idle.batches, { status: 'running' }] }
  const preparing = { ...idle, projects: [{ observer_revisions: [revision('preparing')] }] }
  const testing = { ...idle, projects: [{ observer_revisions: [revision('reviewable', 'running')] }] }
  assert.equal(inProgress(null), false); assert.equal(inProgress(idle), false)
  for (const d of [running, preparing, testing, { batches: [{ status: 'queued' }] }]) assert.equal(inProgress(d), true)
  assert.equal(refreshDue(idle, 0, 59_999), false); assert.equal(refreshDue(idle, 0, 60_000), true)
  assert.equal(refreshDue(running, 0, 29_999), false); assert.equal(refreshDue(running, 0, 30_000), true)
  assert.equal(refreshDue(undefined, 0, 60_000), true)
})

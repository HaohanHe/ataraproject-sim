import assert from 'node:assert/strict'
import test from 'node:test'
import { elapsedText, runningMinutes, waitingForStage1 } from '../src/lib/runProgress.ts'

type R = { slug: string; status: string; started_at?: string | null }
const slug = (r: R) => r.slug
const pick = <T>(en: T, zh: T) => (void en, zh)

test('A1–D1 wait for A–D only in a two-stage evaluation', () => {
  const a = { slug: 'v4-a', status: 'running' }, b = { slug: 'v4-b', status: 'scored' }, a1 = { slug: 'v4-a1-v2', status: 'queued' }
  assert.equal(waitingForStage1(true, [a, b, a1], a1, slug), true)
  assert.equal(waitingForStage1(false, [a, b, a1], a1, slug), false)
  assert.equal(waitingForStage1(true, [a, b, a1], a, slug), false)
  const done = [{ ...a, status: 'failed' }, b, a1]
  assert.equal(waitingForStage1(true, done, a1, slug), false)
})

test('elapsed minutes while running', () => {
  const started_at = '2026-10-06T10:00:00Z', t0 = Date.parse(started_at)
  assert.equal(runningMinutes({ status: 'running', started_at }, t0 + 12.5 * 60_000), 12)
  assert.equal(runningMinutes({ status: 'scored', started_at }, t0 + 60_000), null)
  assert.equal(runningMinutes({ status: 'running', started_at: null }, t0), null)
  assert.equal(elapsedText(pick, 12), '已运行 12 分钟')
  assert.equal(elapsedText(pick, 0), '已运行不到 1 分钟')
})

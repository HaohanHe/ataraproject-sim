import assert from 'node:assert/strict'
import test from 'node:test'
import { SUPER_TAB, isBaseline, parseBaselineRows, toLeaderboardEntry, withBaselines, type BoardRow } from '../src/lib/cardBoard.ts'

const team = (rank: number, score: number, cards: Record<string, number> = {}) =>
  toLeaderboardEntry({ rank, team_id: `t${rank}-${score}`, team_name: `T${score}`, total_score: score, card_scores: cards, overall_score: score }, rank - 1)
const baselines = parseBaselineRows([
  { group: 'basic', overall_score: 22800, card_scores: { 'v4-a': 16000, 'v4-b': 31900 }, runs: 9, updated_at: '2026-10-05T14:50:30Z' },
  { group: 'pro', overall_score: 29700, card_scores: { 'v4-a': 23500 }, runs: 6, updated_at: null },
  { group: 'other', overall_score: 1 }, { group: 'pro' }, null,
])
const shape = (rows: BoardRow[]) => rows.map(r => isBaseline(r) ? `B:${r.baseline}` : `${r.rank}:${r.total_score}`)

test('only well-formed basic/pro rows are parsed', () => {
  assert.deepEqual(baselines.map(b => [b.group, b.runs]), [['basic', 9], ['pro', 6]])
  assert.deepEqual(parseBaselineRows(null), [])
})

test('baselines sit where their score would rank, after equal scores, and ranks are unchanged', () => {
  const entries = [team(1, 31000), team(2, 29700), team(3, 25000), team(3, 25000), team(5, 20000)]
  const rows = withBaselines(entries, baselines, null)
  assert.deepEqual(shape(rows), ['1:31000', '2:29700', 'B:pro', '3:25000', '3:25000', 'B:basic', '5:20000'])
  // The teams and their rank numbers are exactly the database's.
  assert.deepEqual(rows.filter(r => !isBaseline(r)), entries)
})

test('top, bottom and empty boards', () => {
  assert.deepEqual(shape(withBaselines([team(1, 10)], baselines, null)), ['B:pro', 'B:basic', '1:10'])
  assert.deepEqual(shape(withBaselines([team(1, 99999)], baselines, null)), ['1:99999', 'B:pro', 'B:basic'])
  assert.deepEqual(shape(withBaselines([], baselines, null)), ['B:pro', 'B:basic'])
  assert.deepEqual(withBaselines([team(1, 5)], [], null), [team(1, 5)])
})

test('card tabs use the per-card averages; a card without one has no baseline row', () => {
  const entries = [team(1, 30000), team(2, 20000), team(3, 10000)]
  const a = withBaselines(entries, baselines, 'v4-a')
  assert.deepEqual(shape(a), ['1:30000', 'B:pro', '2:20000', 'B:basic', '3:10000'])
  assert.equal(a.find(r => isBaseline(r) && r.baseline === 'pro')!.total_score, 23500)
  assert.equal((a.find(isBaseline) as any).overall_score, 29700)
  assert.deepEqual(shape(withBaselines(entries, baselines, 'v4-b')), ['B:basic', '1:30000', '2:20000', '3:10000'])
  assert.deepEqual(shape(withBaselines(entries, baselines, 'v4-c')), ['1:30000', '2:20000', '3:10000'])
})

test('the super tab places baselines by their total', () => {
  assert.deepEqual(shape(withBaselines([team(1, 31000), team(2, 5)], baselines, SUPER_TAB)), ['1:31000', 'B:pro', 'B:basic', '2:5'])
})

// Pure row mapping for the leaderboards and the card boards (no Supabase client), so it can be unit-tested.
import type { LeaderboardEntry } from './data'
import { scenarioOrder } from './scenarioLabels.ts'

const numberOrNull = (value: unknown) => value == null ? null : Number(value)
function numberMap(value: unknown): Record<string, number> | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  return Object.fromEntries(Object.entries(value as Record<string, unknown>).filter(([, v]) => typeof v === 'number') as [string, number][])
}

/** [lowest, highest] of the averaged evaluations, or null. */
function range(value: unknown): [number, number] | null {
  if (!Array.isArray(value) || value.length !== 2 || value.some(v => typeof v !== 'number')) return null
  return [value[0] as number, value[1] as number]
}

export function toLeaderboardEntry(row: any, index: number): LeaderboardEntry {
  return {
    rank: Number(row.rank ?? index + 1),
    leader_github: row.leader_github ? String(row.leader_github) : null,
    leader_avatar_url: row.leader_avatar_url ? String(row.leader_avatar_url) : null,
    team_id: String(row.team_id),
    team_name: String(row.team_name ?? '—'),
    team_slug: String(row.team_slug ?? ''),
    total_score: Number(row.total_score ?? 0),
    science_score: Number(row.science_score ?? 0),
    completion_rate: Number(row.completion_rate ?? 0),
    uniformity_score: Number(row.uniformity_score ?? 0),
    base_science: Number(row.base_science ?? 0),
    program_bonus: Number(row.program_bonus ?? 0),
    request_reward: Number(row.request_reward ?? 0),
    coverage_bonus: numberOrNull(row.coverage_bonus),
    coverage_evenness: numberOrNull(row.coverage_evenness),
    penalty_total: Number(row.penalty_total ?? 0),
    completed_tiles: numberOrNull(row.completed_tiles),
    required_missing: numberOrNull(row.required_missing),
    submission_count: Number(row.submission_count ?? 0),
    best_submission_id: numberOrNull(row.best_submission_id),
    kind: row.kind ?? null,
    scored_at: row.scored_at ?? null,
    scenario_slug: row.scenario_slug ? String(row.scenario_slug) : null,
    observer_batch_id: row.observer_batch_id ?? null,
    calibrated: row.calibrated === true,
    raw_total_score: numberOrNull(row.raw_total_score),
    report_reward: Number(row.report_reward ?? 0),
    ...('card_scores' in row || 'overall_score' in row ? {
      card_scores: numberMap(row.card_scores),
      unfinished_cards: Array.isArray(row.unfinished_cards) ? row.unfinished_cards.map(String) : [],
      unfinished: row.unfinished === true,
      overall_score: numberOrNull(row.overall_score),
      overall_rank: numberOrNull(row.overall_rank),
      targets_observed: numberOrNull(row.targets_observed),
      components: numberMap(row.components),
      averaged_runs: numberOrNull(row.averaged_runs),
      score_range: range(row.score_range),
      card_ranges: row.card_ranges && typeof row.card_ranges === 'object' && !Array.isArray(row.card_ranges)
        ? Object.fromEntries(Object.entries(row.card_ranges as Record<string, unknown>)
          .map(([slug, value]) => [slug, range(value)]).filter((entry): entry is [string, [number, number]] => entry[1] !== null))
        : null,
      final_version: row.final_version && typeof row.final_version === 'object'
        ? { chosen: row.final_version.chosen === true, score: numberOrNull(row.final_version.score) } : null,
    } : {}),
  }
}

/**
 * Card boards (migration 20260928004100): a complete-project phase whose settings.board_layout is 'cards' (one board
 * per card) or 'cards_overall' (per card plus the overall mean). Every entry comes from the team's best complete
 * evaluation, or in a phase with repeated evaluations (the hidden final) the mean of its first ones (averaged_runs). Card labels are the scenario names returned by the database, so no card is named in the site.
 */
export type BoardLayout = 'overall' | 'cards' | 'cards_overall'
export interface BoardCard { slug: string; name: string }
/** cards: the cards of the board's score (A-D); extraCards: the added cards A1-D1 (migration 20261005200100), which
 * count only on the super board. The super board's tab is SUPER_TAB; an added card's tab is its slug. */
export interface CardBoard { layout: BoardLayout; cards: BoardCard[]; extraCards: BoardCard[]; scenario: string | null; rows: LeaderboardEntry[] }
type TabSource = Pick<CardBoard, 'layout' | 'cards'> & Partial<Pick<CardBoard, 'extraCards'>>

/** The super board's tab (超级总榜): the sum over all cards, A-D and A1-D1. No card slug is ever this. */
export const SUPER_TAB = 'super'

/** The tabs of a board, in order; null is the overall tab. Boards without cards have none. Where the phase has
 * added cards: overall, A-D, then the super board and A1-D1. */
export function cardBoardTabs(board: TabSource | null): (string | null)[] {
  if (!board || board.layout === 'overall' || !board.cards.length) return []
  const extra = board.extraCards ?? []
  return [...(board.layout === 'cards_overall' ? [null] : []), ...board.cards.map(c => c.slug),
    ...(extra.length ? [SUPER_TAB, ...extra.map(c => c.slug)] : [])]
}

/** Whether a tab belongs to the super board (the super tab or an added card's tab). */
export const isSuperTab = (board: Partial<Pick<CardBoard, 'extraCards'>> | null, tab: string | null) =>
  tab === SUPER_TAB || (tab !== null && !!board?.extraCards?.some(c => c.slug === tab))

/** The requested tab when the board has it, else the first one (overall where there is one). */
export function pickCardTab(board: TabSource | null, wanted: string | null | undefined): string | null {
  const tabs = cardBoardTabs(board)
  const want = wanted ?? null
  return tabs.includes(want) ? want : tabs[0] ?? null
}

function parseCards(value: unknown): BoardCard[] {
  return Array.isArray(value)
    ? (value as any[]).filter(c => c && c.slug).map(c => ({ slug: String(c.slug), name: String(c.name ?? c.slug) }))
      .sort((a, b) => scenarioOrder(a.slug) - scenarioOrder(b.slug))
    : []
}

export function parseCardBoard(data: any): CardBoard {
  const layout: BoardLayout = data?.layout === 'cards' || data?.layout === 'cards_overall' ? data.layout : 'overall'
  return { layout, cards: parseCards(data?.cards), extraCards: layout === 'overall' ? [] : parseCards(data?.extra_cards),
    scenario: data?.scenario ? String(data.scenario) : null, rows: parseRows(data?.rows) }
}

export const parseRows = (rows: unknown): LeaderboardEntry[] => (Array.isArray(rows) ? rows : []).map(toLeaderboardEntry)

/** The cards of a super-board row, in order: A-D then A1-D1. */
export const superCards = (board: Pick<CardBoard, 'cards' | 'extraCards'>): BoardCard[] => [...board.cards, ...board.extraCards]

/**
 * Baseline reference rows (migration 20261005100000, public.observer_baseline_rows): the average scores of the
 * official examples run unmodified, basic and pro. They are not ranked: they sit where their score would place them
 * and leave every team's rank number unchanged.
 */
export interface BaselineRow { group: 'basic' | 'pro'; overall_score: number; card_scores: Record<string, number> | null; runs: number; updated_at: string | null }
export interface BaselineEntry { baseline: BaselineRow['group']; total_score: number; overall_score: number; card_scores: Record<string, number> | null; runs: number; updated_at: string | null }
export type BoardRow = LeaderboardEntry | BaselineEntry
export const isBaseline = (row: BoardRow): row is BaselineEntry => 'baseline' in row

export function parseBaselineRows(data: unknown): BaselineRow[] {
  if (!Array.isArray(data)) return []
  return data.filter(r => r && (r.group === 'basic' || r.group === 'pro') && typeof r.overall_score === 'number').map(r => ({
    group: r.group, overall_score: r.overall_score, card_scores: numberMap(r.card_scores),
    runs: Number(r.runs ?? 0), updated_at: r.updated_at ? String(r.updated_at) : null,
  }))
}

/** The board's rows with the baselines inserted after every team scoring at least as much (tab: null = overall). */
export function withBaselines(entries: LeaderboardEntry[], baselines: BaselineRow[], tab: string | null): BoardRow[] {
  const rows: BoardRow[] = [...entries]
  const refs = baselines.map(b => ({ b, score: tab === null || tab === SUPER_TAB ? b.overall_score : b.card_scores?.[tab] }))
    .filter((x): x is { b: BaselineRow; score: number } => typeof x.score === 'number')
    .sort((x, y) => y.score - x.score)
  for (const { b, score } of refs) {
    const at = rows.findIndex(r => r.total_score < score && !isBaseline(r))
    const entry: BaselineEntry = { baseline: b.group, total_score: score, overall_score: b.overall_score, card_scores: b.card_scores, runs: b.runs, updated_at: b.updated_at }
    // Higher baselines go first, so a lower one always lands after them.
    rows.splice(at === -1 ? rows.length : at, 0, entry)
  }
  return rows
}

import { isExtraCard } from './scenarioLabels.ts'

// Live per-card status of an evaluation, from the run rows the team can already read (status, started_at).
type Run = { status: string; started_at?: string | null }

const FINISHED = ['scored', 'failed', 'cancelled']
export const runFinished = (run: { status: string }) => FINISHED.includes(run.status)

/** A queued A1–D1 card of a two-stage evaluation whose A–D cards have not all finished. */
export function waitingForStage1<R extends Run>(staged: boolean | null | undefined, runs: R[], run: R, slug: (run: R) => string): boolean {
  return !!staged && run.status === 'queued' && isExtraCard(slug(run))
    && runs.some(other => !isExtraCard(slug(other)) && !runFinished(other))
}

/** Whole minutes a running card has run, or null when it is not running. */
export function runningMinutes(run: Run, now: number): number | null {
  if (run.status !== 'running' || !run.started_at) return null
  const started = Date.parse(run.started_at)
  return Number.isNaN(started) ? null : Math.max(0, Math.floor((now - started) / 60_000))
}

type Pick = <T>(english: T, chinese: T) => T
export const waitingText = (pick: Pick) => pick('Waiting for A–D', '等待 A–D 完成')
/** Tooltip (and screen-reader text) on the waiting status. */
export const waitingHint = (pick: Pick) =>
  pick('Starts automatically once A–D have all finished, usually about 25 minutes after submission.', 'A–D 全部结束后会自动开始，通常在提交后约 25 分钟。')
export function elapsedText(pick: Pick, minutes: number): string {
  if (minutes < 1) return pick('Running for under a minute', '已运行不到 1 分钟')
  return pick(`Running for ${minutes} min`, `已运行 ${minutes} 分钟`)
}

// How often the project dashboard re-reads the portal 'list' bundle on its own. Every open dashboard
// polls, so the cadence is the database cost; user actions still refresh at once (action() reloads).
type Activity = {
  batches?: { status: string }[] | null
  projects?: { observer_revisions?: { status: string; public_test?: { status?: string } | null }[] | null }[] | null
} | null | undefined

/** While an evaluation or an upload is in progress. */
export const ACTIVE_REFRESH_MS = 30_000
/** When nothing is in progress. */
export const IDLE_REFRESH_MS = 60_000
/** How often the page checks whether a refresh is due (local only, no request). */
export const REFRESH_TICK_MS = 5_000

export function inProgress(data: Activity): boolean {
  return (data?.batches ?? []).some(b => b.status === 'queued' || b.status === 'running')
    || (data?.projects ?? []).some(p => (p.observer_revisions ?? []).some(r =>
      r.status === 'queued' || r.status === 'preparing' || r.public_test?.status === 'queued' || r.public_test?.status === 'running'))
}

/** Whether the periodic refresh should run now, given when the last load started. */
export function refreshDue(data: Activity, lastLoadAt: number, now: number): boolean {
  return now - lastLoadAt >= (inProgress(data) ? ACTIVE_REFRESH_MS : IDLE_REFRESH_MS)
}

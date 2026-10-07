// During the online competition the practice board stays open next to it (current_competition answers
// practice_phase_id in competition mode). The 参赛 page evaluates in the online phase by default; a person can
// switch to practice, and the choice is remembered per user in this browser. Organizers may also offer an optional
// extra (unscored) phase, answered as extra_phase_id in either mode; it adds a third choice, 'extra'.
import type { StorageLike } from './quest'

export type EntryChoice = 'online' | 'practice' | 'extra'
export interface CompetitionState { mode: 'practice' | 'competition'; phaseId: string | null; betaPhaseId: string | null; projectPhaseId: string | null; practicePhaseId: string | null; extraPhaseId: string | null }

export const entryStorageKey = (userId: string) => `survey26.entry.v1.${userId}`

/** The RPC answer, read defensively: unknown or missing keys become null. */
export function parseCompetition(data: unknown): Omit<CompetitionState, 'betaPhaseId'> {
  const d = data && typeof data === 'object' ? data as Record<string, unknown> : {}
  const id = (v: unknown) => typeof v === 'string' ? v : null
  const mode = d.mode === 'competition' ? 'competition' : 'practice'
  return { mode, phaseId: id(d.phase_id), projectPhaseId: id(d.project_phase_id),
    // Only meaningful next to the competition; practice mode keeps using project_phase_id.
    practicePhaseId: mode === 'competition' ? id(d.practice_phase_id) : null,
    // Optional extra (unscored) phase, in either mode.
    extraPhaseId: id(d.extra_phase_id) }
}

/** Whether the 正式赛 / 练习赛 switch is offered. */
export const offersPracticeSwitch = (s: CompetitionState) => s.mode === 'competition' && !!s.practicePhaseId

/** Whether the extra (unscored) phase is offered as a third choice. */
export const offersExtraSwitch = (s: Pick<CompetitionState, 'extraPhaseId'>) => !!s.extraPhaseId

/** A remembered 'extra' counts only while the extra phase is offered; otherwise online, as before. */
export function readEntryChoice(storage: StorageLike | null, userId: string | null | undefined, extraOffered = false): EntryChoice {
  if (!userId) return 'online'
  try {
    const v = storage?.getItem(entryStorageKey(userId))
    return v === 'practice' ? 'practice' : v === 'extra' && extraOffered ? 'extra' : 'online'
  } catch { return 'online' }
}

/** Where the page starts: once the online phase has ended, practice unless the offered extra phase was remembered
 * (online stays viewable, read-only). */
export function initialEntryChoice(storage: StorageLike | null, userId: string | null | undefined, onlineEnded: boolean, extraOffered = false): EntryChoice {
  const remembered = readEntryChoice(storage, userId, extraOffered)
  return onlineEnded && remembered !== 'extra' ? 'practice' : remembered
}

export function rememberEntryChoice(storage: StorageLike | null, userId: string | null | undefined, choice: EntryChoice) {
  if (!userId) return
  try { storage?.setItem(entryStorageKey(userId), choice) } catch { /* storage full or blocked */ }
}

/** The phase the evaluate button binds to first. Practice mode is unchanged (beta entry, project board, global). */
export function entryPhaseId(s: CompetitionState, choice: EntryChoice): string | null {
  if (offersExtraSwitch(s) && choice === 'extra') return s.extraPhaseId
  if (offersPracticeSwitch(s) && choice === 'practice') return s.practicePhaseId
  // A team-restricted extra phase is also its team's beta entry (my_observer_phase); it is reached through the
  // extra choice only, so the other choices bind as for everyone else.
  const beta = s.betaPhaseId && s.betaPhaseId === s.extraPhaseId ? null : s.betaPhaseId
  if (s.mode === 'competition') return beta ?? s.phaseId ?? s.projectPhaseId
  return beta ?? s.projectPhaseId ?? s.phaseId
}

/** Phases the workspace may evaluate in (the workflow still keeps only the open ones). */
export function entryPhaseIds(s: CompetitionState): string[] {
  return [s.phaseId, s.betaPhaseId, s.projectPhaseId, s.practicePhaseId, s.extraPhaseId].filter((x): x is string => !!x)
}

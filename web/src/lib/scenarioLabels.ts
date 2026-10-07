/** `public.scenarios.name` carries a single, locale-agnostic value (see PR #194, which set the v4 practice
 * cards' name column to English text) — never show it to contestants directly. Practice cards and hackathon
 * task cards both follow a fixed slug naming scheme, so their display label is derived from the slug here
 * instead, with a proper translation per locale. Anything outside that scheme (legacy or future scenarios)
 * falls back to the DB name as before. */
const GREEK_ORDER = ['alpha', 'beta', 'gamma', 'delta']
const GREEK: Record<string, string> = { alpha: 'α', beta: 'β', gamma: 'γ', delta: 'δ' }

export function scenarioLabel(slug: string, name: string, locale: string): string {
  const practice = /^v4-practice-(alpha|beta|gamma|delta)$/.exec(slug)
  if (practice) {
    const symbol = GREEK[practice[1]!]
    return locale === 'zh' ? `练习卡 ${symbol}` : `Practice card ${symbol}`
  }
  const card = /^v4-([a-h])$/.exec(slug)
  if (card) return taskCardLabel(card[1]!.toUpperCase(), locale)
  // The added cards A1-D1, whatever their version suffix (v4-a1, v4-a1-v1, v4-a1-v2, ...).
  const extra = EXTRA_CARD.exec(slug)
  if (extra) return taskCardLabel(`${extra[1]!.toUpperCase()}1`, locale)
  return name
}

/** Cards A1-D1 run with A-D in an online evaluation; they count only on the super board. Same pattern as
 * private.observer_extra_card in the database (migration 20261005200000). */
const EXTRA_CARD = /^v4-([a-d])1(?:-v\d+)?$/
export const isExtraCard = (slug: string) => EXTRA_CARD.test(slug)

// ja/fr use the English form, like the rest of the card labels.
const taskCardLabel = (letter: string, locale: string) => locale === 'zh' ? `任务卡 ${letter}` : `Card ${letter}`

/** Canonical display order (α, β, γ, δ / A, B, C, D, A1, B1, C1, D1) — alphabetical slug order puts delta before gamma,
 * so callers that list several cards/scenarios (sub-tabs, table columns, team detail rows) must sort on
 * this instead of the slug text. Unrecognized slugs keep a stable relative order (sort is stable; they
 * all compare equal here), placed after every recognized one. */
export function scenarioOrder(slug: string): number {
  const practice = /^v4-practice-(alpha|beta|gamma|delta)$/.exec(slug)
  if (practice) return GREEK_ORDER.indexOf(practice[1]!)
  const card = /^v4-([a-h])$/.exec(slug)
  if (card) return card[1]!.charCodeAt(0) - 'a'.charCodeAt(0)
  // A, B, C, D, A1, B1, C1, D1 (E-H never share a phase with A1-D1).
  const extra = EXTRA_CARD.exec(slug)
  if (extra) return 8 + extra[1]!.charCodeAt(0) - 'a'.charCodeAt(0)
  return Infinity
}

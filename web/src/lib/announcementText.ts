// The title/body of an announcement in the site locale (pure, unit-tested). zh/ja/fr use their own column
// when filled and fall back to English otherwise; ja/fr columns may be missing entirely on older rows.
import type { Locale } from '../composables/useI18n'

type Field = 'title' | 'body'
export type LocalizedAnnouncement = { [K in `${Field}_${'en' | 'zh'}`]: string | null } & { [K in `${Field}_${'ja' | 'fr'}`]?: string | null }

export function announcementText(row: LocalizedAnnouncement, field: Field, locale: Locale): string {
  const en = row[`${field}_en`] ?? ''
  return locale === 'en' ? en : (row[`${field}_${locale}`] || en)
}

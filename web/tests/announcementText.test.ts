import assert from 'node:assert/strict'
import test from 'node:test'
import { announcementText } from '../src/lib/announcementText.ts'

const row = { title_en: 'Hello', title_zh: '你好', body_en: 'Body', body_zh: '正文' }

test('zh/en pick their own column; ja/fr fall back to English when missing or empty', () => {
  assert.equal(announcementText(row, 'title', 'en'), 'Hello')
  assert.equal(announcementText(row, 'body', 'zh'), '正文')
  assert.equal(announcementText(row, 'title', 'ja'), 'Hello')
  assert.equal(announcementText({ ...row, title_fr: '', body_fr: null }, 'body', 'fr'), 'Body')
  assert.equal(announcementText({ ...row, title_ja: 'こんにちは' }, 'title', 'ja'), 'こんにちは')
  assert.equal(announcementText({ ...row, body_zh: '' }, 'body', 'zh'), 'Body')
})

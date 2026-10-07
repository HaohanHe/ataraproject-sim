import assert from 'node:assert/strict'
import test from 'node:test'
import { parseAnnouncement, parseInline } from '../src/lib/announcementMarkdown.ts'

test('bold, inline code and links become segments; unsafe links stay text', () => {
  assert.deepEqual(parseInline('请设置 **OPENAI_MODEL** 为 `kimi-for-coding`，见 [文档](https://x.org/d)'), [
    { kind: 'text', text: '请设置 ' }, { kind: 'bold', text: 'OPENAI_MODEL' }, { kind: 'text', text: ' 为 ' },
    { kind: 'code', text: 'kimi-for-coding' }, { kind: 'text', text: '，见 ' }, { kind: 'link', text: '文档', href: 'https://x.org/d' },
  ])
  assert.deepEqual(parseInline('[x](javascript:alert(1)) <b>hi</b>'), [{ kind: 'text', text: '[x](javascript:alert(1)) <b>hi</b>' }])
  assert.deepEqual(parseInline('2 ** 3'), [{ kind: 'text', text: '2 ** 3' }])
})

test('bare URLs and images still work', () => {
  assert.deepEqual(parseInline('see https://a.org/x. and https://a.org/p.png'), [
    { kind: 'text', text: 'see ' }, { kind: 'link', href: 'https://a.org/x', text: 'https://a.org/x' }, { kind: 'text', text: '. and ' },
    { kind: 'image', href: 'https://a.org/p.png' },
  ])
})

test('paragraphs, headings and lists', () => {
  const blocks = parseAnnouncement('## 更新\n第一行\n第二行\n\n- a **b**\n- c\n1. one\n2) two\n\n结尾')
  assert.deepEqual(blocks.map(b => b.kind), ['heading', 'para', 'ul', 'ol', 'para'])
  assert.deepEqual(blocks[1], { kind: 'para', inlines: [{ kind: 'text', text: '第一行\n第二行' }] })
  assert.equal(blocks[2].kind === 'ul' && blocks[2].items.length, 2)
  assert.deepEqual(parseAnnouncement(''), [])
  assert.deepEqual(parseAnnouncement(null), [])
})

test('foldable sections: first open, later closed, outside text unchanged', () => {
  const items = parseAnnouncement('导语 **新**\n\n::: 10-05 · 新榜单\n- a\n- b\n:::\n\n::: 10-04 · 修复\n第一行\n::: 10-03\n旧内容\n:::\n结尾')
  assert.deepEqual(items.map(b => b.kind), ['para', 'section', 'section', 'section', 'para'])
  const [, s1, s2, s3] = items
  assert.ok(s1.kind === 'section' && s2.kind === 'section' && s3.kind === 'section')
  assert.deepEqual([s1.open, s2.open, s3.open], [true, false, false])
  assert.deepEqual(s1.title, [{ kind: 'text', text: '10-05 · 新榜单' }])
  assert.deepEqual(s1.blocks, [{ kind: 'ul', items: [[{ kind: 'text', text: 'a' }], [{ kind: 'text', text: 'b' }]] }])
  assert.deepEqual(s2.blocks, [{ kind: 'para', inlines: [{ kind: 'text', text: '第一行' }] }])
  assert.deepEqual(s3.blocks, [{ kind: 'para', inlines: [{ kind: 'text', text: '旧内容' }] }])
  assert.deepEqual(items[4], { kind: 'para', inlines: [{ kind: 'text', text: '结尾' }] })
})

test('sections: unclosed runs to the end, stray closer ignored, inline title markup, empty body', () => {
  const items = parseAnnouncement(':::\n::: **v2** `x`\n## 小标题\n正文')
  assert.equal(items.length, 1)
  const s = items[0]
  assert.ok(s.kind === 'section')
  assert.deepEqual(s.title, [{ kind: 'bold', text: 'v2' }, { kind: 'text', text: ' ' }, { kind: 'code', text: 'x' }])
  assert.deepEqual(s.blocks.map(b => b.kind), ['heading', 'para'])
  assert.deepEqual(parseAnnouncement('::: 空\n:::'), [{ kind: 'section', title: [{ kind: 'text', text: '空' }], blocks: [], open: true }])
  // No section markers: identical to the plain blocks.
  assert.deepEqual(parseAnnouncement('a\n\n- b').map(b => b.kind), ['para', 'ul'])
})

import assert from 'node:assert/strict'
import test from 'node:test'
import { accountMenuItems, dashboardQuickLinks } from '../src/lib/accountLinks.ts'
import { deepLinkFor } from '../src/lib/deepLink.ts'

test('the account menu leads to dashboard, team, profile and the API tokens', () => {
  assert.deepEqual(accountMenuItems.map(i => i.to), ['/dashboard', '/team', '/profile', '/profile#api-tokens'])
})

test('dashboard quick links cover tokens, Kimi relay, CLI guide, team requests and WeChat QR', () => {
  assert.deepEqual(dashboardQuickLinks.map(i => i.to), ['/profile#api-tokens', '/profile#kimi-relay', '/cli', '/team#requests', '/profile#wechat-qr'])
})

test('every hash link lands on a known deep-link anchor', () => {
  for (const item of [...accountMenuItems, ...dashboardQuickLinks]) {
    const [path, hash] = item.to.split('#')
    if (hash) assert.ok(deepLinkFor(path!, '#' + hash), item.to)
    assert.ok(item.en && item.zh && item.testid, item.to)
  }
})

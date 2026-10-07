<script setup lang="ts">
// Organizer view of the temporary Kimi relay: switch, limits, key-pool health (slot numbers
// only, never keys) and per-team usage counters (no prompt content is stored).
import { computed, onMounted, ref } from 'vue'
import { useI18n } from '../../composables/useI18n'
import { supabase } from '../../lib/supabase'
import { fmtUtc } from '../../lib/format'

type Team = { team_id: string; team_name: string; is_hidden: boolean; requests: number; ok: number; errors: number; users: number; tokens: number; last_at: string }
type Key = { slot: number; state: string; cooling_until: string | null; last_status: number | null; last_error: string | null; last_error_at: string | null; last_used_at: string | null; uses: number; ok_today: number }
type View = { config: Record<string, unknown> & { enabled: boolean }; teams: Team[]; keys: Key[] }
const { pick } = useI18n()
const view = ref<View | null>(null), days = ref(1), busy = ref(false), failed = ref(false)
const w = computed(() => pick({
  title: 'Temporary Kimi relay', on: 'On', off: 'Off', turnOn: 'Turn on', turnOff: 'Turn off (kill switch)',
  confirmOff: 'Turn the Kimi relay off for everyone now?', limits: 'Limits', keys: 'Key pool', usage: 'Usage by team',
  slot: 'Key', state: 'State', until: 'Cooling until', lastErr: 'Last error', okToday: 'OK today', uses: 'Picks',
  team: 'Team', req: 'Requests', ok: 'OK', err: 'Errors', users: 'Users', tokens: 'Tokens', last: 'Last', today: 'Today', week: '7 days',
  none: 'No usage.', edit: 'Limits and eligibility: update private.kimi_relay_config (SQL).', failed: 'Could not load.',
}, {
  title: '临时 Kimi 中转', on: '已开启', off: '已关闭', turnOn: '开启', turnOff: '关闭（总开关）',
  confirmOff: '立即为所有人关闭 Kimi 中转？', limits: '限额', keys: '密钥池', usage: '各队用量',
  slot: '密钥', state: '状态', until: '冷却至', lastErr: '最近错误', okToday: '今日成功', uses: '选用次数',
  team: '队伍', req: '请求', ok: '成功', err: '失败', users: '人数', tokens: 'Tokens', last: '最近', today: '今天', week: '7 天',
  none: '暂无用量。', edit: '限额与资格：修改 private.kimi_relay_config（SQL）。', failed: '加载失败。',
}))
const limits = computed(() => {
  const c = view.value?.config
  return c ? `${c.daily_requests} req / ${Number(c.daily_tokens).toLocaleString()} tokens / team / day · ${c.max_concurrent} concurrent · max_tokens ${c.max_tokens} · eligibility ${JSON.stringify(c.eligibility)}` : ''
})
async function load() {
  const { data, error } = await supabase.rpc('admin_kimi_relay', { p_days: days.value })
  failed.value = !!error
  if (!error) view.value = data as View
}
async function toggle() {
  if (!view.value || busy.value) return
  const next = !view.value.config.enabled
  if (!next && !window.confirm(w.value.confirmOff)) return
  busy.value = true
  try { await supabase.rpc('admin_kimi_relay_set_enabled', { p_enabled: next }); await load() } finally { busy.value = false }
}
const when = (v: string | null) => v ? fmtUtc(v) : '—'
onMounted(() => { void load() })
</script>

<template>
  <div class="panel mt-8" data-testid="admin-kimi-relay">
    <div class="hd"><h2>{{ w.title }}</h2>
      <span v-if="view" class="pill" :class="view.config.enabled ? 'ok' : 'bad'">{{ view.config.enabled ? w.on : w.off }}</span></div>
    <p v-if="failed" class="text3 text-sm">{{ w.failed }}</p>
    <template v-if="view">
      <div class="actions-inline">
        <button type="button" class="btn sm" :class="view.config.enabled ? 'danger' : 'primary'" :disabled="busy" @click="toggle">{{ view.config.enabled ? w.turnOff : w.turnOn }}</button>
        <select v-model.number="days" class="ml-2" @change="load"><option :value="1">{{ w.today }}</option><option :value="7">{{ w.week }}</option></select>
      </div>
      <p class="text3 mt-3 text-xs m break-all">{{ w.limits }}: {{ limits }}<br>{{ w.edit }}</p>
      <h3 class="mt-4 text-sm">{{ w.keys }}</h3>
      <div class="table-wrap">
        <table class="data-table">
          <thead><tr><th>{{ w.slot }}</th><th>{{ w.state }}</th><th>{{ w.until }}</th><th>{{ w.lastErr }}</th><th class="r">{{ w.okToday }}</th><th class="r">{{ w.uses }}</th></tr></thead>
          <tbody>
            <tr v-for="k in view.keys" :key="k.slot">
              <td class="m">#{{ k.slot }}</td>
              <td><span class="pill" :class="k.state === 'healthy' ? 'ok' : 'warn'">{{ k.state }}</span></td>
              <td class="text-xs">{{ k.state === 'cooling' ? when(k.cooling_until) : '—' }}</td>
              <td class="text-xs">{{ k.last_error ? `${k.last_status ?? ''} ${k.last_error} · ${when(k.last_error_at)}` : '—' }}</td>
              <td class="r m">{{ k.ok_today }}</td>
              <td class="r m">{{ k.uses }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <h3 class="mt-4 text-sm">{{ w.usage }}</h3>
      <p v-if="!view.teams.length" class="text3 text-sm">{{ w.none }}</p>
      <div v-else class="table-wrap">
        <table class="data-table">
          <thead><tr><th>{{ w.team }}</th><th class="r">{{ w.req }}</th><th class="r">{{ w.ok }}</th><th class="r">{{ w.err }}</th><th class="r">{{ w.users }}</th><th class="r">{{ w.tokens }}</th><th>{{ w.last }}</th></tr></thead>
          <tbody>
            <tr v-for="r in view.teams" :key="r.team_id">
              <td>{{ r.team_name }}<span v-if="r.is_hidden" class="text3 text-xs"> (hidden)</span></td>
              <td class="r m">{{ r.requests }}</td><td class="r m">{{ r.ok }}</td><td class="r m">{{ r.errors }}</td>
              <td class="r m">{{ r.users }}</td><td class="r m">{{ Number(r.tokens).toLocaleString() }}</td>
              <td class="text-xs">{{ when(r.last_at) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>
  </div>
</template>

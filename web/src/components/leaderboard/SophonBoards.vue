<script setup lang="ts">
// Sophon tab: finishers, a flag box and three boards. Every label, score and text comes from the database
// or the sophon function; this file holds no content of its own.
import { computed, onMounted, ref } from 'vue'
import { useI18n } from '../../composables/useI18n'
import { supabase } from '../../lib/supabase'
import { useAuth } from '../../stores/auth'
import { fmtUtc } from '../../lib/format'

type Cell = { board: string; team_name: string; flag: string; solved_at: string; points: number; blood: number | null }
type Total = { team_name: string; debug: number; auto: number; total: number; last_at: string }
type Finisher = { board: string; team_name: string; finished_at: string; text_en: string; text_zh: string }

const { pick, locale } = useI18n()
const auth = useAuth()
const cells = ref<Cell[]>([])
const totals = ref<Total[]>([])
const finishers = ref<Finisher[]>([])
const tab = ref<'debug' | 'auto' | 'total'>('total')
const flag = ref('')
const reply = ref<string | null>(null)
const busy = ref(false)

async function load() {
  const [b, t, f] = await Promise.all([supabase.rpc('sophon_board'), supabase.rpc('sophon_totals'), supabase.rpc('sophon_finishers')])
  cells.value = !b.error && Array.isArray(b.data) ? b.data : []
  totals.value = !t.error && Array.isArray(t.data) ? t.data : []
  finishers.value = !f.error && Array.isArray(f.data) ? f.data : []
}
onMounted(load)

const boardLabel = (b: string) => b === 'auto' ? pick('Automation', '自动化榜') : b === 'debug' ? pick('Debug', '调试榜') : pick('Total', '总榜')
const columns = computed(() => [...new Set(cells.value.filter(c => c.board === tab.value).map(c => c.flag))]
  .sort((a, b) => Number(a.slice(1)) - Number(b.slice(1))))
const rows = computed(() => {
  if (tab.value === 'total') return totals.value.map(r => ({ team: r.team_name, score: r.total, last: r.last_at, cells: {} as Record<string, Cell> }))
  const by = new Map<string, { team: string; score: number; last: string; cells: Record<string, Cell> }>()
  for (const c of cells.value.filter(x => x.board === tab.value)) {
    const r = by.get(c.team_name) ?? { team: c.team_name, score: 0, last: c.solved_at, cells: {} }
    r.score += c.points; r.cells[c.flag] = c; if (c.solved_at > r.last) r.last = c.solved_at
    by.set(c.team_name, r)
  }
  return [...by.values()].sort((a, b) => b.score - a.score || a.last.localeCompare(b.last))
})
const bloodMark = (n: number | null) => n === 1 ? pick('1st blood', '一血') : n === 2 ? pick('2nd blood', '二血') : n === 3 ? pick('3rd blood', '三血') : ''

async function submit() {
  if (!flag.value.trim()) return
  busy.value = true
  try {
    const { data, error } = await supabase.functions.invoke('sophon/desk/flag', { body: { flag: flag.value.trim() }, headers: { 'x-sophon-site': '1' } })
    const body = data ?? (error && 'context' in error ? await (error as { context: Response }).context.json().catch(() => null) : null)
    reply.value = body?.message ?? pick('No answer.', '没有回应。')
    if (body?.solved) { flag.value = ''; await load() }
  } finally { busy.value = false }
}
async function download(name: string) {
  const { data } = await supabase.functions.invoke('sophon/desk/files/' + name, { method: 'GET', headers: { 'x-sophon-site': '1' } })
  if (data?.url) window.location.href = data.url
}
</script>

<template>
  <div data-testid="sophon-boards">
    <section v-if="finishers.length" class="mb-8" data-testid="sophon-finishers">
      <p class="label mb-3">{{ pick('Mission log', '通关记录') }}</p>
      <article v-for="f in finishers" :key="f.board + f.team_name" class="card mb-3 p-4" data-testid="sophon-finisher">
        <p class="label text3 mb-2">{{ boardLabel(f.board) }}</p>
        <pre class="m whitespace-pre-wrap text-sm">{{ locale === 'zh' ? f.text_zh : f.text_en }}</pre>
      </article>
    </section>
    <section v-if="auth.state.session" class="mb-8" data-testid="sophon-flagbox">
      <form class="flex flex-wrap gap-2" @submit.prevent="submit">
        <input v-model="flag" class="input m flex-1" :placeholder="'SOPHON{...}'" autocomplete="off" spellcheck="false" />
        <button type="submit" class="btn" :disabled="busy">{{ pick('Hand in', '提交') }}</button>
      </form>
      <p v-if="reply" class="text2 mt-2 text-sm" data-testid="sophon-reply">{{ reply }}</p>
      <p class="mt-3 flex flex-wrap gap-3 text-sm">
        <button type="button" class="btn sm" @click="download('sophon-card-public.zip')">{{ pick('Card files', '卡片公开文件') }}</button>
        <button type="button" class="btn sm" @click="download('sophon-local.zip')">{{ pick('Local card', '本地卡') }}</button>
      </p>
    </section>
    <div class="mb-4 flex gap-2">
      <button v-for="b in (['total', 'debug', 'auto'] as const)" :key="b" type="button" class="btn sm" :class="{ primary: tab === b }" @click="tab = b">{{ boardLabel(b) }}</button>
    </div>
    <div class="table-wrap">
      <table class="data-table" data-testid="sophon-board">
        <thead>
          <tr><th>#</th><th>{{ pick('Team', '队伍') }}</th><th class="r">{{ pick('Points', '分数') }}</th>
            <template v-if="tab === 'total'"><th class="r">{{ boardLabel('debug') }}</th><th class="r">{{ boardLabel('auto') }}</th></template>
            <th v-for="c in columns" :key="c" class="r m">{{ c }}</th></tr>
        </thead>
        <tbody>
          <tr v-for="(r, i) in rows" :key="r.team">
            <td class="m">{{ i + 1 }}</td><td>{{ r.team }}</td><td class="r m">{{ r.score }}</td>
            <template v-if="tab === 'total'"><td class="r m">{{ totals[i]?.debug }}</td><td class="r m">{{ totals[i]?.auto }}</td></template>
            <td v-for="c in columns" :key="c" class="r m text-xs">
              <template v-if="r.cells[c]">{{ fmtUtc(r.cells[c].solved_at) }}<br><span class="text3">{{ r.cells[c].points }}</span> <span v-if="r.cells[c].blood" class="label accent">{{ bloodMark(r.cells[c].blood) }}</span></template>
            </td>
          </tr>
          <tr v-if="!rows.length"><td :colspan="3 + columns.length" class="text3 py-8 text-center">—</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<script setup lang="ts">
// Temporary organizer Kimi relay for local development (kimi-relay edge function). Shown while
// the relay is switched on; eligible teams (on the leaderboard, or organizer test teams) see the
// base URL and today's remaining team allowance. Callers use their personal API token.
import { computed, onMounted, ref } from 'vue'
import { useI18n } from '../../composables/useI18n'
import { supabase } from '../../lib/supabase'

type Relay = {
  enabled: boolean; has_team: boolean; eligible: boolean
  daily_requests: number; daily_tokens: number; max_concurrent: number; max_tokens: number
  used_requests: number; used_tokens: number
}
const BASE = 'https://vdiemcofukuxglqsmlyz.supabase.co/functions/v1/kimi-relay/v1'
const MODEL = 'kimi-for-coding'
const { pick } = useI18n()
const relay = ref<Relay | null>(null)
const copied = ref(false)
const w = computed(() => pick({
  title: 'Temporary Kimi relay (for development)', kicker: 'Local development',
  notice: "This is a temporary Kimi allowance from the organizers to help with local development and debugging. It's limited and may change or end at any time. Platform evaluations and the final use the model service each team saves in 'Keys and network' — please make sure yours is set up.",
  how: 'OpenAI-compatible. Use it from your own machine: base URL below, your personal API token (s26_…) as the API key, model {model}.',
  base: 'Base URL', copy: 'Copy', copied: 'Copied', token: 'Create a personal API token below if you do not have one.',
  remaining: "Your team's allowance today", requests: 'requests', tokens: 'tokens', limits: 'Up to {c} concurrent requests per team; max_tokens is capped at {m}. Resets daily at 00:00 UTC.',
  noTeam: 'Join a team first.', notEligible: 'Available once your team is on the leaderboard (one scored formal evaluation in the online phase).',
}, {
  title: '平台临时 Kimi 中转（开发用）', kicker: '本地开发',
  notice: '这是组委会临时提供的 Kimi 额度，方便大家本地开发调试，额度有限，可能随时调整或结束。正式评测和决赛会使用各队在「密钥与网络」里保存的模型服务，记得提前配置好哦。',
  how: '兼容 OpenAI 接口，在你自己的电脑上使用：接口地址见下，API key 填你的个人 API 令牌（s26_…），模型名 {model}。',
  base: '接口地址（base URL）', copy: '复制', copied: '已复制', token: '还没有令牌的话，在下方「个人 API 令牌」创建一个。',
  remaining: '本队今天剩余', requests: '次请求', tokens: 'tokens', limits: '每队最多同时 {c} 个请求；max_tokens 上限 {m}。每天 UTC 0 点（北京时间 8 点）重置。',
  noTeam: '请先加入队伍。', notEligible: '上榜后即可使用（正式赛有一次成功评测）。',
}))
const left = computed(() => relay.value ? {
  requests: Math.max(0, relay.value.daily_requests - relay.value.used_requests),
  tokens: Math.max(0, relay.value.daily_tokens - relay.value.used_tokens),
} : null)
const fmt = (n: number) => n.toLocaleString()
const example = computed(() => `export OPENAI_BASE_URL=${BASE}
export OPENAI_API_KEY=s26_...   # ${pick('your personal API token', '你的个人 API 令牌')}
curl $OPENAI_BASE_URL/chat/completions -H "Authorization: Bearer $OPENAI_API_KEY" \\
  -H 'Content-Type: application/json' \\
  -d '{"model":"${MODEL}","messages":[{"role":"user","content":"hi"}]}'`)

async function copy() {
  try { await navigator.clipboard.writeText(BASE); copied.value = true; window.setTimeout(() => { copied.value = false }, 2000) } catch { /* select manually */ }
}
onMounted(async () => {
  try {
    const { data, error } = await supabase.rpc('my_kimi_relay')
    if (!error && data) relay.value = data as Relay
  } catch { /* feature absent: show nothing */ }
})
</script>

<template>
  <div v-if="relay?.enabled" id="kimi-relay" class="panel mt-6" data-testid="kimi-relay-panel">
    <div class="hd"><h2>{{ w.title }}</h2><span class="label">{{ w.kicker }}</span></div>
    <p class="text-sm"><strong>{{ w.notice }}</strong></p>
    <p v-if="!relay.has_team" class="text3 mt-3 text-sm" data-testid="kimi-relay-no-team">{{ w.noTeam }}</p>
    <p v-else-if="!relay.eligible" class="text3 mt-3 text-sm" data-testid="kimi-relay-not-eligible">{{ w.notEligible }}</p>
    <template v-else>
      <p class="text2 mt-3 text-sm">{{ w.how.replace('{model}', MODEL) }} {{ w.token }}</p>
      <div class="mt-3">
        <span class="text3 text-xs">{{ w.base }}</span>
        <div class="actions-inline mt-1">
          <code class="credits-code break-all" data-testid="kimi-relay-base">{{ BASE }}</code>
          <button type="button" class="copy-btn" @click="copy">{{ copied ? w.copied : w.copy }}</button>
        </div>
      </div>
      <pre class="mt-3 text-xs overflow-x-auto"><code>{{ example }}</code></pre>
      <p v-if="left" class="mt-3 text-sm" data-testid="kimi-relay-remaining">
        {{ w.remaining }}: <strong>{{ fmt(left.requests) }}</strong> / {{ fmt(relay.daily_requests) }} {{ w.requests }} ·
        <strong>{{ fmt(left.tokens) }}</strong> / {{ fmt(relay.daily_tokens) }} {{ w.tokens }}
      </p>
      <p class="text3 mt-1 text-xs">{{ w.limits.replace('{c}', String(relay.max_concurrent)).replace('{m}', String(relay.max_tokens)) }}</p>
    </template>
  </div>
</template>

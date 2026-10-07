<script setup lang="ts">
import { ref, watch, nextTick, onMounted, onUnmounted } from 'vue'
import { unreadTeamNotifications, pendingTeamActions, pendingFriendRequests, refreshTeamNotifications, clearTeamNotifications } from '../../stores/teamNotifications'
import { badgeText, bellCount as countForBell, bellLabel, bellTarget, pendingTotal } from '../../lib/notificationBadge'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from '../../composables/useI18n'
import { useAuth } from '../../stores/auth'
import { useFlash } from '../../stores/flash'
import { useRegistrationOpen } from '../../composables/useRegistrationOpen'
import { usePhaseClock } from '../../composables/usePhaseClock'
import { isFullMoonToday } from '../../lib/eggs'
import { mainNavItems, maxFold, moreNavItems, participateItem, type NavItem } from '../../lib/nav'
import { computed } from 'vue'
import { accountMenuItems } from '../../lib/accountLinks'
import UserAvatar from '../UserAvatar.vue'

const { t, pick, toggleLocale, locale } = useI18n()
const route = useRoute()
const router = useRouter()
const { isLoggedIn, isAdmin, signOut, me } = useAuth()
let notificationTimer: ReturnType<typeof setInterval> | undefined
function pollNotifications() { if (isLoggedIn.value && document.visibilityState === 'visible') void refreshTeamNotifications() }
watch(isLoggedIn, logged => { if (logged) void refreshTeamNotifications(); else clearTeamNotifications() }, {immediate:true})
onMounted(() => {
  notificationTimer = setInterval(pollNotifications, 60000)
  document.addEventListener('visibilitychange', pollNotifications)
  window.addEventListener('focus', pollNotifications)
})
onUnmounted(() => {
  if (notificationTimer) clearInterval(notificationTimer)
  document.removeEventListener('visibilitychange', pollNotifications)
  window.removeEventListener('focus', pollNotifications)
})
// Waiting requests stay counted until answered; otherwise the dot shows unread updates.
const badge = computed(() => ({ team: pendingTeamActions.value, friends: pendingFriendRequests.value, unread: unreadTeamNotifications.value }))
const bellCount = computed(() => countForBell(badge.value))
const pendingAll = computed(() => pendingTotal(badge.value))
const bellText = computed(() => { const l = bellLabel(badge.value); return pick(l.en, l.zh) })
const { registrationOpen } = useRegistrationOpen()
const flash = useFlash()
const mobileOpen = ref(false)
const nextLocaleLabel = computed(() => ({ zh: 'EN', en: '日本語', ja: 'FR', fr: '中文' } as const)[locale.value])
const fullMoon = isFullMoonToday()
const { nextLine } = usePhaseClock()

const items = mainNavItems
const more = moreNavItems
// When the desktop header row does not fit (narrow screens, long labels, the extra links of a
// signed-in user), main links move under More by their `fold` order, only as many as needed.
const row = ref<HTMLElement | null>(null)
const foldLevel = ref(0)
const folded = (item: NavItem) => !!item.fold && item.fold <= foldLevel.value
const foldedItems = computed(() => items.filter(folded))
const overflowing = () => { const el = row.value; return !!el && el.scrollWidth > el.clientWidth + 1 }
async function fitRow() {
  foldLevel.value = 0
  await nextTick()
  while (overflowing() && foldLevel.value < maxFold) {
    foldLevel.value += 1
    await nextTick()
  }
}
let rowObserver: ResizeObserver | undefined
onMounted(() => {
  void fitRow()
  void document.fonts?.ready.then(() => fitRow())
  if (row.value && typeof ResizeObserver !== 'undefined') {
    let width = 0
    rowObserver = new ResizeObserver(([entry]) => {
      const next = Math.round(entry?.contentRect.width ?? 0)
      if (next !== width) { width = next; void fitRow() }
    })
    rowObserver.observe(row.value)
  }
})
onUnmounted(() => rowObserver?.disconnect())
watch([locale, isLoggedIn, isAdmin], () => { void fitRow() })
const moreOpen = ref(false)
const seriesOpen = ref(false)
type SeriesItem = { n: string; name: string; sub: string; href: string; current: boolean }
const seriesItems = computed(() => t('nav.series.items') as SeriesItem[])
const isActive = (to: string) => route.path === to || route.path.startsWith(`${to}/`)
const moreActive = () => [...more, ...foldedItems.value].some(item => isActive(item.to))
const dashActive = () => ['/dashboard', '/team', '/compete', '/submissions', '/profile'].some(p => route.path.startsWith(p))
// Account menu (avatar, top right): dashboard, team, profile, API tokens, sign out.
const accountOpen = ref(false)
const accountEl = ref<HTMLElement | null>(null)
const accountName = computed(() => me.value?.nickname || me.value?.name || me.value?.email || '')
function closeAccountOnOutside(event: Event) {
  if (accountOpen.value && accountEl.value && !accountEl.value.contains(event.target as Node)) accountOpen.value = false
}
onMounted(() => document.addEventListener('click', closeAccountOnOutside))
onUnmounted(() => document.removeEventListener('click', closeAccountOnOutside))
watch(() => route.fullPath, () => { mobileOpen.value = false; moreOpen.value = false; seriesOpen.value = false; accountOpen.value = false })

async function logout() {
  mobileOpen.value = false
  accountOpen.value = false
  await signOut()
  flash.success(t('auth.logout_done'))
  router.push('/')
}
</script>

<template>
  <header class="cosmos-header sticky top-0 z-50 border-b border-border backdrop-blur">
    <div ref="row" class="mx-auto flex h-16 max-w-[1600px] items-center justify-between gap-2 px-3 sm:gap-6 sm:px-5 md:px-10 lg:gap-4 xl:gap-6 xl:px-14">
      <div class="flex items-center gap-3">
        <router-link to="/" :aria-label="`${t('meta.brand')} · ${t('meta.pages.home.title')}`" class="flex items-center gap-3">
          <span class="cosmos-wordmark shrink-0 whitespace-nowrap text-lg text-[#f5f5f5]">GOSIM <span class="hidden sm:inline text-[#315efb]">Create</span></span>
        </router-link>
        <span class="hidden h-4 w-px bg-white/25 sm:block"></span>
        <div class="series-drop relative hidden sm:block" @mouseenter="seriesOpen = true" @mouseleave="seriesOpen = false">
          <button type="button" class="inline-flex h-10 items-center gap-1.5 whitespace-nowrap font-mono text-xs uppercase tracking-[.1em] text-white/70 transition-colors hover:text-white" :aria-expanded="seriesOpen" @click="seriesOpen = !seriesOpen">
            {{ t('meta.wordmark_note') }} <span aria-hidden="true" class="text-[.6rem] transition-transform" :class="{ 'rotate-180': seriesOpen }">▾</span>
          </button>
          <div v-show="seriesOpen" class="series-panel">
            <div class="series-head"><span>{{ t('nav.series.label') }}</span><span>{{ t('nav.series.pick') }} ↓</span></div>
            <component :is="item.href ? 'a' : 'router-link'" v-for="item in seriesItems" :key="item.n"
              :href="item.href || undefined" :to="item.href ? undefined : '/'"
              class="series-item" :class="{ current: item.current }">
              <span class="series-n">{{ item.n }}</span>
              <span class="min-w-0">
                <b>{{ item.name }}</b>
                <small>{{ item.sub }}</small>
              </span>
              <span v-if="item.current" class="series-current">{{ t('nav.series.current') }}</span>
            </component>
          </div>
        </div>
      </div>

      <nav class="hidden items-center gap-3 lg:flex xl:gap-5">
        <router-link
          v-for="item in items"
          :key="item.to"
          :to="item.to"
          class="main-link inline-flex h-10 items-center whitespace-nowrap font-mono text-xs uppercase tracking-[.06em] transition-colors hover:text-[#78a6ff]"
          :class="isActive(item.to) ? 'text-[#78a6ff]' : 'text-white/50'"
          v-show="!folded(item)"
        >{{ t(item.key) }}</router-link>
        <div class="nav-drop relative" @mouseenter="moreOpen = true" @mouseleave="moreOpen = false">
          <button type="button" class="main-link inline-flex h-10 items-center gap-1 whitespace-nowrap font-mono text-xs uppercase tracking-[.06em] transition-colors hover:text-[#78a6ff]" :class="moreActive() ? 'text-[#78a6ff]' : 'text-white/50'" :aria-expanded="moreOpen" data-testid="nav-more" @click="moreOpen = !moreOpen">
            {{ t('nav.more') }} <span aria-hidden="true" class="text-[.6rem]">▾</span>
          </button>
          <div v-show="moreOpen" class="nav-drop-panel">
            <router-link v-for="item in foldedItems" :key="item.to" :to="item.to" class="nav-drop-item" :class="{ active: isActive(item.to) }">{{ t(item.key) }}</router-link>
            <router-link v-for="item in more" :key="item.to" :to="item.to" class="nav-drop-item" :class="{ active: isActive(item.to) }">{{ t(item.key) }}</router-link>
            <router-link v-if="isLoggedIn" to="/teammates#find-uid" class="nav-drop-item" data-testid="nav-find-uid">{{ t('nav.find_uid') }}</router-link>
            <router-link v-if="isLoggedIn" to="/profile#wechat-qr" class="nav-drop-item" data-testid="nav-wechat-qr">{{ t('nav.wechat_qr') }}</router-link>
          </div>
        </div>
        <router-link v-if="isAdmin" to="/admin" class="inline-flex h-10 items-center font-mono text-xs uppercase tracking-[.06em] transition-colors hover:text-[#78a6ff]" :class="route.path.startsWith('/admin') ? 'text-[#78a6ff]' : 'text-white/50'">{{ t('nav.admin') }}</router-link>
      </nav>

      <div class="flex items-center gap-1 sm:gap-2">
        <router-link :to="participateItem.to" class="inline-flex h-10 shrink-0 items-center justify-center whitespace-nowrap bg-[#315efb] px-2 sm:px-3 text-sm font-semibold text-white hover:bg-[#244bda]" data-testid="primary-submit">{{ t(participateItem.key) }}</router-link>
        <router-link v-if="isLoggedIn" to="/dashboard" class="dash-btn hidden h-10 shrink-0 items-center justify-center whitespace-nowrap border px-3 text-sm font-semibold sm:inline-flex" :class="{ active: dashActive() }" data-testid="nav-dashboard">{{ t('nav.dashboard') }}<span v-if="pendingAll" class="nav-count" data-testid="nav-dashboard-count">{{ badgeText(pendingAll) }}</span></router-link>
        <router-link v-if="isLoggedIn" :to="bellTarget(badge)" class="relative flex h-10 w-10 shrink-0 items-center justify-center text-white/80"
          :aria-label="bellText" :title="bellText" data-testid="team-notifications">
          <svg aria-hidden="true" class="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4"/></svg>
          <span v-if="bellCount" class="absolute right-0 top-0 flex h-4 min-w-4 items-center justify-center rounded-full bg-red-500 px-1 text-[10px] text-white" data-testid="notification-dot">{{ badgeText(bellCount) }}</span>
        </router-link>
        <span v-if="fullMoon" class="moon-chip hidden md:inline-flex" :title="pick('Full moon tonight.', '今晚满月。')">🌕</span>
        <button data-testid="lang-toggle" type="button" @click="toggleLocale" class="inline-flex h-10 min-w-10 items-center justify-center whitespace-nowrap border border-white/25 px-2 font-mono text-xs uppercase text-white/55 transition-colors hover:border-white/60 hover:text-white">
          {{ nextLocaleLabel }}
        </button>
        <div v-if="isLoggedIn" ref="accountEl" class="relative">
          <button type="button" class="account-trigger flex h-10 shrink-0 items-center gap-1 pl-1 text-white/70 hover:text-white" :aria-expanded="accountOpen" aria-haspopup="menu"
            :aria-label="pick('Account menu', '账号菜单')" :title="accountName" data-testid="account-menu" @click="accountOpen = !accountOpen">
            <UserAvatar :name="accountName || '?'" :github="me?.github" :avatar-url="me?.avatar_url" />
            <span aria-hidden="true" class="hidden text-[.6rem] sm:inline" :class="{ 'rotate-180': accountOpen }">▾</span>
          </button>
          <div v-show="accountOpen" class="account-panel" role="menu" data-testid="account-panel">
            <p class="account-head">{{ accountName }}</p>
            <router-link v-for="item in accountMenuItems" :key="item.to" :to="item.to" role="menuitem" class="account-item" :data-testid="item.testid">{{ pick(item.en, item.zh) }}<span v-if="item.to === '/team' && pendingTeamActions" class="nav-count">{{ badgeText(pendingTeamActions) }}</span></router-link>
            <button type="button" role="menuitem" class="account-item account-logout" data-testid="nav-logout" @click="logout">{{ t('nav.logout') }}</button>
          </div>
        </div>
        <router-link v-else-if="registrationOpen" data-testid="nav-register" to="/register" class="cosmos-register-link ml-1 hidden h-10 items-center whitespace-nowrap border px-4 font-mono text-xs font-semibold uppercase tracking-widest md:inline-flex">{{ t('nav.register') }}</router-link>
        <router-link v-else data-testid="nav-register" to="/register?mode=login" class="cosmos-register-link ml-1 hidden h-10 items-center whitespace-nowrap border px-4 font-mono text-xs font-semibold uppercase tracking-widest md:inline-flex">{{ t('nav.login') }}</router-link>
        <button class="relative ml-1 lg:hidden" type="button" @click="mobileOpen = !mobileOpen" :aria-label="t('nav.menu')" :aria-expanded="mobileOpen">
          <svg class="h-6 w-6 text-text-primary" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M4 7h16M4 12h16M4 17h16"/></svg>
          <span v-if="pendingAll" class="absolute -right-1 -top-1 h-2.5 w-2.5 rounded-full bg-red-500" data-testid="menu-dot"></span>
        </button>
      </div>
    </div>

    <div v-if="mobileOpen" class="max-h-[calc(100dvh-4rem)] overflow-y-auto border-t border-white/20 bg-[#070708] px-5 pb-20 pt-4 lg:hidden" data-testid="mobile-menu">
      <div v-if="isLoggedIn" class="mobile-account" data-testid="mobile-account">
        <router-link to="/dashboard" class="mobile-dash" data-testid="mobile-dashboard">{{ t('nav.dashboard') }}<span v-if="pendingAll" class="nav-count">{{ badgeText(pendingAll) }}</span><span aria-hidden="true" class="ml-auto">→</span></router-link>
        <div class="mobile-account-links">
          <router-link v-for="item in accountMenuItems.slice(1)" :key="item.to" :to="item.to" :data-testid="`mobile-${item.testid}`">{{ pick(item.en, item.zh) }}</router-link>
        </div>
      </div>
      <p v-if="nextLine" class="mb-2 font-mono text-[.68rem] leading-relaxed tracking-[.06em] text-white/60" data-testid="menu-next-phase">{{ nextLine }}</p>
      <router-link v-for="item in items" :key="item.to" :to="item.to" class="block border-b border-white/10 py-3 text-base text-white/60 transition-colors hover:text-white">{{ t(item.key) }}</router-link>
      <router-link :to="participateItem.to" class="block border-b border-white/10 py-3 text-base text-white/60 transition-colors hover:text-white">{{ t(participateItem.key) }}</router-link>
      <router-link v-if="isLoggedIn" to="/teammates#find-uid" class="block border-b border-white/10 py-3 text-base text-white/60 transition-colors hover:text-white" data-testid="mobile-find-uid">{{ t('nav.find_uid') }}</router-link>
      <router-link v-if="isLoggedIn" to="/profile#wechat-qr" class="block border-b border-white/10 py-3 text-base text-white/60 transition-colors hover:text-white" data-testid="mobile-wechat-qr">{{ t('nav.wechat_qr') }}</router-link>
      <router-link v-if="isLoggedIn && pendingTeamActions" to="/team#requests" class="block border-b border-white/10 py-3 text-base text-white transition-colors" data-testid="mobile-team-requests">{{ pick('Team requests waiting for you', '待处理的组队请求') }} <span class="nav-count">{{ badgeText(pendingTeamActions) }}</span></router-link>
      <router-link v-if="isLoggedIn && pendingFriendRequests" to="/profile#friends" class="block border-b border-white/10 py-3 text-base text-white transition-colors" data-testid="mobile-friend-requests">{{ pick('Friend requests waiting for you', '待处理的好友请求') }} <span class="nav-count">{{ badgeText(pendingFriendRequests) }}</span></router-link>
      <router-link v-if="isAdmin" to="/admin" class="block border-b border-white/10 py-3 text-base text-white/60 transition-colors hover:text-white">{{ t('nav.admin') }}</router-link>
      <p class="mt-4 mb-1 font-mono text-[.62rem] uppercase tracking-[.14em] text-white/35">{{ t('nav.series.label') }}</p>
      <template v-for="item in seriesItems" :key="item.n">
        <a v-if="item.href" :href="item.href" class="block border-b border-white/10 py-3 text-base text-white/60 transition-colors hover:text-white">{{ item.name }} <small class="text-white/35">{{ item.sub }}</small></a>
      </template>
      <button v-if="isLoggedIn" type="button" @click="logout" class="mt-3 block w-full border border-white/35 px-4 py-3 text-center font-mono text-xs font-semibold uppercase tracking-widest text-[#f5f5f5]">{{ t('nav.logout') }}</button>
      <template v-else>
        <router-link v-if="registrationOpen" to="/register" class="cosmos-register-link mt-3 block border px-4 py-3 text-center font-mono text-xs font-semibold uppercase tracking-widest">{{ t('nav.register') }}</router-link>
        <router-link to="/register?mode=login" class="mt-3 block border border-white/35 px-4 py-3 text-center font-mono text-xs font-semibold uppercase tracking-widest text-[#f5f5f5]">{{ t('nav.login') }}</router-link>
      </template>
      <p class="mt-4 mb-1 font-mono text-[.62rem] uppercase tracking-[.14em] text-white/35" data-testid="mobile-more">{{ t('nav.more') }}</p>
      <router-link v-for="item in more" :key="item.to" :to="item.to" class="block border-b border-white/10 py-3 text-base text-white/60 transition-colors hover:text-white">{{ t(item.key) }}</router-link>
    </div>
  </header>
</template>

<style scoped>
.nav-count { display: inline-flex; align-items: center; justify-content: center; min-width: 1.1rem; height: 1.1rem; margin-left: .35rem; padding: 0 .3rem;
  border-radius: 999px; background: #ef4444; color: #fff; font-size: .65rem; line-height: 1; letter-spacing: 0; }
@media (max-width: 1279px) { .main-link { letter-spacing: .02em; } }
.dash-btn { border-color: rgba(120,166,255,.7); color: #cfe0ff; background: rgba(49,94,251,.12); transition: background .15s ease, border-color .15s ease, color .15s ease; }
.dash-btn:hover, .dash-btn.active { border-color: #78a6ff; color: #fff; background: rgba(49,94,251,.28); }
.account-panel { position: absolute; top: 100%; right: 0; min-width: 18rem; padding: .35rem 0; z-index: 60;
  border: 1px solid rgba(148,163,255,.25); background: rgba(6,9,18,.97); backdrop-filter: blur(8px); -webkit-backdrop-filter: blur(8px); }
.account-head { padding: .55rem 1.1rem .5rem; border-bottom: 1px solid rgba(255,255,255,.12); margin-bottom: .25rem;
  font-size: .78rem; color: rgba(255,255,255,.45); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 18rem; }
.account-item { display: flex; align-items: center; width: 100%; padding: .6rem 1.1rem; text-align: left; font-size: .86rem; color: rgba(255,255,255,.78);
  transition: color .15s ease, background .15s ease; }
.account-item:hover { color: #fff; background: rgba(49,94,251,.18); }
.account-item.router-link-exact-active { color: #78a6ff; }
.account-logout { border-top: 1px solid rgba(255,255,255,.12); margin-top: .25rem; color: rgba(255,255,255,.55); }
.mobile-account { margin-bottom: .75rem; border: 1px solid rgba(120,166,255,.45); background: rgba(49,94,251,.1); }
.mobile-dash { display: flex; align-items: center; padding: .8rem 1rem; font-size: 1rem; font-weight: 600; color: #fff; border-bottom: 1px solid rgba(120,166,255,.3); }
.mobile-account-links { display: grid; grid-template-columns: 1fr; }
.mobile-account-links a { padding: .65rem 1rem; font-size: .92rem; color: rgba(255,255,255,.72); border-bottom: 1px solid rgba(255,255,255,.08); }
.mobile-account-links a:last-child { border-bottom: 0; }
</style>

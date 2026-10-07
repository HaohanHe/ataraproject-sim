<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from '../../composables/useI18n'
import { cardBoardTabs, SUPER_TAB, type BoardCard, type BoardLayout } from '../../lib/data'
import { scenarioLabel } from '../../lib/scenarioLabels'

// Card boards: one tab per card (labelled from the card slug, localized — see scenarioLabels.ts) and,
// where the phase ranks an overall mean, an Overall tab first. null stands for the overall tab.
// Where the phase has added cards (A1-D1): then the super board's tab and one tab per added card.
const props = defineProps<{ layout: BoardLayout; cards: BoardCard[]; extraCards?: BoardCard[]; modelValue: string | null }>()
const emit = defineEmits<{ 'update:modelValue': [slug: string | null] }>()
const { t, locale } = useI18n()
const label = (slug: string | null) => slug === null ? t('leaderboard.overall') : slug === SUPER_TAB ? t('leaderboard.super_board')
  : scenarioLabel(slug, [...props.cards, ...(props.extraCards ?? [])].find(c => c.slug === slug)?.name ?? slug, locale.value)
const tabs = computed(() => cardBoardTabs({ layout: props.layout, cards: props.cards, extraCards: props.extraCards ?? [] })
  .map(slug => ({ slug, label: label(slug) })))
</script>

<template>
  <div v-if="tabs.length" class="board-cards" role="group" :aria-label="t('leaderboard.cards')" data-testid="board-cards">
    <button
      v-for="tab in tabs" :key="tab.slug ?? ''" type="button"
      :class="{ active: tab.slug === modelValue, overall: tab.slug === null, super: tab.slug === SUPER_TAB }" :aria-pressed="tab.slug === modelValue"
      :data-testid="tab.slug === null ? 'board-card-overall' : tab.slug === SUPER_TAB ? 'board-card-super' : `board-card-${tab.slug}`"
      @click="emit('update:modelValue', tab.slug)"
    >{{ tab.label }}</button>
  </div>
</template>

<style scoped>
.board-cards { display: flex; flex-wrap: wrap; gap: .5rem; }
.board-cards button {
  border: 1px solid rgba(255,255,255,.2); background: rgba(6,6,7,.6); color: #b5b5b5;
  padding: .45rem .8rem; cursor: pointer;
  font-family: 'IBM Plex Mono', ui-monospace, monospace; font-size: .75rem; letter-spacing: .04em;
  transition: border-color .2s ease, color .2s ease, background-color .2s ease;
}
.board-cards button.overall { text-transform: uppercase; letter-spacing: .1em; }
.board-cards button.super { margin-left: .75rem; text-transform: uppercase; letter-spacing: .1em; }
.board-cards button:hover { border-color: #78a6ff; color: #fff; }
.board-cards button.active { border-color: #315efb; background: rgba(49,94,251,.16); color: #fff; }
.board-cards button:focus-visible { outline: 2px solid #78a6ff; outline-offset: 2px; }
</style>

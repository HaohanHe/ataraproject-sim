<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { parseAnnouncement } from '../../lib/announcementMarkdown'
import AnnouncementBlocks from './AnnouncementBlocks.vue'
import AnnouncementInline from './AnnouncementInline.vue'

// Announcement text with a safe markdown subset (bold, inline code, links, lists) and image URLs shown
// as images (no v-html). `poster` sizes images to fit the viewport whole (the popup); otherwise they stay a modest preview.
// "::: Title" … ":::" sections fold: the first starts open, later ones show only their title until clicked.
const props = defineProps<{ text: string | null | undefined; poster?: boolean }>()
const items = computed(() => parseAnnouncement(props.text))
const toggled = ref<Record<number, boolean>>({})
watch(() => props.text, () => { toggled.value = {} })
const uid = Math.random().toString(36).slice(2, 8)
</script>

<template>
  <div class="ann-body leading-relaxed" data-testid="announcement-body">
    <template v-for="(b, i) in items" :key="i">
      <section v-if="b.kind === 'section'" class="ann-section" data-testid="announcement-section">
        <h3 class="ann-section-h">
          <button type="button" class="ann-section-btn" :aria-expanded="toggled[i] ?? b.open" :aria-controls="`ann-sec-${uid}-${i}`"
            @click="toggled[i] = !(toggled[i] ?? b.open)">
            <span class="ann-chev" aria-hidden="true">▸</span><span class="min-w-0"><AnnouncementInline :inlines="b.title" /></span>
          </button>
        </h3>
        <AnnouncementBlocks v-show="toggled[i] ?? b.open" :id="`ann-sec-${uid}-${i}`" class="ann-section-body" :blocks="b.blocks" :poster="poster" />
      </section>
      <AnnouncementBlocks v-else :blocks="[b]" :poster="poster" />
    </template>
  </div>
</template>

<style scoped>
.ann-body > * + * { margin-top: .7em; }
.ann-section { border-top: 1px solid rgba(158,173,255,.18); padding-top: .55em; }
.ann-section + .ann-section { margin-top: .55em; }
.ann-section-h { font: inherit; margin: 0; }
.ann-section-btn { display: flex; width: 100%; align-items: baseline; gap: .5em; padding: .15em 0; text-align: left; font: inherit;
  font-weight: 600; color: inherit; background: none; border: 0; cursor: pointer; }
.ann-section-btn:hover { color: #f5f7ff; }
.ann-section-btn:focus-visible { outline: 2px solid rgba(158,173,255,.8); outline-offset: 2px; border-radius: 3px; }
.ann-chev { display: inline-block; flex: none; transition: transform .15s; opacity: .75; }
.ann-section-btn[aria-expanded="true"] .ann-chev { transform: rotate(90deg); }
.ann-section-body { margin-top: .5em; padding-left: 1.1em; }
</style>

<script setup lang="ts">
import type { Block } from '../../lib/announcementMarkdown'
import AnnouncementInline from './AnnouncementInline.vue'

// Paragraphs, headings and lists of an announcement (interpolation only, no v-html).
defineProps<{ blocks: Block[]; poster?: boolean }>()
</script>

<template>
  <div class="ann-blocks">
    <template v-for="(b, i) in blocks" :key="i">
      <p v-if="b.kind === 'para'" class="whitespace-pre-line"><AnnouncementInline :inlines="b.inlines" :poster="poster" /></p>
      <p v-else-if="b.kind === 'heading'" class="font-semibold"><AnnouncementInline :inlines="b.inlines" :poster="poster" /></p>
      <ul v-else-if="b.kind === 'ul'" class="ann-list list-disc"><li v-for="(item, j) in b.items" :key="j"><AnnouncementInline :inlines="item" :poster="poster" /></li></ul>
      <ol v-else class="ann-list list-decimal"><li v-for="(item, j) in b.items" :key="j"><AnnouncementInline :inlines="item" :poster="poster" /></li></ol>
    </template>
  </div>
</template>

<style scoped>
.ann-blocks > * + * { margin-top: .7em; }
.ann-list { padding-left: 1.4em; }
.ann-list li + li { margin-top: .25em; }
</style>

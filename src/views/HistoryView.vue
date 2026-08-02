<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import {
  ArrowDownUp,
  Bookmark,
  Copy,
  Download,
  ExternalLink,
  FolderOpen,
  Heart,
  Search,
  Star,
  Trash2,
  Upload,
} from '@lucide/vue'
import { useNewsStore } from '../stores/news'
import type { HistoryBackup, HistoryRecord, HistorySort } from '../types'

const store = useNewsStore()
const router = useRouter()
const search = ref('')
const favoritesOnly = ref(false)
const sort = ref<HistorySort>('latest')
const backupInput = ref<HTMLInputElement | null>(null)
const records = computed(() => store.history)

onMounted(async () => {
  if (!store.capabilities) await store.bootstrap()
  else await applyFilters()
})

async function applyFilters() {
  await store.refreshHistory(search.value, favoritesOnly.value || undefined, sort.value)
}

async function setSort(nextSort: HistorySort) {
  sort.value = nextSort
  await applyFilters()
}

async function toggleFavoritesOnly() {
  favoritesOnly.value = !favoritesOnly.value
  await applyFilters()
}

function restore(record: HistoryRecord) {
  store.restoreRecord(record)
  void router.push('/')
}

async function toggleFavorite(record: HistoryRecord) {
  await store.updateRecord(record.id, { favorite: !record.favorite })
}
async function rate(record: HistoryRecord, rating: number) {
  await store.updateRecord(record.id, { rating })
}

async function copy(record: HistoryRecord) {
  try {
    await navigator.clipboard.writeText(`${record.title}\n\n${record.summary}`)
    store.notice = '历史摘要已复制。'
  } catch (reason) {
    store.error = reason instanceof Error ? reason.message : '复制摘要失败。'
  }
}

function download(record: HistoryRecord) {
  const link = document.createElement('a')
  const text = [
    record.title,
    '',
    record.summary,
    '',
    `关键词：${record.keywords.join('、')}`,
    ...(record.source_url ? [`原始新闻链接：${record.source_url}`] : []),
  ].join('\n')
  link.href = URL.createObjectURL(new Blob([text], { type: 'text/plain;charset=utf-8' }))
  link.download = `${record.title.slice(0, 24)}.txt`
  link.click()
  URL.revokeObjectURL(link.href)
}

async function exportBackup() {
  const backup = await store.exportHistoryBackup()
  if (!backup) return
  const link = document.createElement('a')
  link.href = URL.createObjectURL(
    new Blob([JSON.stringify(backup, null, 2)], { type: 'application/json;charset=utf-8' }),
  )
  link.download = `newsbrief-history-${new Date().toISOString().slice(0, 10)}.json`
  link.click()
  URL.revokeObjectURL(link.href)
  store.notice = `已导出 ${backup.records.length} 条本机历史记录。`
}

function selectBackupFile() {
  backupInput.value?.click()
}

async function importBackup(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  try {
    const backup = JSON.parse(await file.text()) as HistoryBackup
    if (!window.confirm('导入会合并新记录，并跳过重复内容。是否继续？')) return
    const outcome = await store.importHistoryBackup(backup)
    if (outcome) await applyFilters()
  } catch (reason) {
    store.error =
      reason instanceof Error ? `读取备份文件失败：${reason.message}` : '读取备份文件失败。'
  }
}

async function remove(record: HistoryRecord) {
  if (!window.confirm(`删除“${record.title}”吗？`)) return
  await store.removeRecord(record.id)
}

async function clearAll() {
  if (!window.confirm('确认清空全部历史记录吗？此操作无法撤销。')) return
  await store.removeAll()
}
</script>

<template>
  <section class="page-heading history-heading">
    <div>
      <p class="eyebrow">本机存储</p>
      <h1>历史记录</h1>
    </div>
    <div class="history-heading-actions">
      <input
        ref="backupInput"
        class="visually-hidden"
        type="file"
        accept="application/json,.json"
        @change="importBackup"
      />
      <button
        class="filter-button history-backup-button"
        type="button"
        title="导入本机历史备份"
        @click="selectBackupFile"
      >
        <Upload :size="16" />导入备份
      </button>
      <button
        class="filter-button history-backup-button"
        type="button"
        title="导出全部本机历史记录"
        @click="exportBackup"
      >
        <Download :size="16" />导出备份
      </button>
      <button v-if="records.length" class="danger-button" type="button" @click="clearAll">
        <Trash2 :size="16" />清空记录
      </button>
    </div>
  </section>
  <section class="history-toolbar" aria-label="历史记录筛选">
    <label class="search-box"
      ><Search :size="17" /><input
        v-model="search"
        placeholder="搜索标题或摘要"
        @input="applyFilters"
    /></label>
    <div class="history-sort" role="group" aria-label="排序方式">
      <button :class="{ active: sort === 'latest' }" type="button" @click="setSort('latest')">
        <ArrowDownUp :size="15" />最近生成</button
      ><button :class="{ active: sort === 'rating' }" type="button" @click="setSort('rating')">
        <Star :size="15" />评分优先
      </button>
    </div>
    <button
      class="filter-button"
      :class="{ active: favoritesOnly }"
      type="button"
      @click="toggleFavoritesOnly"
    >
      <Heart :size="16" />仅看收藏</button
    ><span>{{ records.length }} 条记录</span>
  </section>
  <p v-if="store.error" class="message error-message page-message">{{ store.error }}</p>
  <p v-else-if="store.notice" class="message notice-message page-message">{{ store.notice }}</p>
  <section v-if="records.length" class="history-list">
    <article v-for="record in records" :key="record.id" class="history-item">
      <div class="history-main">
        <div class="history-meta">
          <span class="engine-badge" :class="record.engine">{{ record.engine_label }}</span
          ><time>{{ new Date(record.created_at).toLocaleString('zh-CN') }}</time>
        </div>
        <a
          v-if="record.source_url"
          class="history-source"
          :href="record.source_url"
          target="_blank"
          rel="noopener noreferrer"
        >
          <ExternalLink :size="13" />{{ record.source_domain || '原始新闻链接' }}
        </a>
        <h2>{{ record.title }}</h2>
        <p>{{ record.summary }}</p>
        <div class="history-bottom">
          <span>{{ record.metrics.compression_ratio }}% 压缩率</span
          ><span>{{ record.processing_ms }} ms</span
          ><span>{{ record.keywords.slice(0, 4).join(' · ') }}</span>
        </div>
      </div>
      <div class="history-actions">
        <button
          class="icon-button"
          :title="record.favorite ? '取消收藏' : '收藏记录'"
          :aria-label="record.favorite ? '取消收藏' : '收藏记录'"
          @click="toggleFavorite(record)"
        >
          <Bookmark :size="17" :fill="record.favorite ? 'currentColor' : 'none'" /></button
        ><button class="icon-button" title="复制摘要" aria-label="复制摘要" @click="copy(record)">
          <Copy :size="17" /></button
        ><button
          class="icon-button"
          title="导出文本"
          aria-label="导出文本"
          @click="download(record)"
        >
          <Download :size="17" /></button
        ><button class="icon-button" title="删除记录" aria-label="删除记录" @click="remove(record)">
          <Trash2 :size="17" /></button
        ><button class="restore-button" type="button" @click="restore(record)">
          <FolderOpen :size="16" />恢复
        </button>
        <div class="rating" aria-label="摘要评分">
          <button
            v-for="value in 5"
            :key="value"
            type="button"
            :title="`评分 ${value}`"
            @click="rate(record, value)"
          >
            <Star :size="14" :fill="(record.rating ?? 0) >= value ? 'currentColor' : 'none'" />
          </button>
        </div>
      </div>
    </article>
  </section>
  <section v-else class="empty-page">
    <div class="empty-mark"><Bookmark :size="25" /></div>
    <h2>还没有历史记录</h2>
    <p>生成并保存摘要后，会在这里保留本机记录。</p>
  </section>
</template>

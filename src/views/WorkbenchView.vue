<script setup lang="ts">
import { computed, nextTick, onMounted, ref } from 'vue'
import {
  ArrowRight,
  Ban,
  Bookmark,
  Check,
  CircleAlert,
  Clipboard,
  Columns3,
  Download,
  FileText,
  ExternalLink,
  Link,
  Pin,
  RefreshCw,
  RotateCcw,
  Settings2,
  Search,
  Sparkles,
  Trash2,
  WandSparkles,
  X,
} from '@lucide/vue'
import { RouterLink } from 'vue-router'
import { useNewsStore } from '../stores/news'
import type { NewsSample, SummaryComparisonItem, SummaryLength, VerificationStatus } from '../types'

const store = useNewsStore()
const copied = ref(false)
const showSamples = ref(false)
const articleUrl = ref('')
const activeTab = ref<'summary' | 'facts' | 'verification' | 'evidence' | 'compare'>('summary')
const highlightedSentence = ref<number | null>(null)
const directImageExtensions = [
  '.avif',
  '.bmp',
  '.gif',
  '.heic',
  '.jpeg',
  '.jpg',
  '.png',
  '.svg',
  '.webp',
]
const directVideoExtensions = ['.avi', '.flv', '.m4v', '.mkv', '.mov', '.mp4', '.webm']
const knownDynamicArticleHosts = ['msn.com', 'msn.cn']

const characterCount = computed(() => store.draft.content.replace(/\s/g, '').length)
const result = computed(() => store.currentResult)
const quality = computed(() => result.value?.quality)
const comparison = computed(() => store.comparison)
const facts = computed(() => result.value?.facts ?? [])
const verification = computed(() => result.value?.verification ?? null)
const quickSamples = computed(() => store.samples.slice(0, 2))
const canReviewEvidence = computed(
  () => verification.value?.mode === 'online' && Boolean(verification.value.sources.length),
)
const aiReviewByClaim = computed(
  () => new Map(verification.value?.ai_review?.claims.map((item) => [item.claim_id, item]) ?? []),
)
const activeEngine = computed({
  get: () => store.draft.engine,
  set: (value) => {
    if (value === 'ai' && !store.aiEnabled) return
    store.draft.engine = value
  },
})

const lengths: { id: SummaryLength; label: string; count: string }[] = [
  { id: 'brief', label: '简短', count: '2 句' },
  { id: 'standard', label: '标准', count: '3-4 句' },
  { id: 'detailed', label: '详细', count: '5-6 句' },
]

onMounted(async () => {
  if (!store.capabilities) await store.bootstrap()
})

async function generate() {
  await store.generate()
  if (result.value) activeTab.value = 'summary'
}

async function openComparison() {
  if (!result.value || result.value.engine !== 'local') return
  activeTab.value = 'compare'
  if (!comparison.value) await store.compareLengths()
}

async function applyConstraints() {
  await store.generate()
  if (result.value) activeTab.value = 'summary'
}

function selectComparison(item: SummaryComparisonItem) {
  store.useComparison(item)
  activeTab.value = 'summary'
}

function comparisonLabel(length: SummaryLength) {
  return lengths.find((item) => item.id === length)?.label ?? '摘要'
}

function verificationStatusLabel(status: VerificationStatus) {
  return {
    supported: '已支持',
    partial: '部分支持',
    unverified: '待补充',
    conflicting: '存在冲突',
    offline_only: '未联网核验',
  }[status]
}

function sourceTierLabel(tier: 'official' | 'established_media' | 'other') {
  return {
    official: '官方来源',
    established_media: '主流媒体',
    other: '参考线索',
  }[tier]
}

function aiReviewFor(claimId: string) {
  return aiReviewByClaim.value.get(claimId)
}

async function copySummary() {
  if (!result.value) return
  await navigator.clipboard.writeText(`${result.value.title}\n\n${result.value.summary}`)
  copied.value = true
  window.setTimeout(() => {
    copied.value = false
  }, 1600)
}

function exportSummary() {
  if (!result.value) return
  const body = [
    result.value.title,
    '',
    result.value.summary,
    '',
    '新闻要点',
    ...result.value.bullets.map((item) => `- ${item.text}`),
    '',
    `关键词：${result.value.keywords.join('、')}`,
    ...(store.draft.source_url ? [`原始新闻链接：${store.draft.source_url}`] : []),
    `摘要引擎：${result.value.engine_label}`,
  ].join('\n')
  const link = document.createElement('a')
  link.href = URL.createObjectURL(new Blob([body], { type: 'text/plain;charset=utf-8' }))
  link.download = `${result.value.title.slice(0, 24) || 'NewsBrief摘要'}.txt`
  link.click()
  URL.revokeObjectURL(link.href)
}

async function locateSentence(id: number) {
  activeTab.value = 'evidence'
  highlightedSentence.value = id
  await nextTick()
  document
    .getElementById(`source-sentence-${id}`)
    ?.scrollIntoView({ behavior: 'smooth', block: 'center' })
  window.setTimeout(() => {
    highlightedSentence.value = null
  }, 1800)
}

async function locateSource(id: string) {
  await nextTick()
  document.getElementById(`verification-source-${id}`)?.scrollIntoView({
    behavior: 'smooth',
    block: 'center',
  })
}

function loadSample(sampleIndex: number) {
  const sample = store.samples[sampleIndex]
  if (!sample) return
  loadMenuSample(sample)
}

function loadMenuSample(sample: NewsSample) {
  store.loadSample(sample)
  showSamples.value = false
  activeTab.value = 'summary'
}

function directMediaResourceLabel(url: string): string | null {
  const pathname = new URL(url).pathname.toLowerCase()
  if (directImageExtensions.some((extension) => pathname.endsWith(extension))) return '图片'
  if (directVideoExtensions.some((extension) => pathname.endsWith(extension))) return '视频'
  return null
}

function isKnownDynamicArticleUrl(url: string): boolean {
  const hostname = new URL(url).hostname.toLowerCase()
  return knownDynamicArticleHosts.some((host) => hostname === host || hostname.endsWith(`.${host}`))
}

async function importFromLink() {
  const url = articleUrl.value.trim()
  if (!url) return
  try {
    if (new URL(url).protocol !== 'https:') throw new Error()
  } catch {
    store.error = '请输入有效的 HTTPS 新闻链接。'
    return
  }
  const mediaResource = directMediaResourceLabel(url)
  if (mediaResource) {
    store.error = `该链接是${mediaResource}资源，无法提取新闻正文。请提供对应新闻报道网页链接或手动粘贴文字稿。`
    return
  }
  if (isKnownDynamicArticleUrl(url)) {
    store.error =
      '该 MSN 新闻页需要在浏览器中动态加载正文，当前无法可靠提取。请粘贴原始发布媒体的报道链接，或手动复制标题和文字正文。'
    return
  }
  if (
    (store.draft.title.trim() || store.draft.content.trim()) &&
    !window.confirm('导入新闻链接会替换当前标题、正文和来源信息，是否继续？')
  ) {
    return
  }
  if (await store.importArticle(url)) articleUrl.value = ''
}

function removeArticleSource() {
  store.draft.source_url = null
  store.draft.source_domain = null
  articleUrl.value = ''
  store.notice = '已移除当前草稿的来源链接。'
}
</script>

<template>
  <section class="workspace-heading">
    <div>
      <p class="eyebrow">NEWS READER WORKSPACE</p>
      <h1>读懂新闻，保留依据。</h1>
    </div>
    <div class="status-line" :class="{ offline: !store.capabilities }">
      <span class="status-dot"></span>
      {{ store.capabilities ? '本地摘要引擎已就绪' : '正在连接摘要服务' }}
    </div>
  </section>

  <div class="desktop-workspace">
    <section class="editor-pane" aria-labelledby="input-title">
      <header class="pane-header editor-header">
        <div>
          <span class="section-index">01</span>
          <h2 id="input-title">新闻编辑器</h2>
        </div>
        <button
          class="icon-button"
          title="清空输入"
          aria-label="清空输入"
          @click="store.clearDraft"
        >
          <Trash2 :size="17" />
        </button>
      </header>

      <section class="article-import" aria-label="新闻链接导入">
        <label class="field-label" for="article-url"
          >新闻链接导入 <span>仅支持公开 HTTPS 新闻报道页</span></label
        >
        <div class="article-import-row">
          <Link :size="16" aria-hidden="true" />
          <input
            id="article-url"
            v-model="articleUrl"
            type="url"
            inputmode="url"
            autocomplete="url"
            placeholder="粘贴新闻报道页链接，自动提取标题和正文"
            :disabled="store.isImportingArticle"
            @keydown.enter.prevent="importFromLink"
          />
          <button
            type="button"
            :disabled="store.isImportingArticle || !articleUrl.trim()"
            @click="importFromLink"
          >
            <span v-if="store.isImportingArticle" class="spinner teal"></span>
            <Link v-else :size="16" />{{ store.isImportingArticle ? '正在提取' : '导入' }}
          </button>
        </div>
        <div v-if="store.draft.source_url" class="article-source">
          <ExternalLink :size="14" />
          <a :href="store.draft.source_url" target="_blank" rel="noopener noreferrer">
            来源：{{ store.draft.source_domain || '已导入新闻' }}
          </a>
          <button
            type="button"
            title="移除来源链接"
            aria-label="移除来源链接"
            @click="removeArticleSource"
          >
            <X :size="14" />
          </button>
        </div>
      </section>

      <label class="field-label" for="news-title">新闻标题 <span>可选</span></label>
      <input
        id="news-title"
        v-model="store.draft.title"
        class="title-input"
        maxlength="180"
        placeholder="填写新闻标题，或由系统提取"
      />

      <div class="body-label-row">
        <label class="field-label" for="news-content">新闻正文</label>
        <span :class="{ 'limit-warning': characterCount > 7600 }"
          >{{ characterCount }} / 8,000</span
        >
      </div>
      <textarea
        id="news-content"
        v-model="store.draft.content"
        class="news-input"
        placeholder="粘贴一篇中文新闻正文..."
        maxlength="8500"
        spellcheck="false"
      ></textarea>

      <div class="sample-menu">
        <div class="editor-helper-row">
          <button
            class="text-button"
            type="button"
            :aria-expanded="showSamples"
            @click="showSamples = !showSamples"
          >
            <FileText :size="16" />内置新闻库<ArrowRight
              :size="15"
              :class="{ rotate: showSamples }"
            />
          </button>
          <span>有效文本至少 80 个字符</span>
        </div>
        <div v-if="showSamples" class="sample-list" aria-label="内置新闻示例">
          <button
            v-for="sample in store.samples"
            :key="sample.id"
            class="sample-item"
            type="button"
            @click="loadMenuSample(sample)"
          >
            <span>{{ sample.category }}</span
            ><strong>{{ sample.title }}</strong>
          </button>
        </div>
      </div>

      <section class="summary-toolbar" aria-label="摘要设置">
        <div class="toolbar-group length-group">
          <span class="toolbar-label">摘要长度</span>
          <div class="segment-control" role="radiogroup" aria-label="摘要长度">
            <button
              v-for="item in lengths"
              :key="item.id"
              :class="{ active: store.draft.length === item.id }"
              type="button"
              @click="store.draft.length = item.id"
            >
              <strong>{{ item.label }}</strong
              ><span>{{ item.count }}</span>
            </button>
          </div>
        </div>
        <div class="toolbar-group engine-group">
          <div class="toolbar-label-row">
            <span class="toolbar-label">摘要引擎</span
            ><RouterLink v-if="!store.aiEnabled" to="/settings#ai-config" class="ai-config-link"
              ><Settings2 :size="14" />配置 AI 服务</RouterLink
            >
          </div>
          <div class="engine-toggle">
            <button
              class="engine-toggle-button"
              :class="{ active: activeEngine === 'local' }"
              type="button"
              @click="activeEngine = 'local'"
            >
              <WandSparkles :size="16" />本地可靠
            </button>
            <button
              class="engine-toggle-button"
              :class="{ active: activeEngine === 'ai', unavailable: !store.aiEnabled }"
              :disabled="!store.aiEnabled"
              type="button"
              @click="activeEngine = 'ai'"
            >
              <Sparkles :size="16" />AI 增强
            </button>
          </div>
        </div>
        <button
          class="generate-button"
          type="button"
          :disabled="store.isGenerating || !store.draft.content.trim()"
          @click="generate"
        >
          <span v-if="store.isGenerating" class="spinner"></span
          ><WandSparkles v-else :size="19" />{{ store.isGenerating ? '正在分析' : '生成摘要' }}
        </button>
      </section>
      <div class="trust-note">
        <CircleAlert :size="16" /><span>本地摘要只从原文中选择关键句，可逐句核验。</span>
      </div>
    </section>

    <section class="reader-pane" aria-labelledby="result-title">
      <header class="reader-header">
        <div>
          <span class="section-index">02</span>
          <h2 id="result-title">摘要阅读器</h2>
        </div>
        <div v-if="result" class="result-actions">
          <button
            class="icon-button"
            :title="copied ? '已复制' : '复制摘要'"
            :aria-label="copied ? '已复制' : '复制摘要'"
            @click="copySummary"
          >
            <Check v-if="copied" :size="17" /><Clipboard v-else :size="17" />
          </button>
          <button
            v-if="result.engine === 'local'"
            class="icon-button"
            title="比较摘要长度"
            aria-label="比较摘要长度"
            @click="openComparison"
          >
            <Columns3 :size="17" />
          </button>
          <button class="icon-button" title="导出文本" aria-label="导出文本" @click="exportSummary">
            <Download :size="17" />
          </button>
          <button
            class="icon-button"
            title="保存历史"
            aria-label="保存历史"
            :disabled="store.isSaving"
            @click="store.saveCurrent"
          >
            <Bookmark :size="17" />
          </button>
        </div>
      </header>

      <div v-if="result" class="reader-tabs" role="tablist" aria-label="摘要结果视图">
        <button
          :class="{ active: activeTab === 'summary' }"
          type="button"
          role="tab"
          :aria-selected="activeTab === 'summary'"
          @click="activeTab = 'summary'"
        >
          摘要结果
        </button>
        <button
          :class="{ active: activeTab === 'facts' }"
          type="button"
          role="tab"
          :aria-selected="activeTab === 'facts'"
          @click="activeTab = 'facts'"
        >
          新闻事实 <span>{{ facts.length }}</span>
        </button>
        <button
          :class="{ active: activeTab === 'verification' }"
          type="button"
          role="tab"
          :aria-selected="activeTab === 'verification'"
          @click="activeTab = 'verification'"
        >
          核验线索 <span>{{ verification?.claims.length ?? 0 }}</span>
        </button>
        <button
          :class="{ active: activeTab === 'evidence' }"
          type="button"
          role="tab"
          :aria-selected="activeTab === 'evidence'"
          @click="activeTab = 'evidence'"
        >
          原文依据 <span>{{ result.selected_sentence_ids.length }}</span>
        </button>
        <button
          v-if="result.engine === 'local'"
          :class="{ active: activeTab === 'compare' }"
          type="button"
          role="tab"
          :aria-selected="activeTab === 'compare'"
          @click="openComparison"
        >
          长度对比
        </button>
      </div>

      <p v-if="store.error" class="message error-message">
        <CircleAlert :size="16" />{{ store.error }}
      </p>
      <p v-else-if="store.notice" class="message notice-message">
        <Check :size="16" />{{ store.notice }}
      </p>

      <div v-if="store.isGenerating" class="result-loading" aria-label="正在生成摘要">
        <span class="skeleton headline"></span><span class="skeleton line"></span
        ><span class="skeleton line short"></span><span class="skeleton chip-row"></span>
      </div>

      <div v-else-if="result && activeTab === 'summary'" class="result-content">
        <div class="result-meta-row">
          <span class="engine-badge" :class="result.engine">{{ result.engine_label }}</span
          ><span>{{ result.processing_ms }} ms</span>
        </div>
        <h3>{{ result.title }}</h3>
        <p class="summary-copy">{{ result.summary }}</p>
        <div class="metric-strip">
          <div>
            <span>原文</span><strong>{{ result.metrics.original_characters }}</strong
            ><em>字</em>
          </div>
          <div>
            <span>摘要</span><strong>{{ result.metrics.summary_characters }}</strong
            ><em>字</em>
          </div>
          <div>
            <span>压缩率</span><strong>{{ result.metrics.compression_ratio }}</strong
            ><em>%</em>
          </div>
        </div>
        <div v-if="quality" class="quality-strip" aria-label="摘要依据与质量">
          <div>
            <span>依据完整</span><strong>{{ quality.evidence_coverage }}%</strong>
          </div>
          <div>
            <span>重复风险</span
            ><strong :class="{ safe: (quality.redundancy_risk ?? 0) <= 25 }">{{
              quality.redundancy_risk === null ? '未估算' : `${quality.redundancy_risk}%`
            }}</strong>
          </div>
          <div>
            <span>事实覆盖</span
            ><strong :class="{ safe: quality.fact_coverage >= 70 }"
              >{{ quality.fact_coverage }}%</strong
            >
          </div>
          <div>
            <span>事实覆盖数</span
            ><strong>{{ quality.facts_covered }} / {{ quality.facts_found }}</strong>
          </div>
          <div>
            <span>关键句</span
            ><strong
              >{{ quality.selected_sentence_count }} / {{ quality.source_sentence_count }}</strong
            >
          </div>
          <div>
            <span>处理耗时</span><strong>{{ result.processing_ms }} ms</strong>
          </div>
        </div>
        <div class="detail-block">
          <div class="detail-heading"><span>新闻要点</span><small>点击查看原文依据</small></div>
          <button
            v-for="(bullet, index) in result.bullets"
            :key="`${bullet.text}-${index}`"
            class="bullet-item"
            type="button"
            @click="locateSentence(bullet.source_sentence_ids[0])"
          >
            <span>{{ String(index + 1).padStart(2, '0') }}</span>
            <p>{{ bullet.text }}</p>
            <ArrowRight :size="15" />
          </button>
        </div>
        <div class="detail-block keyword-block">
          <div class="detail-heading"><span>关键词</span></div>
          <div class="keyword-list">
            <span v-for="word in result.keywords" :key="word">{{ word }}</span>
          </div>
        </div>
        <p v-if="result.fallback_reason" class="fallback-note">
          <RotateCcw :size="15" />{{ result.fallback_reason }}
        </p>
      </div>

      <div v-else-if="result && activeTab === 'facts'" class="facts-view">
        <div class="facts-intro">
          <span class="engine-badge local">本地事实</span>
          <p>每项信息均来自原文规则提取，点击可查看对应依据。</p>
        </div>
        <div v-if="facts.length" class="facts-grid">
          <button
            v-for="fact in facts"
            :key="`${fact.kind}-${fact.value}`"
            class="fact-card"
            type="button"
            @click="locateSentence(fact.evidence_sentence_ids[0])"
          >
            <span>{{ fact.label }}</span>
            <strong>{{ fact.value }}</strong>
            <small>依据 {{ fact.evidence_sentence_ids.length }} 句</small>
          </button>
        </div>
        <p v-else class="facts-empty">原文中未识别到可逐句核验的结构化事实。</p>
      </div>

      <div v-else-if="result && activeTab === 'verification'" class="verification-view">
        <div class="verification-intro">
          <div>
            <span class="engine-badge local">核验辅助</span>
            <h3>公开来源核验线索</h3>
          </div>
          <button
            v-if="store.verificationEnabled"
            class="verification-action"
            type="button"
            :disabled="store.isVerifyingNews"
            @click="store.verifyNews"
          >
            <span v-if="store.isVerifyingNews" class="spinner teal"></span>
            <Search v-else :size="16" />{{
              store.isVerifyingNews
                ? '正在检索公开来源'
                : verification?.mode === 'online'
                  ? '重新联网核验'
                  : '开始联网核验'
            }}
          </button>
          <RouterLink v-else to="/settings#search-config" class="verification-config-link">
            <Settings2 :size="15" />配置公开来源检索
          </RouterLink>
          <template v-if="canReviewEvidence">
            <button
              v-if="store.aiEvidenceReviewEnabled"
              class="verification-action ai-review-action"
              type="button"
              :disabled="store.isReviewingEvidence"
              @click="store.reviewVerificationEvidence"
            >
              <span v-if="store.isReviewingEvidence" class="spinner amber"></span>
              <Sparkles v-else :size="16" />{{
                store.isReviewingEvidence ? '正在解读来源证据' : 'AI 解读证据'
              }}
            </button>
            <RouterLink
              v-else
              to="/settings#ai-config"
              class="verification-config-link ai-review-link"
            >
              <Sparkles :size="15" />配置 AI 解读
            </RouterLink>
          </template>
        </div>
        <p class="verification-disclaimer">
          证据状态不等同于新闻真实性结论；请结合来源原文继续判断。
        </p>
        <p v-if="verification?.notice" class="verification-notice">{{ verification.notice }}</p>
        <p v-if="verification?.ai_review?.notice" class="ai-review-disclaimer">
          <Sparkles :size="14" />{{ verification.ai_review.notice }}
        </p>
        <div v-if="verification" class="verification-claims">
          <article
            v-for="claim in verification.claims"
            :key="claim.id"
            class="verification-claim"
            :class="claim.status"
          >
            <button
              class="verification-claim-main"
              type="button"
              @click="locateSentence(claim.source_sentence_ids[0])"
            >
              <span class="verification-claim-label">{{ claim.label }}</span>
              <strong>{{ claim.text }}</strong>
              <footer>
                <span>{{ verificationStatusLabel(claim.status) }}</span>
                <small>原文依据 {{ claim.source_sentence_ids.length }} 句</small>
                <small v-if="claim.evidence_source_ids.length"
                  >外部来源 {{ claim.evidence_source_ids.length }} 条</small
                >
              </footer>
            </button>
            <section v-if="aiReviewFor(claim.id)" class="ai-claim-review">
              <div>
                <span>AI 建议</span>
                <strong>{{
                  verificationStatusLabel(aiReviewFor(claim.id)!.suggested_status)
                }}</strong>
              </div>
              <p>{{ aiReviewFor(claim.id)!.reason }}</p>
              <footer v-if="aiReviewFor(claim.id)!.evidence_source_ids.length">
                <button
                  v-for="sourceId in aiReviewFor(claim.id)!.evidence_source_ids"
                  :key="sourceId"
                  type="button"
                  @click="locateSource(sourceId)"
                >
                  引用 {{ sourceId }}
                </button>
              </footer>
            </section>
          </article>
        </div>
        <div v-if="verification?.sources.length" class="verification-sources">
          <div class="detail-heading">
            <span>公开来源</span><small>{{ verification.sources.length }} 条可打开核查</small>
          </div>
          <a
            v-for="source in verification.sources"
            :id="`verification-source-${source.id}`"
            :key="source.id"
            class="verification-source"
            :href="source.url"
            target="_blank"
            rel="noopener noreferrer"
          >
            <div>
              <span :class="`source-tier ${source.tier}`">{{ sourceTierLabel(source.tier) }}</span>
              <strong>{{ source.title }}</strong>
              <small>{{ source.domain }}</small>
            </div>
            <p>{{ source.excerpt }}</p>
            <ExternalLink :size="15" />
          </a>
        </div>
        <div v-else-if="verification?.mode === 'online'" class="verification-empty">
          本次检索未找到可安全读取的公开来源，已保留本地核验线索。
        </div>
      </div>

      <div v-else-if="result && activeTab === 'compare'" class="compare-view">
        <div class="compare-heading">
          <div>
            <span class="engine-badge local">本地对比</span>
            <h3>选择阅读长度</h3>
          </div>
          <span v-if="comparison">{{ comparison.processing_ms }} ms</span>
        </div>
        <div v-if="store.isComparing" class="result-loading" aria-label="正在生成长度对比">
          <span class="skeleton headline"></span><span class="skeleton line"></span
          ><span class="skeleton line short"></span>
        </div>
        <div v-else-if="comparison" class="compare-list">
          <article
            v-for="item in comparison.results"
            :key="item.length"
            class="compare-item"
            :class="{ selected: result.summary === item.result.summary }"
          >
            <header>
              <div>
                <strong>{{ comparisonLabel(item.length) }}</strong
                ><span>{{ item.result.quality.selected_sentence_count }} 句</span>
              </div>
              <button
                type="button"
                :disabled="result.summary === item.result.summary"
                @click="selectComparison(item)"
              >
                {{ result.summary === item.result.summary ? '当前版本' : '使用此版本' }}
              </button>
            </header>
            <p>{{ item.result.summary }}</p>
            <footer>
              <span>{{ item.result.metrics.compression_ratio }}% 压缩率</span
              ><span>{{ item.result.quality.evidence_coverage }}% 依据完整</span
              ><span>{{ item.result.quality.redundancy_risk }}% 重复风险</span>
            </footer>
          </article>
        </div>
        <p v-else class="compare-empty">暂无可比较的本地结果。</p>
      </div>

      <div v-else-if="result && activeTab === 'evidence'" class="evidence-view">
        <div class="evidence-intro">
          <span class="engine-badge local">原文可追溯</span>
          <p>高亮句已被选入摘要</p>
        </div>
        <div v-if="result.engine === 'local'" class="constraint-toolbar">
          <p>固定句会优先保留，排除句不会参与本地摘要。调整后请重新生成。</p>
          <div>
            <button
              v-if="store.hasPendingConstraints"
              class="constraint-apply-button"
              type="button"
              @click="applyConstraints"
            >
              <RefreshCw :size="15" />按约束更新摘要
            </button>
            <button
              v-if="
                result.selection_constraints.pinned_sentence_ids.length ||
                result.selection_constraints.excluded_sentence_ids.length ||
                store.hasPendingConstraints
              "
              class="constraint-clear-button"
              type="button"
              @click="store.clearConstraints"
            >
              清除约束
            </button>
          </div>
        </div>
        <div class="source-block">
          <div
            v-for="sentence in result.source_sentences"
            :id="`source-sentence-${sentence.id}`"
            :key="sentence.id"
            class="source-sentence"
            :class="{ selected: sentence.selected, focused: highlightedSentence === sentence.id }"
          >
            <b>{{ String(sentence.id + 1).padStart(2, '0') }}</b
            >{{ sentence.text }}
            <div v-if="result.engine === 'local'" class="source-constraint-actions">
              <button
                class="source-constraint-button"
                :class="{
                  active: store.draft.selection_constraints.pinned_sentence_ids.includes(
                    sentence.id,
                  ),
                }"
                type="button"
                title="固定此句"
                :aria-label="`固定第 ${sentence.id + 1} 句`"
                @click="store.toggleConstraint(sentence.id, 'pinned')"
              >
                <Pin :size="14" />
              </button>
              <button
                class="source-constraint-button exclude"
                :class="{
                  active: store.draft.selection_constraints.excluded_sentence_ids.includes(
                    sentence.id,
                  ),
                }"
                type="button"
                title="排除此句"
                :aria-label="`排除第 ${sentence.id + 1} 句`"
                @click="store.toggleConstraint(sentence.id, 'excluded')"
              >
                <Ban :size="14" />
              </button>
            </div>
          </div>
        </div>
      </div>

      <div v-else class="editorial-empty">
        <div class="empty-mark"><WandSparkles :size="22" /></div>
        <p class="empty-kicker">摘要将在此处呈现</p>
        <h3>从一篇新闻开始</h3>
        <div v-if="quickSamples.length" class="quick-samples">
          <button
            v-for="(sample, index) in quickSamples"
            :key="sample.id"
            type="button"
            @click="loadSample(index)"
          >
            <span>{{ sample.category }}</span
            ><strong>{{ sample.title }}</strong
            ><ArrowRight :size="15" />
          </button>
        </div>
        <div class="empty-proof"><span>原文依据</span><span>本地处理</span></div>
      </div>
    </section>
  </div>
</template>

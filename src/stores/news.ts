import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { api } from '../api'
import type {
  AIConfigPayload,
  CapabilityResponse,
  Draft,
  HistoryRecord,
  HistorySort,
  HistoryBackup,
  HistoryImportResult,
  NewsSample,
  SummaryComparison,
  SummaryComparisonItem,
  SummaryResult,
  SelectionConstraints,
  SearchConfigPayload,
  VerificationResult,
} from '../types'

const emptyConstraints = (): SelectionConstraints => ({
  pinned_sentence_ids: [],
  excluded_sentence_ids: [],
})
const createBlankDraft = (): Draft => ({
  title: '',
  content: '',
  source_url: null,
  source_domain: null,
  length: 'standard',
  engine: 'local',
  selection_constraints: emptyConstraints(),
})

export const useNewsStore = defineStore('news', () => {
  const capabilities = ref<CapabilityResponse | null>(null)
  const samples = ref<NewsSample[]>([])
  const history = ref<HistoryRecord[]>([])
  const currentResult = ref<SummaryResult | null>(null)
  const comparison = ref<SummaryComparison | null>(null)
  const draft = ref<Draft>(createBlankDraft())
  const isGenerating = ref(false)
  const isComparing = ref(false)
  const isSaving = ref(false)
  const isImportingArticle = ref(false)
  const isImportingHistory = ref(false)
  const isVerifyingNews = ref(false)
  const isReviewingEvidence = ref(false)
  const activeHistoryId = ref<number | null>(null)
  const error = ref('')
  const notice = ref('')
  const aiEnabled = computed(() => capabilities.value?.ai_engine.enabled ?? false)
  const verificationEnabled = computed(
    () => capabilities.value?.verification_engine.enabled ?? false,
  )
  const aiEvidenceReviewEnabled = computed(
    () => capabilities.value?.ai_evidence_review.enabled ?? false,
  )
  const hasPendingConstraints = computed(() => {
    if (!currentResult.value || currentResult.value.engine !== 'local') return false
    return (
      JSON.stringify(currentResult.value.selection_constraints) !==
      JSON.stringify(draft.value.selection_constraints)
    )
  })

  function setError(reason: unknown, fallback: string) {
    error.value = reason instanceof Error ? reason.message : fallback
  }

  async function refreshCapabilities() {
    capabilities.value = await api.getCapabilities()
  }

  async function bootstrap() {
    try {
      const [capabilityData, sampleData, historyData] = await Promise.all([
        api.getCapabilities(),
        api.getSamples(),
        api.getHistory(),
      ])
      capabilities.value = capabilityData
      samples.value = sampleData
      history.value = historyData
    } catch (reason) {
      error.value =
        reason instanceof Error ? `无法连接摘要服务：${reason.message}` : '无法连接摘要服务。'
    }
  }

  async function configureAI(payload: AIConfigPayload) {
    capabilities.value = await api.configureAI(payload)
  }

  async function configureSearch(payload: SearchConfigPayload) {
    capabilities.value = await api.configureSearch(payload)
  }

  async function refreshHistory(search = '', favorite?: boolean, sort: HistorySort = 'latest') {
    error.value = ''
    try {
      history.value = await api.getHistory(search, favorite, sort)
    } catch (reason) {
      setError(reason, '读取历史记录失败。')
    }
  }

  async function exportHistoryBackup() {
    error.value = ''
    try {
      return await api.exportHistoryBackup()
    } catch (reason) {
      setError(reason, '导出历史备份失败。')
      return null
    }
  }

  async function importHistoryBackup(backup: HistoryBackup): Promise<HistoryImportResult | null> {
    error.value = ''
    notice.value = ''
    isImportingHistory.value = true
    try {
      const outcome = await api.importHistoryBackup(backup)
      notice.value = outcome.imported
        ? `已导入 ${outcome.imported} 条历史记录${outcome.skipped ? `，跳过 ${outcome.skipped} 条重复记录` : ''}。`
        : `没有导入新记录，已跳过 ${outcome.skipped} 条重复记录。`
      return outcome
    } catch (reason) {
      setError(reason, '导入历史备份失败，请确认文件来自 NewsBrief。')
      return null
    } finally {
      isImportingHistory.value = false
    }
  }

  async function generate() {
    error.value = ''
    notice.value = ''
    isGenerating.value = true
    try {
      const result = await api.summarize({
        title: draft.value.title,
        content: draft.value.content,
        length: draft.value.length,
        engine: draft.value.engine,
        selection_constraints: draft.value.selection_constraints,
      })
      currentResult.value = result
      comparison.value = null
      activeHistoryId.value = null
      if (result.fallback_reason) notice.value = result.fallback_reason
    } catch (reason) {
      error.value = reason instanceof Error ? reason.message : '摘要生成失败，请稍后再试。'
    } finally {
      isGenerating.value = false
    }
  }

  async function importArticle(url: string): Promise<boolean> {
    error.value = ''
    notice.value = ''
    isImportingArticle.value = true
    try {
      const article = await api.importArticle(url)
      draft.value = {
        ...draft.value,
        title: article.title,
        content: article.content,
        source_url: article.source_url,
        source_domain: article.source_domain,
        selection_constraints: emptyConstraints(),
      }
      currentResult.value = null
      comparison.value = null
      activeHistoryId.value = null
      notice.value = `已导入 ${article.source_domain} 的新闻内容，请确认后生成摘要。`
      return true
    } catch (reason) {
      setError(reason, '新闻链接导入失败，请手动粘贴正文。')
      return false
    } finally {
      isImportingArticle.value = false
    }
  }

  async function compareLengths() {
    if (!draft.value.content.trim()) return
    error.value = ''
    notice.value = ''
    isComparing.value = true
    try {
      comparison.value = await api.compareSummaries({
        title: draft.value.title,
        content: draft.value.content,
        selection_constraints: draft.value.selection_constraints,
      })
    } catch (reason) {
      error.value = reason instanceof Error ? reason.message : '长度对比生成失败，请稍后再试。'
    } finally {
      isComparing.value = false
    }
  }

  function useComparison(item: SummaryComparisonItem) {
    draft.value.length = item.length
    draft.value.selection_constraints = { ...item.result.selection_constraints }
    currentResult.value = item.result
    activeHistoryId.value = null
    notice.value = `已选择${{ brief: '简短', standard: '标准', detailed: '详细' }[item.length]}版本。`
  }

  async function saveCurrent() {
    if (!currentResult.value) return
    isSaving.value = true
    try {
      const record = activeHistoryId.value
        ? await api.updateHistory(activeHistoryId.value, {
            verification: currentResult.value.verification ?? undefined,
          })
        : await api.saveHistory({ ...draft.value, result: currentResult.value })
      history.value = [record, ...history.value.filter((item) => item.id !== record.id)]
      activeHistoryId.value = record.id
      notice.value = '已保存到本地历史记录。'
    } catch (reason) {
      error.value = reason instanceof Error ? reason.message : '保存历史记录失败。'
    } finally {
      isSaving.value = false
    }
  }

  function loadSample(sample: NewsSample) {
    draft.value = {
      ...draft.value,
      title: sample.title,
      content: sample.content,
      source_url: null,
      source_domain: null,
      selection_constraints: emptyConstraints(),
    }
    currentResult.value = null
    comparison.value = null
    activeHistoryId.value = null
    notice.value = `已载入“${sample.title}”。`
  }

  function restoreRecord(record: HistoryRecord) {
    draft.value = {
      title: record.title,
      content: record.content,
      source_url: record.source_url,
      source_domain: record.source_domain,
      length: record.length,
      engine: record.engine,
      selection_constraints: { ...record.selection_constraints },
    }
    currentResult.value = record
    comparison.value = null
    activeHistoryId.value = record.id
    notice.value = '已恢复历史记录。'
  }

  async function updateRecord(
    id: number,
    payload: { favorite?: boolean; rating?: number; verification?: VerificationResult },
  ) {
    error.value = ''
    try {
      const record = await api.updateHistory(id, payload)
      history.value = history.value.map((item) => (item.id === id ? record : item))
    } catch (reason) {
      setError(reason, '更新历史记录失败。')
    }
  }

  async function removeRecord(id: number) {
    error.value = ''
    try {
      await api.deleteHistory(id)
      history.value = history.value.filter((item) => item.id !== id)
    } catch (reason) {
      setError(reason, '删除历史记录失败。')
    }
  }

  async function removeAll() {
    error.value = ''
    try {
      await api.clearHistory()
      history.value = []
    } catch (reason) {
      setError(reason, '清空历史记录失败。')
    }
  }

  function clearDraft() {
    draft.value = createBlankDraft()
    currentResult.value = null
    comparison.value = null
    activeHistoryId.value = null
    error.value = ''
    notice.value = ''
  }

  function toggleConstraint(sentenceId: number, kind: 'pinned' | 'excluded') {
    if (currentResult.value?.engine !== 'local') return
    const constraints = draft.value.selection_constraints
    const target = kind === 'pinned' ? 'pinned_sentence_ids' : 'excluded_sentence_ids'
    const opposite = kind === 'pinned' ? 'excluded_sentence_ids' : 'pinned_sentence_ids'
    constraints[opposite] = constraints[opposite].filter((id) => id !== sentenceId)
    constraints[target] = constraints[target].includes(sentenceId)
      ? constraints[target].filter((id) => id !== sentenceId)
      : [...constraints[target], sentenceId].sort((left, right) => left - right)
    comparison.value = null
    notice.value = kind === 'pinned' ? '已更新固定句约束。' : '已更新排除句约束。'
  }

  function clearConstraints() {
    draft.value.selection_constraints = emptyConstraints()
    comparison.value = null
    notice.value = '已清除证据约束。'
  }

  async function verifyNews() {
    if (!currentResult.value || !draft.value.content.trim()) return
    error.value = ''
    notice.value = ''
    isVerifyingNews.value = true
    try {
      const verification = await api.verifyNews({
        title: draft.value.title,
        content: draft.value.content,
      })
      currentResult.value = {
        ...currentResult.value,
        verification: { ...verification, ai_review: null },
      }
      if (activeHistoryId.value && verification.mode === 'online') {
        const record = await api.updateHistory(activeHistoryId.value, { verification })
        history.value = [record, ...history.value.filter((item) => item.id !== record.id)]
        currentResult.value = record
      }
      notice.value = verification.notice ?? '核验线索已更新。'
    } catch (reason) {
      setError(reason, '公开来源核验失败，已保留离线核验线索。')
    } finally {
      isVerifyingNews.value = false
    }
  }

  async function reviewVerificationEvidence() {
    const verification = currentResult.value?.verification
    if (
      !currentResult.value ||
      !verification ||
      verification.mode !== 'online' ||
      !verification.sources.length
    ) {
      return
    }
    error.value = ''
    notice.value = ''
    isReviewingEvidence.value = true
    try {
      const reviewedVerification = await api.reviewVerificationEvidence(verification)
      currentResult.value = { ...currentResult.value, verification: reviewedVerification }
      if (activeHistoryId.value) {
        const record = await api.updateHistory(activeHistoryId.value, {
          verification: reviewedVerification,
        })
        history.value = [record, ...history.value.filter((item) => item.id !== record.id)]
        currentResult.value = record
      }
      notice.value = reviewedVerification.ai_review?.notice ?? 'AI 证据解读已更新。'
    } catch (reason) {
      setError(reason, 'AI 证据解读暂不可用，已保留规则核验结果。')
    } finally {
      isReviewingEvidence.value = false
    }
  }

  return {
    capabilities,
    samples,
    history,
    currentResult,
    comparison,
    draft,
    isGenerating,
    isComparing,
    isSaving,
    isImportingArticle,
    isImportingHistory,
    isVerifyingNews,
    isReviewingEvidence,
    activeHistoryId,
    error,
    notice,
    aiEnabled,
    aiEvidenceReviewEnabled,
    verificationEnabled,
    hasPendingConstraints,
    bootstrap,
    refreshCapabilities,
    configureAI,
    configureSearch,
    refreshHistory,
    exportHistoryBackup,
    importHistoryBackup,
    importArticle,
    generate,
    compareLengths,
    useComparison,
    saveCurrent,
    loadSample,
    restoreRecord,
    updateRecord,
    removeRecord,
    removeAll,
    clearDraft,
    toggleConstraint,
    clearConstraints,
    verifyNews,
    reviewVerificationEvidence,
  }
})

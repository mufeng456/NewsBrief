import type {
  AIConfigPayload,
  AIConfigVerification,
  ArticleImportResult,
  SearchConfigPayload,
  SearchConfigVerification,
  CapabilityResponse,
  HistoryBackup,
  HistoryImportResult,
  HistoryRecord,
  HistorySort,
  NewsSample,
  SelectionConstraints,
  BenchmarkOverview,
  SummaryComparison,
  SummaryEngine,
  SummaryLength,
  SummaryResult,
  VerificationResult,
} from './types'

const API_BASE = '/api'

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json', ...(options.headers ?? {}) },
    ...options,
  })
  if (response.status === 204) return undefined as T
  const payload = await response.json().catch(() => ({ detail: '服务返回了无法识别的内容。' }))
  if (!response.ok) {
    const detail = payload.detail
    const message =
      typeof detail === 'string'
        ? detail
        : Array.isArray(detail)
          ? detail
              .map((item) => (typeof item?.msg === 'string' ? item.msg : '请求参数无效。'))
              .join('；')
          : '请求未能完成。'
    throw new Error(message)
  }
  return payload as T
}

export const api = {
  getCapabilities: () => request<CapabilityResponse>('/capabilities'),
  configureAI: (payload: AIConfigPayload) =>
    request<CapabilityResponse>('/ai-config', { method: 'PUT', body: JSON.stringify(payload) }),
  verifyAI: (payload: AIConfigPayload) =>
    request<AIConfigVerification>('/ai-config/verify', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  configureSearch: (payload: SearchConfigPayload) =>
    request<CapabilityResponse>('/search-config', {
      method: 'PUT',
      body: JSON.stringify(payload),
    }),
  verifySearch: (payload: SearchConfigPayload) =>
    request<SearchConfigVerification>('/search-config/verify', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  importArticle: (url: string) =>
    request<ArticleImportResult>('/articles/import', {
      method: 'POST',
      body: JSON.stringify({ url }),
    }),
  getSamples: () => request<NewsSample[]>('/samples'),
  getHistory: (search = '', favorite?: boolean, sort: HistorySort = 'latest') => {
    const query = new URLSearchParams()
    if (search) query.set('search', search)
    if (favorite !== undefined) query.set('favorite', String(favorite))
    if (sort !== 'latest') query.set('sort', sort)
    return request<HistoryRecord[]>(`/history${query.size ? `?${query.toString()}` : ''}`)
  },
  exportHistoryBackup: () => request<HistoryBackup>('/history/backup'),
  importHistoryBackup: (payload: HistoryBackup) =>
    request<HistoryImportResult>('/history/import', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  summarize: (payload: {
    title: string
    content: string
    length: SummaryLength
    engine: SummaryEngine
    selection_constraints: SelectionConstraints
  }) => request<SummaryResult>('/summaries', { method: 'POST', body: JSON.stringify(payload) }),
  compareSummaries: (payload: {
    title: string
    content: string
    selection_constraints: SelectionConstraints
  }) =>
    request<SummaryComparison>('/summaries/compare', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  verifyNews: (payload: { title: string; content: string }) =>
    request<VerificationResult>('/verifications', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  reviewVerificationEvidence: (verification: VerificationResult) =>
    request<VerificationResult>('/verifications/ai-review', {
      method: 'POST',
      body: JSON.stringify({ verification }),
    }),
  saveHistory: (payload: {
    title: string
    content: string
    source_url: string | null
    source_domain: string | null
    length: SummaryLength
    result: SummaryResult
  }) => {
    const {
      title,
      summary,
      bullets,
      keywords,
      source_sentences,
      selected_sentence_ids,
      metrics,
      quality,
      engine,
      engine_label,
      processing_ms,
      fallback_reason,
      facts,
      selection_constraints,
      verification,
    } = payload.result
    const result = {
      title,
      summary,
      bullets,
      keywords,
      source_sentences,
      selected_sentence_ids,
      metrics,
      quality,
      engine,
      engine_label,
      processing_ms,
      fallback_reason,
      facts,
      selection_constraints,
      verification,
    }
    return request<HistoryRecord>('/history', {
      method: 'POST',
      body: JSON.stringify({
        title: payload.title,
        content: payload.content,
        source_url: payload.source_url,
        source_domain: payload.source_domain,
        length: payload.length,
        result,
      }),
    })
  },
  updateHistory: (
    id: number,
    payload: { favorite?: boolean; rating?: number; verification?: VerificationResult },
  ) => request<HistoryRecord>(`/history/${id}`, { method: 'PATCH', body: JSON.stringify(payload) }),
  deleteHistory: (id: number) => request<void>(`/history/${id}`, { method: 'DELETE' }),
  clearHistory: () => request<void>('/history', { method: 'DELETE' }),
  getBenchmarkOverview: () => request<BenchmarkOverview>('/benchmarks/overview'),
  runBenchmark: () => request<BenchmarkOverview>('/benchmarks/run', { method: 'POST' }),
}

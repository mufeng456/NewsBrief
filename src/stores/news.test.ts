import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

const fixtures = vi.hoisted(() => {
  const summaryResult = {
    title: '测试新闻',
    summary: '第一条重要信息。第二条重要信息。',
    bullets: [{ text: '第一条重要信息', source_sentence_ids: [0] }],
    keywords: ['测试', '新闻', '信息'],
    source_sentences: [{ id: 0, text: '第一条重要信息', selected: true, score: 0.8 }],
    selected_sentence_ids: [0],
    metrics: { original_characters: 120, summary_characters: 20, compression_ratio: 16.7 },
    quality: {
      evidence_coverage: 100,
      redundancy_risk: 12,
      selected_sentence_count: 1,
      source_sentence_count: 4,
      fact_coverage: 100,
      facts_found: 2,
      facts_covered: 2,
    },
    facts: [
      {
        kind: 'event' as const,
        label: '核心事件',
        value: '第一条重要信息',
        evidence_sentence_ids: [0],
        method: 'local_rule' as const,
      },
    ],
    selection_constraints: { pinned_sentence_ids: [], excluded_sentence_ids: [] },
    engine: 'local' as const,
    engine_label: '本地可靠摘要',
    processing_ms: 10,
    fallback_reason: null,
    verification: {
      mode: 'offline' as const,
      status: 'unavailable' as const,
      claims: [
        {
          id: 'event-0',
          kind: 'event' as const,
          label: '核心事件',
          text: '第一条重要信息',
          source_sentence_ids: [0],
          status: 'offline_only' as const,
          evidence_source_ids: [],
        },
      ],
      sources: [],
      searched_at: null,
      notice: '尚未联网检索。',
    },
  }
  const detailedResult = {
    ...summaryResult,
    summary: '第一条重要信息。第二条重要信息。第三条补充信息。',
    metrics: { original_characters: 120, summary_characters: 30, compression_ratio: 25 },
    quality: {
      evidence_coverage: 100,
      redundancy_risk: 18,
      selected_sentence_count: 3,
      source_sentence_count: 4,
      fact_coverage: 100,
      facts_found: 2,
      facts_covered: 2,
    },
  }
  return {
    summaryResult,
    comparison: {
      processing_ms: 18,
      results: [
        { length: 'brief' as const, result: summaryResult },
        { length: 'detailed' as const, result: detailedResult },
      ],
    },
    historyRecord: {
      ...detailedResult,
      id: 99,
      content: '这是一段用于验证保存版本的中文新闻正文内容。',
      source_url: null,
      source_domain: null,
      length: 'detailed' as const,
      favorite: false,
      rating: null,
      created_at: '2026-07-19T12:00:00',
    },
  }
})

vi.mock('../api', () => ({
  api: {
    getCapabilities: vi.fn(),
    configureAI: vi.fn(),
    verifyAI: vi.fn(),
    configureSearch: vi.fn(),
    verifySearch: vi.fn(),
    importArticle: vi.fn().mockResolvedValue({
      title: '链接导入新闻',
      content:
        '这是一段从公开新闻链接导入的中文正文内容，用于验证标题、正文与来源信息能够在编辑器中保持可编辑，并可继续进入本地摘要流程。',
      source_url: 'https://news.example.com/article/1',
      source_domain: 'news.example.com',
      retrieved_at: '2026-08-01T08:00:00',
    }),
    getSamples: vi.fn(),
    getHistory: vi.fn().mockResolvedValue([]),
    summarize: vi.fn().mockResolvedValue(fixtures.summaryResult),
    compareSummaries: vi.fn().mockResolvedValue(fixtures.comparison),
    verifyNews: vi.fn().mockResolvedValue({
      ...fixtures.summaryResult.verification,
      mode: 'online',
      status: 'completed',
      claims: [{ ...fixtures.summaryResult.verification.claims[0], status: 'supported' }],
      sources: [
        {
          id: 'source-1',
          title: '官方公告',
          url: 'https://news.gov.cn/example',
          domain: 'news.gov.cn',
          tier: 'official',
          excerpt: '第一条重要信息',
          retrieved_at: '2026-07-20T00:00:00Z',
          content_sha256: 'a'.repeat(64),
        },
      ],
      searched_at: '2026-07-20T00:00:00Z',
    }),
    reviewVerificationEvidence: vi.fn().mockResolvedValue({
      mode: 'online',
      status: 'completed',
      claims: [
        {
          ...fixtures.summaryResult.verification.claims[0],
          status: 'supported',
          evidence_source_ids: ['source-1'],
        },
      ],
      sources: [
        {
          id: 'source-1',
          title: '官方公告',
          url: 'https://news.gov.cn/example',
          domain: 'news.gov.cn',
          tier: 'official',
          excerpt: '第一条重要信息的官方来源摘录。',
          retrieved_at: '2026-07-20T00:00:00Z',
          content_sha256: 'a'.repeat(64),
        },
      ],
      searched_at: '2026-07-20T00:00:00Z',
      notice: '规则核验已完成。',
      ai_review: {
        model: 'test-evidence-model',
        reviewed_at: '2026-07-20T00:00:00Z',
        notice: 'AI 建议仅辅助阅读，不构成新闻真实性结论。',
        claims: [
          {
            claim_id: 'event-0',
            suggested_status: 'supported',
            reason:
              '该建议仅基于当前提供的官方来源短摘录与主张文本的对应关系，来源覆盖了主体和核心事件，但仍应打开原文核对完整语境与后续信息，不能作为新闻真伪裁决。',
            evidence_source_ids: ['source-1'],
          },
        ],
      },
    }),
    saveHistory: vi.fn().mockResolvedValue(fixtures.historyRecord),
    updateHistory: vi.fn(),
    deleteHistory: vi.fn(),
    clearHistory: vi.fn(),
  },
}))

import { useNewsStore } from './news'
import { api } from '../api'

describe('news store', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('loads a sample into the editable draft', () => {
    const store = useNewsStore()
    store.loadSample({
      id: 'sample',
      category: '校园',
      title: '测试新闻',
      content: '这是一段用于验证工作流的中文新闻正文内容。',
    })
    expect(store.draft.title).toBe('测试新闻')
    expect(store.draft.content).toContain('中文新闻正文')
    expect(store.currentResult).toBeNull()
  })

  it('imports an article into the editable draft without creating history', async () => {
    const store = useNewsStore()

    const imported = await store.importArticle('https://example.com/news')

    expect(imported).toBe(true)
    expect(api.importArticle).toHaveBeenLastCalledWith('https://example.com/news')
    expect(store.draft.title).toBe('链接导入新闻')
    expect(store.draft.source_domain).toBe('news.example.com')
    expect(store.history).toEqual([])
  })

  it('keeps the draft and surfaces a readable import failure', async () => {
    const store = useNewsStore()
    store.draft.content = '保留原有编辑内容，导入失败时不应替换用户已经输入的新闻正文。'
    vi.mocked(api.importArticle).mockRejectedValueOnce(
      new Error('无法提取公开新闻正文，请手动粘贴。'),
    )

    const imported = await store.importArticle('https://example.com/unavailable')

    expect(imported).toBe(false)
    expect(store.draft.content).toContain('保留原有编辑内容')
    expect(store.error).toBe('无法提取公开新闻正文，请手动粘贴。')
  })

  it('stores a generated summary result for the workbench', async () => {
    const store = useNewsStore()
    store.draft.content =
      '这是一段至少八十个字符的中文新闻测试内容，用于验证前端状态是否能在接口返回后正确保存摘要结果，并向工作台展示关键词与原文依据。'
    await store.generate()
    expect(store.currentResult?.summary).toContain('第一条重要信息')
    expect(store.currentResult?.engine).toBe('local')
    expect(store.currentResult?.quality.evidence_coverage).toBe(100)
    expect(store.error).toBe('')
    expect(api.summarize).toHaveBeenLastCalledWith({
      title: '',
      content: store.draft.content,
      length: 'standard',
      engine: 'local',
      selection_constraints: { pinned_sentence_ids: [], excluded_sentence_ids: [] },
    })
  })

  it('requests rating-priority history when selected', async () => {
    const store = useNewsStore()
    await store.refreshHistory('', false, 'rating')
    expect(api.getHistory).toHaveBeenLastCalledWith('', false, 'rating')
  })

  it('saves the selected domestic search provider without retaining the key in state', async () => {
    vi.mocked(api.configureSearch).mockResolvedValueOnce({
      verification_engine: {
        enabled: true,
        provider: 'bocha',
        provider_label: '博查 Web Search（国内默认）',
        label: '公开来源核验',
        message: '已配置博查 Web Search（国内默认），可在核验线索中主动检索公开来源。',
        max_sources: 6,
      },
    } as never)
    const store = useNewsStore()

    await store.configureSearch({ provider: 'bocha', api_key: 'key-not-kept-in-store' })

    expect(api.configureSearch).toHaveBeenLastCalledWith({
      provider: 'bocha',
      api_key: 'key-not-kept-in-store',
    })
    expect(store.capabilities?.verification_engine.provider).toBe('bocha')
  })

  it('applies mutually exclusive evidence constraints to the draft', () => {
    const store = useNewsStore()
    store.currentResult = fixtures.summaryResult

    store.toggleConstraint(0, 'pinned')
    expect(store.draft.selection_constraints.pinned_sentence_ids).toEqual([0])
    expect(store.hasPendingConstraints).toBe(true)

    store.toggleConstraint(0, 'excluded')
    expect(store.draft.selection_constraints.pinned_sentence_ids).toEqual([])
    expect(store.draft.selection_constraints.excluded_sentence_ids).toEqual([0])
  })

  it('replaces the offline preview with source-backed verification evidence', async () => {
    const store = useNewsStore()
    store.draft.title = '测试新闻'
    store.draft.content =
      '这是一段满足最小长度的中文新闻测试内容，用于验证公开来源核验状态能够更新当前摘要结果并保留证据链。'.repeat(
        2,
      )
    store.currentResult = fixtures.summaryResult

    await store.verifyNews()

    expect(api.verifyNews).toHaveBeenLastCalledWith({
      title: '测试新闻',
      content: store.draft.content,
    })
    expect(store.currentResult?.verification?.mode).toBe('online')
    expect(store.currentResult?.verification?.claims[0].status).toBe('supported')
  })

  it('adds an AI evidence suggestion only after online verification and clears it on recheck', async () => {
    const store = useNewsStore()
    store.draft.title = '测试新闻'
    store.draft.content =
      '这是一段满足最小长度的中文新闻测试内容，用于验证来源核验与 AI 证据解读流程能够正确保存状态并在重新核验时清除过期建议。'.repeat(
        2,
      )
    store.currentResult = fixtures.summaryResult

    await store.verifyNews()
    await store.reviewVerificationEvidence()

    expect(api.reviewVerificationEvidence).toHaveBeenLastCalledWith(
      expect.objectContaining({ mode: 'online', sources: expect.any(Array) }),
    )
    expect(store.currentResult?.verification?.ai_review?.claims[0].claim_id).toBe('event-0')

    await store.verifyNews()
    expect(store.currentResult?.verification?.ai_review).toBeNull()
  })

  it('keeps history visible and surfaces an update failure', async () => {
    const store = useNewsStore()
    store.history = [fixtures.historyRecord]
    vi.mocked(api.updateHistory).mockRejectedValueOnce(new Error('数据库暂不可用'))

    await store.updateRecord(fixtures.historyRecord.id, { favorite: true })

    expect(store.history).toEqual([fixtures.historyRecord])
    expect(store.error).toBe('数据库暂不可用')
  })

  it('selects a comparison version before saving it to history', async () => {
    const store = useNewsStore()
    store.draft.title = '测试新闻'
    store.draft.content =
      '这是一段至少八十个字符的中文新闻测试内容，用于验证前端状态是否能在接口返回后正确保存摘要结果，并向工作台展示关键词与原文依据。'
    await store.compareLengths()
    expect(api.compareSummaries).toHaveBeenLastCalledWith({
      title: '测试新闻',
      content: store.draft.content,
      selection_constraints: { pinned_sentence_ids: [], excluded_sentence_ids: [] },
    })
    const selected = store.comparison!.results[1]
    store.useComparison(selected)
    expect(store.draft.length).toBe('detailed')
    expect(store.currentResult?.summary).toBe(selected.result.summary)
    await store.saveCurrent()
    expect(api.saveHistory).toHaveBeenLastCalledWith(
      expect.objectContaining({ length: 'detailed', result: selected.result }),
    )
  })
})

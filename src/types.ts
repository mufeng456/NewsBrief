export type SummaryLength = 'brief' | 'standard' | 'detailed'
export type SummaryEngine = 'local' | 'ai'
export type HistorySort = 'latest' | 'rating'
export type FactKind = 'subject' | 'time' | 'location' | 'event' | 'number' | 'impact'

export interface SelectionConstraints {
  pinned_sentence_ids: number[]
  excluded_sentence_ids: number[]
}

export interface FactItem {
  kind: FactKind
  label: string
  value: string
  evidence_sentence_ids: number[]
  method: 'local_rule'
}

export type VerificationStatus =
  | 'supported'
  | 'partial'
  | 'unverified'
  | 'conflicting'
  | 'offline_only'

export type AIEvidenceReviewStatus = Exclude<VerificationStatus, 'offline_only'>

export interface AIEvidenceClaimReview {
  claim_id: string
  suggested_status: AIEvidenceReviewStatus
  reason: string
  evidence_source_ids: string[]
}

export interface AIEvidenceReview {
  model: string
  reviewed_at: string
  notice: string | null
  claims: AIEvidenceClaimReview[]
}

export interface VerificationClaim {
  id: string
  kind: FactKind
  label: string
  text: string
  source_sentence_ids: number[]
  status: VerificationStatus
  evidence_source_ids: string[]
}

export interface VerificationSource {
  id: string
  title: string
  url: string
  domain: string
  tier: 'official' | 'established_media' | 'other'
  excerpt: string
  retrieved_at: string
  content_sha256: string
}

export interface VerificationResult {
  mode: 'offline' | 'online'
  status: 'completed' | 'partial' | 'unavailable'
  claims: VerificationClaim[]
  sources: VerificationSource[]
  searched_at: string | null
  notice: string | null
  ai_review?: AIEvidenceReview | null
}

export interface SourceSentence {
  id: number
  text: string
  selected: boolean
  score: number
}

export interface SummaryBullet {
  text: string
  source_sentence_ids: number[]
}

export interface SummaryMetrics {
  original_characters: number
  summary_characters: number
  compression_ratio: number
  sentence_count?: number
}

export interface SummaryQuality {
  evidence_coverage: number
  redundancy_risk: number | null
  selected_sentence_count: number
  source_sentence_count: number
  fact_coverage: number
  facts_found: number
  facts_covered: number
}

export interface SummaryResult {
  title: string
  summary: string
  bullets: SummaryBullet[]
  keywords: string[]
  source_sentences: SourceSentence[]
  selected_sentence_ids: number[]
  metrics: SummaryMetrics
  quality: SummaryQuality
  facts: FactItem[]
  selection_constraints: SelectionConstraints
  verification?: VerificationResult | null
  engine: SummaryEngine
  engine_label: string
  processing_ms: number
  fallback_reason: string | null
}

export interface SummaryComparisonItem {
  length: SummaryLength
  result: SummaryResult
}

export interface SummaryComparison {
  processing_ms: number
  results: SummaryComparisonItem[]
}

export interface NewsSample {
  id: string
  category: string
  title: string
  content: string
}

export interface CapabilityResponse {
  local_engine: { enabled: boolean; label: string }
  ai_engine: { enabled: boolean; label: string; message: string; base_url: string; model: string }
  ai_evidence_review: {
    enabled: boolean
    label: string
    message: string
    requires_online_sources: boolean
    max_sources: number
  }
  verification_engine: {
    enabled: boolean
    provider: string
    label: string
    message: string
    max_sources: number
  }
  limits: { min_characters: number; max_characters: number; max_sentences: number }
}

export interface AIConfigPayload {
  api_key: string
  base_url: string
  model: string
}

export interface AIConfigVerification {
  available: boolean
  model: string
  message: string
}

export interface SearchConfigPayload {
  api_key: string
}

export interface SearchConfigVerification {
  available: boolean
  provider: string
  message: string
}

export interface HistoryRecord extends SummaryResult {
  id: number
  content: string
  length: SummaryLength
  favorite: boolean
  rating: number | null
  created_at: string
}

export interface Draft {
  title: string
  content: string
  length: SummaryLength
  engine: SummaryEngine
  selection_constraints: SelectionConstraints
}

export interface BenchmarkMetric {
  label: string
  value: string
  detail: string
}

export interface BenchmarkMethod {
  id: string
  name: string
  description: string
  status: 'ready' | 'pending'
  metrics: BenchmarkMetric[]
}

export interface BenchmarkOverview {
  version: string
  dataset: {
    total: number
    categories: { name: string; count: number }[]
    public_metadata_only: boolean
    private_dataset_available: boolean
  }
  methodology: string[]
  methods: BenchmarkMethod[]
  human_review: {
    reviewers_target: string
    samples: number
    dimensions: string[]
  }
}

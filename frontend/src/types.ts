export interface PostComment {
  body?: string
  text?: string
  body_zh?: string
  score?: number
}

export interface Post {
  source: string
  title: string
  title_zh?: string
  content: string
  comments: Array<string | PostComment>
  url: string
  hn_url: string
  score: number
  num_comments: number
  has_need_signals: boolean
  opportunity_score?: number
  comment_read_score?: number
  passes_heat_gate?: boolean
  top_signals?: string[]
  signal_counts?: Record<string, number>
  score_breakdown?: Record<string, number>
  _post_id?: string
  _engine?: string
  _discovery_source?: string
  _evidence_probe_id?: string
  _evidence_probe_query?: string
  _evidence_probe_reason?: string
  _evidence_probe_signal?: string
  _evidence_probe_label?: string
}

export interface Quote {
  text: string
  source_url: string
  author: string
  score: number
  platform: string
  context: string
  signal_type: string
}

export interface Evidence {
  evidence_id: string
  source_id?: string
  text: string
  source_url?: string
  post_id?: string
  comment_id?: string
  post_score?: number
  comment_score?: number
  subreddit?: string
  platform?: string
  source_type?: 'post' | 'comment' | string
  source_title?: string
  signal_type?: string
  signal_label?: string
  supports?: string[]
  context?: string
  quality_score?: number
  verbatim?: boolean
  discovery_source?: string
  probe_id?: string
  probe_query?: string
  probe_reason?: string
}

export interface FemwcDimension {
  score: number
  reasoning: string
}

export interface FemwcResult {
  F: FemwcDimension
  E: FemwcDimension
  M: FemwcDimension
  W: FemwcDimension
  C: FemwcDimension
  total: number
  verdict: string
  summary: string
}

export interface NeedPackage {
  title: string
  description: string
  femwc: FemwcResult
  total_score: number
  quotes: Quote[]
  representative_posts: Post[]
  user_segments: string[]
  existing_solutions: string[]
  signal_summary: string
}

export interface MarketCompetitorSignal {
  source_id?: string
  source_type?: string
  name: string
  publisher?: string
  revenue?: number
  revenue_display?: string
  revenue_delta?: number
  revenue_delta_display?: string
  downloads?: number
  downloads_display?: string
  store_url?: string
  app_store_url?: string
  sensor_tower_url?: string
  growth_pct?: number | null
}

export interface MarketValidation {
  level?: 'strong' | 'medium' | 'validated' | 'early' | 'weak' | 'unknown' | string
  label?: string
  source_id?: string
  source_type?: string
  source_confidence?: string
  max_monthly_revenue?: number
  max_monthly_revenue_display?: string
  total_peer_revenue?: number
  total_peer_revenue_display?: string
  competitor_count?: number
  growth_signal?: 'positive' | 'flat' | 'negative' | 'unknown' | string
  top_competitors?: MarketCompetitorSignal[]
  queries?: string[]
  risk_note?: string
  checked_at?: string
  market_region?: string
  candidate_region?: string
  metrics_region?: string
  minimum_competitor_revenue?: number
  signal_max_monthly_revenue?: number
  signal_total_peer_revenue?: number
  signal_competitor_count?: number
  date_range?: {
    start?: string
    end?: string
    label?: string
  }
}

export interface Need {
  need_title: string
  need_description: string
  need_title_en?: string
  need_description_en?: string
  posts: Post[]
  total_score: number
  total_comments: number
  opportunity_score?: number
  opportunity_level?: string
  top_signals?: string[]
  signal_counts?: Record<string, number>
  heat_summary?: string
  why_this_matters?: string
  ai_review_score?: number | null
  ai_review_reason?: string
  ai_review_model?: string
  evidence?: Evidence[]
  evidence_ids?: string[]
  evidence_summary?: string
  market_validation?: MarketValidation
  source_ids?: string[]
  second_round_probe_ids?: string[]
  second_round_post_count?: number
  second_round_summary?: string
  deep_mine_package?: NeedPackage
}

export interface PersonaQuote {
  text: string
  text_zh: string
  source_url?: string
  context?: string
}

export interface Persona {
  name: string
  avatar_seed: string
  tagline: string
  bio: string
  gender: 'male' | 'female'
  avatar_hint?: string
  demographics: {
    age_range: string
    occupation: string
    location_hint: string
    tech_savviness: 'low' | 'medium' | 'high'
  }
  goals: string[]
  frustrations: string[]
  behaviors: string[]
  tools_used: string[]
  willingness_to_pay: string
  quotes: PersonaQuote[]
  day_in_life: string
  priority_rank: string[]
  switching_trigger: string
  deal_breaker: string
}

export interface EngineStatus {
  engine: string
  preference?: string
  rdt_status?: {
    installed: boolean
    authenticated: boolean
    version: string
    error: string
  }
}

export interface DebateEntry {
  role: 'analyst' | 'critic' | 'director' | 'human' | 'researcher' | 'investor'
  content: string
}

export interface ReportSummary {
  filename: string
  title: string
  created_at: string
  rounds: number
  report_format?: 'json' | 'markdown'
  verdict?: string
  femwc_total?: number | null
  ai_fit?: string
}

export interface FemwcScores {
  F: number
  E: number
  M: number
  W: number
  C: number
  total: number
}

export interface ReportData {
  post?: Post
  need?: Need
  debate_log?: DebateEntry[]
  final_report: string | Record<string, unknown>
  debate_rounds: number
  report_format?: 'json' | 'markdown'
  created_at: string
  feishu?: { url: string; document_id: string }
}

export type DebateStatus =
  | 'idle'
  | 'debating'
  | 'error'
  | 'debate_done'
  | 'generating_report'
  | 'done'

export interface ChatMessage {
  id: string
  role: 'analyst' | 'critic' | 'director' | 'human' | 'researcher' | 'investor'
  label: string
  content: string
  streaming?: boolean
  provider?: 'claude' | 'gpt'
  topicDivider?: { index: number; title: string; total: number }
}

export type DetailView =
  | { type: 'empty' }
  | { type: 'post' }
  | { type: 'analysis' }
  | { type: 'message'; id: string }
  | { type: 'report' }

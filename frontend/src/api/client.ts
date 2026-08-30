import type { Need, ReportSummary, ReportData, DebateEntry, EngineStatus, NeedPackage } from '../types'
import {
  defaultApiError,
  exportFailedMessage,
  normalizedApiErrorMessage,
  refreshFailedMessage,
  reportInterruptedMessage,
  sensorTowerTimeoutMessage,
  streamDisconnectedMessage,
} from '../errorMessages'

const BASE = '/api'

const SESSION_KEY = 'lumon_session_id'

function getSessionId(): string {
  let id = localStorage.getItem(SESSION_KEY)
  if (!id) {
    id = crypto.randomUUID()
    localStorage.setItem(SESSION_KEY, id)
  }
  return id
}

export function sessionHeaders(extra?: Record<string, string>): Record<string, string> {
  return { 'X-Session-Id': getSessionId(), ...extra }
}

export type AnalyticsProperties = Record<string, string | number | boolean | null | string[] | number[]>

export function trackAnalyticsEvent(event: string, properties: AnalyticsProperties = {}) {
  try {
    void fetch(BASE + '/analytics/event', {
      method: 'POST',
      headers: sessionHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ event, properties }),
      keepalive: true,
    }).catch(() => {})
  } catch {
    // 埋点失败不影响主流程
  }
}

function pickErrorText(value: unknown): string {
  if (!value) return ''
  if (value instanceof Error) return value.message
  if (typeof value !== 'string') return String(value)

  const raw = value.trim()
  if (!raw) return ''

  try {
    const parsed = JSON.parse(raw)
    if (parsed && typeof parsed === 'object') {
      const obj = parsed as Record<string, unknown>
      const detail = obj.detail
      if (typeof detail === 'string') return detail
      if (detail && typeof detail === 'object') {
        const detailObj = detail as Record<string, unknown>
        if (typeof detailObj.message === 'string') return detailObj.message
        if (typeof detailObj.error === 'string') return detailObj.error
      }
      if (typeof obj.message === 'string') return obj.message
      if (typeof obj.error === 'string') return obj.error
    }
  } catch {
    // 非 JSON 按纯文本继续处理
  }

  return raw
    .replace(/<[^>]*>/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
}

export function normalizeApiError(value: unknown, status?: number): string {
  const text = pickErrorText(value)
  const low = text.toLowerCase()

  if (status === 401 || low.includes('401') || low.includes('unauthorized')) {
    return normalizedApiErrorMessage('auth')
  }
  if (status === 403 || low.includes('403') || low.includes('forbidden')) {
    return normalizedApiErrorMessage('forbidden')
  }
  if (status === 429 || low.includes('429') || low.includes('rate limit') || low.includes('too many')) {
    return normalizedApiErrorMessage('rateLimit')
  }
  if (status === 404 || low.includes('404')) {
    return normalizedApiErrorMessage('notFound')
  }
  if (status === 503 || low.includes('503') || low.includes('service unavailable') || low.includes('no available')) {
    return normalizedApiErrorMessage('quota')
  }
  if (status && status >= 500) {
    return normalizedApiErrorMessage('server')
  }
  if (low.includes('ssl') || low.includes('unexpected_eof') || low.includes('eof occurred') || low.includes('tls')) {
    return normalizedApiErrorMessage('external')
  }
  if (low.includes('failed to fetch') || low.includes('networkerror') || low.includes('network error')) {
    return normalizedApiErrorMessage('network')
  }
  if (low.includes('timeout') || low.includes('timed out')) {
    return normalizedApiErrorMessage('timeout')
  }
  if (low.includes('stream')) {
    return normalizedApiErrorMessage('stream')
  }

  return text || defaultApiError(status)
}

async function json<T>(url: string, init?: RequestInit): Promise<T> {
  const headers = { ...sessionHeaders(), ...(init?.headers as Record<string, string>) }
  const res = await fetch(BASE + url, { ...init, headers })
  if (!res.ok) {
    const err = await res.text()
    throw new Error(normalizeApiError(err, res.status))
  }
  return res.json()
}

// ---- Config ----

export function getConfigStatus() {
  return json<{ claude_ok: boolean; gpt_ok: boolean; errors: string[] }>('/config/status')
}

export interface ConfigValues {
  CLAUDE_BASE_URL: string
  CLAUDE_API_KEY: string
  CLAUDE_API_KEY_SET: boolean
  CLAUDE_MODEL: string
  GPT_BASE_URL: string
  GPT_API_KEY: string
  GPT_API_KEY_SET: boolean
  GPT_MODEL: string
}

export function getConfigValues() {
  return json<ConfigValues>('/config/values')
}

export function saveConfig(config: Record<string, string | string[]>) {
  return json<{ ok: boolean }>('/config', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(config),
  })
}

export function getRoleNames() {
  return json<Record<string, string>>('/config/role-names')
}

export function saveRoleNames(names: Record<string, string>) {
  return json<{ ok: boolean; role_names: Record<string, string> }>('/config/role-names', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(names),
  })
}

export function getGeneralModel() {
  return json<{ model: string }>('/config/general-model')
}

export function setGeneralModel(model: string) {
  return json<{ ok: boolean }>('/config/general-model', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ model }),
  })
}

export function getServiceUsage() {
  return json<Record<string, Record<string, unknown>>>('/config/usage')
}

export interface TokenStats {
  claude: { input: number; output: number; calls: number }
  gpt: { input: number; output: number; calls: number }
}

export function getTokenStats() {
  return json<TokenStats>('/config/token-stats')
}

export function resetTokenStats() {
  return json<{ ok: boolean }>('/config/token-stats/reset', { method: 'POST' })
}

export function testConnection(prefix: string, opts?: { base_url?: string; api_key?: string; model?: string }) {
  return json<{ ok: boolean; message: string }>('/config/test', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ prefix, ...opts }),
  })
}

// ---- Fetch / Needs ----

export interface FetchParams {
  mode: 'sentence' | 'keywords' | 'open'
  language?: 'zh-CN' | 'en-US'
  query?: string
  keywords?: string[]
  sources: string[]
  category?: string
  reddit_categories?: string[]
  limit: number
  time_period?: 'month' | '3months' | '6months' | '9months'
  product?: string
  market?: string
  demographics?: string
  segment?: string
  pain_points?: number
  competitors?: string
  demo?: boolean
  fetch_model?: string
}

export interface RedditCategory {
  label: string
  subreddits: string[]
}

export function getRedditCategories() {
  return json<{ categories: Record<string, RedditCategory> }>('/reddit-categories')
}

export interface FetchCallbacks {
  onProgress?: (data: { message: string; progress: number }) => void
  onResult?: (data: { needs: Need[]; count: number }) => void
  onError?: (data: { message: string }) => void
  onDone?: () => void
}

export async function streamFetchNeeds(
  params: FetchParams,
  callbacks: FetchCallbacks,
  signal?: AbortSignal,
) {
  let res: Response
  try {
    res = await fetch(BASE + '/fetch', {
      method: 'POST',
      headers: sessionHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify(params),
      signal,
    })
  } catch (err) {
    if (signal?.aborted) return
    callbacks.onError?.({ message: normalizeApiError(err) })
    return
  }

  if (!res.ok || !res.body) {
    const errText = await res.text().catch(() => `HTTP ${res.status}`)
    callbacks.onError?.({ message: normalizeApiError(errText, res.status) })
    return
  }

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  let gotDone = false
  let currentEvent = ''
  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break

      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop() || ''

      for (const line of lines) {
        if (line.startsWith('event: ')) {
          currentEvent = line.slice(7).trim()
        } else if (line.startsWith('data: ')) {
          try {
            const data = JSON.parse(line.slice(6))
            switch (currentEvent) {
              case 'fetch_progress':
                callbacks.onProgress?.(data)
                break
              case 'fetch_result':
                callbacks.onResult?.(data)
                break
              case 'error':
                callbacks.onError?.(data)
                break
              case 'done':
                gotDone = true
                callbacks.onDone?.()
                break
            }
          } catch { /* skip malformed */ }
          currentEvent = ''
        }
      }
    }
  } catch (err) {
    if (!signal?.aborted) {
      callbacks.onError?.({ message: normalizeApiError(err) })
    }
  }
  if (!gotDone && !signal?.aborted) {
    callbacks.onDone?.()
  }
}

export function getNeeds() {
  return json<{ needs: Need[] }>('/needs')
}

export function clearNeeds() {
  return json<{ ok: boolean }>('/needs', { method: 'DELETE' })
}

export function syncNeeds(needs: Need[]) {
  return json<{ ok: boolean; count: number }>('/needs', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ needs }),
  })
}

export interface FetchJobStatus {
  active: boolean
  progress: number
  history: string[]
  error: string
  needs: Need[] | null
  engine: string
}

export function getFetchStatus(language?: string) {
  const query = language ? `?language=${encodeURIComponent(language)}` : ''
  return json<FetchJobStatus>(`/fetch/status${query}`)
}

export function stopFetch() {
  return json<{ ok: boolean }>('/fetch/stop', { method: 'POST' })
}

export function translateText(text: string) {
  return json<{ translation: string }>('/translate', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text }),
  })
}

// ---- Engine Status ----

export function getEngineStatus(force = false) {
  return json<EngineStatus>(`/engine-status${force ? '?force=true' : ''}`)
}

export function getEnginePreference() {
  return json<{ preference: string }>('/engine-preference')
}

export function setEnginePreference(preference: string) {
  return json<{ ok: boolean; preference: string }>('/engine-preference', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ preference }),
  })
}

export function getWebSearchEngine() {
  return json<{ engine: string }>('/web-search-engine')
}

export function setWebSearchEngine(engine: string) {
  return json<{ ok: boolean; engine: string }>('/web-search-engine', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ engine }),
  })
}

export function testWebSearch(engine: string) {
  return json<{ ok: boolean; status?: string; retryable?: boolean; message: string }>('/web-search-test', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ engine }),
  })
}

// ---- Deep Mine ----

export interface DeepMineCallbacks {
  onProgress?: (data: { message: string; progress: number }) => void
  onResult?: (data: { package: NeedPackage; need_index: number }) => void
  onError?: (data: { message: string }) => void
  onDone?: () => void
}

export async function streamDeepMine(
  needIndex: number,
  callbacks: DeepMineCallbacks,
  signal?: AbortSignal,
) {
  let res: Response
  try {
    res = await fetch(BASE + '/deep-mine', {
      method: 'POST',
      headers: sessionHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ need_index: needIndex }),
      signal,
    })
  } catch (err) {
    if (signal?.aborted) return
    callbacks.onError?.({ message: normalizeApiError(err) })
    return
  }

  if (!res.ok || !res.body) {
    const errText = await res.text().catch(() => `HTTP ${res.status}`)
    callbacks.onError?.({ message: normalizeApiError(errText, res.status) })
    return
  }

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let _gotDone = false
  let currentEvent = ''

  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break

      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop() || ''

      for (const line of lines) {
        if (line.startsWith('event: ')) {
          currentEvent = line.slice(7).trim()
        } else if (line.startsWith('data: ')) {
          try {
            const data = JSON.parse(line.slice(6))
            switch (currentEvent) {
              case 'fetch_progress':
                callbacks.onProgress?.(data)
                break
              case 'deep_mine_result':
                _gotDone = true
                callbacks.onResult?.(data)
                break
              case 'error':
                _gotDone = true
                callbacks.onError?.(data)
                break
              case 'done':
                _gotDone = true
                callbacks.onDone?.()
                break
            }
          } catch { /* skip malformed */ }
          currentEvent = ''
        }
      }
    }
  } catch (err) {
    if (!signal?.aborted) {
      callbacks.onError?.({ message: normalizeApiError(err) })
      _gotDone = true
    }
  }
  if (!_gotDone && !signal?.aborted) {
    callbacks.onError?.({ message: streamDisconnectedMessage() })
  }
}

// ---- Debate ----

export function getDebateState() {
  return json<{
    status: string
    round: number
    max_rounds: number
    debate_log: DebateEntry[]
    selected_need_idx: number | null
    final_report: string | null
    product_proposal: string | null
    free_topic_input?: string | null
  }>('/debate/state')
}

export function resetDebate() {
  return json<{ ok: boolean }>('/debate/reset', { method: 'POST' })
}

export interface SSECallbacks {
  onMessageStart?: (data: { role: string; label: string; provider?: string }) => void
  onChunk?: (data: { text: string }) => void
  onMessageEnd?: (data: { role: string; content: string }) => void
  onRoundStart?: (data: { round: number }) => void
  onDebateEnd?: (data: { reason: string; rounds: number }) => void
  onReportEnd?: (data: { report: string; filename: string }) => void
  onProposalEnd?: (data: { proposal: string }) => void
  onSearchProgress?: (data: { query: string; result_count: number; total_results: number; total_queries: number }) => void
  onDeepDiveEnd?: () => void
  onTopicList?: (data: { topics: { title: string; question: string }[] }) => void
  onTopicStart?: (data: { index: number; title: string; total: number }) => void
  onTopicEnd?: (data: { index: number; title: string; summary: string }) => void
  onError?: (data: { message: string }) => void
  onDone?: () => void
}

export async function streamSSE(
  url: string,
  body: unknown,
  callbacks: SSECallbacks,
  signal?: AbortSignal,
) {
  let res: Response
  try {
    res = await fetch(BASE + url, {
      method: 'POST',
      headers: sessionHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify(body),
      signal,
    })
  } catch (err) {
    if (signal?.aborted) return
    callbacks.onError?.({ message: normalizeApiError(err) })
    return
  }

  if (!res.ok || !res.body) {
    callbacks.onError?.({ message: normalizeApiError('', res.status) })
    return
  }

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let gotTerminal = false
  let currentEvent = ''

  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break

      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop() || ''

      for (const line of lines) {
        if (line.startsWith('event: ')) {
          currentEvent = line.slice(7).trim()
        } else if (line.startsWith('data: ')) {
          const raw = line.slice(6)
          try {
            const data = JSON.parse(raw)
            switch (currentEvent) {
              case 'message_start':
                callbacks.onMessageStart?.(data)
                break
              case 'chunk':
                callbacks.onChunk?.(data)
                break
              case 'message_end':
                callbacks.onMessageEnd?.(data)
                break
              case 'round_start':
                callbacks.onRoundStart?.(data)
                break
              case 'debate_end':
                gotTerminal = true
                callbacks.onDebateEnd?.(data)
                break
              case 'report_start':
                break
              case 'report_end':
                gotTerminal = true
                callbacks.onReportEnd?.(data)
                break
              case 'proposal_start':
                break
              case 'proposal_end':
                gotTerminal = true
                callbacks.onProposalEnd?.(data)
                break
              case 'search_progress':
                callbacks.onSearchProgress?.(data)
                break
              case 'deep_dive_end':
                gotTerminal = true
                callbacks.onDeepDiveEnd?.()
                break
              case 'topic_list':
                callbacks.onTopicList?.(data)
                break
              case 'topic_start':
                callbacks.onTopicStart?.(data)
                break
              case 'topic_end':
                callbacks.onTopicEnd?.(data)
                break
              case 'error':
                gotTerminal = true
                callbacks.onError?.(data)
                await reader.cancel()
                return
              case 'done':
                gotTerminal = true
                callbacks.onDone?.()
                break
            }
          } catch {
            // skip malformed JSON lines
          }
          currentEvent = ''
        }
      }
    }
  } catch (err) {
    if (!signal?.aborted) {
      callbacks.onError?.({ message: normalizeApiError(err) })
      gotTerminal = true
    }
  }
  if (!gotTerminal && !signal?.aborted) {
    callbacks.onError?.({ message: streamDisconnectedMessage() })
  }
}

// ---- Direct Report Generation ----

export interface DirectReportCallbacks {
  onProgress?: (data: { message: string; progress: number }) => void
  onChunk?: (data: { text: string }) => void
  onDone?: (data: { report: string; filename: string }) => void
  onError?: (data: { message: string }) => void
}

export type ReportLanguage = 'zh-CN' | 'en-US'

export async function streamGenerateReport(
  needIndex: number,
  callbacks: DirectReportCallbacks,
  signal?: AbortSignal,
  options?: { demo?: boolean; language?: ReportLanguage },
) {
  let res: Response
  try {
    res = await fetch(BASE + '/generate-report', {
      method: 'POST',
      headers: sessionHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({
        need_index: needIndex,
        ...(options?.demo ? { demo: true } : {}),
        ...(options?.language ? { language: options.language } : {}),
      }),
      signal,
    })
  } catch (err) {
    if (signal?.aborted) return
    callbacks.onError?.({ message: normalizeApiError(err) })
    return
  }

  if (!res.ok || !res.body) {
    const errText = await res.text().catch(() => `HTTP ${res.status}`)
    callbacks.onError?.({ message: normalizeApiError(errText, res.status) })
    return
  }

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let _gotDone = false
  let currentEvent = ''

  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break

      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop() || ''

      for (const line of lines) {
        if (line.startsWith('event: ')) {
          currentEvent = line.slice(7).trim()
        } else if (line.startsWith('data: ')) {
          try {
            const data = JSON.parse(line.slice(6))
            switch (currentEvent) {
              case 'report_progress':
                callbacks.onProgress?.(data)
                break
              case 'report_chunk':
                callbacks.onChunk?.(data)
                break
              case 'report_done':
                _gotDone = true
                callbacks.onDone?.(data)
                break
              case 'error':
                _gotDone = true
                callbacks.onError?.(data)
                break
            }
          } catch { /* skip malformed */ }
          currentEvent = ''
        }
      }
    }
    if (!signal?.aborted && !_gotDone) {
      const status = await getReportGenStatus().catch(() => null)
      if (status?.done && status.filename) {
        callbacks.onDone?.({ report: '', filename: status.filename })
      } else if (status?.active) {
        // still running in background, don't error - caller should reconnect
      } else {
        callbacks.onError?.({ message: reportInterruptedMessage() })
      }
    }
  } catch (err) {
    if (!signal?.aborted) {
      callbacks.onError?.({ message: normalizeApiError(err) })
    }
  }
}

export function getReportGenStatus() {
  return json<{ active: boolean; need_index: number; progress: number; message: string; error: string; done: boolean; filename: string; chunk_count: number }>('/report-gen/status')
}

export function streamReportGenResume(
  callbacks: DirectReportCallbacks,
  signal?: AbortSignal,
) {
  const url = BASE + '/report-gen/stream'
  fetch(url, { headers: sessionHeaders(), signal })
    .then(async (res) => {
      if (!res.ok || !res.body) {
        callbacks.onError?.({ message: normalizeApiError('', res.status) })
        return
      }
      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      let _gotDone = false
      let currentEvent = ''
      try {
        while (true) {
          const { done, value } = await reader.read()
          if (done) break
          buffer += decoder.decode(value, { stream: true })
          const lines = buffer.split('\n')
          buffer = lines.pop() || ''
          for (const line of lines) {
            if (line.startsWith('event: ')) {
              currentEvent = line.slice(7).trim()
            } else if (line.startsWith('data: ')) {
              try {
                const data = JSON.parse(line.slice(6))
                switch (currentEvent) {
                  case 'report_progress': callbacks.onProgress?.(data); break
                  case 'report_chunk': callbacks.onChunk?.(data); break
                  case 'report_done': _gotDone = true; callbacks.onDone?.(data); break
                  case 'error': _gotDone = true; callbacks.onError?.(data); break
                }
              } catch { /* skip */ }
              currentEvent = ''
            }
          }
        }
        if (!signal?.aborted && !_gotDone) {
          const status = await getReportGenStatus().catch(() => null)
          if (status?.done && status.filename) {
            callbacks.onDone?.({ report: '', filename: status.filename })
          }
        }
      } catch { /* ignore */ }
    })
    .catch(() => {})
}

// ---- Reports ----

export function listReports() {
  return json<{ reports: ReportSummary[] }>('/reports')
}

export function getReport(filename: string) {
  return json<ReportData>(`/reports/${filename}`)
}

export async function refreshReportCompetitors(filename: string) {
  const controller = new AbortController()
  const timeoutId = window.setTimeout(() => controller.abort(), 70_000)
  try {
    const res = await fetch(BASE + '/reports/' + encodeURIComponent(filename) + '/refresh-competitors', {
      method: 'POST',
      headers: sessionHeaders(),
      signal: controller.signal,
    })
    const data = await res.json().catch(() => ({ ok: false, error: refreshFailedMessage() }))
    if (!res.ok) throw new Error(normalizeApiError(data.error || data.detail || refreshFailedMessage(), res.status))
    return data as { ok: boolean; error?: string; report?: ReportData; queried?: number; matched?: number }
  } catch (e) {
    if (e instanceof DOMException && e.name === 'AbortError') {
      throw new Error(sensorTowerTimeoutMessage())
    }
    throw e
  } finally {
    window.clearTimeout(timeoutId)
  }
}

export async function deleteReport(filename: string) {
  const res = await fetch(BASE + '/reports/' + encodeURIComponent(filename), { method: 'DELETE', headers: sessionHeaders() })
  if (!res.ok) throw new Error(normalizeApiError(await res.text(), res.status))
  return res.json()
}

export async function exportToFeishu(filename: string) {
  const res = await fetch(BASE + '/reports/' + encodeURIComponent(filename) + '/export-feishu', { method: 'POST', headers: sessionHeaders() })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: exportFailedMessage() }))
    throw new Error(normalizeApiError(err.detail || exportFailedMessage(), res.status))
  }
  return res.json() as Promise<{ ok: boolean; url: string; document_id: string }>
}

export async function getFeishuStatus() {
  return json<{ configured: boolean }>('/config/feishu-status')
}

export async function getSensorTowerStatus() {
  return json<{ installed: boolean; available: boolean; api_ok: boolean; error: string }>('/config/st-status')
}

// ---- POC 验证 ----

export interface PocEvalInput {
  idea_name: string
  idea_brief: string
  target_users: string
  pain_points: string
  simple_product: string
}

export interface PocEvalDimension {
  verdict: boolean
  description?: string
  reason: string
  suggestion: string
}

export interface PocEvalResult {
  id: string
  timestamp: string
  input: PocEvalInput
  evaluation: {
    clear_users: PocEvalDimension
    real_needs: PocEvalDimension
    simple_product: PocEvalDimension
    overall_verdict: string
    summary: string
  }
}

export interface OpportunityPoint {
  title: string
  description: string
  target_users: string
  pain_points: string
  features: string[]
  simple_product: string
  eval_id?: string
}

export function extractOpportunities(reportContent: string | Record<string, unknown>, reportFilename?: string) {
  return json<{ opportunities: OpportunityPoint[] }>('/poc-evaluate/extract-opportunities', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ report_content: reportContent, report_filename: reportFilename || '' }),
  })
}

export function runPocEvaluation(input: PocEvalInput & { report_filename?: string; opportunity_index?: number }) {
  return json<PocEvalResult>('/poc-evaluate', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  })
}

export function getPocEvalResult(evalId: string) {
  return json<PocEvalResult>(`/poc-evaluate/${evalId}`)
}

// ===== 用户画像建模 =====

export interface PersonaCallbacks {
  onProgress?: (data: { progress: number; message: string }) => void
  onDone?: (data: { personas: unknown[] }) => void
  onError?: (data: { message: string }) => void
}

export async function streamGeneratePersonas(
  needIndex: number,
  callbacks: PersonaCallbacks,
  signal?: AbortSignal,
  options?: { language?: 'zh-CN' | 'en-US' },
) {
  let res: Response
  try {
    res = await fetch(BASE + '/generate-personas', {
      method: 'POST',
      headers: sessionHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ need_index: needIndex, ...(options?.language ? { language: options.language } : {}) }),
      signal,
    })
  } catch (err) {
    if (signal?.aborted) return
    callbacks.onError?.({ message: normalizeApiError(err) })
    return
  }

  if (!res.ok || !res.body) {
    const errText = await res.text().catch(() => `HTTP ${res.status}`)
    callbacks.onError?.({ message: normalizeApiError(errText, res.status) })
    return
  }

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let currentEvent = ''
  let settled = false

  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break

      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop() || ''

      for (const line of lines) {
        if (line.startsWith('event: ')) {
          currentEvent = line.slice(7).trim()
        } else if (line.startsWith('data: ')) {
          try {
            const data = JSON.parse(line.slice(6))
            switch (currentEvent) {
              case 'persona_progress':
                callbacks.onProgress?.(data)
                break
              case 'persona_done':
                settled = true
                callbacks.onDone?.(data)
                break
              case 'persona_error':
              case 'error':
                settled = true
                callbacks.onError?.(data)
                break
            }
          } catch { /* skip malformed */ }
          currentEvent = ''
        }
      }
    }
    if (!settled && !signal?.aborted) {
      callbacks.onError?.({ message: normalizeApiError('stream') })
    }
  } catch (err) {
    if (!settled && !signal?.aborted) {
      callbacks.onError?.({ message: normalizeApiError(err) })
    }
  }
}


// ===== 快速搜索（Quick Search） =====

export interface QuickSearchParams {
  query: string
  language?: 'zh-CN' | 'en-US'
  time_period?: string
  min_score?: number
  limit?: number
  fetch_comments?: boolean
  market_search?: boolean
  market_time_period?: string
  strategy?: 'auto' | 'community' | 'competitor' | 'hybrid'
}

export interface QuickSearchPost {
  title: string
  title_zh: string
  content: string
  content_zh: string
  url: string
  score: number
  num_comments: number
  source: string
  created_utc: number
  process_dimensions?: string[]
  process_actions?: string[]
  process_scopes?: string[]
  comments: { body: string; body_zh: string; score: number }[]
}

export interface QuickSearchMarketApp {
  name: string
  publisher?: string
  icon_url?: string
  store_url?: string
  app_store_url?: string
  sensor_tower_url?: string
  revenue?: number
  revenue_display?: string
  downloads?: number
  downloads_display?: string
  growth_pct?: number | null
  downloads_growth_pct?: number | null
  dau_display?: string
  matched_queries?: string[]
  is_target_app?: boolean
}

export interface QuickSearchMarketTrendRow {
  app: string
  app_order?: number
  publisher?: string
  region?: string
  platform?: string
  revenue?: number
  revenue_display?: string
  revenue_previous?: number
  revenue_previous_display?: string
  revenue_growth_pct?: number | null
  downloads?: number
  downloads_display?: string
  downloads_previous?: number
  downloads_previous_display?: string
  downloads_growth_pct?: number | null
  rpd?: number | null
  rpd_display?: string
  rpd_60d?: number | null
  rpd_60d_display?: string
  flags?: string[]
  app_store_url?: string
  sensor_tower_url?: string
}

export interface QuickSearchMarketSeriesPoint {
  date: string
  revenue?: number
  downloads?: number
  rpd?: number | null
}

export interface QuickSearchMarketSeries {
  key: string
  app?: string
  publisher?: string
  region?: string
  platform?: string
  label?: string
  app_store_url?: string
  sensor_tower_url?: string
  points: QuickSearchMarketSeriesPoint[]
}

export interface QuickSearchAppReview {
  id?: string | number
  app_id?: string
  platform?: string
  title?: string
  title_zh?: string
  username?: string
  country?: string
  sentiment?: string
  rating?: number | string
  tags?: string[]
  negative_topic_keys?: string[]
  negative_topics?: string[]
  content?: string
  content_zh?: string
  created_at?: string
  version?: string
  vote_sum?: string
  vote_count?: string
  review_url?: string
}

export interface QuickSearchReviewTopic {
  key: string
  label: string
  count: number
  percent: number
}

export interface QuickSearchReviewDistributionGroup {
  total: number
  summary?: string
  items: QuickSearchReviewTopic[]
}

export interface QuickSearchReviewDistribution {
  negative?: QuickSearchReviewDistributionGroup
  positive?: QuickSearchReviewDistributionGroup
  method?: string
  note?: string
}

export interface QuickSearchMarketSignal {
  available: boolean
  metric_trends?: boolean
  review_search?: boolean
  sentiment_filter?: 'negative' | 'positive' | 'all' | string
  source?: string
  source_channel?: string
  fallback?: string
  app?: QuickSearchMarketApp
  reviews?: QuickSearchAppReview[]
  review_distribution?: QuickSearchReviewDistribution
  requested_review_topic_key?: string
  requested_review_topic_label?: string
  countries?: string[]
  country_scope?: 'global' | 'specific' | string
  country_labels?: string[]
  total?: number
  all_total?: number
  negative_total?: number
  positive_total?: number
  raw_total?: number
  source_total?: number
  fetched_pages?: number
  page_count?: number
  apple_rss_empty?: boolean
  apple_rss_partial?: boolean
  apple_reviews_total?: number
  apple_max_raw_capacity?: number
  max_raw_capacity?: number
  metrics?: Array<'revenue' | 'downloads' | 'rpd' | string>
  market_region?: string
  candidate_region?: string
  metrics_region?: string
  metrics_time_period?: string
  sort_by?: 'revenue' | 'growth' | 'downloads' | string
  direct_app?: boolean
  direct_app_competitors?: boolean
  target_app?: QuickSearchMarketApp
  date_range?: { start?: string; end?: string; label?: string }
  queries?: string[]
  product_count?: number
  measured_product_count?: number
  revenue_sum?: number
  revenue_avg?: number
  downloads_sum?: number
  revenue_growth_pct?: number | null
  top_apps?: QuickSearchMarketApp[]
  table_rows?: QuickSearchMarketTrendRow[]
  time_series?: {
    available?: boolean
    granularity?: 'day' | 'week' | 'month' | string
    metrics?: Array<'revenue' | 'downloads' | 'rpd' | string>
    date_range?: { start?: string; end?: string; label?: string }
    regions?: string[]
    rows?: QuickSearchMarketTrendRow[]
    series?: QuickSearchMarketSeries[]
    error?: string
  }
  comparison_range?: { start?: string; end?: string; label?: string }
  regions?: string[]
  highlights?: {
    app?: string
    region?: string
    platform?: string
    flag?: string
    revenue_growth_pct?: number | null
    downloads_growth_pct?: number | null
    revenue_delta_display?: string
    downloads_delta_display?: string
  }[]
  items?: unknown[]
  error?: string
  planning_source?: string
  planning_confidence?: number
  planning_issue?: string
}

export interface QuickSearchHistoryItem {
  id: string
  query: string
  timestamp: number
  summary: string
  posts: QuickSearchPost[]
  marketSignal: QuickSearchMarketSignal | null
  totalSearched?: number
}

export function getQuickSearchHistory() {
  return json<{ items: QuickSearchHistoryItem[] }>('/quick-search/history')
}

export function saveQuickSearchHistory(items: QuickSearchHistoryItem[]) {
  return json<{ ok: boolean; items: QuickSearchHistoryItem[] }>('/quick-search/history', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ items }),
  })
}

export function translateQuickSearchReviews(reviews: QuickSearchAppReview[]) {
  return json<{ ok: boolean; reviews: Array<{ id?: string | number; title_zh?: string; content_zh?: string }> }>('/quick-search/reviews/translate', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reviews }),
  })
}

export interface QuickSearchCallbacks {
  onProgress?: (data: { message: string; progress: number; plan?: { queries: string[]; subreddits: string[]; reasoning: string } }) => void
  onMarket?: (data: QuickSearchMarketSignal) => void
  onPosts?: (data: { posts: QuickSearchPost[]; total: number; total_searched?: number }) => void
  onSummaryChunk?: (data: { text: string }) => void
  onError?: (data: { message: string; placement?: 'composer'; kind?: string }) => void
  onDone?: () => void
}

export async function streamQuickSearch(
  params: QuickSearchParams,
  callbacks: QuickSearchCallbacks,
  signal?: AbortSignal,
) {
  let res: Response
  try {
    res = await fetch(BASE + '/quick-search', {
      method: 'POST',
      headers: sessionHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify(params),
      signal,
    })
  } catch (err) {
    if (signal?.aborted) return
    callbacks.onError?.({ message: normalizeApiError(err) })
    return
  }

  if (!res.ok || !res.body) {
    const errText = await res.text().catch(() => `HTTP ${res.status}`)
    callbacks.onError?.({ message: normalizeApiError(errText, res.status) })
    return
  }

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let currentEvent = ''

  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break

      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop() || ''

      for (const line of lines) {
        if (line.startsWith('event: ')) {
          currentEvent = line.slice(7).trim()
        } else if (line.startsWith('data: ')) {
          try {
            const data = JSON.parse(line.slice(6))
            switch (currentEvent) {
              case 'qs_progress':
                callbacks.onProgress?.(data)
                break
              case 'qs_posts':
                callbacks.onPosts?.(data)
                break
              case 'qs_market':
                callbacks.onMarket?.(data)
                break
              case 'qs_summary_chunk':
                callbacks.onSummaryChunk?.(data)
                break
              case 'error':
                callbacks.onError?.(data)
                break
              case 'done':
                callbacks.onDone?.()
                break
            }
          } catch { /* skip malformed */ }
          currentEvent = ''
        }
      }
    }
  } catch (err) {
    if (!signal?.aborted) {
      callbacks.onError?.({ message: normalizeApiError(err) })
    }
  }
}

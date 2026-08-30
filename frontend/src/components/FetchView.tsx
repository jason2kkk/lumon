import { useState, useEffect, useRef, useCallback } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import {
  Loader2, TrendingUp, MessageSquare,
  ExternalLink, AlertCircle, ChevronDown,
  BarChart3,
  CheckCircle2, Quote,
} from 'lucide-react'
import confetti from 'canvas-confetti'
import { useAppStore } from '../stores/app'
import {
  clearNeeds, getNeeds, getConfigStatus, streamFetchNeeds,
  getRedditCategories, getEngineStatus,
  getFetchStatus, stopFetch, streamGenerateReport,
  getSensorTowerStatus, getReportGenStatus, streamReportGenResume,
  listReports,
  trackAnalyticsEvent,
} from '../api/client'
import type { FetchParams, RedditCategory } from '../api/client'
import type { Need, FemwcDimension, MarketCompetitorSignal } from '../types'
import ConfirmDialog from './ConfirmDialog'
import { HelpButton, FETCH_HELP } from './HelpDialog'
import { CountUp, ShimmerText, RotatingText, ShineBorder, InteractiveHoverButton } from './animations'
import SplitText from './SplitText'
import { LocalizedText, translate, useI18n } from '../i18n'
import { safeExternalUrl } from '../safeUrls'

type FetchMode = 'sentence' | 'keywords' | 'open'
type MiningMode = 'fast' | 'deep'

const MINING_MODE_STORAGE_KEY = 'lumon_mining_mode_v1'
const FETCH_PROGRESS_SHINE_COLORS = ["#ff6b6b", "#feca57", "#48dbfb", "#ff9ff3", "#54a0ff", "#5f27cd"]
const FETCH_PROGRESS_SHINE_COLORS_DARK = ["#ff8585", "#ffd477", "#6fe4ff", "#ffb4f6", "#74b3ff", "#7b4ee0"]

const MODE_OPTIONS: { id: FetchMode; labelKey: string; descKey: string; img: string }[] = [
  { id: 'sentence', labelKey: 'fetch.sentenceMode', descKey: 'fetch.sentenceModeDesc', img: '/book_6_ai_line.png' },
  { id: 'open', labelKey: 'fetch.openMode', descKey: 'fetch.openModeDesc', img: '/mind_map_line.png' },
]

function i18nText(key: string): string {
  return String(translate(useAppStore.getState().languageMode, key))
}

function i18nFormat(key: string, params: Record<string, string | number>): string {
  return i18nText(key).replace(/\{(\w+)\}/g, (_, name: string) => String(params[name] ?? `{${name}}`))
}

function sourceLabel(source: string): string {
  const value = source.trim().toLowerCase()
  if (value.startsWith('synthetic/')) return 'Demo'
  if (value.startsWith('reddit')) return 'Reddit'
  if (value === 'hn' || value.includes('hackernews')) return 'HN'
  return source.trim() || 'Other'
}

function isSyntheticDemoNeed(need?: Need): boolean {
  return Boolean(need?.posts.length) && need!.posts.every((post) => (
    post.source.trim().toLowerCase().startsWith('synthetic/')
    || post._engine === 'synthetic-demo'
  ))
}

function localizedMarketValidationLabel(level: string, rawLabel?: string): string {
  const levelLabelMap: Record<string, string> = {
    strong: i18nText('fetch.businessStrong'),
    medium: i18nText('fetch.businessMedium'),
    validated: i18nText('fetch.businessMedium'),
    early: i18nText('fetch.businessWeak'),
    weak: i18nText('fetch.businessWeak'),
    unknown: i18nText('fetch.businessWeak'),
  }
  if (levelLabelMap[level]) return levelLabelMap[level]

  const label = (rawLabel || '').trim()
  const legacyLabelMap: Record<string, string> = {
    商业信号强: i18nText('fetch.businessStrong'),
    商业强: i18nText('fetch.businessStrong'),
    商业化信号强: i18nText('fetch.businessStrong'),
    商业信号中: i18nText('fetch.businessMedium'),
    商业中: i18nText('fetch.businessMedium'),
    商业化信号中: i18nText('fetch.businessMedium'),
    商业信号弱: i18nText('fetch.businessWeak'),
    商业弱: i18nText('fetch.businessWeak'),
    商业化信号弱: i18nText('fetch.businessWeak'),
    市场待验证: i18nText('fetch.businessWeak'),
  }
  return legacyLabelMap[label] || label || i18nText('fetch.marketCompetitors')
}

function currentLanguageIsEnglish(): boolean {
  return useAppStore.getState().languageMode === 'en-US'
}

function textHasCjk(value?: string): boolean {
  return /[\u3400-\u9fff]/.test(value || '')
}

function localizeSignalLabel(value?: string): string {
  const raw = String(value || '').trim()
  if (!raw) return i18nText('fetch.evidence')
  const normalized = raw.toLowerCase()
  const map: Record<string, string> = {
    pain: i18nText('fetch.painSignal'),
    pain_point: i18nText('fetch.painSignal'),
    workaround: i18nText('fetch.workaroundSignal'),
    willingness_to_pay: i18nText('fetch.paymentSignal'),
    payment: i18nText('fetch.paymentSignal'),
    competitor_complaint: i18nText('fetch.competitorComplaintSignal'),
    journey: i18nText('fetch.journeySignal'),
    software_solvable: currentLanguageIsEnglish() ? 'Software-solvable' : '软件可解',
    alternative: currentLanguageIsEnglish() ? 'Alternatives' : '替代方案',
    resonance: currentLanguageIsEnglish() ? 'Comment resonance' : '评论共鸣',
    early_signal: currentLanguageIsEnglish() ? 'Early signal' : '早期信号',
    痛点: i18nText('fetch.painSignal'),
    软件可解: currentLanguageIsEnglish() ? 'Software-solvable' : '软件可解',
    替代方案: currentLanguageIsEnglish() ? 'Alternatives' : '替代方案',
    评论共鸣: currentLanguageIsEnglish() ? 'Comment resonance' : '评论共鸣',
    早期信号: currentLanguageIsEnglish() ? 'Early signal' : '早期信号',
    付费意愿: i18nText('fetch.paymentSignal'),
    竞品吐槽: i18nText('fetch.competitorComplaintSignal'),
    用户旅程: i18nText('fetch.journeySignal'),
  }
  return map[normalized] || map[raw] || (currentLanguageIsEnglish() && textHasCjk(raw) ? i18nText('fetch.evidence') : raw)
}

function localizeEvidenceSummary(summary?: string): string {
  const raw = String(summary || '').trim()
  if (!raw || !currentLanguageIsEnglish()) return raw
  const count = raw.match(/(\d+)\s*条/)?.[1]
  const signalParts = [
    raw.includes('痛点') ? i18nText('fetch.painSignal').toLowerCase() : '',
    raw.includes('软件可解') ? 'software-solvable signals' : '',
    raw.includes('替代方案') ? 'alternatives' : '',
    raw.includes('评论共鸣') ? 'comment resonance' : '',
    raw.includes('早期信号') ? 'early signals' : '',
    raw.includes('付费意愿') ? 'payment intent' : '',
    raw.includes('竞品吐槽') ? 'competitor complaints' : '',
  ].filter(Boolean)
  if (count) {
    return `${count} traceable evidence items${signalParts.length ? ` covering ${signalParts.join(', ')}` : ''}`
  }
  if (!textHasCjk(raw)) return raw
  return 'Traceable evidence covering core demand signals'
}

function localizeMarketRiskNote(note?: string): string {
  const raw = String(note || '').trim()
  if (!raw || !currentLanguageIsEnglish()) return raw
  if (!textHasCjk(raw)) return raw
  if (raw.includes('未匹配到稳定竞品商业化信号')) {
    return 'SensorTower did not find stable competitor commercialization signals (US candidates; global revenue/download metrics).'
  }
  if (raw.includes('未完成稳定商业化校验')) {
    return 'SensorTower did not complete stable commercialization validation. Displaying a conservative weak signal.'
  }
  if (raw.includes('状态检测失败')) {
    return 'SensorTower status check failed. Displaying a conservative weak signal.'
  }
  if (raw.includes('未生成稳定竞品结果')) {
    return 'SensorTower did not return stable competitor results. Displaying a conservative weak signal.'
  }
  if (raw.includes('校验失败')) {
    return 'SensorTower validation failed. Displaying a conservative weak signal.'
  }
  if (raw.includes('未进入本轮')) {
    return 'This demand was not included in the current SensorTower deep validation batch. Displaying a conservative weak signal.'
  }
  return i18nText('fetch.marketNoDetails')
}

function getStoredMiningMode(): MiningMode {
  try {
    return localStorage.getItem(MINING_MODE_STORAGE_KEY) === 'deep' ? 'deep' : 'fast'
  } catch {
    return 'fast'
  }
}

function setStoredMiningMode(mode: MiningMode) {
  try {
    localStorage.setItem(MINING_MODE_STORAGE_KEY, mode)
  } catch {
    // 本地 UI 偏好保存失败不影响挖掘
  }
}


function localizedNeedTitle(need: Need, isEnglish: boolean): string {
  if (isEnglish && need.need_title_en?.trim()) return need.need_title_en.trim()
  return need.need_title
}

function localizedNeedDescription(need: Need, isEnglish: boolean): string {
  if (isEnglish && need.need_description_en?.trim()) return need.need_description_en.trim()
  return need.need_description
}

function _buildHistoryTitle(mode: string, sentence: string, keywordsText: string, needs: Need[], isEnglish = false): string {
  if (mode === 'sentence' && sentence.trim()) {
    return sentence.trim()
  }
  if (mode === 'keywords' && keywordsText.trim()) {
    const kws = keywordsText.split(/[,，\s]+/).filter(Boolean).slice(0, 3)
    return `${kws.join('·')} ${i18nText('fetch.relatedNeedsSuffix')}`
  }
  if (needs.length > 0) {
    return localizedNeedTitle(needs[0], isEnglish)
  }
  return i18nText('fetch.autonomousTitle')
}

function formatOpportunityScore(score?: number): string | null {
  if (typeof score !== 'number' || !Number.isFinite(score)) return null
  return score.toFixed(1)
}

function formatMarketValidationBadge(need: Need): { label: string; className: string; title: string } | null {
  const market = need.market_validation
  if (!market) {
    const label = i18nText('fetch.businessWeak')
    return {
      label,
      className: 'bg-gray-50 text-gray-500 border-gray-200/70',
      title: `${i18nFormat('fetch.marketValidationTitle', { label })}\n${i18nText('fetch.marketNoDetails')}`,
    }
  }

  const level = String(market.level || 'unknown').toLowerCase()
  const label = localizedMarketValidationLabel(level, market.label)

  const classMap: Record<string, string> = {
    strong: 'bg-emerald-50 text-emerald-700 border-emerald-200/70',
    medium: 'bg-sky-50 text-sky-700 border-sky-200/70',
    validated: 'bg-sky-50 text-sky-700 border-sky-200/70',
    early: 'bg-gray-50 text-gray-500 border-gray-200/70',
    weak: 'bg-gray-50 text-gray-500 border-gray-200/70',
    unknown: 'bg-gray-50 text-gray-500 border-gray-200/70',
  }
  const competitors = market.top_competitors?.map((item) => item.name).filter(Boolean).slice(0, 3).join('、')
  const aggregate = formatMarketAggregateSignal(need)
  const titleParts = [
    i18nFormat('fetch.marketValidationTitle', { label }),
    competitors ? i18nFormat('fetch.relatedCompetitors', { competitors }) : '',
    aggregate,
    localizeMarketRiskNote(market.risk_note) || '',
  ].filter(Boolean)

  return {
    label,
    className: classMap[level] || 'bg-gray-50 text-gray-500 border-gray-200/70',
    title: titleParts.join('\n'),
  }
}

function formatCurrencySignal(value?: number, display?: string): string {
  if (display && display !== '-') return display
  if (typeof value !== 'number' || !Number.isFinite(value) || value <= 0) return ''
  if (value >= 1_000_000) return `$${(value / 1_000_000).toFixed(1)}M`
  if (value >= 100_000) return `$${(value / 1_000).toFixed(1).replace(/\.0$/, '')}K`
  if (value >= 1_000) return `$${Math.round(value / 1_000)}K`
  return `$${Math.round(value)}`
}

function formatMarketAggregateSignal(need: Need): string {
  const market = need.market_validation
  if (!market) return ''
  const maxRevenue = formatCurrencySignal(
    market.max_monthly_revenue || market.signal_max_monthly_revenue,
    market.max_monthly_revenue_display,
  )
  const totalRevenue = formatCurrencySignal(
    market.total_peer_revenue || market.signal_total_peer_revenue,
    market.total_peer_revenue_display,
  )
  const competitorCount = market.competitor_count || market.signal_competitor_count
  const parts = [
    competitorCount ? i18nFormat('fetch.candidateCompetitors', { count: competitorCount }) : '',
    maxRevenue ? i18nFormat('fetch.maxMonthlyRevenue', { value: maxRevenue }) : '',
    totalRevenue ? i18nFormat('fetch.totalPeerRevenue', { value: totalRevenue }) : '',
  ].filter(Boolean)
  return parts.join(' · ')
}

function formatCompetitorRevenue(item: MarketCompetitorSignal): string {
  if (item.revenue_display && item.revenue_display !== '-') {
    return i18nFormat('fetch.monthlyRevenueApprox', { value: item.revenue_display })
  }
  if (typeof item.revenue === 'number' && Number.isFinite(item.revenue) && item.revenue > 0) {
    if (item.revenue >= 1_000_000) return i18nFormat('fetch.monthlyRevenueApprox', { value: `$${(item.revenue / 1_000_000).toFixed(1)}M` })
    if (item.revenue >= 1_000) return i18nFormat('fetch.monthlyRevenueApprox', { value: `$${Math.round(item.revenue / 1_000)}K` })
    return i18nFormat('fetch.monthlyRevenueApprox', { value: `$${Math.round(item.revenue)}` })
  }
  return i18nText('fetch.monthlyRevenueEmpty')
}

function formatMarketScope(need: Need): string {
  const market = need.market_validation
  if (!market) return ''
  const regionMap: Record<string, string> = {
    WW: i18nText('fetch.global'),
    US: i18nText('fetch.usMarket'),
  }
  const rawRegion = String(market.market_region || 'WW').trim()
  const normalizedRegion = rawRegion === '全球' ? 'WW' : rawRegion.toUpperCase()
  const region = regionMap[normalizedRegion] || (currentLanguageIsEnglish() && textHasCjk(rawRegion) ? i18nText('fetch.global') : rawRegion) || i18nText('fetch.global')
  const start = market.date_range?.start
  const end = market.date_range?.end
  if (start && end) return `${region} · ${start}~${end}`
  return region
}

function isDeepFetchStrategy(strategy: string): boolean {
  return strategy === 'deep'
}

function estimateFetchSeconds(options: {
  mode: FetchMode
  sources: string[]
  fetchModelStrategy: string
  hasCustomFetchParams: boolean
  isDemo: boolean
}): number {
  if (options.isDemo) return 22
  const hasReddit = options.sources.includes('reddit')
  const hasHackerNews = options.sources.includes('hackernews')
  const deep = isDeepFetchStrategy(options.fetchModelStrategy)
  const base = 35
  const webSearch = options.mode !== 'open' ? 115 : 40
  const redditTime = hasReddit ? 90 : 0
  const hackerNewsTime = hasHackerNews ? 35 : 0
  const openModeTime = options.mode === 'open' ? 45 : 0
  const deepSearchTime = deep ? 85 : 0
  const evidenceProbeTime = deep && hasReddit ? 45 : 0
  const customParamTime = options.hasCustomFetchParams ? 20 : 0
  return Math.max(
    180,
    Math.min(
      600,
      base + webSearch + redditTime + hackerNewsTime + openModeTime + deepSearchTime + evidenceProbeTime + customParamTime,
    ),
  )
}

function formatClock(totalSeconds: number): string {
  const safe = Math.max(0, Math.floor(totalSeconds))
  const minutes = Math.floor(safe / 60)
  const seconds = safe % 60
  return `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`
}

export default function FetchView() {
  const { text, list, language, isEnglish } = useI18n()
  const {
    needs, setNeeds, selectedNeedIndex, setSelectedNeed,
    setActiveView, resetDebate, debateStatus,
    configReady, setConfigReady, setShowSettingsDialog,
    addFetchHistory, loadFetchHistory,
    activeFetchHistoryId, fetchHistory, setActiveFetchHistory,
    dataSources: sources, loadDataSources,
    reportGenIdx, reportGenProgress, reportGenMsg,
    setReportGenIdx, setReportGenProgress, setReportGenMsg,
    fetchLoading: loading, fetchProgress: progress,
    fetchProgressHistory: progressHistory, fetchError: error,
    fetchDone, needsEpoch,
    setFetchLoading: setLoading, setFetchProgress: setProgress,
    setFetchProgressHistory: setProgressHistory,
    appendFetchProgressHistory,
    setFetchError: setError, setFetchDone,
    resetFetchProgress,
    themeMode,
  } = useAppStore()

  const isViewingHistory = activeFetchHistoryId !== null
  const activeHistoryItem = isViewingHistory
    ? fetchHistory.find(h => h.id === activeFetchHistoryId)
    : null

  const [mode, setMode] = useState<FetchMode>('sentence')
  const [openModeTextKey, setOpenModeTextKey] = useState(0)
  const [openPromptShimmerReady, setOpenPromptShimmerReady] = useState(false)
  const [sentence, setSentence] = useState('')
  const [keywordsText] = useState('')
  const [category] = useState('ask')
  const [limit] = useState(70)
  const [timePeriod, setTimePeriod] = useState<'month' | '3months' | '6months' | '9months'>('6months')
  const [product, setProduct] = useState('')
  const [market, setMarket] = useState('')
  const [demographics, setDemographics] = useState('')
  const [segment, setSegment] = useState('')
  const [competitors, setCompetitors] = useState('')
  const [miningMode, setMiningMode] = useState<MiningMode>(() => getStoredMiningMode())
  const [miningModeOpen, setMiningModeOpen] = useState(false)
  const [showAdvanced, setShowAdvanced] = useState(false)
  const [tagsExpanded, setTagsExpanded] = useState(false)
  const [openTimePeriod, setOpenTimePeriod] = useState(false)
  const [expandedIdx, setExpandedIdx] = useState<number | null>(null)
  const [collapsingIdx, setCollapsingIdx] = useState<number | null>(null)
  const [progressCollapsed, setProgressCollapsed] = useState(() => fetchDone)
  const [progressDismissed, setProgressDismissed] = useState(false)
  const [smoothProgress, setSmoothProgress] = useState(0)
  const smoothRef = useRef({ real: 0, display: 0, lastUpdate: Date.now() })
  const [estimatedSeconds, setEstimatedSeconds] = useState(0)
  const [elapsedSeconds, setElapsedSeconds] = useState(0)
  const startTimeRef = useRef<number>(0)
  const abortRef = useRef<AbortController | null>(null)
  const fetchingRef = useRef(false)
  const historyAddedRef = useRef(false)
  const progressScrollRef = useRef<HTMLDivElement | null>(null)
  const userScrolledUpRef = useRef(false)
  const miningModeTriggerRef = useRef<HTMLDivElement | null>(null)
  const miningModeMenuRef = useRef<HTMLDivElement | null>(null)

  const [redditCategories, setRedditCategories] = useState<Record<string, RedditCategory>>({})
  const [selectedRedditCats, setSelectedRedditCats] = useState<string[]>([])
  const [engineName, setEngineName] = useState<string>('')
  const [stConnected, setStConnected] = useState<boolean | null>(null)
  const reportGenAbort = useRef<AbortController | null>(null)
  const collapseTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const [reportMap, setReportMap] = useState<Map<string, string>>(new Map())
  const [personaCacheKeys, setPersonaCacheKeys] = useState<Set<string>>(new Set())
  const hasRedditCategoryOptions = sources.includes('reddit') && Object.keys(redditCategories).length > 0
  const canUseRedditCategoryPill = mode !== 'open' && hasRedditCategoryOptions
  const selectedRedditCatLabel = selectedRedditCats.length > 0
    ? redditCategories[selectedRedditCats[0]]?.label || text('fetch.restrictedTrack')
    : text('fetch.restrictedTrack')
  const hasCustomFetchParams = timePeriod !== '6months'
    || [product, market, demographics, segment, competitors].some((value) => value.trim())
  const advancedPillLabel = hasCustomFetchParams ? text('fetch.paramsSet') : text('fetch.params')
  const effectiveMiningMode: MiningMode = mode === 'open' ? 'deep' : miningMode
  const effectiveFetchModelStrategy = effectiveMiningMode === 'deep' ? 'deep' : 'default'
  const miningModeInfo = effectiveMiningMode === 'deep'
    ? { label: text('fetch.deepMode'), desc: text('fetch.deepModeDesc') }
    : { label: text('fetch.quickMode'), desc: text('fetch.quickModeDesc') }
  const fetchProgressShineColors = themeMode === 'dark'
    ? FETCH_PROGRESS_SHINE_COLORS_DARK
    : FETCH_PROGRESS_SHINE_COLORS
  const fetchTimeOptions = [
    { value: 'month' as const, label: text('fetch.timeMonth') },
    { value: '3months' as const, label: text('fetch.time3Months') },
    { value: '6months' as const, label: text('fetch.time6Months') },
    { value: '9months' as const, label: text('fetch.time9Months') },
  ]
  const selectedTimeLabel = fetchTimeOptions.find((option) => option.value === timePeriod)?.label || text('fetch.time6Months')

  useEffect(() => {
    if (!miningModeOpen) return

    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target as Node | null
      if (!target) return
      if (miningModeTriggerRef.current?.contains(target)) return
      if (miningModeMenuRef.current?.contains(target)) return
      setMiningModeOpen(false)
    }

    document.addEventListener('pointerdown', handlePointerDown, true)
    return () => document.removeEventListener('pointerdown', handlePointerDown, true)
  }, [miningModeOpen])

  const handleMiningModeChange = (next: MiningMode) => {
    if (loading) return
    if (mode === 'open' && next === 'fast') return
    setMiningMode(next)
    setStoredMiningMode(next)
    setMiningModeOpen(false)
  }

  const toggleMiningModeMenu = () => {
    if (loading) return
    const next = !miningModeOpen
    setMiningModeOpen(next)
    if (next) {
      setTagsExpanded(false)
      setShowAdvanced(false)
      setOpenTimePeriod(false)
    }
  }

  const toggleTagsPanel = () => {
    if (loading) return
    const next = !tagsExpanded
    setTagsExpanded(next)
    if (next) {
      setMiningModeOpen(false)
      setShowAdvanced(false)
      setOpenTimePeriod(false)
    }
  }

  const toggleAdvancedPanel = () => {
    if (loading) return
    const next = !showAdvanced
    setShowAdvanced(next)
    if (next) {
      setMiningModeOpen(false)
      setTagsExpanded(false)
      setOpenTimePeriod(false)
    }
  }

  const handleModeChange = (nextMode: FetchMode) => {
    if (loading) return
    if (nextMode === 'open' && mode !== 'open') {
      setOpenPromptShimmerReady(false)
      setOpenModeTextKey((value) => value + 1)
      setTagsExpanded(false)
    }
    setMode(nextMode)
  }

  const handleToggleNeedCard = (idx: number) => {
    if (collapseTimerRef.current) {
      clearTimeout(collapseTimerRef.current)
      collapseTimerRef.current = null
    }
    const scheduleCollapseEnd = (closingIdx: number) => {
      collapseTimerRef.current = setTimeout(() => {
        setCollapsingIdx((current) => (current === closingIdx ? null : current))
        collapseTimerRef.current = null
      }, 260)
    }
    if (expandedIdx === idx) {
      setCollapsingIdx(idx)
      setExpandedIdx(null)
      scheduleCollapseEnd(idx)
      return
    }
    if (expandedIdx !== null) {
      setCollapsingIdx(expandedIdx)
      scheduleCollapseEnd(expandedIdx)
    } else {
      setCollapsingIdx(null)
    }
    setExpandedIdx(idx)
  }

  const displayNeedTitle = useCallback((need: Need) => {
    return localizedNeedTitle(need, isEnglish)
  }, [isEnglish])

  const displayNeedDescription = useCallback((need: Need) => {
    return localizedNeedDescription(need, isEnglish)
  }, [isEnglish])

  useEffect(() => {
    return () => {
      if (collapseTimerRef.current) {
        clearTimeout(collapseTimerRef.current)
      }
    }
  }, [])

  const [reportSubMsg, setReportSubMsg] = useState('')
  useEffect(() => {
    if (reportGenIdx === null) { setReportSubMsg(''); return }
    const reportMessages = list('reports.writingMessages').slice(0, 7)
    if (reportMessages.length === 0) { setReportSubMsg(''); return }
    let idx = 0
    setReportSubMsg(reportMessages[0])
    const timer = setInterval(() => {
      idx = (idx + 1) % reportMessages.length
      setReportSubMsg(reportMessages[idx])
    }, 6000)
    return () => clearInterval(timer)
  }, [reportGenIdx, list])

  const fireSideCannons = useCallback(() => {
    const colors = ['#26ccff', '#a25afd', '#ff5e7e', '#88ff5a', '#fcff42', '#ffa62d']
    const base = { colors, gravity: 0.9, ticks: 300, disableForReducedMotion: true }
    confetti({ ...base, particleCount: 200, angle: 55, spread: 70, origin: { x: 0, y: 0.55 }, startVelocity: 55 })
    confetti({ ...base, particleCount: 200, angle: 125, spread: 70, origin: { x: 1, y: 0.55 }, startVelocity: 55 })
  }, [])

  useEffect(() => {
    if (progress !== smoothRef.current.real) {
      smoothRef.current.real = progress
      smoothRef.current.lastUpdate = Date.now()
      if (progress >= smoothRef.current.display) {
        smoothRef.current.display = progress
        setSmoothProgress(progress)
      }
    }
  }, [progress])

  useEffect(() => {
    const el = progressScrollRef.current
    if (!el || !loading) return
    if (!userScrolledUpRef.current) {
      el.scrollTop = el.scrollHeight
    }
  }, [progressHistory.length, loading])

  useEffect(() => {
    if (!loading) {
      userScrolledUpRef.current = false
      smoothRef.current = { real: 0, display: 0, lastUpdate: Date.now() }
      setSmoothProgress(fetchDone ? 100 : 0)
      return
    }
    const timer = setInterval(() => {
      const s = smoothRef.current
      if (s.display >= 98) return
      const elapsed = (Date.now() - s.lastUpdate) / 1000
      const gap = s.real - s.display
      let increment: number
      if (gap > 1) {
        increment = Math.max(gap * 0.15, 0.5)
      } else {
        const speed = s.display < 30 ? 0.6 : s.display < 60 ? 0.35 : s.display < 85 ? 0.2 : 0.08
        increment = speed * Math.min(elapsed, 2)
        const ceiling = Math.min(s.real + 8, 98)
        if (s.display + increment > ceiling) {
          increment = Math.max(ceiling - s.display, 0)
        }
      }
      if (increment > 0.05) {
        s.display = Math.min(s.display + increment, 100)
        setSmoothProgress(Math.round(s.display * 10) / 10)
      }
    }, 800)
    return () => clearInterval(timer)
  }, [loading, fetchDone])

  const { confettiFired, setConfettiFired } = useAppStore()
  const confettiTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => {
    if (fetchDone && needs.length > 0 && !confettiFired) {
      setConfettiFired(true)
      confettiTimerRef.current = setTimeout(() => fireSideCannons(), 1000)
    }
  }, [fetchDone, needs.length, confettiFired, setConfettiFired, fireSideCannons])

  useEffect(() => {
    if (!loading) {
      setElapsedSeconds(0)
      return
    }
    const tick = setInterval(() => {
      const nextElapsed = Math.floor((Date.now() - startTimeRef.current) / 1000)
      setElapsedSeconds(nextElapsed)
      setEstimatedSeconds((prev) => {
        if (prev <= 0) return prev
        return nextElapsed >= prev - 12 ? nextElapsed + 35 : prev
      })
    }, 1000)
    return () => clearInterval(tick)
  }, [loading])

  const refreshEngineStatus = () => {
    if (fetchingRef.current) return
    getEngineStatus()
      .then((s) => setEngineName(s.engine || ''))
      .catch(() => setEngineName(''))
    getSensorTowerStatus()
      .then((s) => setStConnected(s.available))
      .catch(() => setStConnected(false))
  }

  const refreshBadges = useCallback(() => {
    listReports().then(({ reports }) => {
      const map = new Map<string, string>()
      for (const r of reports) map.set(r.title.trim().toLowerCase(), r.filename)
      setReportMap(map)
    }).catch(() => {})
    try {
      const raw = localStorage.getItem('lumon_persona_cache_v2')
      if (raw) {
        const cache = JSON.parse(raw) as Record<string, unknown>
        setPersonaCacheKeys(new Set(Object.keys(cache)))
      }
    } catch { /* ignore */ }
  }, [])

  const loadRoleNames = useAppStore((s) => s.loadRoleNames)

  useEffect(() => { refreshBadges() }, [needs, refreshBadges])

  useEffect(() => {
    loadFetchHistory()
    loadDataSources()
    loadRoleNames()
    refreshBadges()
    getRedditCategories()
      .then((r) => setRedditCategories(r.categories))
      .catch(() => {})
    refreshEngineStatus()
    const enginePoll = setInterval(refreshEngineStatus, 30_000)

    // 如果当前不在查看历史且 needs 为空，尝试从后端加载
    if (!activeFetchHistoryId && needs.length === 0) {
      getNeeds().then((r) => {
        if (r.needs && r.needs.length > 0) setNeeds(r.needs)
      }).catch(() => {})
    }

    // Resume active fetch job after page refresh
    getFetchStatus(language).then((status) => {
      if (status.active) {
        fetchingRef.current = true
        setLoading(true)
        setProgress(status.progress)
        setProgressHistory(status.history)
        // Start polling for updates
        const poll = setInterval(async () => {
          try {
            const s = await getFetchStatus(language)
            setProgress(s.progress)
            setProgressHistory(s.history)
            if (s.error) {
              setError(s.error)
              setLoading(false)
              fetchingRef.current = false
              clearInterval(poll)
            }
            if (s.needs) {
              setNeeds(s.needs)
              setFetchDone(true)
              setLoading(false)
              fetchingRef.current = false
              clearInterval(poll)
              setTimeout(() => setProgressCollapsed(true), 2000)
        if (s.needs.length > 0 && !historyAddedRef.current) {
                historyAddedRef.current = true
                const historyTitle = _buildHistoryTitle(mode, sentence, keywordsText, s.needs, isEnglish)
                addFetchHistory({
                  id: `fetch-${Date.now()}`,
                  title: historyTitle,
                  mode,
                  query: mode === 'sentence' ? sentence : mode === 'keywords' ? keywordsText : i18nText('fetch.autonomousTitle'),
                  needs: s.needs,
                  createdAt: Date.now(),
                })
              }
            }
            if (!s.active && !s.needs && !s.error) {
              setLoading(false)
              fetchingRef.current = false
              clearInterval(poll)
            }
          } catch {
            clearInterval(poll)
            setLoading(false)
            fetchingRef.current = false
          }
        }, 500)
      }
    }).catch(() => {})

    // Resume active report generation after page refresh (only if still running)
    getReportGenStatus().then((rStatus) => {
      if (rStatus.active) {
        const resumeIdx = rStatus.need_index >= 0 ? rStatus.need_index : -1
        setReportGenIdx(resumeIdx)
        setReportGenProgress(rStatus.progress)
        setReportGenMsg(rStatus.message || i18nText('fetch.reportGenerating'))
        const abortCtrl = new AbortController()
        reportGenAbort.current = abortCtrl
        streamReportGenResume({
          onProgress: (data) => {
            setReportGenProgress(data.progress)
            setReportGenMsg(data.message)
          },
          onChunk: () => {},
          onDone: (data) => {
            setReportGenProgress(100)
            setReportGenMsg(i18nText('fetch.reportDone'))
            setTimeout(() => {
              setReportGenIdx(null)
              if (data?.filename) {
                useAppStore.getState().setPendingReportFile(data.filename)
              }
              setActiveView('reports')
            }, 600)
          },
          onError: (data) => {
            setReportGenMsg(`${i18nText('fetch.errorPrefix')}${data.message || i18nText('fetch.reportFailed')}`)
            setReportGenProgress(0)
            setTimeout(() => setReportGenIdx(null), 4000)
          },
        }, abortCtrl.signal)
      }
    }).catch(() => {})

    return () => clearInterval(enginePoll)
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const toggleRedditCat = (key: string) => {
    setSelectedRedditCats(prev =>
      prev.includes(key) ? [] : [key]
    )
  }

  const [confirmAction, setConfirmAction] = useState<{
    title: string; message: string; action: () => void
  } | null>(null)

  useEffect(() => {
    if (configReady === null) {
      getConfigStatus().then((s) => {
        setConfigReady((s as Record<string, unknown>).ready as boolean ?? (s.claude_ok || s.gpt_ok))
      }).catch(() => setConfigReady(false))
    }
  }, [configReady, setConfigReady])

  const hasActiveDebate = debateStatus !== 'idle' && debateStatus !== 'error'

  const canFetch = () => {
    if (sources.length === 0) return false
    if (mode === 'sentence' && !sentence.trim()) return false
    if (mode === 'keywords' && !keywordsText.trim()) return false
    return true
  }

  const [fetchHint, setFetchHint] = useState('')

  const handleFetch = () => {
    if (loading || fetchingRef.current) return
    if (!canFetch()) {
      if (mode === 'sentence' && !sentence.trim()) {
        setFetchHint(text('fetch.inputDirectionHint'))
      } else if (mode === 'keywords' && !keywordsText.trim()) {
        setFetchHint(text('fetch.inputKeywordHint'))
      } else if (sources.length === 0) {
        setFetchHint(text('fetch.chooseSourceHint'))
      }
      setTimeout(() => setFetchHint(''), 3000)
      return
    }
    setFetchHint('')
    if (hasActiveDebate) {
      setConfirmAction({
        title: text('fetch.activeDebateTitle'),
        message: text('fetch.activeDebateMessage'),
        action: doFetch,
      })
      return
    }
    doFetch()
  }

  const doFetch = async (options?: { demo?: boolean }) => {
    const isDemo = options?.demo ?? false
    setConfirmAction(null)

    if (abortRef.current) {
      abortRef.current.abort()
      abortRef.current = null
    }
    try {
      await stopFetch()
      await new Promise(r => setTimeout(r, 300))
    } catch { /* 无活跃任务时忽略 */ }

    fetchingRef.current = true
    historyAddedRef.current = false
    setNeeds([])
    resetDebate()
    setExpandedIdx(null)
    setLoading(true)
    setProgress(0)
    setProgressHistory([])
    setProgressCollapsed(false)
    setProgressDismissed(false)
    setFetchDone(false)
    setConfettiFired(false)
    setError('')
    setMiningModeOpen(false)
    const fetchModelStrategy = effectiveFetchModelStrategy

    startTimeRef.current = Date.now()
    setElapsedSeconds(0)
    trackAnalyticsEvent('fetch.start', {
      mode,
      demo: isDemo,
      sources_count: sources.length,
      has_reddit_categories: mode !== 'open' && selectedRedditCats.length > 0,
      has_custom_params: hasCustomFetchParams,
      time_period: timePeriod,
      fetch_model: fetchModelStrategy,
    })

    setEstimatedSeconds(estimateFetchSeconds({
      mode,
      sources,
      fetchModelStrategy,
      hasCustomFetchParams,
      isDemo,
    }))

    const params: FetchParams = isDemo
      ? { mode: 'sentence', query: '海量照片整理与回忆管理的用户痛点', sources: ['reddit', 'hackernews'], limit: 30, demo: true, language }
      : { mode, sources, limit, time_period: timePeriod, fetch_model: fetchModelStrategy, language }

    if (!isDemo) {
      if (product.trim()) params.product = product.trim()
      if (market.trim()) params.market = market.trim()
      if (demographics.trim()) params.demographics = demographics.trim()
      if (segment.trim()) params.segment = segment.trim()
      if (competitors.trim()) params.competitors = competitors.trim()

      if (mode === 'sentence') {
        params.query = sentence.trim()
      } else if (mode === 'keywords') {
        params.keywords = keywordsText.split(/[,，、\s]+/).filter(Boolean)
      } else {
        params.category = category
      }

      if (mode !== 'open' && sources.includes('reddit') && selectedRedditCats.length > 0) {
        params.reddit_categories = selectedRedditCats
      }
    }

    abortRef.current = new AbortController()

    streamFetchNeeds(params, {
      onProgress: (data) => {
        setProgress(data.progress)
        if (!isDemo && data.progress > 5 && data.progress < 96 && startTimeRef.current) {
          const elapsedNow = Math.floor((Date.now() - startTimeRef.current) / 1000)
          if (elapsedNow > 8) {
            const projectedTotal = Math.ceil((elapsedNow / Math.max(data.progress, 8)) * 100 * 1.08)
            setEstimatedSeconds((prev) => {
              const floor = elapsedNow + 35
              const target = Math.max(projectedTotal, floor)
              if (target > prev) {
                return Math.min(target, prev + 45)
              }
              if (target < prev - 30) {
                return Math.max(floor, Math.round(prev * 0.55 + target * 0.45))
              }
              return prev
            })
          }
        }
        appendFetchProgressHistory(data.message)
      },
      onResult: (data) => {
        setNeeds(data.needs)
        setFetchDone(true)
        trackAnalyticsEvent('fetch.result', {
          mode,
          demo: isDemo,
          fetch_model: fetchModelStrategy,
          needs_count: data.needs.length,
          posts_count: data.needs.reduce((sum, need) => sum + need.posts.length, 0),
          duration_ms: Date.now() - startTimeRef.current,
        })
        resetDebate()
        if (!isDemo && data.needs.length > 0 && !historyAddedRef.current) {
          historyAddedRef.current = true
          const historyTitle = _buildHistoryTitle(mode, sentence, keywordsText, data.needs, isEnglish)
          addFetchHistory({
            id: `fetch-${Date.now()}`,
            title: historyTitle,
            mode,
            query: mode === 'sentence' ? sentence : mode === 'keywords' ? keywordsText : text('fetch.autonomousTitle'),
            needs: data.needs,
            createdAt: Date.now(),
          })
        }
      },
      onError: (data) => {
        const msg = data.message || ''
        let displayMsg = msg
        try {
          const parsed = JSON.parse(msg)
          if (parsed.detail) displayMsg = parsed.detail
        } catch { /* 非 JSON，直接使用原文 */ }
        setError(displayMsg)
        setLoading(false)
        fetchingRef.current = false
        trackAnalyticsEvent('fetch.error', {
          mode,
          demo: isDemo,
          fetch_model: fetchModelStrategy,
          duration_ms: Date.now() - startTimeRef.current,
        })
      },
      onDone: () => {
        setLoading(false)
        fetchingRef.current = false
        setTimeout(() => setProgressCollapsed(true), 3000)
        // Recovery: if SSE stream ended but needs weren't received (e.g. Cloudflare dropped the final event),
        // fetch results from the backend API as a fallback.
        setTimeout(() => {
          const currentNeeds = useAppStore.getState().needs
          if (currentNeeds.length === 0) {
            getFetchStatus(language).then((s) => {
              if (s.needs && s.needs.length > 0) {
                setNeeds(s.needs)
                setFetchDone(true)
                if (!historyAddedRef.current) {
                  historyAddedRef.current = true
                  const historyTitle = _buildHistoryTitle(mode, sentence, keywordsText, s.needs, isEnglish)
                  addFetchHistory({
                    id: `fetch-${Date.now()}`,
                    title: historyTitle,
                    mode,
                    query: mode === 'sentence' ? sentence : mode === 'keywords' ? keywordsText : text('fetch.autonomousTitle'),
                    needs: s.needs,
                    createdAt: Date.now(),
                  })
                }
              }
            }).catch(() => {})
          }
        }, 500)
      },
    }, abortRef.current.signal)
  }

  const handleAbort = () => {
    abortRef.current?.abort()
    abortRef.current = null
    resetFetchProgress()
    fetchingRef.current = false
    setEstimatedSeconds(0)
    setElapsedSeconds(0)
    stopFetch().catch(() => {})
    setSmoothProgress(0)
    trackAnalyticsEvent('fetch.stop', {
      mode,
      progress,
      duration_ms: startTimeRef.current ? Date.now() - startTimeRef.current : 0,
    })
  }

  const handleClear = () => {
    setConfirmAction({
      title: text('fetch.clearTitle'),
      message: text('fetch.clearMessage'),
      action: doClear,
    })
  }

  const doClear = async () => {
    setConfirmAction(null)
    trackAnalyticsEvent('fetch.clear', { needs_count: needs.length })
    await clearNeeds()
    setNeeds([])
    resetDebate()
    useAppStore.getState().setActiveFetchHistory(null)
  }

  const handleSelectAndDebate = (idx: number) => {
    if (hasActiveDebate && selectedNeedIndex !== idx) {
      setConfirmAction({
        title: text('fetch.switchNeedTitle'),
        message: text('fetch.switchNeedMessage'),
        action: () => { setConfirmAction(null); doSelectAndDebate(idx) },
      })
      return
    }
    doSelectAndDebate(idx)
  }

  const doSelectAndDebate = (idx: number) => {
    if (selectedNeedIndex !== idx) resetDebate()
    setSelectedNeed(idx)
    setActiveView('debate')
    trackAnalyticsEvent('debate.open_from_need', { need_index: idx })
  }

  const handleGenerateReport = (idx: number) => {
    setReportGenIdx(idx)
    setReportGenProgress(0)
    setReportGenMsg(text('reports.preparingReport'))
    reportGenAbort.current = new AbortController()

    const isDemo = isSyntheticDemoNeed(needs[idx])
    const reportStartedAt = Date.now()
    trackAnalyticsEvent('report.start', { source: 'fetch_card', need_index: idx, demo: isDemo })
    let chunkCount = 0
    let maxProgress = 0
    let lastMsgIdx = -1
    const _writingMsgs = list('reports.writingMessages')
    // 文案切换的进度阈值，均匀分布在 53-97 之间
    const _msgThresholds = _writingMsgs.map((_, i) => 53 + Math.floor(i * 44 / (_writingMsgs.length - 1)))
    streamGenerateReport(idx, {
      onProgress: (data) => {
        if (data.progress >= maxProgress) {
          maxProgress = data.progress
          setReportGenProgress(data.progress)
        }
        setReportGenMsg(data.message)
      },
      onChunk: () => {
        chunkCount++
        if (chunkCount % 2 === 0) {
          // 双曲线：永远不会真正停住，始终缓慢增长
          const p = Math.min(52 + Math.floor(46 * chunkCount / (chunkCount + 350)), 98)
          if (p > maxProgress) {
            maxProgress = p
            setReportGenProgress(p)
          }
        }
        // 文案跟随进度阈值切换，而非固定 chunk 间隔
        if (chunkCount === 1) {
          lastMsgIdx = 0
          setReportGenMsg(_writingMsgs[0])
        } else {
          const nextIdx = lastMsgIdx + 1
          if (nextIdx < _writingMsgs.length && maxProgress >= _msgThresholds[nextIdx]) {
            lastMsgIdx = nextIdx
            setReportGenMsg(_writingMsgs[nextIdx])
          }
        }
      },
      onDone: (data) => {
        setReportGenProgress(100)
        setReportGenMsg(text('fetch.reportDone'))
        refreshBadges()
        trackAnalyticsEvent('report.done', {
          source: 'fetch_card',
          need_index: idx,
          demo: isDemo,
          duration_ms: Date.now() - reportStartedAt,
          chunk_count: chunkCount,
          has_output_file: Boolean(data?.filename),
        })
        setTimeout(() => {
          setReportGenIdx(null)
          if (data?.filename) {
            useAppStore.getState().setPendingReportFile(data.filename)
          }
          setActiveView('reports')
        }, 600)
      },
      onError: (data) => {
        const msg = data.message || text('fetch.reportFailed')
        setReportGenMsg(`${text('fetch.errorPrefix')}${msg}`)
        setReportGenProgress(0)
        console.error('[ReportGen] error:', msg)
        trackAnalyticsEvent('report.error', {
          source: 'fetch_card',
          need_index: idx,
          demo: isDemo,
          duration_ms: Date.now() - reportStartedAt,
        })
        setTimeout(() => setReportGenIdx(null), 4000)
      },
    }, reportGenAbort.current.signal, { ...(isDemo ? { demo: true } : {}), language })
  }

  const isDebatingNeed = (idx: number) => selectedNeedIndex === idx && debateStatus === 'debating'

  const needReportFile = (need: Need) => reportMap.get(need.need_title.trim().toLowerCase()) || null
  const needHasPersona = (need: Need) => personaCacheKeys.has(need.need_title.trim().toLowerCase())

  return (
    <div className="h-full flex flex-col">
      {!isViewingHistory && configReady === false && (
        <div className="shrink-0 bg-gray-50 border-b border-gray-200 px-6 max-md:px-4 py-2.5 flex items-center gap-3">
          <AlertCircle size={14} className="text-gray-500 shrink-0" />
          <p className="text-xs text-gray-700 flex-1">{text('common.modelUnavailable')}</p>
          <button onClick={() => setShowSettingsDialog(true)}
            className="flex items-center gap-1.5 text-xs font-medium text-gray-700 border border-gray-300 h-8 px-3 rounded-xl hover:bg-gray-100 transition-colors shrink-0">
            <img src="/settings_1_line.png" alt="" className="w-3 h-3 opacity-60" /> {text('fetch.goSettings')}
          </button>
        </div>
      )}

      {/* Mobile history strip */}
      {fetchHistory.length > 0 && (
        <div className="md:hidden shrink-0 px-4 pt-3 pb-1">
          <div className="flex items-center gap-1.5 overflow-x-auto pb-1">
            <button
              onClick={() => setActiveFetchHistory(null)}
              className={`shrink-0 text-[11px] font-medium px-3 py-1.5 rounded-full transition-all ${
                !activeFetchHistoryId ? 'bg-accent/10 text-accent' : 'text-muted border border-border/50'
              }`}
            >
              {text('fetch.current')}
            </button>
            {fetchHistory.map((item) => (
              <button
                key={item.id}
                onClick={() => setActiveFetchHistory(item.id)}
                className={`shrink-0 text-[11px] font-medium px-3 py-1.5 rounded-full transition-all truncate max-w-[140px] ${
                  activeFetchHistoryId === item.id ? 'bg-accent/10 text-accent' : 'text-muted border border-border/50'
                }`}
              >
                {item.title}
              </button>
            ))}
          </div>
        </div>
      )}

      <div className="shrink-0 px-6 max-md:px-4 pt-5 max-md:pt-3 pb-2">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-1.5">
              <h1 className="text-base font-bold break-words">
                {isViewingHistory
                  ? ((activeHistoryItem?.query && activeHistoryItem.query !== text('fetch.autonomousTitle'))
                      ? activeHistoryItem.query
                      : (needs.length > 0 ? displayNeedTitle(needs[0]) : (activeHistoryItem?.title || text('nav.history'))))
                  : <LocalizedText variant="roll" className="inline-block">{text('fetch.title')}</LocalizedText>}
              </h1>
              {!isViewingHistory && <HelpButton {...FETCH_HELP} />}
            </div>
            <p className="page-header-subtitle">
              {isViewingHistory
                ? text('fetch.historySummary')
                  .replace('{needs}', String(activeHistoryItem?.needs.length || 0))
                  .replace('{query}', activeHistoryItem?.query || '')
                : <LocalizedText variant="roll" className="inline-block">{text('fetch.subtitle')}</LocalizedText>}
            </p>
          </div>
          {!isViewingHistory && (
            <div className="flex items-center gap-1.5 shrink-0">
              {!loading && (
                <button onClick={() => doFetch({ demo: true })}
                  className="max-md:hidden text-[12px] font-medium text-accent/70 hover:text-accent transition-colors">
                  {text('fetch.demo')}
                </button>
              )}
              {loading && (
                <button onClick={handleAbort}
                  className="inline-flex md:hidden items-center gap-2 text-[14px] font-semibold h-10 px-5 rounded-xl bg-signal/10 text-signal border-x-2 border-t-2 border-b-[5px] border-signal/25 active:border-b-2 whitespace-nowrap">
                  <img
                    src="/hand_line.png"
                    alt=""
                    className="w-4 h-4 object-contain brightness-0"
                    style={{ filter: 'brightness(0) saturate(100%) invert(24%) sepia(79%) saturate(1834%) hue-rotate(345deg) brightness(89%) contrast(97%)' }}
                  /> {text('fetch.stop')}
                </button>
              )}
              {engineName && (
                <div className={`max-md:hidden flex items-center gap-1.5 text-[10px] font-medium px-2.5 py-1 rounded-xl border ${
                  engineName === 'rdt-cli'
                    ? 'border-emerald-200 bg-emerald-50 text-emerald-700'
                    : 'border-border/40 bg-bg text-muted'
                }`}>
                  <span className={`w-1.5 h-1.5 rounded-full ${
                    engineName === 'rdt-cli' ? 'bg-emerald-500' : 'bg-muted'
                  }`} />
                  rdt-cli
                </div>
              )}
              {stConnected !== null && (
                <div className={`max-md:hidden flex items-center gap-1.5 text-[10px] font-medium px-2.5 py-1 rounded-xl border ${
                  stConnected
                    ? 'border-emerald-200 bg-emerald-50 text-emerald-700'
                    : 'border-border/40 bg-bg text-muted'
                }`}>
                  <span className={`w-1.5 h-1.5 rounded-full ${
                    stConnected ? 'bg-emerald-500' : 'bg-muted'
                  }`} />
                  st-cli
                </div>
              )}
            </div>
          )}
        </div>
      </div>

      {/* Scrollable area: mode selector, inputs, progress, needs list */}
      <div className="flex-1 overflow-y-auto scrollbar-auto">

      {reportGenIdx === -1 && (
        <div className="shrink-0 mx-6 max-md:mx-4 mt-3 mb-2 bg-accent/5 border border-accent/20 rounded-xl px-4 py-3 space-y-1.5">
          <div className="flex items-center justify-between min-w-0">
            <div className="flex items-center gap-1.5 min-w-0">
              <Loader2 size={12} className="text-accent animate-spin flex-shrink-0" />
              <span className="text-[12px] text-accent font-medium truncate">{reportGenMsg || text('fetch.reportGenerating')}</span>
            </div>
            <button
              onClick={() => { reportGenAbort.current?.abort(); setReportGenIdx(null) }}
              className="shrink-0 text-[11px] text-signal border border-signal/30 h-6 px-2 rounded-md hover:bg-signal/5"
            >{text('common.stop')}</button>
          </div>
          <div className="flex items-center gap-2">
            <div className="flex-1 h-1.5 bg-black/[0.06] rounded-full overflow-hidden">
              <motion.div className="h-full bg-accent rounded-full" animate={{ width: `${reportGenProgress}%` }} transition={{ duration: 1.5, ease: 'easeOut' }} />
            </div>
            <span className="text-[11px] font-medium text-foreground tabular-nums w-8 text-right flex-shrink-0">{reportGenProgress}%</span>
          </div>
        </div>
      )}

      {/* Mode selector + action button (hidden when viewing history) */}
      {!isViewingHistory && <div className="shrink-0 px-6 max-md:px-4 py-2.5">
        {!loading && (
          <button onClick={() => doFetch({ demo: true })}
            className="hidden max-md:inline-flex text-[11px] font-medium text-accent/70 hover:text-accent transition-colors mb-1.5">
            {text('fetch.demo')}
          </button>
        )}
        <div className="flex items-center gap-1.5 max-md:flex-wrap max-md:gap-2">
          <div className="fetch-mode-selector relative inline-flex items-center rounded-full bg-white/82 border border-border/70 shadow-[0_2px_8px_rgba(38,45,50,0.06)] ring-1 ring-white/70 p-1">
            {MODE_OPTIONS.map(({ id, labelKey, descKey, img }) => {
              const isActive = mode === id
              const label = text(labelKey)
              return (
                <button
                  key={id}
                  onClick={() => handleModeChange(id)}
                  title={text(descKey)}
                  className={`relative h-9 px-4 max-md:px-3 rounded-full flex items-center gap-1.5 text-[12px] font-semibold transition-colors whitespace-nowrap ${
                    isActive ? 'text-white' : 'text-muted hover:text-text/80'
                  }`}
                >
                  {isActive && (
                    <motion.span
                      layoutId="fetch-mode-slider"
                      className="absolute inset-0 rounded-full bg-accent shadow-sm"
                      transition={{ type: 'spring', stiffness: 430, damping: 28, mass: 0.7 }}
                    />
                  )}
                  <img
                    src={img}
                    alt=""
                    className={`relative z-10 w-[15px] h-[15px] ${isActive ? 'brightness-0 invert' : 'opacity-50'}`}
                  />
                  <span className="relative z-10">{label}</span>
                </button>
              )
            })}
          </div>
          <div className="flex-1 max-md:hidden" />
          {loading && (
            <button onClick={handleAbort}
              className="max-md:hidden inline-flex items-center justify-center gap-2 text-[14px] font-semibold h-10 px-5 rounded-xl transition-all select-none origin-bottom bg-signal/10 text-signal border-x-2 border-t-2 border-b-[5px] border-signal/25 hover:bg-signal/20 active:border-b-2 active:scale-y-[0.97] -translate-y-[2px] whitespace-nowrap">
              <img
                src="/hand_line.png"
                alt=""
                className="w-4 h-4 object-contain brightness-0"
                style={{ filter: 'brightness(0) saturate(100%) invert(24%) sepia(79%) saturate(1834%) hue-rotate(345deg) brightness(89%) contrast(97%)' }}
              /> {text('fetch.stop')}
            </button>
          )}
        </div>
      </div>}

      {/* Mode-specific inputs + controls (hidden when viewing history or mining) */}
      {!isViewingHistory && !loading && <div className="shrink-0 px-6 max-md:px-4 py-3">
        <div>
          <div className="relative mb-3">
          <motion.div
            className="fetch-search-glass relative px-5 max-md:px-4 pt-4 pb-4"
            initial={{ opacity: 0, y: 6, scale: 0.99 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            transition={{ duration: 0.2, ease: 'easeOut' }}
          >
            <div className="fetch-search-inner relative min-h-[104px]">
              {mode === 'sentence' ? (
                <div className="relative h-20">
                  <img src="/star2.png" alt="" className="fetch-input-star fetch-input-star--large absolute left-0 top-[4px]" />
                  <textarea
                    value={sentence}
                    onChange={(e) => setSentence(e.target.value)}
                    placeholder={text('fetch.sentencePlaceholder')}
                    disabled={loading}
                    rows={2}
                    className="fetch-search-textarea block w-full h-20 resize-none bg-transparent pl-7 pr-40 max-md:pr-36 pb-10 pt-1 text-[14px] max-md:text-[13px] leading-6 text-text placeholder:text-[14px] max-md:placeholder:text-[13px] placeholder:text-muted/38 focus:outline-none disabled:opacity-50"
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' && !e.shiftKey) {
                        e.preventDefault()
                        handleFetch()
                      }
                    }}
                  />
                </div>
              ) : (
                <div className="fetch-open-prompt-area relative min-h-20 pr-40 max-md:pr-36 pb-10 pt-1">
                  <div className="fetch-open-prompt-scan relative inline-flex max-w-full items-center pl-7">
                    <span className={`fetch-open-prompt-icon-shell absolute left-0 top-0 ${openPromptShimmerReady ? 'fetch-open-prompt-icon-shell--shimmer' : ''}`}>
                      <img src="/star2.png" alt="" className="fetch-input-star fetch-input-star--large" />
                    </span>
                    <div className="fetch-open-prompt-content text-[14px] max-md:text-[13px] font-medium leading-6 text-text/78">
                      <SplitText
                        key={`open-mode-prompt-${openModeTextKey}-${isEnglish ? 'en' : 'zh'}`}
                        text={text('fetch.openPrompt')}
                        className={`fetch-open-prompt-base ${openPromptShimmerReady ? 'fetch-open-prompt-base--hidden' : ''}`}
                        delay={18}
                        duration={0.42}
                        splitType="chars"
                        from={{ opacity: 0, y: 10 }}
                        to={{ opacity: 1, y: 0 }}
                        textAlign="left"
                        tag="p"
                        onLetterAnimationComplete={() => setOpenPromptShimmerReady(true)}
                      />
                      {openPromptShimmerReady && (
                        <ShimmerText
                          className="fetch-open-prompt-shimmer-overlay"
                          shimmerColor="rgba(26,26,26,1)"
                          duration={3.2}
                          shimmerSpread={24}
                        >
                          {text('fetch.openPrompt')}
                        </ShimmerText>
                      )}
                    </div>
                  </div>
                </div>
              )}
              <div className="fetch-search-controls absolute left-0 bottom-0 flex items-center gap-1.5 max-w-[calc(100%-150px)] max-md:max-w-[calc(100%-132px)]">
                <div ref={miningModeTriggerRef} className="relative shrink-0">
                  <button
                    type="button"
                    onClick={toggleMiningModeMenu}
                    className="fetch-filter-pill"
                    title={miningModeInfo.desc}
                  >
                    <span className="truncate">{miningModeInfo.label}</span>
                    <ChevronDown size={12} className={`ml-1 shrink-0 text-text/42 transition-transform ${miningModeOpen ? 'rotate-180' : ''}`} />
                  </button>
                </div>
                {canUseRedditCategoryPill && (
                  <button
                    type="button"
                    onClick={toggleTagsPanel}
                    className={`fetch-filter-pill ${
                      tagsExpanded || selectedRedditCats.length > 0 ? 'fetch-filter-pill--active' : ''
                    }`}
                    title={selectedRedditCatLabel}
                  >
                    <span className="truncate">{selectedRedditCatLabel}</span>
                  </button>
                )}
                <button
                  type="button"
                  onClick={toggleAdvancedPanel}
                  className={`fetch-filter-pill ${
                    showAdvanced || hasCustomFetchParams ? 'fetch-filter-pill--active' : ''
                  }`}
                >
                  <span className="truncate">{advancedPillLabel}</span>
                </button>
              </div>
              <div className="fetch-search-start-slot absolute right-0 bottom-0 flex items-end">
                <button
                  onClick={handleFetch}
                  className="fetch-start-button"
                >
                  <img src="/ai-tools.png" alt="" className="w-4 h-4 object-contain opacity-95" />
                  <span>{text('fetch.start')}</span>
                </button>
              </div>
              {fetchHint && (
                <div className="absolute left-1/2 bottom-[-8px] -translate-x-1/2 px-3 py-1.5 rounded-full bg-neutral-900/92 text-white text-[11px] whitespace-nowrap shadow-[0_8px_18px_rgba(0,0,0,0.14)] z-30 pointer-events-none">
                  {fetchHint}
                </div>
              )}
            </div>

            <AnimatePresence initial={false}>
              {canUseRedditCategoryPill && tagsExpanded && (
                <motion.div
                  initial={{ height: 0, opacity: 0, y: -4 }}
                  animate={{ height: 'auto', opacity: 1, y: 0 }}
                  exit={{ height: 0, opacity: 0, y: -4 }}
                  transition={{ duration: 0.18, ease: 'easeOut' }}
                  className="overflow-hidden"
                >
                  <div className="fetch-inline-panel">
                    <p className="text-[11px] text-muted/60 mb-2">{text('fetch.optionalAutoPlan')}</p>
                    <div className="flex flex-wrap gap-1.5 p-px">
                      {Object.entries(redditCategories).map(([key, cat]) => (
                        <button
                          key={key}
                          onClick={() => { if (!loading) toggleRedditCat(key) }}
                          className={`text-[11px] font-medium px-2.5 py-1 rounded-xl transition-all border ${
                            selectedRedditCats.includes(key)
                              ? 'bg-accent/8 text-accent border-accent/40'
                              : 'text-muted/70 border-border hover:border-accent/30 hover:text-text'
                          }`}
                        >
                          {cat.label}
                        </button>
                      ))}
                    </div>
                  </div>
                </motion.div>
              )}
            </AnimatePresence>

            <AnimatePresence>
              {showAdvanced && (
                <motion.div
                  initial={{ height: 0, opacity: 0 }}
                  animate={{ height: 'auto', opacity: 1 }}
                  exit={{ height: 0, opacity: 0 }}
                  transition={{ duration: 0.2, ease: 'easeInOut' }}
                  className="overflow-hidden"
                >
                  <div className="fetch-inline-panel">
                    <div className="grid grid-cols-2 max-md:grid-cols-1 gap-x-4 gap-y-3">
                      <div>
                        <label className="block text-[11px] text-muted mb-1.5 font-medium">{text('fetch.timeRange')}</label>
                        <div className="relative">
                          <button
                            type="button"
                            onClick={() => !loading && setOpenTimePeriod(!openTimePeriod)}
                            onBlur={() => setTimeout(() => setOpenTimePeriod(false), 150)}
                            disabled={loading}
                            className="w-full flex items-center justify-between rounded-xl border border-border/50 bg-bg h-9 pl-3 pr-7 text-[13px] focus:outline-none focus:ring-2 focus:ring-accent/15 disabled:opacity-50 transition-shadow cursor-pointer text-left"
                          >
                            <span>{selectedTimeLabel}</span>
                          </button>
                          <ChevronDown size={13} className="absolute right-2.5 top-1/2 -translate-y-1/2 text-muted/50 pointer-events-none" />
                          {openTimePeriod && (
                            <div className="absolute left-0 top-full mt-1 bg-card border border-border/60 rounded-xl shadow-lg z-50 overflow-hidden w-full">
                              {fetchTimeOptions.map((opt) => (
                                <button
                                  key={opt.value}
                                  type="button"
                                  onMouseDown={(e) => {
                                    e.preventDefault()
                                    setTimePeriod(opt.value)
                                    setOpenTimePeriod(false)
                                  }}
                                  className={`w-full text-left px-3 py-2 text-[13px] hover:bg-accent/8 transition-colors ${
                                    timePeriod === opt.value ? 'text-accent font-medium bg-accent/5' : 'text-text'
                                  }`}
                                >
                                  {opt.label}
                                </button>
                              ))}
                            </div>
                          )}
                        </div>
                      </div>
                      <div>
                        <label className="block text-[11px] text-muted mb-1.5 font-medium">{text('fetch.existingProduct')}</label>
                        <input type="text" value={product} onChange={(e) => setProduct(e.target.value)} disabled={loading}
                          placeholder={text('fetch.existingProductPlaceholder')} className="w-full rounded-xl border border-border/50 bg-bg h-9 px-3 text-[13px] placeholder:text-muted/40 focus:outline-none focus:ring-2 focus:ring-accent/15 disabled:opacity-50 transition-shadow" />
                      </div>
                      <div>
                        <label className="block text-[11px] text-muted mb-1.5 font-medium">{text('fetch.targetMarket')}</label>
                        <input type="text" value={market} onChange={(e) => setMarket(e.target.value)} disabled={loading}
                          placeholder={text('fetch.targetMarketPlaceholder')} className="w-full rounded-xl border border-border/50 bg-bg h-9 px-3 text-[13px] placeholder:text-muted/40 focus:outline-none focus:ring-2 focus:ring-accent/15 disabled:opacity-50 transition-shadow" />
                      </div>
                      <div>
                        <label className="block text-[11px] text-muted mb-1.5 font-medium">{text('fetch.targetUserPersona')}</label>
                        <input type="text" value={demographics} onChange={(e) => setDemographics(e.target.value)} disabled={loading}
                          placeholder={text('fetch.targetUserPlaceholder')} className="w-full rounded-xl border border-border/50 bg-bg h-9 px-3 text-[13px] placeholder:text-muted/40 focus:outline-none focus:ring-2 focus:ring-accent/15 disabled:opacity-50 transition-shadow" />
                      </div>
                      <div>
                        <label className="block text-[11px] text-muted mb-1.5 font-medium">{text('fetch.behaviorSegment')}</label>
                        <input type="text" value={segment} onChange={(e) => setSegment(e.target.value)} disabled={loading}
                          placeholder={text('fetch.behaviorPlaceholder')} className="w-full rounded-xl border border-border/50 bg-bg h-9 px-3 text-[13px] placeholder:text-muted/40 focus:outline-none focus:ring-2 focus:ring-accent/15 disabled:opacity-50 transition-shadow" />
                      </div>
                      <div>
                        <label className="block text-[11px] text-muted mb-1.5 font-medium">{text('fetch.knownCompetitors')}</label>
                        <input type="text" value={competitors} onChange={(e) => setCompetitors(e.target.value)} disabled={loading}
                          placeholder={text('fetch.competitorsPlaceholder')} className="w-full rounded-xl border border-border/50 bg-bg h-9 px-3 text-[13px] placeholder:text-muted/40 focus:outline-none focus:ring-2 focus:ring-accent/15 disabled:opacity-50 transition-shadow" />
                      </div>
                    </div>
                  </div>
                </motion.div>
              )}
            </AnimatePresence>
          </motion.div>

          <AnimatePresence>
            {miningModeOpen && (
              <motion.div
                ref={miningModeMenuRef}
                initial={{ opacity: 0, y: -4 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -4 }}
                transition={{ duration: 0.16, ease: 'easeOut' }}
                className="fetch-mining-mode-menu absolute left-5 max-md:left-4 top-full mt-2 z-50 w-[252px] rounded-[16px] border border-border/45 bg-white p-1.5"
              >
                {(() => {
                  const displayedMiningMode = effectiveMiningMode
                  const fastDisabled = mode === 'open'
                  return (
                    <>
                <button
                  type="button"
                  disabled={fastDisabled}
                  onClick={() => handleMiningModeChange('fast')}
                  className={`w-full text-left rounded-[12px] px-2.5 py-2 transition-colors ${
                    fastDisabled
                      ? 'cursor-not-allowed opacity-[0.38]'
                      : displayedMiningMode === 'fast'
                        ? 'bg-black/[0.06]'
                        : 'hover:bg-black/[0.035]'
                  }`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className={`text-[13px] font-semibold ${fastDisabled ? 'text-text/38' : 'text-text'}`}>{text('fetch.quickMode')}</span>
                    {displayedMiningMode === 'fast' && <CheckCircle2 size={13} className="text-text/65 shrink-0" />}
                  </div>
                  <p className={`mt-0.5 text-[12px] leading-relaxed ${fastDisabled ? 'text-muted/38' : 'text-muted/62'}`}>
                    {fastDisabled
                      ? text('fetch.autonomousDeepOnly')
                      : text('fetch.quickModeDesc')}
                  </p>
                </button>
                <button
                  type="button"
                  onClick={() => handleMiningModeChange('deep')}
                  className={`mt-0.5 w-full text-left rounded-[12px] px-2.5 py-2 transition-colors ${
                    displayedMiningMode === 'deep' ? 'bg-black/[0.06]' : 'hover:bg-black/[0.035]'
                  }`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-[13px] font-semibold text-text">{text('fetch.deepMode')}</span>
                    {displayedMiningMode === 'deep' && <CheckCircle2 size={13} className="text-text/65 shrink-0" />}
                  </div>
                  <p className="mt-0.5 text-[12px] leading-relaxed text-muted/62">{text('fetch.deepModeDesc')}</p>
                </button>
                    </>
                  )
                })()}
              </motion.div>
            )}
          </AnimatePresence>
          </div>

        </div>
      </div>}
      {/* Progress indicator (hidden when viewing history) */}
      <AnimatePresence>
        {!isViewingHistory && (loading || (progressHistory.length > 1 && !progressDismissed)) && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.25, ease: 'easeInOut' }}
            className="shrink-0 overflow-hidden"
          >
            <div className="fetch-progress-card mx-6 max-md:mx-4 my-2.5 bg-[#f5f5f5] rounded-[28px] p-4 relative overflow-hidden">
              {loading && (
                <ShineBorder
                  shineColor={fetchProgressShineColors}
                  duration={8}
                  borderWidth={2}
                />
              )}
              <div
                className={`flex items-center gap-3 ${progressCollapsed && !loading ? 'cursor-pointer' : ''}`}
                onClick={() => { if (!loading && progressCollapsed) setProgressCollapsed(false) }}
              >
                {!loading && fetchDone ? (
                  <div className="w-5 h-5 rounded-full bg-emerald-500 flex items-center justify-center shrink-0">
                    <svg width="10" height="10" viewBox="0 0 8 8" fill="none"><path d="M1.5 4L3 5.5L6.5 2" stroke="white" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/></svg>
                  </div>
                ) : (
                  <img src="/vibe_coding_line.png" alt="" className="w-5 h-5 shrink-0 opacity-80" />
                )}
                {progressCollapsed && !loading ? (
                  <>
                    <p className="flex-1 text-xs text-text/70 line-clamp-1">
                      {progressHistory.slice(-2).join(' · ')}
                    </p>
                    <button onClick={(e) => { e.stopPropagation(); setProgressCollapsed(false) }}
                      className="text-[11px] text-accent hover:underline shrink-0">{text('fetch.expand')}</button>
                    <button onClick={(e) => { e.stopPropagation(); setProgressDismissed(true) }}
                      className="text-[11px] text-muted/40 hover:text-signal shrink-0 ml-1">✕</button>
                  </>
                ) : (
                  <>
                    <div className="flex-1">
                      <div className="h-1.5 bg-black/[0.06] rounded-full overflow-hidden">
                        <motion.div
                          className={`h-full rounded-full ${loading ? 'bg-accent' : fetchDone ? 'bg-emerald-500' : 'bg-accent'}`}
                          initial={{ width: 0 }}
                          animate={{ width: `${smoothProgress}%` }}
                          transition={{ duration: 0.6, ease: 'easeOut' }}
                        />
                      </div>
                    </div>
                    <span className={`text-xs font-semibold shrink-0 w-10 text-right tabular-nums ${loading ? 'text-accent' : fetchDone ? 'text-emerald-600' : 'text-accent'}`}>
                      <CountUp to={Math.round(smoothProgress)} duration={0.6} />%
                    </span>
                    {!loading && (
                      <>
                        <button onClick={() => setProgressCollapsed(true)}
                          className="text-[11px] text-muted hover:text-accent shrink-0">{text('fetch.collapse')}</button>
                        <button onClick={() => setProgressDismissed(true)}
                          className="text-[11px] text-muted/40 hover:text-signal shrink-0 ml-0.5">✕</button>
                      </>
                    )}
                  </>
                )}
              </div>

              <AnimatePresence initial={false}>
                {!progressCollapsed && (
                  <motion.div
                    key="progress-messages"
                    initial={{ height: 0, opacity: 0 }}
                    animate={{ height: 'auto', opacity: 1 }}
                    exit={{ height: 0, opacity: 0 }}
                    transition={{ duration: 0.35, ease: [0.25, 0.1, 0.25, 1] }}
                    className="overflow-hidden"
                  >
                    <div className="max-h-[130px] overflow-y-auto scrollbar-auto flex flex-col gap-1 mt-3"
                      ref={progressScrollRef}
                      onScroll={(e) => {
                        const el = e.currentTarget
                        const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40
                        userScrolledUpRef.current = !nearBottom
                      }}
                    >
                      <AnimatePresence initial={false}>
                        {progressHistory.map((msg, i) => {
                          const isCurrent = i === progressHistory.length - 1
                          const remaining = Math.max(estimatedSeconds - elapsedSeconds, loading ? 1 : 0)
                          const showCountdown = isCurrent && loading && estimatedSeconds > 0
                          return (
                            <motion.div
                              key={`${i}-${msg}`}
                              initial={{ opacity: 0, y: 8, height: 0 }}
                              animate={{ opacity: 1, y: 0, height: 'auto' }}
                              transition={{ duration: 0.4, ease: [0.25, 0.1, 0.25, 1] }}
                              className={`text-xs flex items-center gap-2 ${
                                isCurrent ? 'text-text/80 font-medium' : 'text-muted/60'
                              }`}
                            >
                              <span className={`inline-block w-1.5 h-1.5 rounded-full shrink-0 ${
                                isCurrent && loading ? 'bg-accent' : isCurrent && fetchDone ? 'bg-emerald-500' : isCurrent ? 'bg-accent' : 'bg-muted/30'
                              }`} />
                              <span className="flex-1 min-w-0">
                                {isCurrent && loading ? (
                                  <ShimmerText className="text-xs" shimmerColor="rgba(44,44,44,0.3)" duration={2}>
                                    {msg}
                                  </ShimmerText>
                                ) : msg}
                              </span>
                              {showCountdown && (
                                <span className="text-[10px] text-muted/50 tabular-nums shrink-0">
                                  {text('fetch.estimated')} {formatClock(remaining)}
                                </span>
                              )}
                            </motion.div>
                          )
                        })}
                      </AnimatePresence>
                    </div>
                  </motion.div>
                )}
              </AnimatePresence>


            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Needs list or empty state */}
      <div className="px-6 max-md:px-4 pt-2 pb-4">
        {needs.length === 0 && !loading ? (
          <div className="flex flex-col items-center justify-center py-10 text-center">
            {error ? (
              <div className="max-w-md mx-auto">
                <div className="flex items-start gap-3 px-5 py-4 bg-signal/5 border border-signal/20 rounded-xl text-left">
                  <AlertCircle size={18} className="text-signal shrink-0 mt-0.5" />
                  <div>
                    <p className="text-[13px] font-semibold text-signal mb-1">{text('fetch.failedTitle')}</p>
                    <p className="text-xs text-signal/80 leading-relaxed">{error}</p>
                  </div>
                </div>
              </div>
            ) : (
              <>
                <img src="/logo.png" alt="" className="w-14 h-14 rounded-2xl mb-3" />
                <p className="text-sm font-medium mb-1">{text('fetch.noDataTitle')}</p>
                <p className="text-xs text-muted flex items-center gap-1">
                  {text('fetch.try')}
                  <RotatingText
                    texts={[text('fetch.sentenceMode'), text('fetch.openMode')]}
                    rotationInterval={2500}
                    className="text-accent font-medium"
                    staggerDuration={0.02}
                  />
                </p>
              </>
            )}
          </div>
        ) : (
          <>
            {needs.length > 0 && (
              <>
                <div className="flex items-center justify-between mb-4">
                  <p className="text-xs text-muted">
                    {text('fetch.foundSummary')
                      .split('{needs}')[0]}
                    <CountUp to={needs.length} duration={0.8} className="font-semibold text-text" />
                    {text('fetch.foundSummary')
                      .split('{needs}')[1]
                      ?.split('{posts}')[0]}
                    <CountUp to={needs.reduce((s, n) => s + n.posts.length, 0)} duration={1} className="font-semibold text-text" />
                    {text('fetch.foundSummary')
                      .split('{posts}')[1]}
                  </p>
                  {!loading && (
                    <button onClick={handleClear}
                      className="group flex items-center gap-1.5 text-[11px] text-muted border border-border/40 h-7 px-3 rounded-lg hover:bg-signal/10 hover:border-signal/30 hover:text-signal transition-colors">
                      <img
                        src="/delete_2_line.png"
                        alt=""
                        className="w-3 h-3 opacity-50 transition-[opacity,filter] group-hover:opacity-80 group-hover:[filter:brightness(0)_saturate(100%)_invert(25%)_sepia(73%)_saturate(1608%)_hue-rotate(344deg)_brightness(88%)_contrast(83%)]"
                      /> {text('common.clear')}
                    </button>
                  )}
                </div>
                <div className="grid grid-cols-2 max-md:grid-cols-1 gap-3 items-start">
                  {needs.map((need, i) => {
                    const debating = isDebatingNeed(i)
                    const isExpanded = expandedIdx === i
                    const isCollapsing = collapsingIdx === i
                    const isSelected = selectedNeedIndex === i
                    const opportunityScore = formatOpportunityScore(need.opportunity_score)
                    const marketBadge = formatMarketValidationBadge(need)
                    const isReportGenerating = reportGenIdx === i
                    const keepExpandedLayout = isExpanded || isCollapsing
                    const allowAutoHeight = keepExpandedLayout || isReportGenerating
                    return (
                      <motion.div
                        key={`card-${needsEpoch}-${i}`}
                        initial={{ opacity: 0, y: 20, scale: 0.96 }}
                        animate={{ opacity: 1, y: 0, scale: 1 }}
                        transition={{ delay: 0.15 + i * 0.07, duration: 0.5, ease: [0.22, 1, 0.36, 1] }}
                        onClick={() => handleToggleNeedCard(i)}
                        className={`fetch-need-card ${allowAutoHeight ? 'h-auto' : 'h-[210px] max-md:h-[224px]'} bg-white/96 rounded-[28px] border overflow-hidden flex flex-col cursor-pointer shadow-[0_2px_8px_rgba(38,45,50,0.025)] ${
                          isSelected ? 'border-accent/45 shadow-[0_3px_10px_rgba(38,45,50,0.035)] ring-1 ring-accent/10' : 'border-border/55 hover:border-accent/24 hover:shadow-[0_4px_12px_rgba(38,45,50,0.04)]'
                        } ${isExpanded ? 'ring-1 ring-accent/20' : ''}`}
                      >
                        <div className={`px-4 pt-4 pb-4 flex flex-col min-h-0 ${
                          isReportGenerating
                            ? 'shrink-0 min-h-[210px] max-md:min-h-[224px]'
                            : keepExpandedLayout
                              ? 'shrink-0 min-h-[210px] max-md:min-h-[224px]'
                              : 'flex-1'
                        }`}>
                          <div className="flex items-center gap-2 mb-2">
                            <div className="fetch-need-icon-shell w-7 h-7 bg-accent/7 rounded-xl flex items-center justify-center shrink-0">
                              <img src="/firstline.png" alt="" className="w-4 h-4 object-contain opacity-85 brightness-0" />
                            </div>
                            <h3 className="text-[13px] font-semibold leading-snug flex-1 min-w-0 line-clamp-1">{displayNeedTitle(need)}</h3>
                            {opportunityScore && (
                              <span className="shrink-0 text-[10px] font-semibold px-1.5 py-0.5 rounded-full bg-text/[0.04] text-text/70 border border-border/35">
                                {text('fetch.opportunity')} {opportunityScore}
                              </span>
                            )}
                            {marketBadge && (
                              <span
                                className={`shrink-0 h-5 max-w-[240px] whitespace-nowrap px-2 rounded-full border text-[10px] leading-5 font-semibold ${marketBadge.className}`}
                                title={marketBadge.title}
                              >
                                {marketBadge.label}
                              </span>
                            )}
                            {debating && (
                              <span className="shrink-0 text-[10px] font-medium px-1.5 py-0.5 rounded-full bg-blue-50 text-blue-600">{text('fetch.debating')}</span>
                            )}
                            {!debating && isSelected && (debateStatus === 'debate_done' || debateStatus === 'done') && (
                              <span className="shrink-0 text-[10px] font-medium px-1.5 py-0.5 rounded-full bg-emerald-50 text-emerald-600">{text('fetch.completed')}</span>
                            )}
                            {need.deep_mine_package && (
                              <span className="flex items-center gap-1 text-[10px] font-medium px-1.5 py-0.5 rounded-full bg-emerald-50 text-emerald-600 shrink-0">
                                <CheckCircle2 size={10} />
                                {need.deep_mine_package.femwc?.total?.toFixed(2) || '—'}{text('fetch.points')}
                              </span>
                            )}
                          </div>

                          <p className={`text-[11px] text-muted leading-relaxed mt-1 ${
                            isExpanded
                              ? 'mb-2 min-h-[18px] whitespace-normal overflow-visible'
                              : 'mb-3 overflow-hidden line-clamp-3 min-h-[54px] max-h-[54px]'
                          }`}>{displayNeedDescription(need)}</p>

                          <div className="flex items-center gap-2.5 text-[10px] text-muted mb-3 shrink-0">
                            <span className="flex items-center gap-1"><MessageSquare size={10} /> {need.posts.length}</span>
                            <span className="flex items-center gap-1"><TrendingUp size={10} /> {need.total_score}</span>
                            {(() => {
                              const srcs = [...new Set(need.posts.map(p => sourceLabel(p.source)))]
                              return srcs.map(s => (
                                <span key={s} className="fetch-source-chip px-1 py-0.5 rounded text-[9px] bg-white font-medium border border-border/30">{s}</span>
                              ))
                            })()}
                          </div>

                          <div className="mt-auto flex items-center gap-1.5 shrink-0" onClick={(e) => e.stopPropagation()}>
                            {reportGenIdx !== i ? (
                              needReportFile(need) ? (
                                <button
                                  onClick={() => { useAppStore.getState().setPendingReportFile(needReportFile(need)!); setActiveView('reports') }}
                                  className="report-action-button flex items-center gap-1 text-[11px] font-medium text-accent border border-accent/30 h-7 px-3 rounded-lg hover:bg-accent/5 transition-colors"
                                >
                                  <span className="report-action-button-content gap-1">
                                    <img src="/book_2_ai_line.png" alt="" className="report-action-icon w-3.5 h-3.5 opacity-70" />
                                    <span>{text('fetch.viewReport')}</span>
                                  </span>
                                </button>
                              ) : (
                                <button
                                  onClick={() => handleGenerateReport(i)}
                                  disabled={reportGenIdx !== null}
                                  className="report-action-button flex items-center gap-1 text-[11px] font-medium text-white bg-accent h-7 px-3 rounded-lg hover:opacity-90 transition-opacity disabled:opacity-50"
                                >
                                  <span className="report-action-button-content gap-1">
                                    <img src="/book_2_ai_line.png" alt="" className="report-action-icon w-3.5 h-3.5 opacity-90 brightness-0 invert" />
                                    <span>{text('fetch.generateReport')}</span>
                                  </span>
                                </button>
                              )
                            ) : (
                              <button
                                onClick={() => { reportGenAbort.current?.abort(); setReportGenIdx(null) }}
                                className="report-action-button flex items-center gap-1 text-[11px] font-medium text-signal border border-signal/30 h-7 px-3 rounded-lg"
                              >
                                <span className="report-action-button-content gap-1">
                                  <Loader2 size={10} className="report-action-icon animate-spin" />
                                  <span>{text('fetch.generating')}</span>
                                </span>
                              </button>
                            )}
                            <InteractiveHoverButton
                              onClick={() => handleSelectAndDebate(i)}
                              className="fetch-card-action !h-[30px] !px-3.5 !py-0 !text-[11px] !font-medium !border-border/55 !shadow-none text-text/75"
                              contentClassName="!gap-1"
                              icon={<img src="/chat_4_ai_line.png" alt="" className="w-3.5 h-3.5 opacity-70 brightness-0" />}
                              hoverIcon={<img src="/chat_4_ai_line.png" alt="" className="w-3.5 h-3.5 brightness-0 invert" />}
                            >
                              {debating ? text('fetch.continueDebate') : text('fetch.debate')}
                            </InteractiveHoverButton>
                            {needHasPersona(need) ? (
                              <InteractiveHoverButton
                                onClick={() => { useAppStore.getState().setPersonaNeedIndex(i); setActiveView('personas') }}
                                className="fetch-card-action !h-[30px] !px-3.5 !py-0 !text-[11px] !font-medium !border-border/55 !shadow-none text-text/75"
                                contentClassName="!gap-1"
                                icon={<img src="/group_2_line.png" alt="" className="w-3.5 h-3.5 opacity-70 brightness-0" />}
                                hoverIcon={<img src="/group_2_line.png" alt="" className="w-3.5 h-3.5 brightness-0 invert" />}
                              >
                                {text('fetch.viewPersona')}
                              </InteractiveHoverButton>
                            ) : (
                              <InteractiveHoverButton
                                onClick={() => { useAppStore.getState().setPersonaNeedIndex(i); setActiveView('personas') }}
                                className="fetch-card-action !h-[30px] !px-3.5 !py-0 !text-[11px] !font-medium !border-border/55 !shadow-none text-text/75"
                                contentClassName="!gap-1"
                                icon={<img src="/group_2_line.png" alt="" className="w-3.5 h-3.5 opacity-70 brightness-0" />}
                                hoverIcon={<img src="/group_2_line.png" alt="" className="w-3.5 h-3.5 brightness-0 invert" />}
                              >
                                {text('fetch.persona')}
                              </InteractiveHoverButton>
                            )}
                            <div className="flex-1" />
                            <ChevronDown size={13} className={`text-muted transition-transform duration-200 ${isExpanded ? 'rotate-180' : ''}`} />
                          </div>
                        </div>

                        <div
                          className="grid transition-[grid-template-rows,opacity] duration-300 ease-in-out"
                          style={{
                            gridTemplateRows: reportGenIdx === i ? '1fr' : '0fr',
                            opacity: reportGenIdx === i ? 1 : 0,
                          }}
                        >
                          <div className="overflow-hidden">
                            <div className="mx-4 h-[1px] bg-border/25" />
                            <div className="px-4 pb-3 pt-1.5 space-y-1.5 -translate-y-1">
                              <div className="flex items-center gap-1.5 min-w-0">
                                <img src="/vibe_coding_line.png" alt="" className="w-3 h-3 opacity-60 flex-shrink-0" />
                                <ShimmerText className="text-[11px] text-accent font-medium truncate" shimmerColor="rgba(44,44,44,0.3)" duration={2.5}>
                                  {reportGenMsg}<span className="inline-block w-[1.2em] text-left animate-[dotPulse_1.4s_ease-in-out_infinite]">...</span>
                                </ShimmerText>
                                {reportSubMsg && (
                                  <span className="text-[11px] text-muted/40 truncate ml-1">{reportSubMsg}</span>
                                )}
                              </div>
                              <div className="flex items-center gap-2">
                                <div className="flex-1 h-1.5 bg-black/[0.06] rounded-full overflow-hidden">
                                  <motion.div
                                    className="h-full bg-accent rounded-full"
                                    animate={{ width: `${reportGenProgress}%` }}
                                    transition={{ duration: 1.5, ease: 'easeOut' }}
                                  />
                                </div>
                                <span className="text-[11px] font-medium text-foreground tabular-nums w-8 text-right flex-shrink-0">{reportGenProgress}%</span>
                              </div>
                            </div>
                          </div>
                        </div>

                        {/* Expanded detail inside card */}
                        <AnimatePresence>
                          {isExpanded && (
                            <motion.div
                              initial={{ height: 0, opacity: 0 }}
                              animate={{ height: 'auto', opacity: 1 }}
                              exit={{ height: 0, opacity: 0 }}
                              transition={{ duration: 0.25, ease: 'easeInOut' }}
                              className="overflow-hidden"
                              onClick={(e) => e.stopPropagation()}
                            >
                              <div className="mx-4 h-[1px] bg-border/25" />
                              <div className="px-4 pb-4 pt-3 max-h-[320px] overflow-y-auto scrollbar-auto">
                                {need.deep_mine_package?.femwc && (
                                  <div className="mb-3">
                                    <div className="flex items-center gap-2 mb-2">
                                      <BarChart3 size={13} className="text-accent" />
                                      <span className="text-[11px] font-semibold">{text('fetch.femwcScore')}</span>
                                      <span className="text-[11px] font-bold text-accent ml-auto">{need.deep_mine_package.femwc.total?.toFixed(2)} {text('fetch.points')}</span>
                                    </div>
                                    <div className="grid grid-cols-5 max-md:grid-cols-3 gap-1.5">
                                      {(['F', 'E', 'M', 'W', 'C'] as const).map((dim) => {
                                        const d = need.deep_mine_package!.femwc[dim] as FemwcDimension
                                        const labels: Record<string, string> = {
                                          F: text('fetch.frequency'),
                                          E: text('fetch.emotion'),
                                          M: text('fetch.market'),
                                          W: text('fetch.payment'),
                                          C: text('fetch.competition'),
                                        }
                                        const sc = d?.score || 0
                                        return (
                                          <div key={dim} className="bg-bg rounded-xl p-2 text-center">
                                            <p className="text-[10px] text-muted mb-0.5">{labels[dim]}</p>
                                            <p className="text-sm font-bold">{sc}</p>
                                            <div className="h-1 bg-black/[0.06] rounded-full mt-1"><div className="h-full bg-accent rounded-full" style={{ width: `${sc * 20}%` }} /></div>
                                          </div>
                                        )
                                      })}
                                    </div>
                                    <p className="text-[11px] text-muted mt-1.5">{need.deep_mine_package.femwc.verdict} — {need.deep_mine_package.femwc.summary}</p>
                                  </div>
                                )}
                                {need.deep_mine_package?.quotes && need.deep_mine_package.quotes.length > 0 && (
                                  <div className="mb-3">
                                    <div className="flex items-center gap-2 mb-2">
                                      <Quote size={13} className="text-violet-500" />
                                      <span className="text-[11px] font-semibold">{text('fetch.quotes').replace('{count}', String(need.deep_mine_package.quotes.length))}</span>
                                    </div>
                                    <div className="space-y-1.5">
                                      {need.deep_mine_package.quotes.slice(0, 6).map((q, qi) => (
                                        <div key={qi} className="bg-bg rounded-xl px-3 py-2">
                                          <p className="text-[12px] italic text-text/80 leading-relaxed mb-1">"{q.text.slice(0, 200)}{q.text.length > 200 ? '...' : ''}"</p>
                                          <div className="flex items-center gap-2 text-[10px] text-muted">
                                            <span className="px-1.5 py-0.5 rounded bg-card text-[9px] font-medium">
                                              {q.signal_type === 'pain' ? text('fetch.painSignal') : q.signal_type === 'workaround' ? text('fetch.workaroundSignal') : q.signal_type === 'willingness_to_pay' ? text('fetch.paymentSignal') : q.signal_type === 'competitor_complaint' ? text('fetch.competitorComplaintSignal') : q.signal_type === 'journey' ? text('fetch.journeySignal') : q.signal_type}
                                            </span>
                                            {q.score > 0 && <span>{text('fetch.upvoteScore').replace('{score}', String(q.score))}</span>}
                                            {q.source_url && <a href={safeExternalUrl(q.source_url)} target="_blank" rel="noopener noreferrer" className="text-accent hover:underline">{text('fetch.source')}</a>}
                                          </div>
                                        </div>
                                      ))}
                                    </div>
                                  </div>
                                )}
                                {need.market_validation && (
                                  <div className="mb-3 rounded-[16px] border border-border/45 bg-bg/65 px-3 py-2.5">
                                    <div className="flex items-center gap-2 mb-2">
                                      <BarChart3 size={13} className="text-emerald-600" />
                                      <span className="text-[11px] font-semibold">{text('fetch.marketCompetitors')}</span>
                                      <span className="ml-auto text-[10px] text-muted/65">{formatMarketScope(need)}</span>
                                    </div>
                                    {formatMarketAggregateSignal(need) && (
                                      <p className="mb-2 text-[10px] leading-relaxed text-muted/78">{formatMarketAggregateSignal(need)}</p>
                                    )}
                                    {need.market_validation.top_competitors && need.market_validation.top_competitors.length > 0 ? (
                                      <div className="grid grid-cols-2 max-md:grid-cols-1 gap-1.5">
                                        {need.market_validation.top_competitors.slice(0, 4).map((item, ci) => (
                                          <div key={`${item.name}-${ci}`} className="fetch-competitor-chip min-w-0 rounded-[12px] bg-white/62 border border-white/70 px-2.5 py-2">
                                            <p className="text-[11px] font-medium text-text/82 truncate">{item.name}</p>
                                            <p className="text-[10px] text-muted mt-0.5">{formatCompetitorRevenue(item)}</p>
                                          </div>
                                        ))}
                                      </div>
                                    ) : (
                                      <p className="text-[10px] leading-relaxed text-muted/68">
                                        {localizeMarketRiskNote(need.market_validation.risk_note) || text('fetch.marketNoDetails')}
                                      </p>
                                    )}
                                  </div>
                                )}
                                {need.evidence && need.evidence.length > 0 && (
                                  <div className="mb-3">
                                    <div className="flex items-center gap-2 mb-2">
                                      <Quote size={13} className="text-accent" />
                                      <span className="text-[11px] font-semibold">{text('fetch.evidenceChain').replace('{count}', String(need.evidence.length))}</span>
                                      {need.evidence_summary && <span className="text-[10px] text-muted/60 truncate">{localizeEvidenceSummary(need.evidence_summary)}</span>}
                                    </div>
                                    <div className="space-y-1.5">
                                      {need.evidence.slice(0, 4).map((ev) => (
                                        <div key={ev.evidence_id} className="bg-bg rounded-xl px-3 py-2">
                                          <p className="text-[12px] italic text-text/80 leading-relaxed mb-1">"{ev.text.slice(0, 180)}{ev.text.length > 180 ? '...' : ''}"</p>
                                          <div className="flex items-center gap-2 text-[10px] text-muted">
                                            <span className="px-1.5 py-0.5 rounded bg-card text-[9px] font-medium">{localizeSignalLabel(ev.signal_label || ev.signal_type)}</span>
                                            {typeof ev.comment_score === 'number' && ev.comment_score > 0 && <span>{text('fetch.commentScore').replace('{score}', String(ev.comment_score))}</span>}
                                            {ev.subreddit && <span>r/{ev.subreddit}</span>}
                                            {ev.source_url && <a href={safeExternalUrl(ev.source_url)} target="_blank" rel="noopener noreferrer" className="text-accent hover:underline">{text('fetch.source')}</a>}
                                          </div>
                                        </div>
                                      ))}
                                    </div>
                                  </div>
                                )}
                                <div className="space-y-2">
                                  {need.posts.map((post, pi) => (
                                    <div key={pi} className="bg-bg rounded-xl px-3 py-2.5">
                                      <div className="flex items-start gap-2">
                                        <div className="flex-1 min-w-0">
                                          <p className="text-[12px] font-medium leading-snug mb-0.5">{post.title}</p>
                                          {!isEnglish && post.title_zh && <p className="text-[11px] text-muted leading-snug mb-1">{post.title_zh}</p>}
                                          <div className="flex items-center gap-2.5 text-[10px] text-muted">
                                            <span>▲ {post.score}</span>
                                            <span>💬 {post.num_comments}</span>
                                            <span className="px-1 py-0.5 rounded text-[9px] bg-card font-medium">{sourceLabel(post.source)}</span>
                                            {post.has_need_signals && <span className="px-1 py-0.5 rounded-full text-[9px] font-medium bg-signal/10 text-signal">{text('fetch.needSignal')}</span>}
                                          </div>
                                        </div>
                                        {post.url && <a href={safeExternalUrl(post.url)} target="_blank" rel="noopener noreferrer" className="shrink-0 text-muted hover:text-accent transition-colors"><ExternalLink size={11} /></a>}
                                      </div>
                                    </div>
                                  ))}
                                </div>
                              </div>
                            </motion.div>
                          )}
                        </AnimatePresence>
                      </motion.div>
                    )
                  })}
                </div>
              </>
            )}
          </>
        )}
      </div>

      </div>{/* end scrollable area */}

      <ConfirmDialog
        open={confirmAction !== null}
        title={confirmAction?.title || ''}
        message={confirmAction?.message || ''}
        onConfirm={() => confirmAction?.action()}
        onCancel={() => setConfirmAction(null)}
      />

    </div>
  )
}

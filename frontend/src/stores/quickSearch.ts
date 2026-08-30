// 管理雷达搜索的全局运行状态，让搜索流在切换页面时不中断。
import { create } from 'zustand'
import {
  getQuickSearchHistory,
  saveQuickSearchHistory,
  streamQuickSearch,
  trackAnalyticsEvent,
  type QuickSearchHistoryItem,
  type QuickSearchMarketSignal,
  type QuickSearchPost,
} from '../api/client'
import { useAppStore } from './app'

export type QuickSearchTimePeriod = 'week' | 'month' | '3months' | '6months'
export type QuickSearchMinScore = 0 | 10 | 25 | 50 | 100
export type QuickSearchMarketTimePeriod = '30d' | '6months' | 'all_time'

type QuickSearchPlan = {
  queries: string[]
  subreddits: string[]
  reasoning: string
}

interface QuickSearchState {
  query: string
  timePeriod: QuickSearchTimePeriod
  minScore: QuickSearchMinScore
  marketTimePeriod: QuickSearchMarketTimePeriod
  searching: boolean
  progress: number
  progressMsg: string
  progressHistory: string[]
  plan: QuickSearchPlan | null
  marketSignal: QuickSearchMarketSignal | null
  posts: QuickSearchPost[]
  totalSearched: number | null
  summary: string
  error: string
  composerNotice: string
  done: boolean
  searchHistory: QuickSearchHistoryItem[]
  searchStartedAt: number
  setQuery: (value: string) => void
  setTimePeriod: (value: QuickSearchTimePeriod) => void
  setMinScore: (value: QuickSearchMinScore) => void
  setMarketTimePeriod: (value: QuickSearchMarketTimePeriod) => void
  startSearch: () => boolean
  stopSearch: () => void
  resetToSearch: () => void
  openHistoryItem: (item: QuickSearchHistoryItem) => void
  loadHistory: () => void
  clearComposerNotice: () => void
}

const QUICK_SEARCH_HISTORY_KEY = 'lumon.quickSearchHistory'

let activeRunId: string | null = null
let abortController: AbortController | null = null
let progressTimer: number | null = null
let composerNoticeTimer: number | null = null
let historyLoading = false

function readQuickSearchHistory(): QuickSearchHistoryItem[] {
  try {
    const raw = window.localStorage?.getItem(QUICK_SEARCH_HISTORY_KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw)
    if (!Array.isArray(parsed)) return []
    return parsed
      .filter((item) => item && typeof item.query === 'string' && typeof item.timestamp === 'number')
      .slice(0, 12)
  } catch {
    return []
  }
}

function writeQuickSearchHistory(items: QuickSearchHistoryItem[]): boolean {
  try {
    window.localStorage?.setItem(QUICK_SEARCH_HISTORY_KEY, JSON.stringify(items.slice(0, 12)))
    return true
  } catch {
    // 本地历史是辅助体验，写入失败不影响搜索。
    return false
  }
}

function mergeQuickSearchHistory(...groups: QuickSearchHistoryItem[][]): QuickSearchHistoryItem[] {
  const byQuery = new Map<string, QuickSearchHistoryItem>()
  for (const item of groups.flat()) {
    if (!item?.query?.trim()) continue
    const key = item.query.trim()
    const existing = byQuery.get(key)
    if (!existing || item.timestamp > existing.timestamp) {
      byQuery.set(key, item)
    }
  }
  return Array.from(byQuery.values())
    .sort((a, b) => b.timestamp - a.timestamp)
    .slice(0, 12)
}

function stopProgressTimer() {
  if (progressTimer !== null) {
    window.clearInterval(progressTimer)
    progressTimer = null
  }
}

function clearActiveRun() {
  activeRunId = null
  abortController = null
  stopProgressTimer()
}

function quickSearchLocaleText(zh: string, en: string): string {
  return useAppStore.getState().languageMode === 'en-US' ? en : zh
}

export const useQuickSearchStore = create<QuickSearchState>((set, get) => {
  const showComposerNotice = (message: string) => {
    if (composerNoticeTimer !== null) {
      window.clearTimeout(composerNoticeTimer)
    }
    set({ composerNotice: message })
    composerNoticeTimer = window.setTimeout(() => {
      set({ composerNotice: '' })
      composerNoticeTimer = null
    }, 5000)
  }

  const startProgressTimer = () => {
    stopProgressTimer()
    progressTimer = window.setInterval(() => {
      const state = get()
      if (!state.searching) {
        stopProgressTimer()
        return
      }
      if (state.progress >= 96) return
      const elapsed = state.searchStartedAt ? Date.now() - state.searchStartedAt : 0
      const step = state.progress < 28 ? 0.22 : state.progress < 58 ? 0.14 : state.progress < 82 ? 0.075 : 0.03
      const lateStep = elapsed > 90_000 ? Math.min(step, 0.018) : step
      set({ progress: Math.min(96, state.progress + lateStep) })
    }, 120)
  }

  const resetResultState = () => ({
    searching: false,
    progress: 0,
    progressMsg: '',
    progressHistory: [],
    plan: null,
    marketSignal: null,
    posts: [],
    totalSearched: null,
    summary: '',
    error: '',
    done: false,
    searchStartedAt: 0,
  })

  return {
    query: '',
    timePeriod: '3months',
    minScore: 10,
    marketTimePeriod: '30d',
    searching: false,
    progress: 0,
    progressMsg: '',
    progressHistory: [],
    plan: null,
    marketSignal: null,
    posts: [],
    totalSearched: null,
    summary: '',
    error: '',
    composerNotice: '',
    done: false,
    searchHistory: readQuickSearchHistory(),
    searchStartedAt: 0,

    setQuery: (value) => set({ query: value }),
    setTimePeriod: (value) => set({ timePeriod: value }),
    setMinScore: (value) => set({ minScore: value }),
    setMarketTimePeriod: (value) => set({ marketTimePeriod: value }),
    clearComposerNotice: () => {
      if (composerNoticeTimer !== null) {
        window.clearTimeout(composerNoticeTimer)
        composerNoticeTimer = null
      }
      set({ composerNotice: '' })
    },

    loadHistory: () => {
      if (historyLoading) return
      historyLoading = true
      getQuickSearchHistory()
        .then(({ items }) => {
          set((state) => {
            const next = mergeQuickSearchHistory(items || [], state.searchHistory)
            if (next.length > 0) writeQuickSearchHistory(next)
            if ((items || []).length === 0 && state.searchHistory.length > 0) {
              void saveQuickSearchHistory(state.searchHistory).catch(() => {})
            }
            return { searchHistory: next }
          })
        })
        .catch(() => {
          // 后端历史只是兜底，失败不影响搜索。
        })
        .finally(() => {
          historyLoading = false
        })
    },

    startSearch: () => {
      const state = get()
      const q = state.query.trim()
      if (!q || state.searching) return false

      const runId = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
      const startedAt = Date.now()
      activeRunId = runId
      abortController = new AbortController()

      set({
        searching: true,
        progress: 0,
        progressMsg: quickSearchLocaleText('准备搜索...', 'Preparing search...'),
        progressHistory: [quickSearchLocaleText('准备搜索...', 'Preparing search...')],
        plan: null,
        marketSignal: null,
        posts: [],
        totalSearched: null,
        summary: '',
        error: '',
        done: false,
        searchStartedAt: startedAt,
      })
      startProgressTimer()

      trackAnalyticsEvent('quick_search.start', {
        input_length: q.length,
        time_period: state.timePeriod,
        min_score: state.minScore,
      })

      const ctrl = abortController
      void streamQuickSearch({
        query: q,
        language: useAppStore.getState().languageMode,
        time_period: state.timePeriod,
        min_score: state.minScore,
        limit: 30,
        fetch_comments: true,
        market_search: true,
        market_time_period: state.marketTimePeriod,
      }, {
        onProgress: (data) => {
          if (activeRunId !== runId) return
          set((current) => {
            const message = data.message || quickSearchLocaleText('正在搜索...', 'Searching...')
            return {
              progress: Math.max(current.progress, data.progress),
              progressMsg: message,
              progressHistory: current.progressHistory[current.progressHistory.length - 1] === message
                ? current.progressHistory
                : [...current.progressHistory, message],
              ...(data.plan ? { plan: data.plan } : {}),
            }
          })
        },
        onPosts: (data) => {
          if (activeRunId !== runId) return
          set({
            posts: data.posts,
            totalSearched: typeof data.total_searched === 'number' ? data.total_searched : null,
          })
        },
        onMarket: (data) => {
          if (activeRunId !== runId) return
          set({ marketSignal: data })
        },
        onSummaryChunk: (data) => {
          if (activeRunId !== runId) return
          set((current) => ({ summary: current.summary + data.text }))
        },
        onError: (data) => {
          if (activeRunId !== runId) return
          clearActiveRun()
          if (data.placement === 'composer') {
            set({
              ...resetResultState(),
              query: q,
            })
            showComposerNotice(data.message)
            trackAnalyticsEvent('quick_search.blocked', {
              input_length: q.length,
              kind: data.kind || 'composer',
              time_period: state.timePeriod,
              min_score: state.minScore,
              duration_ms: Date.now() - startedAt,
            })
            return
          }
          set((current) => ({
            error: data.message,
            searching: false,
            progressHistory: [...current.progressHistory, `${quickSearchLocaleText('搜索失败：', 'Search failed: ')}${data.message}`],
          }))
          trackAnalyticsEvent('quick_search.error', {
            input_length: q.length,
            time_period: state.timePeriod,
            min_score: state.minScore,
            duration_ms: Date.now() - startedAt,
          })
        },
        onDone: () => {
          if (activeRunId !== runId) return
          clearActiveRun()
          set((current) => {
            const doneText = quickSearchLocaleText('完成', 'Done')
            const progressHistory = current.progressHistory[current.progressHistory.length - 1] === doneText
              ? current.progressHistory
              : [...current.progressHistory, doneText]
            const historyItem: QuickSearchHistoryItem = {
              id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
              query: q,
              timestamp: Date.now(),
              summary: current.summary,
              posts: current.posts.slice(0, 30),
              marketSignal: current.marketSignal,
              ...(typeof current.totalSearched === 'number' ? { totalSearched: current.totalSearched } : {}),
            }
            let searchHistory = current.searchHistory
            if (historyItem.summary || historyItem.posts.length > 0 || historyItem.marketSignal) {
              searchHistory = [
                historyItem,
                ...current.searchHistory.filter((item) => item.query.trim() !== q).slice(0, 11),
              ]
              writeQuickSearchHistory(searchHistory)
              void saveQuickSearchHistory(searchHistory).catch(() => {})
            }
            return {
              searching: false,
              progress: 100,
              progressHistory,
              done: true,
              searchHistory,
            }
          })
          const latest = get()
          trackAnalyticsEvent('quick_search.done', {
            input_length: q.length,
            time_period: state.timePeriod,
            min_score: state.minScore,
            posts_count: latest.posts.length,
            has_summary: latest.summary.length > 0,
            duration_ms: Date.now() - startedAt,
          })
        },
      }, ctrl.signal).finally(() => {
        if (activeRunId !== runId || ctrl.signal.aborted || !get().searching) return
        clearActiveRun()
        const interruptedText = quickSearchLocaleText('搜索连接中断，请重试。', 'Search connection interrupted. Please try again.')
        set((current) => ({
          error: interruptedText,
          searching: false,
          progressHistory: [...current.progressHistory, interruptedText],
        }))
      })

      return true
    },

    stopSearch: () => {
      const state = get()
      abortController?.abort()
      clearActiveRun()
      set({ searching: false })
      trackAnalyticsEvent('quick_search.stop', {
        progress: state.progress,
        duration_ms: state.searchStartedAt ? Date.now() - state.searchStartedAt : 0,
      })
    },

    resetToSearch: () => {
      abortController?.abort()
      clearActiveRun()
      set(resetResultState())
    },

    openHistoryItem: (item) => {
      abortController?.abort()
      clearActiveRun()
      set({
        query: item.query,
        searching: false,
        progress: 100,
        progressMsg: quickSearchLocaleText('历史结果', 'History result'),
        progressHistory: [],
        plan: null,
        marketSignal: item.marketSignal,
        posts: item.posts || [],
        totalSearched: typeof item.totalSearched === 'number' ? item.totalSearched : null,
        summary: item.summary || '',
        error: '',
        done: true,
        searchStartedAt: 0,
      })
      trackAnalyticsEvent('quick_search.history_open', {
        age_ms: Date.now() - item.timestamp,
        posts_count: (item.posts || []).length,
        has_summary: Boolean(item.summary),
        has_market: Boolean(item.marketSignal),
      })
    },
  }
})

import { create } from 'zustand'
import { syncNeeds } from '../api/client'
import type { Need, ChatMessage, DebateStatus, DetailView } from '../types'
import { QUICK_SEARCH_ENABLED } from '../featureFlags'
import { fallbackNeedTitle } from '../errorMessages'

/** 保证每条 need 结构完整，避免后端/历史数据缺字段导致渲染崩溃 */
function normalizeNeeds(needs: Need[]): Need[] {
  return needs.map((n) => ({
    ...n,
    need_title: n.need_title ?? fallbackNeedTitle(),
    need_description: n.need_description ?? '',
    posts: Array.isArray(n.posts) ? n.posts : [],
  }))
}

export type ActiveView = 'fetch' | 'debate' | 'reports' | 'personas' | 'quickSearch'
export type SettingsSection = 'models' | 'websearch' | 'sources' | 'cli' | 'roles' | 'feishu' | 'guide'
export type ThemeMode = 'light' | 'dark'
export type LanguageMode = 'zh-CN' | 'en-US'

export interface FetchHistoryItem {
  id: string
  title: string
  mode: string
  query: string
  needs: Need[]
  createdAt: number
}

interface AppState {
  activeView: ActiveView
  setActiveView: (v: ActiveView) => void
  themeMode: ThemeMode
  setThemeMode: (mode: ThemeMode) => void
  toggleThemeMode: () => void
  languageMode: LanguageMode
  setLanguageMode: (mode: LanguageMode) => void
  toggleLanguageMode: () => void

  pendingReportFile: string | null
  setPendingReportFile: (f: string | null) => void

  needs: Need[]
  selectedNeedIndex: number | null
  setNeeds: (needs: Need[]) => void
  setSelectedNeed: (idx: number | null) => void

  fetchHistory: FetchHistoryItem[]
  activeFetchHistoryId: string | null
  addFetchHistory: (item: FetchHistoryItem) => void
  removeFetchHistory: (id: string) => void
  setActiveFetchHistory: (id: string | null) => void
  loadFetchHistory: () => void

  debateStatus: DebateStatus
  debateRound: number
  maxRounds: number
  messages: ChatMessage[]
  finalReport: string | null
  productProposal: string | null
  isStreaming: boolean
  errorMessage: string | null
  setDebateStatus: (s: DebateStatus) => void
  setDebateRound: (r: number) => void
  setMaxRounds: (r: number) => void
  setFinalReport: (r: string | null) => void
  setProductProposal: (p: string | null) => void
  setIsStreaming: (v: boolean) => void
  setErrorMessage: (msg: string | null) => void
  addMessage: (msg: ChatMessage) => void
  appendToLastMessage: (text: string) => void
  finalizeLastMessage: (content: string) => void
  cancelLastStreamingMessage: () => void
  clearMessages: () => void
  resetDebate: () => void
  resetDebateKeepPost: () => void

  detailView: DetailView
  setDetailView: (v: DetailView) => void

  configReady: boolean | null
  setConfigReady: (v: boolean | null) => void

  showSettingsDialog: boolean
  setShowSettingsDialog: (v: boolean) => void
  settingsSection: SettingsSection
  setSettingsSection: (section: SettingsSection) => void

  roleNames: Record<string, string>
  setRoleNames: (names: Record<string, string>) => void
  loadRoleNames: () => Promise<void>

  dataSources: string[]
  setDataSources: (sources: string[]) => void
  loadDataSources: () => void

  fetchLoading: boolean
  fetchProgress: number
  fetchProgressHistory: string[]
  fetchError: string
  fetchDone: boolean
  needsEpoch: number
  confettiFired: boolean
  setConfettiFired: (v: boolean) => void
  setFetchLoading: (v: boolean) => void
  setFetchProgress: (p: number) => void
  setFetchProgressHistory: (h: string[]) => void
  appendFetchProgressHistory: (msg: string) => void
  setFetchError: (msg: string) => void
  setFetchDone: (v: boolean) => void
  resetFetchProgress: () => void

  reportGenIdx: number | null
  reportGenProgress: number
  reportGenMsg: string
  setReportGenIdx: (idx: number | null) => void
  setReportGenProgress: (p: number) => void
  setReportGenMsg: (msg: string) => void

  personaNeedIndex: number | null
  setPersonaNeedIndex: (idx: number | null) => void
  whatsNewVisible: boolean
  setWhatsNewVisible: (v: boolean) => void
  checkAppVersion: (version: string) => void
}

let messageCounter = 0

const _savedView = (typeof localStorage !== 'undefined' && localStorage.getItem('lumon_active_view')) || 'fetch'
const _validViews = QUICK_SEARCH_ENABLED
  ? ['fetch', 'debate', 'reports', 'personas', 'quickSearch']
  : ['fetch', 'debate', 'reports', 'personas']
const _savedTheme = (typeof localStorage !== 'undefined' && localStorage.getItem('lumon_theme_mode')) || 'light'
const _savedLanguage = (typeof localStorage !== 'undefined' && localStorage.getItem('lumon_language_mode')) || 'zh-CN'

export const useAppStore = create<AppState>((set) => ({
  activeView: _validViews.includes(_savedView) ? _savedView as AppState['activeView'] : 'fetch',
  setActiveView: (v) => {
    try { localStorage.setItem('lumon_active_view', v) } catch { /* */ }
    set({ activeView: v })
  },
  themeMode: _savedTheme === 'dark' ? 'dark' : 'light',
  setThemeMode: (mode) => {
    try { localStorage.setItem('lumon_theme_mode', mode) } catch { /* */ }
    set({ themeMode: mode })
  },
  toggleThemeMode: () => set((state) => {
    const next = state.themeMode === 'dark' ? 'light' : 'dark'
    try { localStorage.setItem('lumon_theme_mode', next) } catch { /* */ }
    return { themeMode: next }
  }),
  languageMode: _savedLanguage === 'en-US' ? 'en-US' : 'zh-CN',
  setLanguageMode: (mode) => {
    try { localStorage.setItem('lumon_language_mode', mode) } catch { /* */ }
    set({ languageMode: mode })
  },
  toggleLanguageMode: () => set((state) => {
    const next = state.languageMode === 'en-US' ? 'zh-CN' : 'en-US'
    try { localStorage.setItem('lumon_language_mode', next) } catch { /* */ }
    return { languageMode: next }
  }),

  pendingReportFile: null,
  setPendingReportFile: (f) => set({ pendingReportFile: f }),

  needs: [],
  selectedNeedIndex: null,
  setNeeds: (needs) => {
    const normalized = normalizeNeeds(needs)
    set((state) => ({ needs: normalized, needsEpoch: normalized.length > 0 ? state.needsEpoch + 1 : state.needsEpoch }))
    if (normalized.length > 0) {
      syncNeeds(normalized).catch(() => {})
    }
  },
  setSelectedNeed: (idx) => set({ selectedNeedIndex: idx }),

  fetchHistory: [],
  activeFetchHistoryId: null,
  addFetchHistory: (item) => set((state) => {
    const isDup = state.fetchHistory.some(
      (h) => h.title === item.title && item.createdAt - h.createdAt < 60_000
    )
    if (isDup) return {}
    const updated = [item, ...state.fetchHistory].slice(0, 50)
    try { localStorage.setItem('lumon_fetch_history', JSON.stringify(updated)) } catch { /* */ }
    return { fetchHistory: updated }
  }),
  removeFetchHistory: (id) => set((state) => {
    const updated = state.fetchHistory.filter((h) => h.id !== id)
    try { localStorage.setItem('lumon_fetch_history', JSON.stringify(updated)) } catch { /* */ }
    return {
      fetchHistory: updated,
      activeFetchHistoryId: state.activeFetchHistoryId === id ? null : state.activeFetchHistoryId,
      needs: state.activeFetchHistoryId === id ? [] : state.needs,
    }
  }),
  setActiveFetchHistory: (id) => set((state) => {
    if (!id) return { activeFetchHistoryId: null }
    const item = state.fetchHistory.find((h) => h.id === id)
    if (!item) return {}
    const normalized = normalizeNeeds(item.needs)
    syncNeeds(normalized).catch(() => {})
    return { activeFetchHistoryId: id, needs: normalized }
  }),
  loadFetchHistory: () => {
    try {
      const raw = localStorage.getItem('lumon_fetch_history')
      if (raw) {
        const data = JSON.parse(raw) as FetchHistoryItem[]
        set({ fetchHistory: data })
      }
    } catch { /* */ }
  },

  debateStatus: 'idle',
  debateRound: 0,
  maxRounds: 5,
  messages: [],
  finalReport: null,
  productProposal: null,
  isStreaming: false,
  errorMessage: null,

  setDebateStatus: (s) => set({ debateStatus: s }),
  setDebateRound: (r) => set({ debateRound: r }),
  setMaxRounds: (r) => set({ maxRounds: r }),
  setFinalReport: (r) => set({ finalReport: r }),
  setProductProposal: (p) => set({ productProposal: p }),
  setIsStreaming: (v) => set({ isStreaming: v }),
  setErrorMessage: (msg) => set({ errorMessage: msg }),

  addMessage: (msg) =>
    set((state) => {
      const prev = state.messages[state.messages.length - 1]
      if (prev && prev.role === msg.role && !msg.streaming && msg.content && prev.content === msg.content && !msg.topicDivider) {
        return state
      }
      return { messages: [...state.messages, { ...msg, id: msg.id || `msg-${++messageCounter}` }] }
    }),

  appendToLastMessage: (text) =>
    set((state) => {
      const msgs = [...state.messages]
      if (msgs.length === 0) return state
      const last = { ...msgs[msgs.length - 1] }
      last.content += text
      msgs[msgs.length - 1] = last
      return { messages: msgs }
    }),

  finalizeLastMessage: (content) =>
    set((state) => {
      const msgs = [...state.messages]
      if (msgs.length === 0) return state
      const last = { ...msgs[msgs.length - 1] }
      last.content = content
      last.streaming = false
      msgs[msgs.length - 1] = last
      return { messages: msgs }
    }),

  cancelLastStreamingMessage: () =>
    set((state) => {
      if (state.messages.length === 0) return state
      const msgs = [...state.messages]
      const last = msgs[msgs.length - 1]
      if (!last.streaming) return state
      if (!last.content.trim()) {
        msgs.pop()
      } else {
        msgs[msgs.length - 1] = { ...last, streaming: false }
      }
      return { messages: msgs }
    }),

  clearMessages: () => set({ messages: [] }),

  resetDebate: () =>
    set({
      debateStatus: 'idle',
      debateRound: 0,
      messages: [],
      finalReport: null,
      productProposal: null,
      selectedNeedIndex: null,
      isStreaming: false,
      errorMessage: null,
      detailView: { type: 'empty' },
    }),

  resetDebateKeepPost: () =>
    set({
      debateStatus: 'idle',
      debateRound: 0,
      messages: [],
      finalReport: null,
      productProposal: null,
      isStreaming: false,
      errorMessage: null,
      detailView: { type: 'empty' },
    }),

  detailView: { type: 'empty' },
  setDetailView: (v) => set({ detailView: v }),

  configReady: null,
  setConfigReady: (v) => set({ configReady: v }),

  showSettingsDialog: false,
  setShowSettingsDialog: (v) => set({ showSettingsDialog: v }),
  settingsSection: 'models',
  setSettingsSection: (section) => set({ settingsSection: section }),

  roleNames: { director: '导演', analyst: '产品经理', critic: '杠精', investor: '投资人' },
  setRoleNames: (names) => set({ roleNames: names }),
  loadRoleNames: async () => {
    try {
      const { getRoleNames } = await import('../api/client')
      const names = await getRoleNames()
      set({ roleNames: { director: '导演', analyst: '产品经理', critic: '杠精', investor: '投资人', ...names } })
    } catch { /* */ }
  },

  dataSources: ['reddit'],
  setDataSources: (sources) => {
    try { localStorage.setItem('lumon_data_sources', JSON.stringify(sources)) } catch { /* */ }
    set({ dataSources: sources })
  },
  loadDataSources: () => {
    try {
      const raw = localStorage.getItem('lumon_data_sources')
      if (raw) set({ dataSources: JSON.parse(raw) })
    } catch { /* */ }
  },

  fetchLoading: false,
  fetchProgress: 0,
  fetchProgressHistory: [],
  fetchError: '',
  fetchDone: false,
  needsEpoch: 0,
  confettiFired: false,
  setConfettiFired: (v) => set({ confettiFired: v }),
  setFetchLoading: (v) => set({ fetchLoading: v }),
  setFetchProgress: (p) => set({ fetchProgress: p }),
  setFetchProgressHistory: (h) => set({ fetchProgressHistory: h }),
  appendFetchProgressHistory: (msg) => set((state) => ({ fetchProgressHistory: [...state.fetchProgressHistory, msg] })),
  setFetchError: (msg) => set({ fetchError: msg }),
  setFetchDone: (v) => set({ fetchDone: v }),
  resetFetchProgress: () => set({ fetchLoading: false, fetchProgress: 0, fetchProgressHistory: [], fetchError: '', fetchDone: false, confettiFired: false }),

  reportGenIdx: null,
  reportGenProgress: 0,
  reportGenMsg: '',
  setReportGenIdx: (idx) => set({ reportGenIdx: idx }),
  setReportGenProgress: (p) => set({ reportGenProgress: p }),
  setReportGenMsg: (msg) => set({ reportGenMsg: msg }),

  personaNeedIndex: null,
  setPersonaNeedIndex: (idx) => set({ personaNeedIndex: idx }),

  whatsNewVisible: false,
  setWhatsNewVisible: (v) => set({ whatsNewVisible: v }),
  checkAppVersion: (version) => {
    if (!version) return
    try {
      if (localStorage.getItem('lumon_whats_new_seen') !== version) set({ whatsNewVisible: true })
    } catch {
      set({ whatsNewVisible: true })
    }
  },

}))

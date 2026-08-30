import { useState, useEffect, useCallback, Component, type ReactNode, type ErrorInfo } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import NavSidebar from './components/NavSidebar'
import FetchView from './components/FetchView'
import ChatPanel from './components/ChatPanel'
import DetailPanel from './components/DetailPanel'
import ReportsView from './components/ReportsView'
import PersonaView from './components/PersonaView'
import QuickSearchView from './components/QuickSearchView'
import SettingsDialog from './components/SettingsDialog'
import WhatsNewModal from './components/WhatsNewModal'
import ResizeHandle from './components/ResizeHandle'
import { useAppStore } from './stores/app'
import { trackAnalyticsEvent } from './api/client'
import { QUICK_SEARCH_ENABLED } from './featureFlags'
import { useI18n } from './i18n'
import { APP_VERSION } from './version'

class ErrorBoundary extends Component<{ children: ReactNode; isEnglish: boolean }, { hasError: boolean; error: string }> {
  state = { hasError: false, error: '' }
  static getDerivedStateFromError(error: Error) { return { hasError: true, error: error.message } }
  componentDidCatch(error: Error, info: ErrorInfo) { console.error('[ErrorBoundary]', error, info) }
  render() {
    if (this.state.hasError) {
      return (
        <div className="flex items-center justify-center h-screen bg-bg">
          <div className="text-center p-8 bg-card rounded-2xl shadow-sm max-w-md">
            <p className="text-sm font-semibold mb-2">{this.props.isEnglish ? 'Something went wrong' : '页面出了点问题'}</p>
            <p className="text-xs text-muted mb-4 break-all">{this.state.error}</p>
            <button onClick={() => { this.setState({ hasError: false, error: '' }); window.location.reload() }}
              className="text-xs font-medium text-white bg-accent px-4 py-2 rounded-xl hover:opacity-90">
              {this.props.isEnglish ? 'Reload page' : '刷新页面'}
            </button>
          </div>
        </div>
      )
    }
    return this.props.children
  }
}

const MIN_DETAIL = 260
const MAX_DETAIL = 600
const DEFAULT_DETAIL = 380

const viewTransition = {
  initial: { opacity: 0, y: 6 },
  animate: { opacity: 1, y: 0 },
  exit: { opacity: 0, y: -6 },
  transition: { duration: 0.18, ease: 'easeOut' as const },
}

export default function App() {
  const { text, isEnglish } = useI18n()
  const activeView = useAppStore((s) => s.activeView)
  const setActiveView = useAppStore((s) => s.setActiveView)
  const checkAppVersion = useAppStore((s) => s.checkAppVersion)
  const themeMode = useAppStore((s) => s.themeMode)
  const languageMode = useAppStore((s) => s.languageMode)
  const [detailWidth, setDetailWidth] = useState(DEFAULT_DETAIL)
  const [mobileDebateTab, setMobileDebateTab] = useState<'chat' | 'detail'>('chat')

  useEffect(() => {
    const root = document.documentElement
    root.classList.toggle('dark', themeMode === 'dark')
    root.style.colorScheme = themeMode
  }, [themeMode])

  useEffect(() => {
    document.documentElement.lang = languageMode === 'en-US' ? 'en' : 'zh-CN'
    document.documentElement.dataset.languageMode = languageMode
    document.documentElement.classList.remove('language-fade-in')
    void document.documentElement.offsetWidth
    document.documentElement.classList.add('language-fade-in')
    const timer = window.setTimeout(() => {
      document.documentElement.classList.remove('language-fade-in')
    }, 260)
    return () => window.clearTimeout(timer)
  }, [languageMode])

  useEffect(() => {
    let frame = 0
    const resetOuterScroll = () => {
      if (frame) return
      frame = window.requestAnimationFrame(() => {
        frame = 0
        window.scrollTo(0, 0)
        document.documentElement.scrollTop = 0
        document.documentElement.scrollLeft = 0
        document.body.scrollTop = 0
        document.body.scrollLeft = 0
        const shell = document.querySelector<HTMLElement>('.liquid-glass-shell')
        if (shell) {
          shell.scrollTop = 0
          shell.scrollLeft = 0
        }
      })
    }
    resetOuterScroll()
    window.addEventListener('scroll', resetOuterScroll, { passive: true })
    document.addEventListener('scroll', resetOuterScroll, { passive: true, capture: true })
    return () => {
      if (frame) window.cancelAnimationFrame(frame)
      window.removeEventListener('scroll', resetOuterScroll)
      document.removeEventListener('scroll', resetOuterScroll, { capture: true })
    }
  }, [])

  useEffect(() => {
    trackAnalyticsEvent('app.view', { view: activeView })
  }, [activeView])

  useEffect(() => {
    checkAppVersion(APP_VERSION)
  }, [checkAppVersion])

  useEffect(() => {
    if (!QUICK_SEARCH_ENABLED && activeView === 'quickSearch') {
      setActiveView('fetch')
    }
  }, [activeView, setActiveView])

  const handleResize = useCallback((delta: number) => {
    setDetailWidth((w) => Math.min(MAX_DETAIL, Math.max(MIN_DETAIL, w - delta)))
  }, [])

  return (
    <ErrorBoundary isEnglish={isEnglish}>
      <div className="liquid-glass-shell flex h-[100dvh] overflow-hidden bg-bg p-2 gap-2 max-md:flex-col max-md:p-0 max-md:gap-0 max-md:bg-bg">
        <NavSidebar />

        <AnimatePresence mode="wait">
          {activeView === 'fetch' && (
            <motion.div key="fetch" {...viewTransition}
              className="liquid-glass-panel flex-1 min-w-0 min-h-0 bg-card rounded-3xl overflow-hidden shadow-sm max-md:rounded-none max-md:shadow-none max-md:bg-bg"
            >
              <FetchView />
            </motion.div>
          )}

          {QUICK_SEARCH_ENABLED && activeView === 'quickSearch' && (
            <motion.div key="quickSearch" {...viewTransition}
              className="liquid-glass-panel flex-1 min-w-0 min-h-0 bg-card rounded-3xl overflow-hidden shadow-sm max-md:rounded-none max-md:shadow-none max-md:bg-bg"
            >
              <QuickSearchView />
            </motion.div>
          )}

          {activeView === 'debate' && (
            <motion.div
              key="debate"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              transition={{ duration: 0.18, ease: 'easeOut' as const }}
              className="flex-1 flex min-w-0 min-h-0 overflow-hidden items-stretch max-md:flex-col"
            >
              <div className="hidden max-md:flex items-center gap-1 px-4 pt-3 pb-2 bg-bg shrink-0">
                {(['chat', 'detail'] as const).map((tab) => (
                  <button
                    key={tab}
                    onClick={() => setMobileDebateTab(tab)}
                    className={`px-4 py-1.5 rounded-lg text-sm font-medium transition-colors ${
                      mobileDebateTab === tab ? 'bg-accent/10 text-accent' : 'text-muted'
                    }`}
                  >
                    {tab === 'chat' ? text('common.chat') : text('common.details')}
                  </button>
                ))}
              </div>
              <div className={`liquid-glass-panel debate-glass-panel flex-1 min-w-0 min-h-0 bg-card rounded-3xl overflow-hidden shadow-sm max-md:rounded-none max-md:shadow-none max-md:bg-bg ${
                mobileDebateTab === 'detail' ? 'max-md:hidden' : ''
              }`}>
                <ChatPanel />
              </div>
              <ResizeHandle onResize={handleResize} className="max-md:hidden" />
              <div
                style={{ '--detail-w': `${detailWidth}px` } as React.CSSProperties}
                className={`liquid-glass-panel debate-glass-panel shrink-0 w-[var(--detail-w)] min-w-0 max-w-full bg-card rounded-3xl overflow-hidden shadow-sm max-md:rounded-none max-md:shadow-none max-md:bg-bg max-md:w-full max-md:flex-1 max-md:min-h-0 ${
                  mobileDebateTab === 'chat' ? 'max-md:hidden' : ''
                }`}
              >
                <DetailPanel />
              </div>
            </motion.div>
          )}

          {activeView === 'reports' && (
            <motion.div key="reports" {...viewTransition}
              className="liquid-glass-panel flex-1 min-w-0 min-h-0 bg-card rounded-3xl overflow-hidden shadow-sm max-md:rounded-none max-md:shadow-none max-md:bg-bg"
            >
              <ReportsView />
            </motion.div>
          )}

          {activeView === 'personas' && (
            <motion.div key="personas" {...viewTransition}
              className="liquid-glass-panel flex-1 min-w-0 min-h-0 bg-card rounded-3xl overflow-hidden shadow-sm max-md:rounded-none max-md:shadow-none max-md:bg-bg"
            >
              <PersonaView />
            </motion.div>
          )}
        </AnimatePresence>
      </div>

      <SettingsDialog />
      <WhatsNewModal />
    </ErrorBoundary>
  )
}

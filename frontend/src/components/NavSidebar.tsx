import { motion, AnimatePresence } from 'framer-motion'
import { ChevronDown, Info, Sun, X } from 'lucide-react'
import { flushSync } from 'react-dom'
import { useAppStore, type ActiveView } from '../stores/app'
import { useState, useRef, useCallback, type KeyboardEvent, type MouseEvent } from 'react'
import { GradientText } from './animations'
import { QUICK_SEARCH_ENABLED } from '../featureFlags'
import { LocalizedText, useI18n } from '../i18n'

const NAV_ITEMS: { id: ActiveView; img: string; labelKey: string }[] = [
  { id: 'fetch', img: '/ai-tools.png', labelKey: 'nav.fetch' },
  ...(QUICK_SEARCH_ENABLED ? [{ id: 'quickSearch' as ActiveView, img: '/global-search.png', labelKey: 'nav.quickSearch' }] : []),
  { id: 'debate', img: '/ai-commentary.png', labelKey: 'nav.debate' },
  { id: 'reports', img: '/book-saved.png', labelKey: 'nav.reports' },
  { id: 'personas', img: '/ai-users (1).png', labelKey: 'nav.personas' },
]

const THEME_TRANSITION_MS = 520

type ViewTransitionDocument = Document & {
  startViewTransition?: (callback: () => void) => {
    ready?: Promise<void>
    finished?: Promise<void>
  }
}

export default function NavSidebar() {
  const {
    activeView, setActiveView, setShowSettingsDialog, setSettingsSection,
    fetchHistory, activeFetchHistoryId,
    setActiveFetchHistory, removeFetchHistory,
    themeMode, setThemeMode, toggleLanguageMode, setWhatsNewVisible,
  } = useAppStore()
  const { text, isEnglish } = useI18n()
  const [historyCollapsed, setHistoryCollapsed] = useState(false)
  const [hoveredHistoryId, setHoveredHistoryId] = useState<string | null>(null)
  const navButtonRefs = useRef<Partial<Record<ActiveView, HTMLButtonElement | null>>>({})

  const openSettings = () => {
    setSettingsSection('models')
    setShowSettingsDialog(true)
  }

  const handleLanguageClick = () => {
    toggleLanguageMode()
  }

  const activateNavItem = useCallback((id: ActiveView, focusAfterSwitch = false) => {
    setActiveFetchHistory(null)
    setActiveView(id)
    if (focusAfterSwitch) {
      requestAnimationFrame(() => navButtonRefs.current[id]?.focus())
    }
  }, [setActiveFetchHistory, setActiveView])

  const handleNavClick = useCallback((id: ActiveView) => {
    activateNavItem(id)
  }, [activateNavItem])

  const handleNavKeyDown = useCallback((event: KeyboardEvent<HTMLElement>) => {
    if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return

    event.preventDefault()
    const focusedItem = NAV_ITEMS.find(({ id }) => navButtonRefs.current[id] === document.activeElement)
    const currentId = focusedItem?.id ?? activeView
    const currentIndex = Math.max(0, NAV_ITEMS.findIndex(({ id }) => id === currentId))
    const direction = event.key === 'ArrowDown' ? 1 : -1
    const nextIndex = (currentIndex + direction + NAV_ITEMS.length) % NAV_ITEMS.length
    activateNavItem(NAV_ITEMS[nextIndex].id, true)
  }, [activeView, activateNavItem])

  const runThemeToggle = useCallback((button: HTMLButtonElement) => {
    const nextTheme = themeMode === 'dark' ? 'light' : 'dark'
    const applyTheme = () => {
      const root = document.documentElement
      root.classList.toggle('dark', nextTheme === 'dark')
      root.style.colorScheme = nextTheme
      flushSync(() => setThemeMode(nextTheme))
    }

    const viewTransitionDocument = document as ViewTransitionDocument
    if (!button || typeof viewTransitionDocument.startViewTransition !== 'function') {
      applyTheme()
      return
    }

    const viewportWidth = window.visualViewport?.width ?? window.innerWidth
    const viewportHeight = window.visualViewport?.height ?? window.innerHeight
    const { top, left, width, height } = button.getBoundingClientRect()
    const x = left + width / 2
    const y = top + height / 2
    const maxRadius = Math.hypot(
      Math.max(x, viewportWidth - x),
      Math.max(y, viewportHeight - y),
    )
    const clipPath = [
      `circle(0px at ${x}px ${y}px)`,
      `circle(${maxRadius}px at ${x}px ${y}px)`,
    ]
    const root = document.documentElement
    root.dataset.magicuiThemeVt = 'active'
    root.style.setProperty('--magicui-theme-toggle-vt-duration', `${THEME_TRANSITION_MS}ms`)
    root.style.setProperty('--magicui-theme-vt-clip-from', clipPath[0])
    const cleanup = () => {
      delete root.dataset.magicuiThemeVt
      root.style.removeProperty('--magicui-theme-toggle-vt-duration')
      root.style.removeProperty('--magicui-theme-vt-clip-from')
    }

    let transition: ReturnType<NonNullable<ViewTransitionDocument['startViewTransition']>>
    try {
      transition = viewTransitionDocument.startViewTransition(() => {
        applyTheme()
      })
    } catch {
      cleanup()
      applyTheme()
      return
    }

    if (transition.finished) {
      transition.finished.finally(cleanup)
    } else {
      window.setTimeout(cleanup, THEME_TRANSITION_MS + 120)
    }
    transition.ready?.then(() => {
      try {
        document.documentElement.animate(
          { clipPath },
          {
            duration: THEME_TRANSITION_MS,
            easing: 'ease-in-out',
            fill: 'forwards',
            pseudoElement: '::view-transition-new(root)',
          },
        )
      } catch {
        cleanup()
      }
    }).catch(cleanup)
  }, [setThemeMode, themeMode])

  const handleThemeClick = useCallback((event: MouseEvent<HTMLButtonElement>) => {
    runThemeToggle(event.currentTarget)
  }, [runThemeToggle])

  const isDarkMode = themeMode === 'dark'

  return (
    <>
    <div className="liquid-glass-panel sidebar-panel bg-card rounded-3xl flex flex-col pt-5 pb-3 shrink-0 shadow-sm max-md:hidden">
      {/* Logo + brand */}
      <div className="flex items-center gap-3 px-5 mb-6">
        <div className="w-10 h-10 shrink-0 flex items-center justify-center">
          <img
            src="/Group 47260.png"
            alt="Logo"
            className="w-full h-full object-contain drop-shadow-[0_2px_5px_rgba(0,0,0,0.18)]"
          />
        </div>
        <span className="-ml-2" style={{ fontFamily: "ui-monospace, 'SFMono-Regular', Menlo, Consolas, monospace" }}>
          <GradientText
            className="text-[22px] tracking-normal"
            colors={isDarkMode ? ['#f4f7fb', '#9da7b4', '#f4f7fb'] : ['#2c2c2c', '#6b6b6b', '#2c2c2c']}
            animationSpeed={6}
          >
            lumon
          </GradientText>
        </span>
      </div>

      {/* Main nav */}
      <nav className="px-3 space-y-1" aria-label={text('nav.main')} onKeyDown={handleNavKeyDown}>
        {NAV_ITEMS.map(({ id, img, labelKey }) => {
          const isActive = activeView === id
          const label = text(labelKey)
          return (
            <button
              key={id}
              ref={(element) => { navButtonRefs.current[id] = element }}
              onClick={() => handleNavClick(id)}
              aria-current={isActive ? 'page' : undefined}
              className={`relative w-full flex items-center gap-2.5 px-3.5 h-11 rounded-xl text-[14px] transition-all ${
                isActive
                  ? 'font-semibold text-accent'
                  : 'text-text/50 hover:text-text/80 hover:bg-bg/60'
              }`}
            >
              {isActive && (
                <motion.div
                  layoutId="nav-active"
                  className="absolute inset-0 bg-accent/8 rounded-xl"
                  transition={{ type: 'spring', stiffness: 400, damping: 30 }}
                />
              )}
              <img
                src={img}
                alt=""
                className={`relative z-10 w-[18px] h-[18px] object-contain brightness-0 transition-[opacity,transform] ${
                  isActive ? 'opacity-90' : 'opacity-45'
                }`}
              />
              <LocalizedText variant="roll" className="sidebar-nav-label relative z-10 block flex-1 min-w-0 text-left truncate whitespace-nowrap">
                {label}
              </LocalizedText>
            </button>
          )
        })}
      </nav>

      {/* Fetch history */}
      {fetchHistory.length > 0 && (
        <div className="mt-5 px-3 shrink-0">
          <div className="sidebar-history-panel rounded-2xl bg-[#f1f3f5]/80 border border-white/50 overflow-hidden shadow-[inset_0_1px_0_rgba(255,255,255,0.5)]">
          <button
            onClick={() => setHistoryCollapsed(!historyCollapsed)}
            className="w-full flex items-center gap-1.5 px-3.5 h-9 text-[12px] text-muted font-medium hover:text-text/70 hover:bg-white/35 transition-colors"
          >
            <ChevronDown size={11} className={`transition-transform duration-200 ${historyCollapsed ? '-rotate-90' : ''}`} />
            <LocalizedText variant="roll">{text('nav.history')}</LocalizedText>
              <span className="ml-auto text-[10px] text-muted/50 font-normal tabular-nums">{fetchHistory.length}</span>
          </button>
          <AnimatePresence>
            {!historyCollapsed && (
              <motion.div
                initial={{ height: 0, opacity: 0 }}
                animate={{ height: 'auto', opacity: 1, overflow: 'visible' }}
                exit={{ height: 0, opacity: 0, overflow: 'hidden' }}
                transition={{ duration: 0.2, ease: 'easeInOut' }}
                style={{ overflow: 'hidden' }}
              >
                <div className="overflow-y-auto scrollbar-auto space-y-0.5 px-2 pb-2 overscroll-contain" style={{ maxHeight: '196px' }}>
                  {fetchHistory.map((item) => {
                    const isActive = activeFetchHistoryId === item.id && activeView === 'fetch'
                    const isHovered = hoveredHistoryId === item.id
                    return (
                      <div
                        key={item.id}
                        onMouseEnter={() => setHoveredHistoryId(item.id)}
                        onMouseLeave={() => setHoveredHistoryId(null)}
                        className={`w-full flex items-center gap-2.5 px-3.5 h-[34px] rounded-xl text-left transition-all cursor-pointer group relative ${
                          isActive
                            ? 'bg-accent/8 text-accent'
                            : isHovered
                              ? 'bg-white/55 text-text/80'
                              : 'text-text/60'
                        }`}
                        onClick={() => {
                          setActiveFetchHistory(item.id)
                          setActiveView('fetch')
                        }}
                      >
                        <img
                          src="/firstline.png"
                          alt=""
                          className={`w-[13px] h-[13px] object-contain brightness-0 shrink-0 ${isActive ? 'opacity-80' : 'opacity-35'}`}
                        />
                        <span className="flex-1 min-w-0 text-[12px] leading-snug truncate pr-5">{item.title}</span>
                        <AnimatePresence>
                          {(isHovered || isActive) && (
                            <motion.button
                              initial={{ opacity: 0, scale: 0.8 }}
                              animate={{ opacity: 1, scale: 1 }}
                              exit={{ opacity: 0, scale: 0.8 }}
                              transition={{ duration: 0.12 }}
                              onClick={(e) => {
                                e.stopPropagation()
                                removeFetchHistory(item.id)
                              }}
                              className="absolute right-2 top-1/2 -translate-y-1/2 w-5 h-5 rounded-md flex items-center justify-center text-muted/50 hover:text-signal hover:bg-signal/10 transition-colors"
                            >
                              <X size={11} />
                            </motion.button>
                          )}
                        </AnimatePresence>
                      </div>
                    )
                  })}
                </div>
              </motion.div>
            )}
          </AnimatePresence>
          </div>
        </div>
      )}

      {/* Spacer */}
      <div className="flex-1" />

      {/* Settings - pushed to bottom */}
      <div className="px-3">
        <div className="mx-2 mb-3 h-[1.5px] bg-border/35 rounded-full" />
        <div className="sidebar-action-row flex items-center justify-center gap-2 px-1">
          <button
            onClick={openSettings}
            aria-label={text('common.settings')}
            title={text('common.settings')}
            className="sidebar-settings-button sidebar-action-button w-10 h-10 rounded-xl flex items-center justify-center text-text/50 hover:text-text/80 hover:bg-bg/60 transition-all"
          >
            <img src="/setting (2).png" alt="" className="sidebar-settings-icon w-[19px] h-[19px] object-contain brightness-0 opacity-45" />
          </button>
          <button
            onClick={() => setWhatsNewVisible(true)}
            aria-label={text('common.changelog')}
            title={text('common.changelog')}
            className="sidebar-action-button w-10 h-10 rounded-xl flex items-center justify-center text-text/50 hover:text-text/80 hover:bg-bg/60 transition-all"
          >
            <Info size={18} />
          </button>
          <button
            onClick={handleLanguageClick}
            aria-label={text('common.language')}
            title={text('common.language')}
            className="sidebar-language-button sidebar-action-button w-10 h-10 rounded-xl flex items-center justify-center text-text/50 hover:text-text/80 hover:bg-bg/60 transition-all"
          >
            <img src="/translate.png" alt="" className="sidebar-language-icon w-[20px] h-[20px] object-contain brightness-0 opacity-50" />
          </button>
          <button
            onClick={handleThemeClick}
            aria-label={isDarkMode ? text('common.themeLight') : text('common.themeDark')}
            title={isDarkMode ? text('common.themeLight') : text('common.themeDark')}
            className="sidebar-theme-button sidebar-action-button w-10 h-10 rounded-xl flex items-center justify-center text-text/50 hover:text-text/80 transition-all"
          >
            {isDarkMode ? <Sun size={18} /> : <img src="/moon.png" alt="" className="sidebar-theme-icon w-[20px] h-[20px] object-contain brightness-0 opacity-55" />}
          </button>
        </div>
      </div>
    </div>

    {/* Mobile bottom tab bar */}
    <nav
      className="liquid-glass-panel md:hidden bg-bg border-t border-border/20 flex items-center shrink-0"
      style={{ paddingBottom: 'env(safe-area-inset-bottom, 0px)' }}
    >
      {NAV_ITEMS.map(({ id, img, labelKey }) => {
        const isActive = activeView === id
        const label = text(labelKey)
        return (
          <button
            key={id}
            onClick={() => handleNavClick(id)}
            className={`relative flex-1 flex flex-col items-center gap-0.5 pt-2 pb-1.5 transition-colors ${
              isActive ? 'text-accent' : 'text-muted'
            }`}
          >
            <img
              src={img}
              alt=""
              className={`w-[18px] h-[18px] object-contain brightness-0 ${isActive ? 'opacity-90' : 'opacity-45'}`}
            />
              <LocalizedText variant="roll" className={`${isEnglish ? 'text-[9px]' : 'text-[10px]'} leading-tight max-w-full truncate ${isActive ? 'font-semibold' : ''}`}>
                {label}
              </LocalizedText>
          </button>
        )
      })}
      <button
        onClick={openSettings}
        aria-label={text('common.settings')}
        className="sidebar-settings-button flex-1 flex items-center justify-center py-2 text-muted transition-colors"
      >
        <img src="/setting (2).png" alt="" className="sidebar-settings-icon w-[18px] h-[18px] object-contain brightness-0 opacity-40" />
      </button>
      <button
        onClick={() => setWhatsNewVisible(true)}
        aria-label={text('common.changelog')}
        className="flex-1 flex items-center justify-center py-2 text-muted transition-colors"
      >
        <Info size={18} />
      </button>
      <button
        onClick={handleLanguageClick}
        aria-label={text('common.language')}
        className="sidebar-language-button flex-1 flex items-center justify-center py-2 text-muted transition-colors"
      >
        <img src="/translate.png" alt="" className="sidebar-language-icon w-[19px] h-[19px] object-contain brightness-0 opacity-45" />
      </button>
      <button
        onClick={handleThemeClick}
        aria-label={isDarkMode ? text('common.themeLight') : text('common.themeDark')}
        className="sidebar-theme-button flex-1 flex items-center justify-center py-2 text-muted transition-colors"
      >
        {isDarkMode ? <Sun size={18} /> : <img src="/moon.png" alt="" className="sidebar-theme-icon w-[20px] h-[20px] object-contain brightness-0 opacity-55" />}
      </button>
    </nav>
    </>
  )
}

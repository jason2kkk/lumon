import { useRef, useEffect, useState, useCallback } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import {
  Play, RotateCcw, Send,
  Square, XCircle, ArrowDown,
} from 'lucide-react'
import { useAppStore } from '../stores/app'
import { streamSSE, getDebateState, resetDebate as apiResetDebate, streamGenerateReport, trackAnalyticsEvent } from '../api/client'
import ChatMessageComponent from './ChatMessage'
import ConfirmDialog from './ConfirmDialog'
import { ShimmerText } from './animations'
import { HelpButton, DEBATE_HELP } from './HelpDialog'
import { localizeRoleName, useI18n } from '../i18n'

export default function ChatPanel() {
  const { text, list, language, isEnglish } = useI18n()
  const needs = useAppStore((s) => s.needs)
  const selectedNeedIndex = useAppStore((s) => s.selectedNeedIndex)
  const debateStatus = useAppStore((s) => s.debateStatus)
  const messages = useAppStore((s) => s.messages)
  const maxRounds = useAppStore((s) => s.maxRounds)
  const isStreaming = useAppStore((s) => s.isStreaming)
  const errorMessage = useAppStore((s) => s.errorMessage)
  const roleNames = useAppStore((s) => s.roleNames)
  const setDebateStatus = useAppStore((s) => s.setDebateStatus)
  const setDebateRound = useAppStore((s) => s.setDebateRound)
  const addMessage = useAppStore((s) => s.addMessage)
  const appendToLastMessage = useAppStore((s) => s.appendToLastMessage)
  const finalizeLastMessage = useAppStore((s) => s.finalizeLastMessage)
  const cancelLastStreamingMessage = useAppStore((s) => s.cancelLastStreamingMessage)
  const clearMessages = useAppStore((s) => s.clearMessages)
  const setFinalReport = useAppStore((s) => s.setFinalReport)
  const resetDebateKeepPost = useAppStore((s) => s.resetDebateKeepPost)
  const setDetailView = useAppStore((s) => s.setDetailView)
  const setIsStreaming = useAppStore((s) => s.setIsStreaming)
  const setErrorMessage = useAppStore((s) => s.setErrorMessage)
  const setActiveView = useAppStore((s) => s.setActiveView)
  const setPendingReportFile = useAppStore((s) => s.setPendingReportFile)

  const [reportProgress, setReportProgress] = useState(0)
  const [reportMsg, setReportMsg] = useState('')
  const [freeTopicTitle, setFreeTopicTitle] = useState<string | null>(null)
  const [confirmAction, setConfirmAction] = useState<{
    title: string; message: string; action: () => void
  } | null>(null)
  const bottomRef = useRef<HTMLDivElement>(null)
  const scrollContainerRef = useRef<HTMLDivElement>(null)
  const abortRef = useRef<AbortController | null>(null)
  const userScrolledUpRef = useRef(false)
  const debateStartedAtRef = useRef(0)
  const reportStartedAtRef = useRef(0)
  const [showScrollBtn, setShowScrollBtn] = useState(false)
  const prevNeedIdxRef = useRef<number | null>(selectedNeedIndex)

  const hasNeed = selectedNeedIndex !== null && needs.length > 0 && selectedNeedIndex < needs.length
  const need = hasNeed ? needs[selectedNeedIndex] : null
  const needTitle = need
    ? (isEnglish && need.need_title_en?.trim() ? need.need_title_en.trim() : need.need_title)
    : ''

  const scrollMessagesToBottom = useCallback((behavior: ScrollBehavior = 'smooth') => {
    const el = scrollContainerRef.current
    if (!el) return
    el.scrollTo({ top: el.scrollHeight, behavior })
    requestAnimationFrame(() => {
      window.scrollTo(0, 0)
      document.documentElement.scrollTop = 0
      document.body.scrollTop = 0
      const shell = document.querySelector<HTMLElement>('.liquid-glass-shell')
      if (shell) {
        shell.scrollTop = 0
        shell.scrollLeft = 0
      }
    })
  }, [])

  useEffect(() => {
    getDebateState().then((state) => {
      if (state.debate_log.length > 0 && messages.length === 0) {
        const rn = useAppStore.getState().roleNames
        const roleLabels: Record<string, string> = {
          analyst: localizeRoleName('analyst', rn.analyst, language),
          critic: localizeRoleName('critic', rn.critic, language),
          director: localizeRoleName('director', rn.director, language),
          investor: localizeRoleName('investor', rn.investor, language),
          human: text('debate.humanRole') || 'You',
        }
        for (const entry of state.debate_log) {
          const role = entry.role as 'analyst' | 'critic' | 'director' | 'human' | 'investor'
          addMessage({
            id: '', role,
            label: roleLabels[role] || role,
            content: entry.content,
          })
        }
        if (state.selected_need_idx !== null) useAppStore.getState().setSelectedNeed(state.selected_need_idx)
        if (state.free_topic_input) setFreeTopicTitle(state.free_topic_input)
        const legacyMap: Record<string, string> = { generating_proposal: 'debate_done', proposal_done: 'debate_done', deep_diving: 'debate_done', deep_dive_done: 'debate_done' }
        const normalizedStatus = legacyMap[state.status] || state.status
        setDebateStatus(normalizedStatus as typeof debateStatus)
        setDebateRound(state.round)
        if (state.final_report) setFinalReport(state.final_report)
        const hasAnalyst = state.debate_log.some((e) => e.role === 'analyst')
        setDetailView({ type: hasAnalyst ? 'analysis' : 'post' })
      }
    }).catch(() => {})
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    if (prevNeedIdxRef.current !== null && selectedNeedIndex !== null && prevNeedIdxRef.current !== selectedNeedIndex && debateStatus !== 'idle') {
      abortRef.current?.abort()
      abortRef.current = null
      setIsStreaming(false)
      resetDebateKeepPost()
      apiResetDebate().catch(() => {})
    }
    prevNeedIdxRef.current = selectedNeedIndex
  }, [selectedNeedIndex, debateStatus, setIsStreaming, resetDebateKeepPost])

  useEffect(() => {
    if (hasNeed && messages.length === 0 && debateStatus === 'idle') {
      setDetailView({ type: 'post' })
    }
  }, [hasNeed, messages.length, debateStatus, setDetailView])

  // Track user scroll position — only auto-scroll when user is near bottom
  useEffect(() => {
    const el = scrollContainerRef.current
    if (!el) return
    const onScroll = () => {
      const distFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight
      const isUp = distFromBottom > 120
      userScrolledUpRef.current = isUp
      setShowScrollBtn(isUp)
    }
    el.addEventListener('scroll', onScroll, { passive: true })
    return () => el.removeEventListener('scroll', onScroll)
  }, [])

  // Auto-scroll: only on new message count (not every chunk), and only when near bottom
  const msgCountRef = useRef(messages.length)
  const lastStreamIdRef = useRef<string | null>(null)

  useEffect(() => {
    const countChanged = messages.length !== msgCountRef.current
    msgCountRef.current = messages.length

    if (messages.length === 0) return
    const last = messages[messages.length - 1]

    if (countChanged && !userScrolledUpRef.current) {
      scrollMessagesToBottom('smooth')
    }

    // Auto-show analysis panel when PM's first message starts
    if (last.role === 'analyst' && last.streaming && last.id !== lastStreamIdRef.current) {
      lastStreamIdRef.current = last.id
      const { detailView } = useAppStore.getState()
      if (detailView.type === 'empty' || detailView.type === 'post') {
        setDetailView({ type: 'analysis' })
      }
    }
    if (!last.streaming) {
      lastStreamIdRef.current = null
    }
  }, [messages.length, setDetailView]) // eslint-disable-line react-hooks/exhaustive-deps

  const handleShowPost = useCallback(() => {
    setDetailView({ type: 'post' })
  }, [setDetailView])


  const handleStop = useCallback(() => {
    abortRef.current?.abort()
    abortRef.current = null
    cancelLastStreamingMessage()
    setIsStreaming(false)
    setDebateStatus('debate_done')
    trackAnalyticsEvent('debate.stop', {
      round: useAppStore.getState().debateRound,
      duration_ms: debateStartedAtRef.current ? Date.now() - debateStartedAtRef.current : 0,
    })
  }, [cancelLastStreamingMessage, setIsStreaming, setDebateStatus])

  const runDebateStream = useCallback(async (endpoint: string, body: Record<string, unknown>) => {
    setErrorMessage(null)
    setIsStreaming(true)
    clearMessages()
    setDebateStatus('debating')
    setFinalReport(null)
    debateStartedAtRef.current = Date.now()
    trackAnalyticsEvent('debate.start', {
      endpoint,
      demo: Boolean(body.demo),
      max_rounds: typeof body.max_rounds === 'number' ? body.max_rounds : 0,
      has_need: typeof body.need_index === 'number',
    })

    const controller = new AbortController()
    abortRef.current = controller
    const sig = controller.signal

    await streamSSE(endpoint, body, {
      onMessageStart: (data) => {
        if (sig.aborted) return
        addMessage({ id: '', role: data.role as 'analyst' | 'critic' | 'director', label: data.label, content: '', streaming: true, provider: data.provider as 'claude' | 'gpt' | undefined })
      },
      onChunk: (data) => { if (!sig.aborted) appendToLastMessage(data.text) },
      onMessageEnd: (data) => { if (!sig.aborted) finalizeLastMessage(data.content) },
      onRoundStart: (data) => { if (!sig.aborted) setDebateRound(data.round) },
      onTopicStart: (data) => {
        if (sig.aborted) return
        addMessage({
          id: '', role: 'director', label: '',
          content: '', topicDivider: { index: data.index, title: data.title, total: data.total },
        })
      },
      onDebateEnd: () => {
        if (!sig.aborted) {
          setDebateStatus('debate_done')
          trackAnalyticsEvent('debate.done', {
            endpoint,
            round: useAppStore.getState().debateRound,
            duration_ms: Date.now() - debateStartedAtRef.current,
          })
        }
      },
      onError: (data) => {
        if (sig.aborted) return
        cancelLastStreamingMessage()
        setErrorMessage(data.message)
        setDebateStatus('error')
        trackAnalyticsEvent('debate.error', {
          endpoint,
          round: useAppStore.getState().debateRound,
          duration_ms: Date.now() - debateStartedAtRef.current,
        })
      },
    }, controller.signal)

    abortRef.current = null
    setIsStreaming(false)
  }, [clearMessages, setDebateStatus, setFinalReport, addMessage, appendToLastMessage, finalizeLastMessage, cancelLastStreamingMessage, setDebateRound, setIsStreaming, setErrorMessage])

  const handleStartDebate = useCallback(async (demo = false) => {
    if (!hasNeed || selectedNeedIndex === null) return
    await runDebateStream('/debate/start', { need_index: selectedNeedIndex, max_rounds: maxRounds, demo, language })
  }, [hasNeed, selectedNeedIndex, maxRounds, language, runDebateStream])

  // TODO: 加入讨论功能即将更新 — 以下功能暂时禁用
  // const handleStartFreeDebate = useCallback(async (text: string) => { ... }, [runDebateStream])
  // const handleSendMessage = useCallback(async () => { ... }, [...])

  const handleGenerateReport = useCallback(async () => {
    if (selectedNeedIndex === null) return
    setIsStreaming(true)
    setErrorMessage(null)
    setDebateStatus('generating_report')
    setReportProgress(0)
    setReportMsg(text('reports.preparingReport') || 'Preparing report...')
    reportStartedAtRef.current = Date.now()
    trackAnalyticsEvent('report.start', { source: 'debate_panel', need_index: selectedNeedIndex })

    const controller = new AbortController()
    abortRef.current = controller

    let chunkCount = 0
    let maxProgress = 0
    let lastMsgIdx = -1
    const writingMsgs = list('reports.writingMessages')
    const msgThresholds = writingMsgs.map((_, i) => 53 + Math.floor(i * 44 / (writingMsgs.length - 1)))

    await streamGenerateReport(selectedNeedIndex, {
      onProgress: (data) => {
        if (data.progress >= maxProgress) {
          maxProgress = data.progress
          setReportProgress(data.progress)
        }
        setReportMsg(data.message)
      },
      onChunk: () => {
        chunkCount++
        if (chunkCount % 2 === 0) {
          const p = Math.min(52 + Math.floor(46 * chunkCount / (chunkCount + 350)), 98)
          if (p > maxProgress) { maxProgress = p; setReportProgress(p) }
        }
        if (chunkCount === 1) { lastMsgIdx = 0; setReportMsg(writingMsgs[0]) }
        else {
          const nextIdx = lastMsgIdx + 1
          if (nextIdx < writingMsgs.length && maxProgress >= msgThresholds[nextIdx]) {
            lastMsgIdx = nextIdx; setReportMsg(writingMsgs[nextIdx])
          }
        }
      },
      onDone: (data) => {
        setReportProgress(100)
        setReportMsg(text('reports.reportDone') || 'Report ready!')
        setDebateStatus('done')
        trackAnalyticsEvent('report.done', {
          source: 'debate_panel',
          need_index: selectedNeedIndex,
          chunk_count: chunkCount,
          has_output_file: Boolean(data?.filename),
          duration_ms: Date.now() - reportStartedAtRef.current,
        })
        setTimeout(() => {
          if (data?.filename) setPendingReportFile(data.filename)
          setActiveView('reports')
        }, 600)
      },
      onError: (data) => {
        const msg = data.message || (text('reports.reportFailed') || 'Report generation failed')
        setErrorMessage(msg)
        setDebateStatus('debate_done')
        trackAnalyticsEvent('report.error', {
          source: 'debate_panel',
          need_index: selectedNeedIndex,
          duration_ms: Date.now() - reportStartedAtRef.current,
        })
      },
    }, controller.signal, { language })

    abortRef.current = null
    setIsStreaming(false)
  }, [selectedNeedIndex, language, list, text, setDebateStatus, setIsStreaming, setErrorMessage, setActiveView, setPendingReportFile])

  const handleReset = useCallback(() => {
    setConfirmAction({
      title: text('debate.resetTitle'),
      message: text('debate.resetMessage'),
      action: async () => {
        setConfirmAction(null)
        abortRef.current?.abort()
        abortRef.current = null
        setIsStreaming(false)
        setErrorMessage(null)
        await new Promise((r) => setTimeout(r, 50))
        setFreeTopicTitle(null)
        resetDebateKeepPost()
        apiResetDebate().catch(() => {})
        trackAnalyticsEvent('debate.reset', { had_need: hasNeed })
      },
    })
  }, [resetDebateKeepPost, setIsStreaming, setErrorMessage, hasNeed, text])

  const statusLabels: Record<string, string> = {
    idle: text('debate.statusIdle'), debating: text('debate.statusDebating'), error: text('debate.statusError'), debate_done: text('debate.statusDone'),
    generating_report: text('debate.statusGeneratingReport'), done: text('debate.statusReportReady'),
    generating_proposal: text('debate.statusDone'), proposal_done: text('debate.statusDone'),
    deep_diving: text('debate.statusDone'), deep_dive_done: text('debate.statusDone'),
  }

  return (
    <div className="flex flex-col h-full min-w-0">
      {/* Header */}
      <div className="shrink-0 px-5 py-3">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2.5 min-w-0">
            {need ? (
              <button
                onClick={handleShowPost}
                className="font-semibold text-sm truncate text-left transition-colors hover:text-accent max-md:max-w-[35vw]"
                title={text('detail.needDetail')}
              >
                {needTitle}
              </button>
            ) : freeTopicTitle ? (
              <h2 className="font-semibold text-sm truncate">{freeTopicTitle}</h2>
            ) : (
              <h2 className="font-semibold text-sm truncate text-muted">{text('debate.chooseNeed')}</h2>
            )}
            <span className="max-md:hidden"><HelpButton {...DEBATE_HELP} /></span>
            {(hasNeed || freeTopicTitle) && (
              <span className={`shrink-0 text-[10px] font-medium px-2 py-0.5 rounded-full ${
                debateStatus === 'debating' ? 'bg-blue-50 text-blue-600' :
                debateStatus === 'error' ? 'bg-red-50 text-red-600' :
                debateStatus === 'debate_done' ? 'bg-gray-100 text-gray-600' :
                debateStatus === 'generating_report' ? 'bg-violet-50 text-violet-600' :
                debateStatus === 'done' ? 'bg-violet-50 text-violet-600' :
                'bg-bg text-muted'
              }`}>
                {statusLabels[debateStatus] || debateStatus}
              </span>
            )}
          </div>
          <div className="flex gap-2 shrink-0">
            {debateStatus === 'idle' && hasNeed && (
              <>
                <button onClick={() => handleStartDebate(true)}
                  className="text-[13px] font-medium text-accent/70 hover:text-accent transition-colors whitespace-nowrap">
                  {text('debate.demo')}
                </button>
                <button onClick={() => handleStartDebate(false)}
                  className="flex items-center gap-1.5 bg-accent text-white text-xs font-medium h-8 px-3.5 rounded-xl hover:opacity-90 transition-opacity">
                  <Play size={11} strokeWidth={2.5} /> {text('debate.start')}
                </button>
              </>
            )}

            {(debateStatus === 'debating' ||
              debateStatus === 'generating_report') && (
              <>
                <button onClick={handleStop}
                  className="flex items-center gap-1.5 text-xs font-medium text-signal border border-signal/30 h-8 px-3.5 rounded-xl hover:bg-signal/5 transition-colors">
                  <Square size={9} fill="currentColor" /> {text('debate.stop')}
                </button>
                <button onClick={handleReset}
                  className="flex items-center gap-1.5 text-xs text-muted border border-border/50 h-8 px-2.5 rounded-xl hover:border-accent/40 transition-colors">
                  <RotateCcw size={11} />
                </button>
              </>
            )}

            {debateStatus === 'debate_done' && (
              <>
                <button onClick={handleGenerateReport} disabled={isStreaming || selectedNeedIndex === null}
                  className="report-action-button flex items-center gap-1.5 bg-accent text-white text-xs font-medium h-8 px-3.5 rounded-xl hover:opacity-90 transition-opacity disabled:opacity-50">
                  <span className="report-action-button-content gap-1.5">
                    <img src="/book_2_ai_line.png" alt="" className="report-action-icon w-3 h-3 brightness-0 invert opacity-80" />
                    <span>{text('debate.generateReport')}</span>
                  </span>
                </button>
                <button onClick={handleReset}
                  className="flex items-center gap-1.5 text-xs text-muted border border-border/50 h-8 px-2.5 rounded-xl hover:border-accent/40 transition-colors">
                  <RotateCcw size={11} />
                </button>
              </>
            )}

            {debateStatus === 'done' && (
              <>
                <button onClick={() => setActiveView('reports')}
                  className="report-action-button flex items-center gap-1.5 text-xs font-medium text-accent border border-border/50 h-8 px-3 rounded-xl hover:border-accent/40 transition-colors">
                  <span className="report-action-button-content gap-1.5">
                    <img src="/book_2_ai_line.png" alt="" className="report-action-icon w-3 h-3 opacity-60" />
                    <span>{text('debate.viewReport')}</span>
                  </span>
                </button>
                <button onClick={handleReset}
                  className="flex items-center gap-1.5 text-xs text-muted border border-border/50 h-8 px-2.5 rounded-xl hover:border-accent/40 transition-colors">
                  <RotateCcw size={11} />
                </button>
              </>
            )}

            {/* 兜底：旧会话状态（proposal_done / deep_dive_done 等） */}
            {debateStatus !== 'idle' && debateStatus !== 'debating' && debateStatus !== 'debate_done' && debateStatus !== 'generating_report' && debateStatus !== 'done' && (
              <button onClick={handleReset}
                className="flex items-center gap-1.5 text-xs text-muted border border-border/50 h-8 px-2.5 rounded-xl hover:border-accent/40 transition-colors">
                <RotateCcw size={11} /> {text('debate.reset')}
              </button>
            )}
          </div>
        </div>
      </div>
      <div className="mx-5 h-[1px] bg-border/30" />

      {/* Messages */}
      <div className="relative flex-1 min-h-0">
      <div ref={scrollContainerRef} className="h-full overflow-y-auto scrollbar-auto px-5 py-4">
        {messages.length === 0 && !hasNeed && debateStatus === 'idle' && (
          <div className="h-full flex flex-col items-center justify-center text-center">
            <img src="/group_3_line.png" alt="" className="h-9 w-auto mb-3 opacity-80" />
            <p className="text-sm font-medium mb-1">{text('debate.fourRoles')}</p>
            <p className="text-xs text-muted leading-relaxed max-w-[260px] mb-4">
              {text('debate.fourRolesDesc')}
            </p>
            <div className="flex flex-wrap items-center justify-center gap-4">
              {[
                localizeRoleName('director', roleNames.director, language),
                localizeRoleName('analyst', roleNames.analyst, language),
                localizeRoleName('critic', roleNames.critic, language),
                localizeRoleName('investor', roleNames.investor, language),
              ].map((name) => (
                <div key={name} className="border border-border/60 rounded-xl px-5 py-2.5 flex items-center justify-center">
                  <span className="text-xs text-muted font-medium leading-none">{name}</span>
                </div>
              ))}
            </div>
          </div>
        )}

        {messages.length === 0 && hasNeed && debateStatus === 'idle' && (
          <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }}
            className="h-full flex flex-col items-center justify-center text-center">
            <p className="text-sm font-medium mb-1">{text('debate.ready')}</p>
            <p className="text-xs text-muted">{text('debate.readyDesc')}</p>
            <p className="text-[11px] text-muted/60 mt-1">{text('debate.needDetailPanelHint')}</p>
          </motion.div>
        )}

        <div className="space-y-4">
          {messages.map((msg) => (
            <ChatMessageComponent
              key={msg.id}
              message={msg}
              roleNames={roleNames}
            />
          ))}

          {/* Inline error */}
          {errorMessage && (
            <motion.div
              initial={{ opacity: 0, y: 6 }}
              animate={{ opacity: 1, y: 0 }}
              className="flex items-start gap-2.5 bg-red-50 border border-red-200 rounded-xl p-3"
            >
              <XCircle size={14} className="text-red-500 mt-0.5 shrink-0" />
              <div className="flex-1 min-w-0">
                <p className="text-xs font-medium text-red-700 mb-0.5">{text('debate.errorTitle')}</p>
                <p className="text-[11px] text-red-600 break-all">{errorMessage}</p>
              </div>
              <button onClick={() => setErrorMessage(null)}
                className="text-[11px] text-red-500 hover:text-red-700 shrink-0">
                {text('common.close')}
              </button>
            </motion.div>
          )}

          {debateStatus === 'generating_report' && (
            <div className="flex flex-col items-center gap-2 text-muted text-xs py-4">
              <div className="flex items-center gap-1.5">
                <img src="/apple_intelligence_line.png" alt="" className="w-3.5 h-3.5" />
                <ShimmerText className="text-xs" shimmerColor="rgba(44,44,44,0.3)" duration={2}>
                  {reportMsg || text('debate.generatingReport')}
                </ShimmerText>
              </div>
              {reportProgress > 0 && (
                <div className="w-48 h-1 bg-border/30 rounded-full overflow-hidden">
                  <div className="h-full bg-accent/60 rounded-full transition-all duration-500"
                    style={{ width: `${reportProgress}%` }} />
                </div>
              )}
            </div>
          )}
        </div>
        <div ref={bottomRef} />
      </div>

      <AnimatePresence>
        {showScrollBtn && (
          <motion.button
            initial={{ opacity: 0, scale: 0.8 }}
            animate={{ opacity: 1, scale: 1 }}
            exit={{ opacity: 0, scale: 0.8 }}
            transition={{ duration: 0.15 }}
            onClick={() => {
              scrollMessagesToBottom('smooth')
              userScrolledUpRef.current = false
              setShowScrollBtn(false)
            }}
            className="absolute bottom-3 right-5 z-10 flex items-center gap-1 bg-white/90 backdrop-blur border border-border/40 shadow-sm rounded-full h-7 px-2.5 text-[11px] text-muted hover:text-text hover:shadow transition-all"
          >
            <ArrowDown size={11} />
            <span>{text('debate.latest')}</span>
          </motion.button>
        )}
      </AnimatePresence>
      </div>{/* end relative wrapper */}

      {/* Input bar — 加入讨论功能暂时禁用 */}
      <div className="mx-4 h-[1px] bg-border/30" />
      <div className="shrink-0 px-4 py-2.5">
        <div className="flex gap-2 items-center">
          <div className="relative flex-1">
            <input
              type="text"
              disabled
              placeholder={text('debate.inputSoon')}
              className="w-full rounded-xl border border-border/50 bg-bg h-10 px-4 text-[13px] placeholder:text-accent/50 focus:outline-none disabled:opacity-60 disabled:cursor-not-allowed transition-all"
            />
          </div>
          <button
            disabled
            className="bg-accent text-white rounded-xl h-10 w-10 flex items-center justify-center disabled:opacity-20 shrink-0"
          >
            <Send size={14} />
          </button>
        </div>
      </div>

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

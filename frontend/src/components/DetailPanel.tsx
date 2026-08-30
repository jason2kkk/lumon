import { useMemo, useState, useCallback } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { X, ExternalLink, Loader2, Languages } from 'lucide-react'
import { useAppStore } from '../stores/app'
import { extractDetailText } from './ChatMessage'
import { translateText } from '../api/client'
import AnalysisCard from './AnalysisCard'
import ReportView from './ReportView'
import type { PostComment } from '../types'
import { useI18n } from '../i18n'
import { safeExternalUrl } from '../safeUrls'

function commentText(comment: string | PostComment): string {
  if (typeof comment === 'string') return comment
  return comment.body || comment.text || comment.body_zh || ''
}

function sanitizeJsonString(raw: string): string {
  let result = ''
  let inString = false
  let escape = false
  for (let i = 0; i < raw.length; i++) {
    const ch = raw[i]
    if (escape) { result += ch; escape = false; continue }
    if (ch === '\\') { result += ch; escape = true; continue }
    if (ch === '"') { inString = !inString; result += ch; continue }
    if (inString && ch === '\n') { result += '\\n'; continue }
    if (inString && ch === '\r') { result += '\\r'; continue }
    if (inString && ch === '\t') { result += '\\t'; continue }
    result += ch
  }
  return result
}

function extractJson(text: string): Record<string, unknown> | null {
  if (!text) return null
  try { return JSON.parse(text.trim()) } catch { /* */ }
  const m = text.match(/```(?:json)?\s*\n?([\s\S]*?)\n?\s*```/)
  if (m) { try { return JSON.parse(m[1].trim()) } catch { /* */ } }
  const first = text.indexOf('{')
  const last = text.lastIndexOf('}')
  if (first !== -1 && last > first) {
    const slice = text.slice(first, last + 1)
    try { return JSON.parse(slice) } catch { /* */ }
    try { return JSON.parse(sanitizeJsonString(slice)) } catch { /* */ }
  }
  try { return JSON.parse(sanitizeJsonString(text.trim())) } catch { /* */ }
  return null
}

function NeedDetail() {
  const { text, isEnglish } = useI18n()
  const { needs, selectedNeedIndex } = useAppStore()
  const [translations, setTranslations] = useState<Record<string, string>>({})
  const [translating, setTranslating] = useState<string | null>(null)

  const handleTranslate = useCallback(async (key: string, sourceText: string) => {
    if (translations[key] || translating) return
    setTranslating(key)
    try {
      const res = await translateText(sourceText)
      setTranslations(prev => ({ ...prev, [key]: res.translation }))
    } catch {
      setTranslations(prev => ({ ...prev, [key]: text('detail.translationFailed') }))
    } finally {
      setTranslating(null)
    }
  }, [text, translations, translating])

  if (selectedNeedIndex === null || selectedNeedIndex >= needs.length) return null
  const need = needs[selectedNeedIndex]
  const needTitle = isEnglish && need.need_title_en?.trim() ? need.need_title_en.trim() : need.need_title
  const needDescription = isEnglish && need.need_description_en?.trim() ? need.need_description_en.trim() : need.need_description

  return (
    <div className="space-y-5 min-w-0">
      <div className="min-w-0">
        <div className="flex items-center gap-2 mb-2">
          <div className="w-7 h-7 bg-accent/8 rounded-xl flex items-center justify-center shrink-0">
            <img src="/blockquote_line.png" alt="" className="w-3.5 h-3.5 opacity-70" />
          </div>
          <h3 className="font-semibold text-sm leading-snug min-w-0 break-words">{needTitle}</h3>
        </div>
        <p className="text-xs text-muted leading-relaxed break-words">{needDescription}</p>
      </div>

      <div className="flex gap-3 text-xs text-muted flex-wrap">
        <span>{need.posts.length} {text('detail.relatedPosts')}</span>
        <span>{text('detail.totalScore')} {need.total_score ?? 0}</span>
        <span>{text('detail.totalComments')} {need.total_comments ?? 0}</span>
      </div>

      {need.posts.map((post, pi) => {
        const contentKey = `content-${pi}`
        const hasContentTranslation = !!translations[contentKey]

        return (
          <div key={pi} className="border border-border/40 rounded-xl overflow-hidden min-w-0">
            <div className="px-3.5 py-3 bg-bg/50">
              <p className="text-[13px] font-medium leading-snug mb-0.5 break-words">{post.title}</p>
              {post.title_zh && (
                <p className="text-xs text-accent/80 leading-snug break-words">{post.title_zh}</p>
              )}
              <div className="flex items-center gap-3 mt-1.5 text-[11px] text-muted flex-wrap">
                <span>▲ {post.score}</span>
                <span>💬 {post.num_comments}</span>
                {post.url && (
                  <a href={safeExternalUrl(post.url)} target="_blank" rel="noopener noreferrer"
                    className="flex items-center gap-0.5 text-accent hover:underline">
                    <ExternalLink size={10} /> {text('detail.originalPost')}
                  </a>
                )}
              </div>
            </div>

            {post.content && (
              <div className="mx-3.5 h-[1px] bg-border/25" />
            )}
            {post.content && (
              <div className="px-3.5 py-2.5">
                <div className="flex items-center justify-between mb-1.5">
                  <p className="text-[10px] font-semibold text-muted uppercase tracking-wide">{text('detail.content')}</p>
                  {!hasContentTranslation && (
                    <button
                      onClick={() => handleTranslate(contentKey, post.content)}
                      disabled={translating === contentKey}
                      className="flex items-center gap-1 text-[10px] text-accent hover:underline disabled:opacity-50"
                    >
                      {translating === contentKey
                        ? <><Loader2 size={10} className="animate-spin" /> {text('detail.translating')}</>
                        : <><Languages size={10} /> {text('detail.translate')}</>
                      }
                    </button>
                  )}
                </div>
                <div className="text-xs leading-relaxed whitespace-pre-wrap break-words text-text/80 max-h-40 overflow-y-auto">
                  {post.content.slice(0, 600)}{post.content.length > 600 ? '...' : ''}
                </div>
                {hasContentTranslation && (
                  <div className="mt-2 pt-2 text-xs leading-relaxed whitespace-pre-wrap break-words text-accent/80 max-h-40 overflow-y-auto">
                    {translations[contentKey]}
                  </div>
                )}
              </div>
            )}

            {(post.comments?.length ?? 0) > 0 && (
              <div className="mx-3.5 h-[1px] bg-border/25" />
            )}
            {(post.comments?.length ?? 0) > 0 && (
              <div className="px-3.5 py-2.5">
                <p className="text-[10px] font-semibold text-muted uppercase tracking-wide mb-1.5">
                  {text('detail.selectedComments')} ({post.comments!.length})
                </p>
                <div className="space-y-1 max-h-48 overflow-y-auto min-w-0">
                  {post.comments!.slice(0, 5).map((c, ci) => {
                    const text = commentText(c)
                    return (
                      <div key={ci} className="text-[11px] text-text/70 bg-bg rounded-xl p-2 leading-relaxed whitespace-pre-wrap break-words">
                        {text.slice(0, 300)}{text.length > 300 ? '...' : ''}
                      </div>
                    )
                  })}
                </div>
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}

function AnalysisDetail() {
  const { text } = useI18n()
  const rn = useAppStore((s) => s.roleNames)
  const debateStatus = useAppStore((s) => s.debateStatus)
  const pmMsg = useAppStore((s) => s.messages.find((m) => m.role === 'analyst' && m.content.includes('<think>')))

  const detailContent = useMemo(
    () => (pmMsg ? extractDetailText(pmMsg.content) : ''),
    [pmMsg?.content], // eslint-disable-line react-hooks/exhaustive-deps
  )
  const jsonData = useMemo(() => extractJson(detailContent), [detailContent])
  const isThinking = pmMsg ? pmMsg.content.includes('<think>') && !pmMsg.content.includes('</think>') : false

  const modelIcon = pmMsg?.provider === 'gpt' ? '/openai_line.png' : '/claude_line.png'
  const pmLabel = rn.analyst || text('detail.productManager')

  if (!pmMsg) {
    const isDebating = debateStatus === 'debating'
    return (
      <div className="flex flex-col items-center justify-center py-16 text-center">
        {isDebating ? (
          <>
            <Loader2 size={20} className="animate-spin text-muted mb-3" />
            <p className="text-sm text-muted">{text('detail.analyzingNeed')}</p>
          </>
        ) : (
          <>
            <img src="/head_ai_line.png" alt="" className="w-8 h-8 opacity-30 mb-3" />
            <p className="text-sm text-muted">{text('detail.analysisAfterDebate')}</p>
          </>
        )}
      </div>
    )
  }

  if (isThinking && !detailContent) {
    return (
      <div className="space-y-4">
        <div className="flex items-center gap-2">
          <img src={modelIcon} alt="" className="w-5 h-5 opacity-70" />
          <span className="text-xs font-semibold text-text/70">{pmLabel} · {text('detail.analysisReport')}</span>
          <span className="flex items-center gap-1 text-[10px] text-accent/70 bg-accent/8 px-2 py-0.5 rounded-full font-medium">
            <img src="/apple_intelligence_line.png" alt="" className="w-3 h-3 animate-[spin_3s_linear_infinite]" />
            {text('detail.analyzing')}
          </span>
        </div>
        <div className="text-center py-4">
          <Loader2 size={16} className="animate-spin text-muted mx-auto" />
          <p className="text-xs text-muted mt-2">{text('detail.deepAnalyzing')}</p>
        </div>
      </div>
    )
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        <img src={modelIcon} alt="" className="w-5 h-5 opacity-70" />
        <span className="text-xs font-semibold text-text/70">{pmLabel} · {text('detail.analysisReport')}</span>
        {isThinking && (
          <span className="flex items-center gap-1 text-[10px] text-accent/70 bg-accent/8 px-2 py-0.5 rounded-full font-medium">
            <img src="/apple_intelligence_line.png" alt="" className="w-3 h-3 animate-[spin_3s_linear_infinite]" />
            {text('detail.analyzing')}
          </span>
        )}
      </div>

      {jsonData ? (
        <AnalysisCard data={jsonData} />
      ) : detailContent ? (
        <div className="text-[13px] leading-[1.7] whitespace-pre-wrap break-words text-text/80">
          {detailContent.replace(/\n{3,}/g, '\n\n')}
          {isThinking && (
            <span className="inline-block w-1.5 h-4 bg-accent/40 ml-0.5 animate-pulse rounded-sm align-middle" />
          )}
        </div>
      ) : null}
    </div>
  )
}

function ReportDetail() {
  const { text } = useI18n()
  const { finalReport } = useAppStore()
  if (!finalReport) return <p className="text-muted text-sm">{text('detail.noReport')}</p>
  return <ReportView report={finalReport} />
}

function EmptyDetail() {
  const { text } = useI18n()
  return (
    <div className="flex items-center justify-center px-4" style={{ minHeight: 'calc(100vh - 120px)' }}>
      <div className="text-center">
        <p className="text-sm font-medium mb-1">{text('detail.panel')}</p>
        <p className="text-xs text-muted leading-relaxed">
          {text('detail.panelDesc')}
        </p>
      </div>
    </div>
  )
}

export default function DetailPanel() {
  const { text } = useI18n()
  const { detailView, setDetailView } = useAppStore()

  const isTabView = detailView.type === 'post' || detailView.type === 'analysis'

  const title =
    detailView.type === 'report' ? text('detail.finalReport') : ''

  return (
    <div className="flex flex-col h-full min-w-0">
      {detailView.type !== 'empty' && (
        <>
          <div className="shrink-0 h-[49px] flex items-center justify-between px-4 min-w-0">
            {isTabView ? (
              <div className="flex items-center gap-4 min-w-0">
                <button
                  onClick={() => setDetailView({ type: 'post' })}
                  className={`text-sm font-semibold transition-colors ${
                    detailView.type === 'post' ? 'text-text' : 'text-muted hover:text-text/70'
                  }`}
                >
                  {text('detail.needDetail')}
                </button>
                <button
                  onClick={() => setDetailView({ type: 'analysis' })}
                  className={`text-sm font-semibold transition-colors ${
                    detailView.type === 'analysis' ? 'text-text' : 'text-muted hover:text-text/70'
                  }`}
                >
                  {text('detail.needAnalysis')}
                </button>
              </div>
            ) : (
              <span className="font-semibold text-sm">{title}</span>
            )}
            <button onClick={() => setDetailView({ type: 'empty' })}
              className="w-6 h-6 rounded-xl flex items-center justify-center text-muted hover:text-text hover:bg-bg transition-colors">
              <X size={14} />
            </button>
          </div>
          <div className="mx-4 h-[1px] bg-border/30" />
        </>
      )}

      <div className="flex-1 min-w-0 overflow-y-auto scrollbar-auto p-4">
        <AnimatePresence mode="wait">
          <motion.div
            key={detailView.type}
            initial={{ opacity: 0, x: 12 }}
            animate={{ opacity: 1, x: 0 }}
            exit={{ opacity: 0, x: -12 }}
            transition={{ duration: 0.15 }}
            className="min-w-0"
          >
            {detailView.type === 'empty' && <EmptyDetail />}
            {detailView.type === 'post' && <NeedDetail />}
            {detailView.type === 'analysis' && <AnalysisDetail />}
            {detailView.type === 'report' && <ReportDetail />}
          </motion.div>
        </AnimatePresence>
      </div>
    </div>
  )
}

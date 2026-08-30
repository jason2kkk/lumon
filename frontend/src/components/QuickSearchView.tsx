import { useState, useRef, useCallback, useEffect, useMemo, type ReactNode, type PointerEvent as ReactPointerEvent } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import {
  Loader2, ExternalLink, MessageSquare,
  TrendingUp, Clock, X, ArrowUp, ChevronDown, ChevronUp,
  ChevronRight, ArrowLeft,
} from 'lucide-react'
import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import {
  trackAnalyticsEvent,
  translateQuickSearchReviews,
  type QuickSearchAppReview,
  type QuickSearchHistoryItem,
  type QuickSearchMarketApp,
  type QuickSearchReviewDistributionGroup,
  type QuickSearchMarketSeries,
  type QuickSearchMarketSignal,
  type QuickSearchMarketTrendRow,
  type QuickSearchPost,
} from '../api/client'
import SplitText from './SplitText'
import { useAppStore } from '../stores/app'
import { LocalizedText, translate, useI18n } from '../i18n'
import {
  useQuickSearchStore,
  type QuickSearchMarketTimePeriod,
  type QuickSearchMinScore,
  type QuickSearchTimePeriod,
} from '../stores/quickSearch'
import { safeExternalUrl } from '../safeUrls'

type TimePeriod = QuickSearchTimePeriod
const TIME_OPTIONS: { id: TimePeriod; label: string; labelEn: string }[] = [
  { id: 'week', label: '近一周', labelEn: 'Past week' },
  { id: 'month', label: '近一月', labelEn: 'Past month' },
  { id: '3months', label: '近三月', labelEn: 'Past 3 months' },
  { id: '6months', label: '近半年', labelEn: 'Past 6 months' },
]

type MinScore = QuickSearchMinScore
const HEAT_OPTIONS: { id: MinScore; label: string; labelEn: string }[] = [
  { id: 0, label: '不限热度', labelEn: 'Any score' },
  { id: 10, label: '10+ 赞', labelEn: '10+ upvotes' },
  { id: 25, label: '25+ 赞', labelEn: '25+ upvotes' },
  { id: 50, label: '50+ 赞', labelEn: '50+ upvotes' },
  { id: 100, label: '100+ 赞', labelEn: '100+ upvotes' },
]

type MarketTimePeriod = QuickSearchMarketTimePeriod
const MARKET_TIME_OPTIONS: { id: MarketTimePeriod; label: string; desc: string; labelEn: string; descEn: string }[] = [
  { id: '30d', label: '过去 30 天', desc: '最近一段完整窗口', labelEn: 'Past 30 days', descEn: 'Most recent complete window' },
  { id: '6months', label: '过去 6 个月', desc: '更稳定的中期规模', labelEn: 'Past 6 months', descEn: 'More stable mid-term scale' },
  { id: 'all_time', label: '全部累计', desc: '历史累计规模', labelEn: 'All time', descEn: 'Historical cumulative scale' },
]

function i18nText(key: string): string {
  return String(translate(useAppStore.getState().languageMode, key))
}

function i18nFormat(key: string, params: Record<string, string | number>): string {
  return i18nText(key).replace(/\{(\w+)\}/g, (_, name: string) => String(params[name] ?? `{${name}}`))
}

type QuickSearchHotspot = {
  title: string
  signal: string
  evidenceIndexes: number[]
}

type StructuredQuickSearchSummary = {
  conclusion: string
  hotspots: QuickSearchHotspot[]
  supplement: string
  isWorkflow: boolean
}


function formatHistoryTime(timestamp: number): string {
  if (!timestamp) return ''
  const diff = Date.now() - timestamp
  if (diff < 60_000) return i18nText('quickSearch.justNow')
  if (diff < 3_600_000) return i18nFormat('quickSearch.minuteAgo', { count: Math.floor(diff / 60_000) })
  if (diff < 86_400_000) return i18nFormat('quickSearch.hourAgo', { count: Math.floor(diff / 3_600_000) })
  return i18nFormat('quickSearch.dayAgo', { count: Math.floor(diff / 86_400_000) })
}

function splitQuickSearchSummary(text: string): { main: string; supplement: string } {
  const source = text.trim()
  if (!source) return { main: '', supplement: '' }
  const markers = ['\n## 分歧与风险', '\n## 数据局限', '\n## Disagreements and Risks', '\n## Data Limitations']
  const indices = markers
    .map((marker) => source.indexOf(marker))
    .filter((index) => index > 0)
  if (indices.length === 0) return { main: source, supplement: '' }
  const splitAt = Math.min(...indices)
  return {
    main: source.slice(0, splitAt).trim(),
    supplement: source.slice(splitAt).trim(),
  }
}

function cleanMarkdownLine(line: string): string {
  return line
    .replace(/^\s*[-*]\s*/, '')
    .replace(/^#+\s*/, '')
    .replace(/\*\*/g, '')
    .trim()
}

function sectionFromMarkdown(text: string, heading: string | string[]): string {
  const source = text.trim()
  if (!source) return ''
  const headings = Array.isArray(heading) ? heading : [heading]
  const escaped = headings.map((item) => item.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|')
  const pattern = new RegExp(`(^|\\n)##\\s*(?:${escaped})\\s*\\n`, 'i')
  const match = source.match(pattern)
  if (!match || match.index === undefined) return ''
  const start = match.index + match[0].length
  const rest = source.slice(start)
  const next = rest.search(/\n##\s+/)
  return (next >= 0 ? rest.slice(0, next) : rest).trim()
}

function parseEvidenceIndexes(line: string): number[] {
  const indexes: number[] = []
  const normalized = toAsciiDigits(line)
  const matcher = /(?:帖子|Post)\s*([0-9]+)/gi
  let match: RegExpExecArray | null
  while ((match = matcher.exec(normalized)) !== null) {
    const index = Number(match[1]) - 1
    if (Number.isInteger(index) && index >= 0) indexes.push(index)
  }
  return Array.from(new Set(indexes))
}

function parseQuickSearchSummary(text: string, posts: QuickSearchPost[]): StructuredQuickSearchSummary | null {
  const conclusionSection = sectionFromMarkdown(text, ['结论', 'Conclusion'])
  const workflowSection = sectionFromMarkdown(text, ['流程阶段', 'Workflow Stages'])
  const discussionSection = workflowSection || sectionFromMarkdown(text, ['讨论热点', 'Discussion Hotspots', 'Discussion Hotspots / Themes'])
  if (!conclusionSection || !discussionSection) return null

  const conclusion = conclusionSection
    .split('\n')
    .map(cleanMarkdownLine)
    .find(Boolean) || ''

  const hotspots: QuickSearchHotspot[] = []
  let current: QuickSearchHotspot | null = null

  for (const line of discussionSection.split('\n')) {
    const titleMatch = line.match(/^\s*###\s+(.+?)\s*$/)
    if (titleMatch) {
      if (current && (current.signal || current.evidenceIndexes.length > 0)) {
        hotspots.push(current)
      }
      current = {
        title: cleanMarkdownLine(titleMatch[1]),
        signal: '',
        evidenceIndexes: [],
      }
      continue
    }
    if (!current) continue

    const signalMatch = line.match(/^\s*[-*]?\s*(?:信号|Signal|工作|Work)\s*[:：]\s*(.+?)\s*$/i)
    if (signalMatch) {
      current.signal = cleanMarkdownLine(signalMatch[1])
      continue
    }

    if (isEvidenceLine(line)) {
      current.evidenceIndexes = Array.from(new Set([
        ...current.evidenceIndexes,
        ...parseEvidenceIndexes(line).filter((index) => posts[index]),
      ]))
    }
  }

  if (current && (current.signal || current.evidenceIndexes.length > 0)) {
    hotspots.push(current)
  }

  const isEnglish = useAppStore.getState().languageMode === 'en-US'
  const supplementHeadings: Array<[string, string[]]> = [
    [isEnglish ? 'Opportunity Leads' : '机会线索', ['机会线索', 'Opportunity Leads']],
    [isEnglish ? 'Roles and Tools' : '角色与工具', ['角色与工具', 'Roles and Tools']],
    [isEnglish ? 'Disagreements and Risks' : '分歧与风险', ['分歧与风险', 'Disagreements and Risks', 'Risks']],
    [isEnglish ? 'Evidence Limits' : '证据边界', ['证据边界', 'Evidence Limits']],
    [isEnglish ? 'Data Limitations' : '数据局限', ['数据局限', 'Data Limitations']],
  ]
  const supplement = supplementHeadings
    .map(([displayHeading, headings]) => {
      const body = sectionFromMarkdown(text, headings)
      return body ? `## ${displayHeading}\n${body}` : ''
    })
    .filter(Boolean)
    .join('\n\n')

  if (!conclusion || hotspots.length === 0) return null
  return { conclusion, hotspots: hotspots.slice(0, 5), supplement, isWorkflow: Boolean(workflowSection) }
}

function withSentenceEnd(text: string): string {
  const source = text.trim()
  if (!source) return ''
  if (/[。！？!?.]$/.test(source)) return source
  return useAppStore.getState().languageMode === 'en-US' ? `${source}.` : `${source}。`
}

function normalizeConclusionForDisplay(conclusion: string): string {
  let source = cleanMarkdownLine(conclusion)
  if (!source) return ''

  if (useAppStore.getState().languageMode === 'en-US') {
    return withSentenceEnd(source)
  }

  const leadingInsufficient = source.match(/^((?:当前)?数据不足以判断[^，,。！？!?；;]*)(?:[，,；;]\s*(?:但|不过|但是)?\s*)?(.+)$/)
  if (leadingInsufficient?.[2]?.trim()) {
    source = `${leadingInsufficient[2].trim()}，但不足以做全站排名判断。`
  }

  source = source
    .replace(/(?:在|从|基于|根据)?(?:这|这次|本次|当前)?\s*(?:\d+|[一二三四五六七八九十]+)\s*条\s*(?:帖子|结果|样本|数据)?(?:中|里|来看|看)?[，,：:]?/g, '从当前命中的社区讨论看，')
    .replace(/(?:这|这次|本次|当前)?\s*(?:\d+|[一二三四五六七八九十]+)\s*条\s*(?:帖子|结果|样本|数据)/g, '当前命中的社区讨论')
    .replace(/从当前命中的社区讨论看，\s*从当前命中的社区讨论看，/g, '从当前命中的社区讨论看，')

  return withSentenceEnd(source)
}

function bestPostEvidence(post: QuickSearchPost): {
  kind: string
  en: string
  zh: string
  score?: number
} | null {
  const actionPattern = /brainstorm|draft|outline|write|revise|rewrite|feedback|review|critique|proofread|counselor|teacher|parent|mentor|submit|submission|deadline|track|calendar|spreadsheet|Common App|UCAS|personal statement|选题|初稿|修改|反馈|顾问|老师|家长|提交|截止|流程|步骤/i
  const comments = [...(post.comments || [])]
    .filter((comment) => (comment.body || comment.body_zh || '').trim())
    .filter((comment) => !post.process_actions?.length || actionPattern.test(`${comment.body} ${comment.body_zh || ''}`))
    .sort((a, b) => (b.score || 0) - (a.score || 0))
  const comment = comments[0]
  if (comment) {
    return {
      kind: i18nText('quickSearch.commentEvidence'),
      en: comment.body || '',
      zh: comment.body_zh || '',
      score: comment.score,
    }
  }
  if ((post.content || post.content_zh || '').trim()) {
    return {
      kind: i18nText('quickSearch.postEvidence'),
      en: post.content || '',
      zh: post.content_zh || '',
    }
  }
  return null
}

function toAsciiDigits(value: string): string {
  return value.replace(/[０-９]/g, (char) => String(char.charCodeAt(0) - 0xff10))
}

function evidencePostIndexFromLine(line: string): number | null {
  const match = line.match(/^\s*[-*]?\s*(?:证据(?:帖)?|Evidence)\s*[:：]\s*(?:(?:帖子|Post)\s*)?([0-9０-９]+)/i)
  if (!match) return null
  const index = Number(toAsciiDigits(match[1])) - 1
  return Number.isInteger(index) ? index : null
}

function isEvidenceLine(line: string): boolean {
  return /^\s*[-*]?\s*(?:证据(?:帖)?|Evidence)\s*[:：]/i.test(line)
}

function fallbackEvidenceIndex(posts: QuickSearchPost[], used: Set<number>): number | null {
  const index = posts.findIndex((post, i) => !used.has(i) && bestPostEvidence(post))
  return index >= 0 ? index : null
}

function BilingualBlock({
  en,
  zh,
  enClass = 'text-sm font-medium text-text/90 leading-snug',
  zhClass = 'text-xs text-accent/80 leading-relaxed mt-0.5',
  lineClamp,
}: {
  en?: string
  zh?: string
  enClass?: string
  zhClass?: string
  lineClamp?: boolean
}) {
  if (!en && !zh) return null
  const clamp = lineClamp ? 'line-clamp-2' : ''
  return (
    <motion.div layout={false}>
      {en && <p className={`${enClass} ${clamp}`}>{en}</p>}
      {zh && zh !== en && (
        <p className={`${zhClass} ${lineClamp ? 'line-clamp-2' : ''}`}>{zh}</p>
      )}
    </motion.div>
  )
}

function formatPostScale(posts: QuickSearchPost[]): string {
  const validPosts = posts.filter(Boolean)
  if (validPosts.length === 0) return i18nText('quickSearch.noEvidence')
  const score = validPosts.reduce((sum, post) => sum + (post.score || 0), 0)
  const comments = validPosts.reduce((sum, post) => sum + (post.num_comments || 0), 0)
  return i18nFormat('quickSearch.evidenceScale', { count: validPosts.length, score, comments })
}

function formatPrimarySubreddits(posts: QuickSearchPost[]): string {
  const counts = new Map<string, number>()
  posts.forEach((post) => {
    const source = (post.source || '').trim()
    if (!source) return
    counts.set(source, (counts.get(source) || 0) + 1)
  })
  const names = Array.from(counts.entries())
    .sort((a, b) => b[1] - a[1])
    .slice(0, 2)
    .map(([source]) => source)
  return names.length > 0 ? names.join(' / ') : 'Reddit'
}

function usedHotspotEvidenceIndexes(structured: StructuredQuickSearchSummary | null): Set<number> {
  const used = new Set<number>()
  structured?.hotspots.forEach((hotspot) => {
    hotspot.evidenceIndexes.forEach((index) => used.add(index))
  })
  return used
}

const EXTRA_EVIDENCE_STOPWORDS = new Set([
  'this', 'that', 'with', 'from', 'have', 'using', 'about', 'anyone', 'else',
  'best', 'better', 'share', 'subscription', 'people', 'community', 'help',
])

function evidenceTokens(text: string): Set<string> {
  return new Set(
    (text || '')
      .toLowerCase()
      .match(/[a-z][a-z0-9-]{3,}/g)
      ?.filter((token) => !EXTRA_EVIDENCE_STOPWORDS.has(token)) || []
  )
}

function moreEvidencePosts(posts: QuickSearchPost[], structured: StructuredQuickSearchSummary | null): QuickSearchPost[] {
  const used = usedHotspotEvidenceIndexes(structured)
  const usedPosts = posts.filter((_, index) => used.has(index))
  const sourceSet = new Set(usedPosts.map((post) => post.source).filter(Boolean))
  const anchorTokens = new Set<string>()
  usedPosts.forEach((post) => {
    evidenceTokens(`${post.title} ${post.content}`).forEach((token) => anchorTokens.add(token))
  })

  return posts.filter((post, index) => {
    if (used.has(index)) return false
    if (!(post.title || '').trim() || !post.url) return false
    if (!structured || sourceSet.size === 0 || anchorTokens.size === 0) return true
    if (!sourceSet.has(post.source)) return false
    const tokens = evidenceTokens(`${post.title} ${post.content}`)
    const overlap = Array.from(tokens).filter((token) => anchorTokens.has(token)).length
    return overlap >= 1
  })
}

function resultScopeText(totalSearched: number | null, keptCount: number): string {
  if (typeof totalSearched === 'number' && totalSearched > 0) {
    return i18nFormat('quickSearch.searchedScope', { total: totalSearched, kept: keptCount })
  }
  if (keptCount > 0) {
    return i18nFormat('quickSearch.keptScope', { kept: keptCount })
  }
  return ''
}

function timeAgo(utc: number): string {
  if (!utc) return ''
  const diff = Date.now() / 1000 - utc
  if (diff < 3600) return i18nFormat('quickSearch.minuteAgo', { count: Math.floor(diff / 60) })
  if (diff < 86400) return i18nFormat('quickSearch.hourAgo', { count: Math.floor(diff / 3600) })
  if (diff < 604800) return i18nFormat('quickSearch.dayAgo', { count: Math.floor(diff / 86400) })
  return i18nFormat('quickSearch.weekAgo', { count: Math.floor(diff / 604800) })
}

function PostCard({ post, index }: { post: QuickSearchPost; index: number }) {
  const [expanded, setExpanded] = useState(false)
  const { text } = useI18n()
  const hasComments = post.comments && post.comments.length > 0

  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: index * 0.03, duration: 0.2 }}
      className="quick-search-post-card group"
    >
      <div className="flex items-start gap-3">
        <div className="quick-search-post-score">
          <ArrowUp size={12} className="text-accent/60" />
          <span className="text-xs font-semibold text-accent tabular-nums">{post.score}</span>
        </div>
        <div className="flex-1 min-w-0">
          <motion.div className="flex items-start gap-1.5">
            <a
              href={safeExternalUrl(post.url)}
              target="_blank"
              rel="noopener noreferrer"
              className="flex-1 min-w-0 hover:opacity-90 transition-opacity"
            >
              <BilingualBlock en={post.title} zh={post.title_zh} lineClamp />
            </a>
            <a
              href={safeExternalUrl(post.url)}
              target="_blank"
              rel="noopener noreferrer"
              className="quick-search-post-open-button"
              aria-label={text('quickSearch.openOriginalPost')}
            >
              {text('quickSearch.openOriginalPost')}
              <ExternalLink size={12} />
            </a>
          </motion.div>

          {(post.content || post.content_zh) && (
            <motion.div className="mt-2 pt-2 border-t border-border/15">
              <p className="text-[10px] font-medium text-muted/50 mb-1">{text('quickSearch.body')}</p>
              <BilingualBlock
                en={post.content}
                zh={post.content_zh}
                enClass="text-xs text-text/75 leading-relaxed"
                zhClass="text-xs text-muted leading-relaxed mt-1"
                lineClamp={!expanded}
              />
            </motion.div>
          )}

          <div className="flex items-center gap-3 mt-2 text-[11px] text-muted/60">
            <span className="flex items-center gap-1">
              <MessageSquare size={10} />
              {post.num_comments}
            </span>
            <span>{post.source}</span>
            {post.created_utc > 0 && (
              <span className="flex items-center gap-1">
                <Clock size={10} />
                {timeAgo(post.created_utc)}
              </span>
            )}
            {hasComments && (
              <button
                onClick={() => setExpanded(!expanded)}
                className="flex items-center gap-0.5 text-accent/70 hover:text-accent transition-colors"
              >
                {expanded ? <ChevronUp size={10} /> : <ChevronDown size={10} />}
                {expanded ? text('quickSearch.collapseComments') : text('quickSearch.viewComments')}
              </button>
            )}
          </div>
        </div>
      </div>

      <AnimatePresence>
        {expanded && hasComments && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.2 }}
            className="overflow-hidden"
          >
            <div className="mt-3 ml-8 pl-3 border-l-2 border-accent/15 space-y-3">
              {post.comments.map((c, i) => (
                <motion.div key={i} className="bg-bg/50 rounded-lg p-2.5">
                  {c.score > 0 && (
                    <span className="text-[10px] text-accent/50 font-medium">[{c.score}] </span>
                  )}
                  <BilingualBlock
                    en={c.body}
                    zh={c.body_zh}
                    enClass="text-xs text-text/75 leading-relaxed"
                    zhClass="text-xs text-muted leading-relaxed mt-1"
                  />
                </motion.div>
              ))}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  )
}

function HotspotEvidenceCard({ post, index }: { post: QuickSearchPost; index: number }) {
  const { text } = useI18n()
  const evidence = bestPostEvidence(post)
  if (!evidence) return null
  const evidenceKind = evidence.kind.replace(text('quickSearch.evidenceOriginal'), '').trim()

  return (
    <motion.div
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: index * 0.04, duration: 0.2 }}
      className="quick-hotspot-evidence-card"
    >
      <div className="quick-hotspot-evidence-head">
        <div className="min-w-0">
          <p className="quick-hotspot-evidence-label">{text('quickSearch.originalPostTitle')}</p>
          <p className="quick-hotspot-evidence-title">{post.title}</p>
          {post.title_zh && post.title_zh !== post.title && (
            <p className="quick-hotspot-evidence-title-zh">{post.title_zh}</p>
          )}
          <p className="quick-hotspot-evidence-meta">
            {post.score} / {post.num_comments} · {post.source}
          </p>
        </div>
        <a
          href={safeExternalUrl(post.url)}
          target="_blank"
          rel="noopener noreferrer"
          className="quick-hotspot-evidence-link"
          aria-label={text('quickSearch.openOriginalPost')}
        >
          {text('quickSearch.openOriginalPost')}
          <ExternalLink size={11} />
        </a>
      </div>
      <div className="quick-hotspot-evidence-quote">
        <p className="quick-hotspot-evidence-kind">
          {text('quickSearch.evidenceOriginal')} · {evidenceKind || evidence.kind}
          {typeof evidence.score === 'number' && evidence.score > 0 && ` · ${evidence.score}`}
        </p>
        <BilingualBlock
          en={evidence.en}
          zh={evidence.zh}
          enClass="text-[12px] text-text/78 leading-relaxed line-clamp-3"
          zhClass="text-[12px] text-muted/72 leading-relaxed mt-1 line-clamp-2"
        />
      </div>
    </motion.div>
  )
}

function QuickSearchSummaryContent({ text, posts }: { text: string; posts: QuickSearchPost[] }) {
  const lines = text.split('\n')
  const blocks: ReactNode[] = []
  const usedEvidence = new Set<number>()
  let markdownBuffer: string[] = []
  let evidenceCount = 0

  const flushMarkdown = () => {
    const content = markdownBuffer.join('\n').trim()
    if (content) {
      blocks.push(
        <Markdown key={`md-${blocks.length}`} remarkPlugins={[remarkGfm]}>
          {content}
        </Markdown>
      )
    }
    markdownBuffer = []
  }

  lines.forEach((line) => {
    if (!isEvidenceLine(line)) {
      markdownBuffer.push(line)
      return
    }

    flushMarkdown()
    const parsedIndex = evidencePostIndexFromLine(line)
    const validIndex = parsedIndex !== null && posts[parsedIndex] && bestPostEvidence(posts[parsedIndex])
      ? parsedIndex
      : fallbackEvidenceIndex(posts, usedEvidence)

    if (validIndex === null) return

    usedEvidence.add(validIndex)
    blocks.push(
      <HotspotEvidenceCard
        key={`hotspot-evidence-${validIndex}-${evidenceCount}`}
        post={posts[validIndex]}
        index={evidenceCount}
      />
    )
    evidenceCount += 1
  })

  flushMarkdown()
  return <>{blocks}</>
}

function QuickSearchStructuredSummary({
  structured,
  posts,
  totalSearched,
  supplementExpanded,
  onToggleSupplement,
}: {
  structured: StructuredQuickSearchSummary
  posts: QuickSearchPost[]
  totalSearched: number | null
  supplementExpanded: boolean
  onToggleSupplement: () => void
}) {
  const { text } = useI18n()
  const conclusion = normalizeConclusionForDisplay(structured.conclusion)
  const effectiveKeptCount = typeof totalSearched === 'number' && totalSearched > 0
    ? posts.length
    : usedHotspotEvidenceIndexes(structured).size + moreEvidencePosts(posts, structured).length
  const scopeText = resultScopeText(totalSearched, effectiveKeptCount)

  return (
    <div className="quick-structured-result">
      <section className="quick-result-conclusion">
        <p className="quick-result-kicker">{text('quickSearch.conclusion')}</p>
        <p className="quick-result-main">
          {conclusion}
          {scopeText && <span className="quick-result-inline-note">{scopeText}</span>}
        </p>
      </section>

      <section className="quick-hotspots-section">
        <div className="quick-result-section-title">
          <TrendingUp size={14} />
          <span>{structured.isWorkflow ? text('quickSearch.workflowStages') : text('quickSearch.discussionHotspots')}</span>
        </div>
        <div className="quick-hotspot-list">
          {structured.hotspots.map((hotspot, index) => {
            const evidencePosts = hotspot.evidenceIndexes
              .map((postIndex) => posts[postIndex])
              .filter((post): post is QuickSearchPost => Boolean(post))
            const visibleEvidence = evidencePosts.slice(0, 2)
            return (
              <motion.article
                key={`${hotspot.title}-${index}`}
                className="quick-hotspot-card"
                initial={{ opacity: 0, y: 8 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: index * 0.04, duration: 0.2 }}
              >
                <div className="quick-hotspot-card-head">
                  <div className="min-w-0">
                    <p className="quick-hotspot-title">{hotspot.title}</p>
                    {hotspot.signal && <p className="quick-hotspot-signal">{hotspot.signal}</p>}
                  </div>
                  <span className="quick-hotspot-index">{index + 1}</span>
                </div>
                <div className="quick-hotspot-meta-row">
                  <span>{formatPostScale(evidencePosts)}</span>
                  <span>{formatPrimarySubreddits(evidencePosts)}</span>
                </div>
                {visibleEvidence.length > 0 && (
                  <div className="quick-hotspot-evidence-list">
                    {visibleEvidence.map((post, evidenceIndex) => (
                      <HotspotEvidenceCard
                        key={`${post.url}-${evidenceIndex}`}
                        post={post}
                        index={evidenceIndex}
                      />
                    ))}
                  </div>
                )}
              </motion.article>
            )
          })}
        </div>
      </section>

      {structured.supplement && (
        <section className="quick-search-supplement">
          <button
            type="button"
            className="quick-search-supplement-toggle"
            onClick={onToggleSupplement}
          >
            {supplementExpanded ? <ChevronUp size={13} /> : <ChevronDown size={13} />}
            {supplementExpanded ? text('quickSearch.collapseSupplement') : text('quickSearch.moreSupplement')}
          </button>
          <AnimatePresence initial={false}>
            {supplementExpanded && (
              <motion.div
                initial={{ height: 0, opacity: 0 }}
                animate={{ height: 'auto', opacity: 1 }}
                exit={{ height: 0, opacity: 0 }}
                transition={{ duration: 0.2, ease: 'easeOut' }}
                className="quick-search-supplement-body"
              >
                <div className="quick-search-analysis">
                  <Markdown remarkPlugins={[remarkGfm]}>{structured.supplement}</Markdown>
                </div>
              </motion.div>
            )}
          </AnimatePresence>
        </section>
      )}
    </div>
  )
}

function marketMetricHeader(dateText: string, metric: 'revenue' | 'downloads'): string {
  const period = dateText.includes('30') ? i18nText('quickSearch.past30Days') : dateText
  return i18nFormat(metric === 'revenue' ? 'quickSearch.globalRevenue' : 'quickSearch.globalDownloads', { period })
}

function sensorTowerSearchUrl(appName?: string): string {
  const text = String(appName || '').trim()
  return text ? `https://app.sensortower.com/search?search_term=${encodeURIComponent(text)}` : ''
}

function formatMarketScene(app: QuickSearchMarketApp, signal: QuickSearchMarketSignal): string {
  if (app.is_target_app) return i18nText('quickSearch.targetApp')
  const matched = (app.matched_queries || []).filter(Boolean)
  if (matched.length > 0) return matched.slice(0, 2).join(' / ')
  const query = (signal.queries || []).find(Boolean)
  return query || i18nText('quickSearch.relatedApp')
}

function marketPriorityText(apps: QuickSearchMarketApp[]): string {
  const top = apps[0]
  if (!top) return i18nText('quickSearch.priorityLow')
  const revenue = top.revenue || 0
  const downloads = top.downloads || 0
  if (revenue >= 1_000_000 || downloads >= 500_000) {
    return i18nText('quickSearch.priorityHigh')
  }
  if (revenue >= 100_000 || downloads >= 80_000) {
    return i18nText('quickSearch.priorityMedium')
  }
  return i18nText('quickSearch.priorityCautious')
}

function marketRedditSuggestion(apps: QuickSearchMarketApp[]): string {
  if (apps.length === 0) return i18nText('quickSearch.continueRedditEmpty')
  return i18nText('quickSearch.continueRedditText')
}

function formatTrendPercent(value?: number | null): string {
  if (typeof value !== 'number' || Number.isNaN(value)) return '-'
  return `${value > 0 ? '+' : ''}${value.toFixed(1)}%`
}

function trendPercentClass(value?: number | null): string {
  if (typeof value !== 'number' || Number.isNaN(value)) return ''
  if (value >= 30) return 'quick-market-trend-positive'
  if (value <= -30) return 'quick-market-trend-negative'
  return ''
}

function formatTrendPlatform(value?: string): string {
  const text = (value || '').toLowerCase()
  if (text === 'all') return 'All'
  if (text === 'ios') return 'iOS'
  if (text === 'android') return 'Android'
  return value || '-'
}

type TrendMetric = 'revenue' | 'downloads' | 'rpd'

const TREND_METRIC_META: Record<TrendMetric, { titleKey: string; labelKey: string }> = {
  revenue: { titleKey: 'quickSearch.revenueTrend', labelKey: 'quickSearch.revenue' },
  downloads: { titleKey: 'quickSearch.downloadsTrend', labelKey: 'quickSearch.downloads' },
  rpd: { titleKey: 'quickSearch.rpdTrend', labelKey: 'quickSearch.rpdTrend' },
}

function trendMetricsFromSignal(signal: QuickSearchMarketSignal): TrendMetric[] {
  const rawMetrics = signal.metrics || signal.time_series?.metrics || ['revenue', 'downloads', 'rpd']
  const metrics = rawMetrics.filter((metric): metric is TrendMetric => (
    metric === 'revenue' || metric === 'downloads' || metric === 'rpd'
  ))
  return metrics.length > 0 ? metrics : ['revenue']
}

function formatTrendMetricList(metrics: TrendMetric[]): string {
  return metrics.map((metric) => i18nText(TREND_METRIC_META[metric].labelKey)).join(useAppStore.getState().languageMode === 'en-US' ? ', ' : '、')
}

function compactChartValue(value: number, metric: 'revenue' | 'downloads' | 'rpd'): string {
  if (!Number.isFinite(value)) return '-'
  if (metric === 'rpd') return `$${value.toFixed(value >= 1 ? 2 : 3)}`
  if (metric === 'revenue') {
    if (value >= 1_000_000) return `$${(value / 1_000_000).toFixed(1)}M`
    if (value >= 1_000) return `$${Math.round(value / 1_000)}K`
    return `$${Math.round(value)}`
  }
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`
  if (value >= 1_000) return `${Math.round(value / 1_000)}K`
  return `${Math.round(value)}`
}

function formatChartTickLabel(dateValue: string, granularity?: string): string {
  const parts = String(dateValue || '').split('-').map((part) => Number(part))
  if (parts.length < 3 || parts.some((part) => Number.isNaN(part))) return dateValue
  const [, month, day] = parts
  const isEnglish = useAppStore.getState().languageMode === 'en-US'
  if (granularity === 'month') return isEnglish ? `M${month}` : `${month}月`
  if (granularity === 'day') return `${month}/${day}`
  return isEnglish ? `${month}/${day} wk` : `${month}/${day}周`
}

function seriesColor(index: number): string {
  return [
    '#2563eb',
    '#16a34a',
    '#dc2626',
    '#9333ea',
    '#ea580c',
    '#0891b2',
  ][index % 6]
}

function MarketTrendChart({
  title,
  metric,
  series,
  granularity,
}: {
  title: string
  metric: TrendMetric
  series: QuickSearchMarketSeries[]
  granularity?: string
}) {
  const { text } = useI18n()
  const [activeDate, setActiveDate] = useState<string | null>(null)
  const visibleSeries = series
    .map((item) => ({
      ...item,
      points: (item.points || [])
        .filter((point) => typeof point[metric] === 'number')
        .sort((a, b) => String(a.date).localeCompare(String(b.date))),
    }))
    .filter((item) => item.points.length > 0)
    .slice(0, 6)

  const dates = Array.from(new Set(visibleSeries.flatMap((item) => item.points.map((point) => point.date)))).sort()
  const values = visibleSeries.flatMap((item) => item.points.map((point) => Number(point[metric] || 0)))
  const maxValue = Math.max(...values, 0)
  const minValue = Math.min(...values, 0)
  const width = 900
  const height = 280
  const pad = { left: 68, right: 28, top: 24, bottom: 58 }
  const plotWidth = width - pad.left - pad.right
  const plotHeight = height - pad.top - pad.bottom
  const valueSpan = Math.max(1, maxValue - minValue)

  const xForDate = (dateValue: string) => {
    const index = dates.indexOf(dateValue)
    if (dates.length <= 1) return pad.left
    return pad.left + (index / (dates.length - 1)) * plotWidth
  }
  const yForValue = (value: number) => pad.top + (1 - (value - minValue) / valueSpan) * plotHeight
  const pathFor = (points: typeof visibleSeries[number]['points']) => points
    .map((point, index) => `${index === 0 ? 'M' : 'L'} ${xForDate(point.date).toFixed(1)} ${yForValue(Number(point[metric] || 0)).toFixed(1)}`)
    .join(' ')
  const firstDate = dates[0] || ''
  const lastDate = dates[dates.length - 1] || ''
  const tickStep = dates.length <= 10 ? 1 : Math.ceil(dates.length / 8)
  const tickDates = dates.filter((_, index) => index % tickStep === 0 || index === dates.length - 1)
  const activeIndex = activeDate ? dates.indexOf(activeDate) : -1
  const activeX = activeIndex >= 0 ? xForDate(activeDate || '') : null
  const activeRows = activeDate
    ? visibleSeries
      .map((item, index) => ({
        label: item.label || item.app || `Series ${index + 1}`,
        color: seriesColor(index),
        value: item.points.find((point) => point.date === activeDate)?.[metric],
      }))
      .filter((item) => typeof item.value === 'number')
    : []
  const tooltipLeft = activeX === null ? 50 : Math.min(92, Math.max(8, (activeX / width) * 100))

  const handlePointerMove = (event: ReactPointerEvent<SVGSVGElement>) => {
    if (dates.length === 0) return
    const rect = event.currentTarget.getBoundingClientRect()
    const svgX = ((event.clientX - rect.left) / rect.width) * width
    const ratio = Math.min(1, Math.max(0, (svgX - pad.left) / plotWidth))
    const index = Math.min(dates.length - 1, Math.max(0, Math.round(ratio * (dates.length - 1))))
    setActiveDate(dates[index])
  }

  if (visibleSeries.length === 0) return null

  return (
    <div className="quick-market-chart-card">
      <div className="quick-market-chart-head">
        <span>{title}</span>
        <small>{firstDate && lastDate ? `${firstDate} ~ ${lastDate}` : text('quickSearch.trend')}</small>
      </div>
      <div className="quick-market-chart-plot">
        <svg
          viewBox={`0 0 ${width} ${height}`}
          role="img"
          aria-label={title}
          onPointerMove={handlePointerMove}
          onPointerLeave={() => setActiveDate(null)}
        >
          <line x1={pad.left} y1={pad.top} x2={pad.left} y2={pad.top + plotHeight} className="quick-market-chart-axis" />
          <line x1={pad.left} y1={pad.top + plotHeight} x2={pad.left + plotWidth} y2={pad.top + plotHeight} className="quick-market-chart-axis" />
          {[0, 0.5, 1].map((ratio) => {
            const y = pad.top + plotHeight * ratio
            const value = maxValue - valueSpan * ratio
            return (
              <g key={ratio}>
                <line x1={pad.left} y1={y} x2={pad.left + plotWidth} y2={y} className="quick-market-chart-grid" />
                <text x={pad.left - 12} y={y + 4} textAnchor="end" className="quick-market-chart-label">
                  {compactChartValue(value, metric)}
                </text>
              </g>
            )
          })}
          {tickDates.map((dateValue) => {
            const x = xForDate(dateValue)
            return (
              <g key={dateValue}>
                <line x1={x} y1={pad.top + plotHeight} x2={x} y2={pad.top + plotHeight + 5} className="quick-market-chart-axis" />
                <text x={x} y={height - 18} textAnchor="middle" className="quick-market-chart-label">
                  {formatChartTickLabel(dateValue, granularity)}
                </text>
              </g>
            )
          })}
          {visibleSeries.map((item, index) => (
            <g key={item.key || item.label || index}>
              <path d={pathFor(item.points)} fill="none" stroke={seriesColor(index)} strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" />
              {item.points.map((point) => (
                <circle key={`${item.key}-${point.date}`} cx={xForDate(point.date)} cy={yForValue(Number(point[metric] || 0))} r="3.2" fill={seriesColor(index)} />
              ))}
            </g>
          ))}
          {activeX !== null && (
            <line x1={activeX} y1={pad.top} x2={activeX} y2={pad.top + plotHeight} className="quick-market-chart-crosshair" />
          )}
        </svg>
        {activeDate && activeRows.length > 0 && (
          <div className="quick-market-chart-tooltip" style={{ left: `${tooltipLeft}%` }}>
            <strong>{formatChartTickLabel(activeDate, granularity)}</strong>
            {activeRows.map((item) => (
              <span key={`${item.label}-${activeDate}`}>
                <i style={{ background: item.color }} />
                {item.label}：{compactChartValue(Number(item.value || 0), metric)}
              </span>
            ))}
          </div>
        )}
      </div>
      <div className="quick-market-chart-legend">
        {visibleSeries.map((item, index) => (
          <span key={item.key || item.label || index}>
            <i style={{ background: seriesColor(index) }} />
            {item.label || item.app || `Series ${index + 1}`}
          </span>
        ))}
      </div>
    </div>
  )
}

function reviewSentimentLabel(value?: string): string {
  const text = (value || '').toLowerCase()
  if (text === 'negative') return i18nText('quickSearch.negativeReview')
  if (text === 'positive') return i18nText('quickSearch.positiveReview')
  return i18nText('quickSearch.review')
}

function reviewStars(value?: number | string): string {
  const rating = Math.max(0, Math.min(5, Number(value || 0)))
  return `${'★'.repeat(Math.round(rating))}${'☆'.repeat(5 - Math.round(rating))}`
}

function formatReviewDate(value?: string): string {
  const text = String(value || '')
  return text ? text.slice(0, 10) : '-'
}

function formatReviewVersion(value?: string): string {
  const text = String(value || '').trim()
  if (!text) return ''
  return text.toLowerCase().startsWith('v') ? text : `v${text}`
}

type AppReviewFilter = 'negative' | 'positive' | 'all'
type AppReviewSort = 'newest' | 'oldest'

function defaultReviewFilter(value?: string): AppReviewFilter {
  const text = (value || '').toLowerCase()
  if (text === 'positive') return 'positive'
  if (text === 'all') return 'all'
  return 'negative'
}

function reviewMatchesFilter(review: QuickSearchAppReview, filter: AppReviewFilter): boolean {
  const rating = Math.max(0, Math.min(5, Number(review.rating || 0)))
  if (filter === 'negative') return rating >= 1 && rating <= 3
  if (filter === 'positive') return rating >= 4 && rating <= 5
  return rating > 0
}

function reviewMatchesNegativeTopic(review: QuickSearchAppReview, topicKey: string): boolean {
  if (topicKey === 'all') return true
  return (review.negative_topic_keys || []).includes(topicKey)
}

function reviewDisplayTags(review: QuickSearchAppReview): string[] {
  if (!reviewMatchesFilter(review, 'negative')) return []
  return (review.negative_topics || []).slice(0, 5)
}

function reviewTimeValue(review: QuickSearchAppReview): number {
  const value = Date.parse(String(review.created_at || ''))
  return Number.isFinite(value) ? value : 0
}

function reviewStableKey(review: QuickSearchAppReview): string {
  return [
    review.id || '',
    review.country || '',
    review.created_at || '',
    review.rating || '',
    review.title || '',
    (review.content || '').slice(0, 80),
  ].join('|')
}

function normalizeReviewCountry(value?: string): string {
  const text = (value || '').replace(/[^A-Za-z]/g, '').toUpperCase()
  return text || 'UNKNOWN'
}

const REVIEW_COUNTRY_LABELS: Record<string, string> = {
  US: '美国',
  CN: '中国',
  JP: '日本',
  GB: '英国',
  UK: '英国',
  CA: '加拿大',
  AU: '澳大利亚',
  DE: '德国',
  FR: '法国',
  KR: '韩国',
  IN: '印度',
  BR: '巴西',
  PH: '菲律宾',
  MX: '墨西哥',
  CO: '哥伦比亚',
  TW: '台湾',
  HK: '香港',
  SG: '新加坡',
  IT: '意大利',
  ES: '西班牙',
  NZ: '新西兰',
  ID: '印度尼西亚',
  TH: '泰国',
  VN: '越南',
  MY: '马来西亚',
  NG: '尼日利亚',
  CL: '智利',
  PE: '秘鲁',
  EC: '厄瓜多尔',
  GT: '危地马拉',
  AR: '阿根廷',
  NL: '荷兰',
  SE: '瑞典',
  NO: '挪威',
  DK: '丹麦',
  FI: '芬兰',
  PL: '波兰',
  TR: '土耳其',
  SA: '沙特阿拉伯',
  AE: '阿联酋',
  ZA: '南非',
}

const REVIEW_COUNTRY_LABELS_EN: Record<string, string> = {
  US: 'United States',
  CN: 'China',
  JP: 'Japan',
  GB: 'United Kingdom',
  UK: 'United Kingdom',
  CA: 'Canada',
  AU: 'Australia',
  DE: 'Germany',
  FR: 'France',
  KR: 'South Korea',
  IN: 'India',
  BR: 'Brazil',
  PH: 'Philippines',
  MX: 'Mexico',
  CO: 'Colombia',
  TW: 'Taiwan',
  HK: 'Hong Kong',
  SG: 'Singapore',
  IT: 'Italy',
  ES: 'Spain',
  NZ: 'New Zealand',
  ID: 'Indonesia',
  TH: 'Thailand',
  VN: 'Vietnam',
  MY: 'Malaysia',
  NG: 'Nigeria',
  CL: 'Chile',
  PE: 'Peru',
  EC: 'Ecuador',
  GT: 'Guatemala',
  AR: 'Argentina',
  NL: 'Netherlands',
  SE: 'Sweden',
  NO: 'Norway',
  DK: 'Denmark',
  FI: 'Finland',
  PL: 'Poland',
  TR: 'Turkey',
  SA: 'Saudi Arabia',
  AE: 'United Arab Emirates',
  ZA: 'South Africa',
}

const REVIEW_PRIMARY_COUNTRIES = ['US', 'CN', 'JP', 'GB', 'CA', 'AU', 'DE', 'FR', 'KR', 'IN', 'BR', 'PH', 'MX', 'ZA']
const REVIEW_VISIBLE_PRIMARY_COUNTRY_LIMIT = 6
const REVIEW_OTHER_COUNTRY_FILTER = '__other_countries'

function reviewCountryLabel(value?: string): string {
  const code = normalizeReviewCountry(value)
  if (code === 'UNKNOWN') return i18nText('quickSearch.unknownRegion')
  const language = useAppStore.getState().languageMode
  return (language === 'en-US' ? REVIEW_COUNTRY_LABELS_EN[code] : REVIEW_COUNTRY_LABELS[code]) || code
}

function ReviewDistributionColumn({
  title,
  tone,
  group,
}: {
  title: string
  tone: 'negative' | 'positive'
  group?: QuickSearchReviewDistributionGroup
}) {
  const { isEnglish, text } = useI18n()
  const items = (group?.items || []).slice(0, 4)
  const totalLabel = isEnglish ? `${group?.total || 0} ${text('quickSearch.review')}` : `${group?.total || 0} 条`
  return (
    <div className={`quick-market-review-distribution-card quick-market-review-distribution-card--${tone}`}>
      <div className="quick-market-review-distribution-head">
        <strong>{title}</strong>
        <span>{totalLabel}</span>
      </div>
      {items.length > 0 ? (
        <div className="quick-market-review-distribution-bars">
          {items.map((item) => (
            <div className="quick-market-review-distribution-row" key={`${tone}-${item.key}`}>
              <div className="quick-market-review-distribution-label">
                <span>{item.label}</span>
                <em>{item.percent}% · {isEnglish ? `${item.count} ${text('quickSearch.review')}` : `${item.count} 条`}</em>
              </div>
              <div className="quick-market-review-distribution-track">
                <i style={{ width: `${Math.max(4, Math.min(100, item.percent))}%` }} />
              </div>
            </div>
          ))}
        </div>
      ) : (
        <p>{text('quickSearch.noReviewDistribution')}</p>
      )}
    </div>
  )
}

type ReviewCountryOption = {
  id: string
  label: string
  count: number
  countryCodes?: string[]
}

function AppReviewPanel({ signal }: { signal: QuickSearchMarketSignal }) {
  const { text, format, isEnglish } = useI18n()
  const [expanded, setExpanded] = useState(false)
  const initialFilter = defaultReviewFilter(signal.sentiment_filter)
  const requestedNegativeTopicKey = signal.requested_review_topic_key?.trim() || 'all'
  const initialNegativeTopicFilter = initialFilter === 'negative' ? requestedNegativeTopicKey : 'all'
  const [reviewFilter, setReviewFilter] = useState<AppReviewFilter>(initialFilter)
  const [negativeTopicFilter, setNegativeTopicFilter] = useState(initialNegativeTopicFilter)
  const [countryFilter, setCountryFilter] = useState('all')
  const [reviewSort, setReviewSort] = useState<AppReviewSort>('newest')
  const [reviewTranslations, setReviewTranslations] = useState<Record<string, { title_zh?: string; content_zh?: string }>>({})
  const [manualTranslatingReviewKeys, setManualTranslatingReviewKeys] = useState<Record<string, boolean>>({})
  const app = signal.app
  const reviews = useMemo(() => (signal.reviews || []) as QuickSearchAppReview[], [signal.reviews])
  const translatingReviewKeysRef = useRef<Set<string>>(new Set())
  const countryKey = signal.countries?.join(',') || ''
  const countryOptions = useMemo<ReviewCountryOption[]>(() => {
    const counts = new Map<string, number>()
    reviews.forEach((review) => {
      const code = normalizeReviewCountry(review.country)
      counts.set(code, (counts.get(code) || 0) + 1)
    })
    const sortedCountries = Array.from(counts.entries())
      .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
      .slice(0, 24)
    if (signal.country_scope !== 'specific') {
      const primaryCodes = new Set(REVIEW_PRIMARY_COUNTRIES)
      const visibleCountries = sortedCountries
        .filter(([code]) => primaryCodes.has(code))
        .slice(0, REVIEW_VISIBLE_PRIMARY_COUNTRY_LIMIT)
      const visibleCodeSet = new Set(visibleCountries.map(([code]) => code))
      const otherCountries = sortedCountries.filter(([code]) => !visibleCodeSet.has(code))
      const otherCount = otherCountries.reduce((sum, [, count]) => sum + count, 0)
      return [
        {
          id: 'all',
          label: text('quickSearch.allCountries'),
          count: reviews.length,
        },
        ...visibleCountries.map(([code, count]) => ({
          id: code,
          label: reviewCountryLabel(code),
          count,
          countryCodes: [code],
        })),
        ...(otherCount > 0 ? [{
          id: REVIEW_OTHER_COUNTRY_FILTER,
          label: text('quickSearch.otherCountries'),
          count: otherCount,
          countryCodes: otherCountries.map(([code]) => code),
        }] : []),
      ]
    }
    return [
      {
        id: 'all',
        label: text('quickSearch.selectedCountries'),
        count: reviews.length,
      },
      ...sortedCountries.map(([code, count]) => ({
        id: code,
        label: reviewCountryLabel(code),
        count,
        countryCodes: [code],
      })),
    ]
  }, [reviews, signal.country_scope, text])
  const selectedCountryCodes = useMemo(() => {
    const option = countryOptions.find((item) => item.id === countryFilter)
    return option?.countryCodes || (countryFilter === 'all' ? [] : [countryFilter])
  }, [countryFilter, countryOptions])
  const countryFilteredReviews = useMemo(() => {
    if (countryFilter === 'all' || selectedCountryCodes.length === 0) return reviews
    const codeSet = new Set(selectedCountryCodes)
    return reviews.filter((review) => codeSet.has(normalizeReviewCountry(review.country)))
  }, [countryFilter, reviews, selectedCountryCodes])
  const totalNegativeCount = typeof signal.negative_total === 'number'
    ? signal.negative_total
    : reviews.filter((review) => reviewMatchesFilter(review, 'negative')).length
  const totalPositiveCount = typeof signal.positive_total === 'number'
    ? signal.positive_total
    : reviews.filter((review) => reviewMatchesFilter(review, 'positive')).length
  const negativeCount = countryFilter === 'all'
    ? totalNegativeCount
    : countryFilteredReviews.filter((review) => reviewMatchesFilter(review, 'negative')).length
  const positiveCount = countryFilter === 'all'
    ? totalPositiveCount
    : countryFilteredReviews.filter((review) => reviewMatchesFilter(review, 'positive')).length
  const allCount = countryFilter === 'all' && typeof signal.all_total === 'number'
    ? signal.all_total
    : countryFilteredReviews.length
  const sentimentFilteredReviews = countryFilteredReviews.filter((review) => reviewMatchesFilter(review, reviewFilter))
  const filteredReviews = reviewFilter === 'negative'
    ? sentimentFilteredReviews.filter((review) => reviewMatchesNegativeTopic(review, negativeTopicFilter))
    : sentimentFilteredReviews
  const sortedReviews = useMemo(() => [...filteredReviews].sort((a, b) => {
    const diff = reviewTimeValue(b) - reviewTimeValue(a)
    return reviewSort === 'newest' ? diff : -diff
  }), [filteredReviews, reviewSort])
  const visibleReviews = useMemo(
    () => expanded ? sortedReviews : sortedReviews.slice(0, 5),
    [expanded, sortedReviews],
  )
  const visibleReviewTranslationKey = useMemo(
    () => visibleReviews.slice(0, 20).map((review) => reviewStableKey(review)).join('||'),
    [visibleReviews],
  )
  const source = signal.source || 'App Store'
  const dateText = signal.date_range?.label || [signal.date_range?.start, signal.date_range?.end].filter(Boolean).join(' ~ ') || (isEnglish ? 'recent period' : '近期')
  const sentimentLabel = reviewSentimentLabel(reviewFilter)
  const appStoreUrl = app?.app_store_url || app?.store_url || ''
  const sensorTowerUrl = app?.sensor_tower_url || ''
  const rawCount = typeof signal.raw_total === 'number' ? signal.raw_total : reviews.length
  const capacity = typeof signal.max_raw_capacity === 'number' ? signal.max_raw_capacity : 500
  const pageCount = typeof signal.page_count === 'number' ? signal.page_count : undefined
  const fetchedPages = typeof signal.fetched_pages === 'number' ? signal.fetched_pages : undefined
  const countryScopeText = signal.country_scope === 'specific'
    ? (isEnglish
      ? ((signal.countries || []).map((code) => reviewCountryLabel(code)).join(', ') || 'selected countries')
      : (signal.country_labels?.join('、') || signal.countries?.map((code) => reviewCountryLabel(code)).join('、') || '指定国家'))
    : text('quickSearch.allCountries')
  const conclusion = signal.available
    ? format('quickSearch.reviewConclusion', {
      app: app?.name || signal.queries?.[0] || text('quickSearch.targetApp'),
      source,
      countryScope: countryScopeText,
      dateText,
      rawCount,
      negativeCount: totalNegativeCount,
      positiveCount: totalPositiveCount,
      topic: signal.requested_review_topic_label ? format('quickSearch.reviewTopicSuffix', { topic: signal.requested_review_topic_label }) : '',
    })
    : signal.error || text('quickSearch.sensorTowerUnavailable')
  const filterOptions: Array<{ id: AppReviewFilter; label: string; count: number }> = [
    { id: 'negative', label: text('quickSearch.negativeReview'), count: negativeCount },
    { id: 'positive', label: text('quickSearch.positiveReview'), count: positiveCount },
    { id: 'all', label: text('quickSearch.allReviews'), count: allCount },
  ]
  const reviewSourceNote = signal.fallback === 'sensor_tower'
    ? (signal.apple_rss_partial
      ? format('quickSearch.appleRssPartialNote', { appleTotal: signal.apple_reviews_total || 0, capacity, fetchedPages: fetchedPages || 1, pageCount: pageCount || fetchedPages || 1 })
      : format('quickSearch.appleRssFallbackNote', { capacity, fetchedPages: fetchedPages || 1, pageCount: pageCount || fetchedPages || 1 }))
    : text('quickSearch.appleRssNote')
  const distribution = signal.review_distribution
  const negativeTopicOptions = useMemo(() => {
    const counts = new Map<string, { label: string; count: number }>()
    countryFilteredReviews.forEach((review) => {
      if (!reviewMatchesFilter(review, 'negative')) return
      const keys = review.negative_topic_keys || []
      const labels = review.negative_topics || []
      keys.forEach((key, index) => {
        if (!key || key === 'other') return
        const current = counts.get(key) || { label: labels[index] || key, count: 0 }
        current.count += 1
        counts.set(key, current)
      })
    })
    const options = Array.from(counts.entries())
      .map(([key, value]) => ({ key, label: value.label, count: value.count, percent: 0 }))
      .sort((a, b) => b.count - a.count)
      .filter((item) => item.key !== 'other' && item.count > 0)
    if (
      requestedNegativeTopicKey !== 'all'
      && !options.some((item) => item.key === requestedNegativeTopicKey)
    ) {
      options.unshift({
        key: requestedNegativeTopicKey,
        label: signal.requested_review_topic_label || requestedNegativeTopicKey,
        count: 0,
        percent: 0,
      })
    }
    return options.slice(0, 8)
  }, [countryFilteredReviews, requestedNegativeTopicKey, signal.requested_review_topic_label])

  useEffect(() => {
    setReviewFilter(initialFilter)
    setNegativeTopicFilter(initialNegativeTopicFilter)
    setCountryFilter('all')
    setReviewSort('newest')
    setReviewTranslations({})
    setManualTranslatingReviewKeys({})
    translatingReviewKeysRef.current.clear()
    setExpanded(false)
  }, [initialFilter, initialNegativeTopicFilter, app?.name, source, rawCount, signal.country_scope, countryKey])

  useEffect(() => {
    if (!signal.available) return
    const translatingKeys = translatingReviewKeysRef.current
    const targetReviews = visibleReviews.slice(0, 20).filter((review) => {
      const key = reviewStableKey(review)
      if (!key) return false
      if (reviewTranslations[key]) return false
      if (translatingKeys.has(key)) return false
      return Boolean(review.title || review.content)
        && !(review.title_zh || review.content_zh)
    })
    if (!targetReviews.length) return
    const pendingKeys = targetReviews.map((review) => reviewStableKey(review)).filter(Boolean)
    pendingKeys.forEach((key) => translatingKeys.add(key))
    let cancelled = false
    translateQuickSearchReviews(targetReviews.map((review) => ({
      ...review,
      id: reviewStableKey(review),
    })))
      .then((resp) => {
        if (cancelled) return
        const next: Record<string, { title_zh?: string; content_zh?: string }> = {}
        ;(resp.reviews || []).forEach((item) => {
          const key = String(item.id || '').trim()
          if (!key) return
          next[key] = {
            title_zh: item.title_zh || '',
            content_zh: item.content_zh || '',
          }
        })
        if (Object.keys(next).length > 0) {
          setReviewTranslations((prev) => ({ ...prev, ...next }))
        }
      })
      .catch(() => {})
      .finally(() => {
        pendingKeys.forEach((key) => translatingKeys.delete(key))
      })
    return () => {
      cancelled = true
      pendingKeys.forEach((key) => translatingKeys.delete(key))
    }
  }, [signal.available, visibleReviewTranslationKey, visibleReviews, reviewTranslations])

  const handleTranslateReview = useCallback((review: QuickSearchAppReview) => {
    const key = reviewStableKey(review)
    if (!key || translatingReviewKeysRef.current.has(key)) return
    if (!(review.title || review.content)) return

    translatingReviewKeysRef.current.add(key)
    setManualTranslatingReviewKeys((prev) => ({ ...prev, [key]: true }))

    translateQuickSearchReviews([{
      ...review,
      id: key,
    }])
      .then((resp) => {
        const item = (resp.reviews || [])[0]
        const itemKey = String(item?.id || key).trim()
        if (!itemKey) return
        setReviewTranslations((prev) => ({
          ...prev,
          [itemKey]: {
            title_zh: item?.title_zh || '',
            content_zh: item?.content_zh || '',
          },
        }))
      })
      .catch(() => {})
      .finally(() => {
        translatingReviewKeysRef.current.delete(key)
        setManualTranslatingReviewKeys((prev) => {
          const next = { ...prev }
          delete next[key]
          return next
        })
      })
  }, [])

  return (
    <div className="quick-market-analysis">
      <div className="quick-market-heading">
        <img src="/appstore_line.png" alt="" />
        <span>{text('quickSearch.appStoreReviews')}</span>
      </div>

      <section className="quick-result-conclusion quick-result-conclusion--market">
        <p className="quick-result-kicker">{text('quickSearch.conclusion')}</p>
        <p className="quick-result-main">{conclusion}</p>
      </section>

      {signal.available && (
        <>
          <div className="quick-market-review-target">
            {app?.icon_url && <img src={app.icon_url} alt="" />}
            <div className="min-w-0">
              <strong>{app?.name || signal.queries?.[0] || 'Unknown App'}</strong>
              {app?.publisher && <small>{app.publisher}</small>}
            </div>
            {(appStoreUrl || sensorTowerUrl) && (
              <div className="quick-market-row-links">
                {appStoreUrl && (
                  <a href={appStoreUrl} target="_blank" rel="noreferrer" aria-label={text('quickSearch.openAppStore')}>
                    <ExternalLink size={11} />
                  </a>
                )}
                {sensorTowerUrl && (
                  <a href={sensorTowerUrl} target="_blank" rel="noreferrer" aria-label={text('quickSearch.openSensorTower')}>
                    <img src="/sensortower.png" alt="" />
                  </a>
                )}
              </div>
            )}
          </div>

          <div className="quick-market-scope">
            <span>{text('quickSearch.source')} {source}</span>
            <span>{text('quickSearch.country')} {countryScopeText}</span>
            <span>{text('quickSearch.time')} {dateText}</span>
            <span>{text('quickSearch.defaultFilter')} {reviewSentimentLabel(signal.sentiment_filter)}</span>
            <span>{text('quickSearch.fetched')} {rawCount}/{capacity} {text('quickSearch.evidenceCountSuffix')}</span>
            <span>{text('quickSearch.showing')} {visibleReviews.length}/{filteredReviews.length} {text('quickSearch.evidenceCountSuffix')}</span>
          </div>

          {distribution && (
            <section className="quick-market-review-distribution" aria-label={text('quickSearch.contentDistribution')}>
              <div className="quick-market-review-distribution-title">
                <strong>{text('quickSearch.contentDistribution')}</strong>
                {distribution.note && <span>{distribution.note}</span>}
              </div>
              <div className="quick-market-review-distribution-grid">
                <ReviewDistributionColumn title={text('quickSearch.negativeDistribution')} tone="negative" group={distribution.negative} />
                <ReviewDistributionColumn title={text('quickSearch.positiveDistribution')} tone="positive" group={distribution.positive} />
              </div>
            </section>
          )}

          <div className="quick-market-review-filter-stack">
            {countryOptions.length > 2 || (signal.country_scope === 'global' && countryOptions.length > 1) ? (
              <div className="quick-market-review-filter-row">
                <span className="quick-market-review-filter-label">{text('quickSearch.country')}</span>
                <div className="quick-market-review-filter quick-market-review-country-filter" role="tablist" aria-label={text('quickSearch.country')}>
                  {countryOptions.map((option) => (
                    <button
                      key={option.id}
                      type="button"
                      className={option.id === countryFilter ? 'is-active' : ''}
                      onClick={() => {
                        setCountryFilter(option.id)
                        setReviewFilter(initialFilter)
                        setNegativeTopicFilter(initialNegativeTopicFilter)
                        setExpanded(false)
                      }}
                    >
                      {option.label}
                      <span>{option.count}</span>
                    </button>
                  ))}
                </div>
              </div>
            ) : null}

            <div className="quick-market-review-filter-row">
              <span className="quick-market-review-filter-label">{text('quickSearch.rating')}</span>
              <div className="quick-market-review-filter" role="tablist" aria-label={text('quickSearch.rating')}>
                {filterOptions.map((option) => (
                  <button
                    key={option.id}
                    type="button"
                    className={option.id === reviewFilter ? 'is-active' : ''}
                    onClick={() => {
                      setReviewFilter(option.id)
                      setNegativeTopicFilter('all')
                      setExpanded(false)
                    }}
                  >
                    {option.label}
                    <span>{option.count}</span>
                  </button>
                ))}
              </div>
            </div>

            {reviewFilter === 'negative' && negativeTopicOptions.length > 0 && (
              <div className="quick-market-review-filter-row">
                <span className="quick-market-review-filter-label">{text('quickSearch.type')}</span>
                <div className="quick-market-review-topic-filter" role="tablist" aria-label={text('quickSearch.type')}>
                  <button
                    type="button"
                    className={negativeTopicFilter === 'all' ? 'is-active' : ''}
                    onClick={() => {
                      setNegativeTopicFilter('all')
                      setExpanded(false)
                    }}
                  >
                    {text('quickSearch.allTypes')}
                    <span>{negativeCount}</span>
                  </button>
                  {negativeTopicOptions.map((topic) => (
                    <button
                      key={topic.key}
                      type="button"
                      className={negativeTopicFilter === topic.key ? 'is-active' : ''}
                      onClick={() => {
                        setNegativeTopicFilter(topic.key)
                        setExpanded(false)
                      }}
                    >
                      {topic.label}
                      <span>{topic.count}</span>
                    </button>
                  ))}
                </div>
              </div>
            )}

            <div className="quick-market-review-filter-row">
              <span className="quick-market-review-filter-label">{text('quickSearch.time')}</span>
              <div className="quick-market-review-filter" role="tablist" aria-label={text('quickSearch.time')}>
                <button
                  type="button"
                  className={reviewSort === 'newest' ? 'is-active' : ''}
                  onClick={() => {
                    setReviewSort('newest')
                    setExpanded(false)
                  }}
                >
                  {text('quickSearch.newestFirst')}
                </button>
                <button
                  type="button"
                  className={reviewSort === 'oldest' ? 'is-active' : ''}
                  onClick={() => {
                    setReviewSort('oldest')
                    setExpanded(false)
                  }}
                >
                  {text('quickSearch.oldestFirst')}
                </button>
              </div>
            </div>
          </div>

          <div className="quick-market-review-list">
            {visibleReviews.length > 0 ? visibleReviews.map((review, index) => {
              const displayTags = reviewDisplayTags(review)
              const reviewKey = reviewStableKey(review)
              const translated = reviewTranslations[reviewKey]
              const titleZh = review.title_zh || translated?.title_zh || ''
              const contentZh = review.content_zh || translated?.content_zh || ''
              const isTranslating = Boolean(manualTranslatingReviewKeys[reviewKey])
              const hasTranslation = Boolean(titleZh || contentZh)
              return (
                <article className="quick-market-review-card" key={`${review.id || index}`}>
	                  <div className="quick-market-review-head">
	                    <span className="quick-market-review-rating">{reviewStars(review.rating)}</span>
	                    <span>{formatReviewDate(review.created_at)}</span>
	                    {review.country && <span>{reviewCountryLabel(review.country)}</span>}
	                    {review.version && <span>{formatReviewVersion(review.version)}</span>}
                      <button
                        type="button"
                        className="quick-market-review-translate-button"
                        disabled={isTranslating || !(review.title || review.content)}
                        onClick={() => handleTranslateReview(review)}
                      >
                        {isTranslating ? text('quickSearch.translating') : hasTranslation ? text('quickSearch.retranslate') : text('quickSearch.translate')}
                      </button>
	                  </div>
                  <h3>{titleZh || review.title || 'Untitled review'}</h3>
                  {contentZh && <p className="quick-market-review-zh">{contentZh}</p>}
                  <p className={contentZh ? 'quick-market-review-original' : ''}>{review.content || '-'}</p>
                  {displayTags.length > 0 && (
                    <div className="quick-market-review-tags">
                      {displayTags.map((tag) => <span key={tag}>{tag}</span>)}
                    </div>
                  )}
                </article>
              )
            }) : (
              <div className="quick-market-review-empty">
                {format('quickSearch.noReviews', { sentiment: sentimentLabel })}
              </div>
            )}
          </div>

          {filteredReviews.length > 5 && (
            <button
              type="button"
              className="quick-market-review-toggle"
              onClick={() => setExpanded((value) => !value)}
            >
              {expanded ? (
                <>
                  <ChevronUp size={13} />
                  {text('quickSearch.collapseToFive')}
                </>
              ) : (
                <>
                  <ChevronDown size={13} />
                  {format('quickSearch.expandAllReviews', { count: filteredReviews.length })}
                </>
              )}
            </button>
          )}

          <p className="quick-market-note">
            {format('quickSearch.reviewDefinition', { note: reviewSourceNote })}
          </p>
        </>
      )}
    </div>
  )
}

function MarketTrendPanel({ signal }: { signal: QuickSearchMarketSignal }) {
  const { text, format } = useI18n()
  const rows = (signal.table_rows || []) as QuickSearchMarketTrendRow[]
  const rawSeries = signal.time_series?.series || []
  const metrics = trendMetricsFromSignal(signal)
  const showRevenue = metrics.includes('revenue')
  const showDownloads = metrics.includes('downloads')
  const showRpd = metrics.includes('rpd')
  const showTrendFlags = showRevenue || showDownloads
  const metricText = formatTrendMetricList(metrics)
  const chartSeries = rawSeries
    .filter((item) => (item.platform || '').toLowerCase() === 'all')
    .slice(0, 6)
  const regions = signal.regions?.join('、') || signal.metrics_region || 'US'
  const currentRange = [signal.date_range?.start, signal.date_range?.end].filter(Boolean).join(' ~ ') || signal.date_range?.label || text('quickSearch.currentPeriod')
  const comparisonRange = [signal.comparison_range?.start, signal.comparison_range?.end].filter(Boolean).join(' ~ ') || signal.comparison_range?.label || text('quickSearch.comparisonPeriod')
  const allRows = rows.filter((row) => row.platform === 'all')
  const displayRows = rows.length > 0 ? rows : allRows
  const highlights = signal.highlights || []
  const leadHighlight = highlights[0]
  const conclusion = leadHighlight
    ? format('quickSearch.trendHighlightConclusion', {
      count: signal.queries?.length || allRows.length || rows.length,
      regions,
      app: leadHighlight.app || text('quickSearch.targetApp'),
      flag: leadHighlight.flag || text('quickSearch.trend'),
    })
    : format('quickSearch.trendStableConclusion', {
      count: signal.queries?.length || allRows.length || rows.length,
      regions,
      metrics: metricText,
    })

  return (
    <div className="quick-market-analysis">
      <div className="quick-market-heading">
        <img src="/sensortower.png" alt="" />
        <span>{text('quickSearch.marketTrend')}</span>
      </div>

      <section className="quick-result-conclusion quick-result-conclusion--market">
        <p className="quick-result-kicker">{text('quickSearch.conclusion')}</p>
        <p className="quick-result-main">{conclusion}</p>
      </section>

      <div className="quick-market-scope">
        <span>{text('quickSearch.region')} {regions}</span>
        <span>{text('quickSearch.current')} {currentRange}</span>
        <span>{text('quickSearch.comparison')} {comparisonRange}</span>
        <span>{text('quickSearch.metrics')} {metricText}</span>
        {showRpd && <span>{text('quickSearch.rpdFormula')}</span>}
      </div>

      <div className="quick-market-table-wrap quick-market-trend-table-wrap">
        <table className={`quick-market-table quick-market-trend-table quick-market-trend-table--${metrics.length <= 1 ? 'compact' : metrics.length === 2 ? 'medium' : 'full'}`}>
          <thead>
            <tr>
              <th>{text('quickSearch.product')}</th>
              <th>{text('quickSearch.countryHeader')}</th>
              <th>{text('quickSearch.platform')}</th>
              {showRevenue && <th>{text('quickSearch.currentRevenue')}</th>}
              {showRevenue && <th>{text('quickSearch.revenueChange')}</th>}
              {showDownloads && <th>{text('quickSearch.currentDownloads')}</th>}
              {showDownloads && <th>{text('quickSearch.downloadsChange')}</th>}
              {showRpd && <th>RPD</th>}
              {showTrendFlags && <th>{text('quickSearch.trend')}</th>}
            </tr>
          </thead>
          <tbody>
            {displayRows.map((row, index) => {
              const appStoreUrl = row.app_store_url || ''
              const sensorTowerUrl = row.sensor_tower_url || ''
              return (
                <tr key={`${row.app}-${row.region}-${row.platform}-${index}`}>
                  <td>
                    <div className="quick-market-product quick-market-product--links">
                      <span>
                        <strong>{row.app || 'Unknown App'}</strong>
                        {row.publisher && <small>{row.publisher}</small>}
                      </span>
                      {(appStoreUrl || sensorTowerUrl) && (
                        <div className="quick-market-row-links">
                          {appStoreUrl && (
                            <a href={appStoreUrl} target="_blank" rel="noreferrer" aria-label={text('quickSearch.openAppStore')}>
                              <ExternalLink size={11} />
                            </a>
                          )}
                          {sensorTowerUrl && (
                            <a href={sensorTowerUrl} target="_blank" rel="noreferrer" aria-label={text('quickSearch.openSensorTower')}>
                              <img src="/sensortower.png" alt="" />
                            </a>
                          )}
                        </div>
                      )}
                    </div>
                  </td>
                  <td>{row.region || '-'}</td>
                  <td>{formatTrendPlatform(row.platform)}</td>
                  {showRevenue && <td>{row.revenue_display || '-'}</td>}
                  {showRevenue && <td className={trendPercentClass(row.revenue_growth_pct)}>{formatTrendPercent(row.revenue_growth_pct)}</td>}
                  {showDownloads && <td>{row.downloads_display || '-'}</td>}
                  {showDownloads && <td className={trendPercentClass(row.downloads_growth_pct)}>{formatTrendPercent(row.downloads_growth_pct)}</td>}
                  {showRpd && <td>{row.rpd_display || row.rpd_60d_display || '-'}</td>}
                  {showTrendFlags && (
                    <td>
                      <div className="quick-market-trend-flags">
                        {(row.flags || []).length > 0
                          ? row.flags?.map((flag) => <span key={flag}>{flag}</span>)
                          : <em>{text('quickSearch.noMajorChange')}</em>}
                      </div>
                    </td>
                  )}
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      {chartSeries.length > 0 && (
        <div className="quick-market-chart-grid-wrap">
          {metrics.map((metric) => (
            <MarketTrendChart
              key={metric}
              title={text(TREND_METRIC_META[metric].titleKey)}
              metric={metric}
              series={chartSeries}
              granularity={signal.time_series?.granularity}
            />
          ))}
        </div>
      )}

      <p className="quick-market-note">
        {format('quickSearch.trendFooter', {
          metrics: metricText,
          rpdNote: showRpd ? text('quickSearch.rpdNote') : '',
        })}
      </p>
    </div>
  )
}

function MarketAnalysisPanel({ signal }: { signal: QuickSearchMarketSignal }) {
  const { text, format } = useI18n()
  if (signal.review_search) {
    return <AppReviewPanel signal={signal} />
  }

  if (signal.metric_trends) {
    return <MarketTrendPanel signal={signal} />
  }

  const apps = signal.top_apps || []
  const preserveBackendOrder = signal.direct_app_competitors || signal.sort_by === 'app_competitor'
  const candidateRegion = signal.candidate_region || 'US'
  const metricsRegion = signal.metrics_region || signal.market_region || text('quickSearch.globalRegion')
  const dateText = signal.date_range?.label || [signal.date_range?.start, signal.date_range?.end].filter(Boolean).join(' ~ ') || text('quickSearch.previousFullMonth')
  const sortLabel = signal.sort_by === 'growth'
    ? text('quickSearch.revenueGrowth')
    : signal.sort_by === 'downloads'
      ? text('quickSearch.downloadsScale')
      : signal.sort_by === 'scale'
        ? text('quickSearch.overallScale')
        : signal.sort_by === 'app_competitor'
          ? text('quickSearch.directRelevance')
        : text('quickSearch.revenueScale')
  const sortedApps = preserveBackendOrder
    ? apps.slice(0, 8)
    : [...apps]
      .sort((a, b) => ((b.revenue || 0) + (b.downloads || 0) * 0.02) - ((a.revenue || 0) + (a.downloads || 0) * 0.02))
      .slice(0, 8)
  const targetApp = sortedApps.find((app) => app.is_target_app)
  const leader = preserveBackendOrder
    ? sortedApps.find((app) => !app.is_target_app) || sortedApps[0]
    : sortedApps[0]
  const conclusion = signal.direct_app_competitors && targetApp
    ? format('quickSearch.targetAppCompetitorsConclusion', { app: targetApp.name || text('quickSearch.targetApp') })
    : leader
      ? format('quickSearch.leaderConclusion', { app: leader.name || text('quickSearch.currentLeadingProduct') })
      : text('quickSearch.noStableMarketSignal')

  if (!signal.available) {
    return (
      <div className="quick-market-analysis">
        <div className="quick-market-heading">
          <img src="/sensortower.png" alt="" />
          <span>{text('quickSearch.marketSignal')}</span>
        </div>
        <p className="text-[13px] leading-relaxed text-muted/75">{signal.error || text('quickSearch.sensorTowerUnavailable')}</p>
      </div>
    )
  }

  return (
    <div className="quick-market-analysis">
      <div className="quick-market-heading">
        <img src="/sensortower.png" alt="" />
        <span>{text('quickSearch.marketSignal')}</span>
      </div>

      <section className="quick-result-conclusion quick-result-conclusion--market">
        <p className="quick-result-kicker">{text('quickSearch.conclusion')}</p>
        <p className="quick-result-main">{conclusion}</p>
      </section>

      {leader && (
        <div className="quick-market-conclusion">
          <div className="min-w-0">
            <p className="text-[11px] font-semibold text-muted/62">{text('quickSearch.currentLeadingProduct')}</p>
	            <p className="mt-1 text-[17px] font-bold text-text/88 leading-snug">{leader.name || 'Unknown App'}</p>
	            <p className="mt-1 text-[12px] text-muted/66">
	              {text('quickSearch.revenue')} {leader.revenue_display || '-'} · {text('quickSearch.downloads')} {leader.downloads_display || '-'}
	              {typeof leader.growth_pct === 'number' && ` · ${text('quickSearch.revenue')} ${leader.growth_pct > 0 ? '+' : ''}${leader.growth_pct}%`}
	            </p>
          </div>
          {((leader.app_store_url || leader.store_url) || (leader.sensor_tower_url || sensorTowerSearchUrl(leader.name))) && (
            <div className="quick-market-row-links quick-market-conclusion-links">
              {(leader.app_store_url || leader.store_url) && (
                <a href={leader.app_store_url || leader.store_url} target="_blank" rel="noreferrer" aria-label={text('quickSearch.openAppStore')}>
                  <ExternalLink size={11} />
                </a>
              )}
              {(leader.sensor_tower_url || sensorTowerSearchUrl(leader.name)) && (
                <a href={leader.sensor_tower_url || sensorTowerSearchUrl(leader.name)} target="_blank" rel="noreferrer" aria-label={text('quickSearch.openSensorTower')}>
                  <img src="/sensortower.png" alt="" />
                </a>
              )}
            </div>
          )}
          {leader.icon_url && (
            <img src={leader.icon_url} alt="" className="w-11 h-11 rounded-xl object-cover shrink-0" />
          )}
        </div>
      )}

      <div className="quick-market-scope">
        <span>{text('quickSearch.competitorRegion')} {candidateRegion}</span>
        <span>{text('quickSearch.revenueDownloads')} {metricsRegion}</span>
        <span>{dateText}</span>
        <span>{sortLabel}</span>
      </div>

      <div className="quick-market-table-wrap">
        <table className="quick-market-table">
          <thead>
            <tr>
              <th>{text('quickSearch.product')}</th>
              <th>{text('quickSearch.typeScene')}</th>
              <th>{marketMetricHeader(dateText, 'revenue')}</th>
              <th>{marketMetricHeader(dateText, 'downloads')}</th>
            </tr>
          </thead>
          <tbody>
            {sortedApps.map((app, index) => {
              const appStoreUrl = app.app_store_url || app.store_url || ''
              const sensorTowerUrl = app.sensor_tower_url || sensorTowerSearchUrl(app.name)
              return (
              <tr key={`${app.name}-${index}`}>
                <td>
                  <div className="quick-market-product quick-market-product--linked">
                    <div className="quick-market-product-main">
                      {app.icon_url ? (
                        <img src={app.icon_url} alt="" className="w-7 h-7 rounded-lg object-cover shrink-0" />
                      ) : (
                        <div className="w-7 h-7 rounded-lg bg-white/75 shrink-0" />
                      )}
                      <span>
                        <strong>{app.name || 'Unknown App'}</strong>
                        {app.publisher && <small>{app.publisher}</small>}
                      </span>
                    </div>
                    {(appStoreUrl || sensorTowerUrl) && (
                      <div className="quick-market-row-links">
                        {appStoreUrl && (
                          <a href={appStoreUrl} target="_blank" rel="noreferrer" aria-label={text('quickSearch.openAppStore')}>
                            <ExternalLink size={11} />
                          </a>
                        )}
                        {sensorTowerUrl && (
                          <a href={sensorTowerUrl} target="_blank" rel="noreferrer" aria-label={text('quickSearch.openSensorTower')}>
                            <img src="/sensortower.png" alt="" />
                          </a>
                        )}
                      </div>
                    )}
                  </div>
                </td>
                <td>{formatMarketScene(app, signal)}</td>
                <td>{app.revenue_display || '-'}</td>
                <td>{app.downloads_display || '-'}</td>
              </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      {signal.queries && signal.queries.length > 0 && (
        <div className="quick-market-query-row">
          <span>{text('quickSearch.queryTerms')}</span>
          <div>
            {signal.queries.slice(0, 8).map((q) => (
              <em key={q}>{q}</em>
            ))}
          </div>
        </div>
      )}

      <p className="quick-market-note">
        {text('quickSearch.marketNote')}
      </p>
      <div className="quick-market-supplement-grid">
        <div>
          <span>{text('quickSearch.dataScope')}</span>
          <p>{format('quickSearch.marketDataScopeText', { candidateRegion, metricsRegion, dateText })}</p>
        </div>
        <div>
          <span>{text('quickSearch.missedReason')}</span>
          <p>{text('quickSearch.missedReasonText')}</p>
        </div>
        <div>
          <span>{text('quickSearch.continueReddit')}</span>
          <p>{marketRedditSuggestion(sortedApps)}</p>
        </div>
        <div>
          <span>{text('quickSearch.priorityAdvice')}</span>
          <p>{marketPriorityText(sortedApps)}</p>
        </div>
      </div>
    </div>
  )
}

function SearchHistoryTimeline({
  items,
  onSelect,
}: {
  items: QuickSearchHistoryItem[]
  onSelect: (item: QuickSearchHistoryItem) => void
}) {
  const { text, format } = useI18n()
  const [expanded, setExpanded] = useState(false)
  const [hoveredItem, setHoveredItem] = useState<QuickSearchHistoryItem | null>(null)
  const [hoverCardTop, setHoverCardTop] = useState<number | null>(null)
  const panelRef = useRef<HTMLElement | null>(null)
  const visibleItems = items
    .slice(0, 12)
    .sort((a, b) => a.timestamp - b.timestamp)

  const updateHoveredItem = useCallback((item: QuickSearchHistoryItem, node: HTMLButtonElement) => {
    const panel = panelRef.current
    if (!panel) {
      setHoveredItem(item)
      setHoverCardTop(null)
      return
    }
    const panelRect = panel.getBoundingClientRect()
    const nodeRect = node.getBoundingClientRect()
    setHoveredItem(item)
    setHoverCardTop(nodeRect.top - panelRect.top + nodeRect.height / 2)
  }, [])

  const clearHoveredItem = useCallback(() => {
    setHoveredItem(null)
    setHoverCardTop(null)
  }, [])

  if (items.length === 0) return null

  return (
    <aside ref={panelRef} className="quick-search-history-panel" aria-label={text('quickSearch.history')}>
      <div className="quick-search-history-heading">
        <button
          type="button"
          className="quick-search-history-toggle"
          onClick={() => {
            setExpanded((value) => !value)
            clearHoveredItem()
          }}
          aria-expanded={expanded}
        >
          <span>{text('quickSearch.history')}</span>
          {expanded ? <ChevronUp size={13} /> : <ChevronDown size={13} />}
        </button>
      </div>
      <AnimatePresence initial={false}>
        {expanded && (
          <motion.div
            key="quick-search-history-timeline"
            className={`quick-search-history-timeline ${visibleItems.length === 1 ? 'quick-search-history-timeline--single' : ''}`}
            onMouseLeave={clearHoveredItem}
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.18, ease: 'easeOut' }}
          >
            {visibleItems.map((item, index) => (
              <motion.button
                type="button"
                key={item.id}
                className="quick-search-history-item"
                initial={{ opacity: 0, y: -3, scale: 0.88 }}
                animate={{ opacity: 1, y: 0, scale: 1 }}
                transition={{ duration: 0.18, delay: index * 0.045, ease: 'easeOut' }}
                onClick={(event) => {
                  event.stopPropagation()
                  onSelect(item)
                }}
                onPointerEnter={(event) => updateHoveredItem(item, event.currentTarget)}
                onPointerLeave={clearHoveredItem}
                onMouseEnter={(event) => updateHoveredItem(item, event.currentTarget)}
                onMouseLeave={clearHoveredItem}
                onFocus={(event) => updateHoveredItem(item, event.currentTarget)}
                onBlur={clearHoveredItem}
                aria-label={format('quickSearch.openHistory', { query: item.query })}
              >
                <span className="quick-search-history-dot" />
              </motion.button>
            ))}
          </motion.div>
        )}
      </AnimatePresence>
      <AnimatePresence>
        {expanded && hoveredItem && hoverCardTop !== null && (
          <motion.div
            key={hoveredItem.id}
            className="quick-search-history-hover-card"
            style={{ top: hoverCardTop }}
            initial={{ opacity: 0, x: 6 }}
            animate={{ opacity: 1, x: 0 }}
            exit={{ opacity: 0, x: 4 }}
            transition={{ duration: 0.16, ease: 'easeOut' }}
          >
            <strong>{hoveredItem.query}</strong>
            <small>{formatHistoryTime(hoveredItem.timestamp)}</small>
          </motion.div>
        )}
      </AnimatePresence>
    </aside>
  )
}

export default function QuickSearchView() {
  const themeMode = useAppStore((s) => s.themeMode)
  const { text, list, isEnglish } = useI18n()
  const query = useQuickSearchStore((s) => s.query)
  const setQuickSearchQuery = useQuickSearchStore((s) => s.setQuery)
  const timePeriod = useQuickSearchStore((s) => s.timePeriod)
  const setTimePeriod = useQuickSearchStore((s) => s.setTimePeriod)
  const minScore = useQuickSearchStore((s) => s.minScore)
  const setMinScore = useQuickSearchStore((s) => s.setMinScore)
  const marketTimePeriod = useQuickSearchStore((s) => s.marketTimePeriod)
  const setMarketTimePeriod = useQuickSearchStore((s) => s.setMarketTimePeriod)
  const searching = useQuickSearchStore((s) => s.searching)
  const progress = useQuickSearchStore((s) => s.progress)
  const progressMsg = useQuickSearchStore((s) => s.progressMsg)
  const progressHistory = useQuickSearchStore((s) => s.progressHistory)
  const plan = useQuickSearchStore((s) => s.plan)
  const marketSignal = useQuickSearchStore((s) => s.marketSignal)
  const posts = useQuickSearchStore((s) => s.posts)
  const totalSearched = useQuickSearchStore((s) => s.totalSearched)
  const summary = useQuickSearchStore((s) => s.summary)
  const error = useQuickSearchStore((s) => s.error)
  const composerNotice = useQuickSearchStore((s) => s.composerNotice)
  const done = useQuickSearchStore((s) => s.done)
  const searchHistory = useQuickSearchStore((s) => s.searchHistory)
  const startQuickSearch = useQuickSearchStore((s) => s.startSearch)
  const stopQuickSearch = useQuickSearchStore((s) => s.stopSearch)
  const resetQuickSearch = useQuickSearchStore((s) => s.resetToSearch)
  const openQuickSearchHistoryItem = useQuickSearchStore((s) => s.openHistoryItem)
  const loadQuickSearchHistory = useQuickSearchStore((s) => s.loadHistory)
  const [queryReveal, setQueryReveal] = useState<{ text: string; key: number } | null>(null)
  const [examplesVisible, setExamplesVisible] = useState(false)
  const [settingsPanel, setSettingsPanel] = useState<'reddit' | 'sensor' | null>(null)
  const [postsExpanded, setPostsExpanded] = useState(false)
  const [summarySupplementExpanded, setSummarySupplementExpanded] = useState(false)

  const summaryRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const progressLinesRef = useRef<HTMLDivElement>(null)
  const clampedProgress = Math.min(100, Math.max(0, progress))
  const visibleProgress = searching ? Math.max(6, clampedProgress) : clampedProgress
  const hasResultContent = Boolean(plan || marketSignal || summary || error || posts.length > 0)
  const canShowResults = done || Boolean(error && !searching)
  const showingResults = canShowResults && hasResultContent
  const isMarketOnlyResult = Boolean(marketSignal && posts.length === 0 && (!plan || plan.subreddits.length === 0))
  const visiblePosts = postsExpanded ? posts : posts.slice(0, 2)
  const visibleProgressMessages = progressHistory.slice(-16)
  const summaryParts = splitQuickSearchSummary(summary)
  const structuredSummary = parseQuickSearchSummary(summary, posts)
  const isWorkflowResult = structuredSummary?.isWorkflow || /##\s*(?:流程证据不足|流程阶段|Workflow Evidence Insufficient|Workflow Stages)/i.test(summary)
  const extraEvidencePosts = moreEvidencePosts(posts, structuredSummary)
  const visibleExtraPosts = postsExpanded ? extraEvidencePosts : extraEvidencePosts.slice(0, 2)
  const quickSearchExamples = list('quickSearch.examples').map((example, index) => ({
    query: example,
    note: list('quickSearch.exampleNotes')[index] || '',
  }))
  const timeOptions = TIME_OPTIONS.map((item) => ({ ...item, display: isEnglish ? item.labelEn : item.label }))
  const heatOptions = HEAT_OPTIONS.map((item) => ({ ...item, display: isEnglish ? item.labelEn : item.label }))
  const marketTimeOptions = MARKET_TIME_OPTIONS.map((item) => ({
    ...item,
    display: isEnglish ? item.labelEn : item.label,
    description: isEnglish ? item.descEn : item.desc,
  }))

  useEffect(() => {
    inputRef.current?.focus()
    setExamplesVisible(false)
    let timer: number | undefined
    const raf = requestAnimationFrame(() => {
      timer = window.setTimeout(() => setExamplesVisible(true), 90)
    })
    return () => {
      cancelAnimationFrame(raf)
      if (timer !== undefined) window.clearTimeout(timer)
    }
  }, [])

  useEffect(() => {
    loadQuickSearchHistory()
  }, [loadQuickSearchHistory])

  useEffect(() => {
    if (!searching) return
    const el = progressLinesRef.current
    if (!el) return
    el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' })
  }, [progressHistory.length, searching])

  const handleQueryChange = useCallback((value: string) => {
    setQueryReveal(null)
    setQuickSearchQuery(value)
  }, [setQuickSearchQuery])

  const handleExampleClick = useCallback((example: string) => {
    setQuickSearchQuery(example)
    setQueryReveal({ text: example, key: Date.now() })
    inputRef.current?.focus()
    trackAnalyticsEvent('quick_search.example_click', { example_length: example.length })
  }, [setQuickSearchQuery])

  const handleSearch = useCallback(() => {
    setPostsExpanded(false)
    setSummarySupplementExpanded(false)
    setSettingsPanel(null)
    startQuickSearch()
  }, [startQuickSearch])

  const handleStop = useCallback(() => {
    stopQuickSearch()
  }, [stopQuickSearch])

  const handleBackToSearch = useCallback(() => {
    resetQuickSearch()
    setPostsExpanded(false)
    setSummarySupplementExpanded(false)
    setSettingsPanel(null)
    requestAnimationFrame(() => inputRef.current?.focus())
  }, [resetQuickSearch])

  const handleHistorySelect = useCallback((item: QuickSearchHistoryItem) => {
    setQueryReveal(null)
    setPostsExpanded(false)
    setSummarySupplementExpanded(false)
    setSettingsPanel(null)
    openQuickSearchHistoryItem(item)
  }, [openQuickSearchHistoryItem])

  const handleKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSearch()
    }
  }, [handleSearch])

  return (
    <div className="quick-search-view h-full flex flex-col">
      {/* Header */}
      {!showingResults && (
        <div className="quick-search-header-shell shrink-0 px-6 max-md:px-4 pt-5 max-md:pt-3 pb-2">
          <div className="quick-search-header-row">
            <div className="min-w-0">
              <h1 className="text-base font-bold break-words">
                <LocalizedText variant="roll" className="inline-block">{text('quickSearch.title')}</LocalizedText>
              </h1>
              <p className="page-header-subtitle">
                <LocalizedText variant="roll" className="inline-block">{text('quickSearch.subtitle')}</LocalizedText>
              </p>
            </div>
            <SearchHistoryTimeline items={searchHistory} onSelect={handleHistorySelect} />
          </div>
        </div>
      )}

	      {showingResults && (
	        <div className="shrink-0 px-6 max-md:px-4 pt-5 max-md:pt-3 pb-3">
	          <button
	            type="button"
	            onClick={handleBackToSearch}
            className="h-9 px-3 rounded-full border border-border/25 bg-white/65 text-[12px] font-medium text-text/72 hover:bg-white/90 hover:text-text active:scale-95 transition-all flex items-center gap-1.5"
	          >
	            <ArrowLeft size={14} />
	            {text('quickSearch.backToSearch')}
	          </button>
	          {searching && (
	            <div className="quick-search-result-progress">
	              <div className="flex items-center justify-between gap-3">
	                <span className="truncate">{progressMsg || text('quickSearch.continuing')}</span>
	                <span className="tabular-nums shrink-0">{Math.round(clampedProgress)}%</span>
	              </div>
	              <div>
	                <motion.span
	                  initial={{ width: '6%' }}
	                  animate={{ width: `${visibleProgress}%` }}
		                  transition={{ duration: 0.85, ease: 'linear' }}
		                />
	              </div>
	            </div>
	          )}
	        </div>
	      )}

      {/* Search / progress */}
      {!showingResults && (
        <div className="quick-search-composer-section flex-1 min-h-0 px-6 max-md:px-4 pt-[4.5rem] max-md:pt-12 pb-5">
          <AnimatePresence mode="wait" initial={false}>
            {!searching ? (
            <motion.div
              key="quick-search-composer"
              className="quick-search-composer-shell quick-search-composer-shell--search"
              initial={{ opacity: 0, y: 8, scale: 0.98 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, y: -10, scale: 0.985 }}
              transition={{ duration: 0.22, ease: 'easeOut' }}
            >
              <div className="quick-search-composer-spacer" />
              <div className="quick-search-composer-main">
                <div className="quick-search-glow-stage">
                  <img
                    src={themeMode === 'dark' ? '/ai-antenna.png' : '/ai-antenna (1).png'}
                    alt=""
                    className="quick-search-antenna"
                  />
                  <div className="quick-search-glass">
                    <div className="relative min-h-[84px] px-5 max-md:px-4 pt-4 pb-1">
                      <img src="/star2.png" alt="" className="fetch-input-star fetch-input-star--large absolute left-5 max-md:left-4 top-[20px]" />
                        <textarea
                          ref={inputRef}
                          value={query}
                          onChange={(e) => handleQueryChange(e.target.value)}
                          onKeyDown={handleKeyDown}
                          placeholder={text('quickSearch.placeholder')}
                          rows={1}
                          className="block w-full h-16 resize-none bg-transparent pl-7 pr-11 pt-1 text-[14px] max-md:text-[13px] leading-6 text-text placeholder:text-[14px] max-md:placeholder:text-[13px] placeholder:text-muted/36 focus:outline-none disabled:opacity-60"
                          style={queryReveal ? { color: 'transparent', caretColor: 'var(--color-text)' } : undefined}
                        />
                        {queryReveal && (
                          <div className="quick-search-input-reveal">
                            <SplitText
                              key={`quick-query-reveal-${queryReveal.key}`}
                              text={queryReveal.text}
                              delay={13}
                              duration={0.38}
                              splitType="chars"
                              from={{ opacity: 0, y: 14 }}
                              to={{ opacity: 1, y: 0 }}
                              textAlign="left"
                              tag="p"
                            />
                          </div>
                        )}
                        {query && (
                          <button
                            onClick={() => handleQueryChange('')}
                            className="absolute right-5 top-4 w-9 h-9 rounded-xl flex items-center justify-center text-muted/40 hover:text-muted hover:bg-white/60 transition-colors"
                            aria-label={text('common.clear')}
                          >
                          <X size={14} />
                        </button>
                      )}
                    </div>

	                    <div className="px-5 max-md:px-4 pb-4">
	                      <div className="flex items-center justify-between gap-3">
	                        <div className="quick-search-settings-triggers">
	                          <button
	                            type="button"
	                            onClick={() => setSettingsPanel(settingsPanel === 'reddit' ? null : 'reddit')}
	                            className={`quick-search-settings-icon-button ${settingsPanel === 'reddit' ? 'quick-search-settings-icon-button--active' : ''}`}
	                            aria-label={text('quickSearch.redditSettings')}
	                            title={text('quickSearch.redditSettings')}
	                          >
	                            <img src="/reddit_line.png" alt="" className="quick-search-settings-icon quick-search-settings-icon--reddit" />
	                          </button>
	                          <button
	                            type="button"
	                            onClick={() => setSettingsPanel(settingsPanel === 'sensor' ? null : 'sensor')}
	                            className={`quick-search-settings-icon-button ${settingsPanel === 'sensor' ? 'quick-search-settings-icon-button--active' : ''}`}
	                            aria-label={text('quickSearch.sensorSettings')}
	                            title={text('quickSearch.sensorSettings')}
	                          >
	                            <img src="/sensortower.png" alt="" className="quick-search-settings-icon quick-search-settings-icon--sensor" />
	                          </button>
	                        </div>
	                        <button
	                          onClick={handleSearch}
	                          className="quick-search-submit-bead"
	                          aria-label={text('common.search')}
	                        >
	                          <img src="/arrow-up2.png" alt="" className="relative z-10 w-[25px] h-[25px] brightness-0 invert opacity-95" />
	                        </button>
	                      </div>

	                      <AnimatePresence>
	                        {settingsPanel && (
	                          <motion.div
	                            initial={{ height: 0, opacity: 0, y: -4 }}
	                            animate={{ height: 'auto', opacity: 1, y: 0 }}
	                            exit={{ height: 0, opacity: 0, y: -4 }}
	                            transition={{ duration: 0.18, ease: 'easeOut' }}
	                            className="overflow-hidden"
	                          >
	                            <div className="quick-search-settings-panel">
	                              <div className="quick-search-settings-divider" />
	                              {settingsPanel === 'reddit' ? (
	                                <>
	                                  <p className="quick-search-settings-title">{text('quickSearch.redditSettings')}</p>
	                                  <div className="quick-search-settings-content">
	                                    <div className="quick-search-settings-row">
	                                      <span>{text('quickSearch.period')}</span>
	                                      <div>
                                        {timeOptions.map(t => (
                                          <button
                                            type="button"
                                            key={t.id}
                                            onClick={() => setTimePeriod(t.id)}
                                            className={`quick-search-settings-pill ${timePeriod === t.id ? 'quick-search-settings-pill--active' : ''}`}
                                          >
                                            {t.display}
                                          </button>
                                        ))}
	                                      </div>
	                                    </div>
	                                    <div className="quick-search-settings-row">
	                                      <span>{text('quickSearch.heat')}</span>
	                                      <div>
                                        {heatOptions.map(h => (
                                          <button
                                            type="button"
                                            key={h.id}
                                            onClick={() => setMinScore(h.id)}
                                            className={`quick-search-settings-pill ${minScore === h.id ? 'quick-search-settings-pill--active' : ''}`}
                                          >
                                            {h.display}
                                          </button>
                                        ))}
	                                      </div>
	                                    </div>
	                                  </div>
	                                </>
	                              ) : (
	                                <>
	                                  <div className="quick-search-settings-content">
	                                    <div className="quick-search-settings-row quick-search-settings-row--stacked">
	                                      <span>{text('quickSearch.marketScale')}</span>
	                                      <div>
                                        {marketTimeOptions.map(option => (
                                          <button
                                            type="button"
                                            key={option.id}
                                            onClick={() => setMarketTimePeriod(option.id)}
                                            className={`quick-search-market-period ${marketTimePeriod === option.id ? 'quick-search-market-period--active' : ''}`}
                                          >
                                            <strong>{option.display}</strong>
                                            <small>{option.description}</small>
                                          </button>
                                        ))}
	                                      </div>
	                                    </div>
	                                  </div>
	                                </>
	                              )}
	                            </div>
	                          </motion.div>
	                        )}
	                      </AnimatePresence>
                    </div>
                  </div>
                </div>
                <div className="quick-search-examples quick-search-examples--scrollable">
	                  <p className="quick-search-examples-title">{text('quickSearch.recommended')}</p>
                  <div className="quick-search-examples-list">
                    {quickSearchExamples.map((example, index) => (
                      <motion.button
                        key={example.query}
                        type="button"
                        onClick={() => handleExampleClick(example.query)}
                        className="quick-search-example-pill"
                        initial={false}
                        animate={{
                          opacity: examplesVisible ? 1 : 0,
                          y: examplesVisible ? 0 : 12,
                          filter: examplesVisible ? 'blur(0px)' : 'blur(3px)',
                        }}
                        transition={{ delay: examplesVisible ? 0.12 + index * 0.11 : 0, duration: 0.46, ease: [0.22, 1, 0.36, 1] }}
                      >
                        <span className="quick-search-example-copy">
                          <span className="quick-search-example-query">{example.query}</span>
                          <span className="quick-search-example-note">{example.note}</span>
                        </span>
                        <ChevronRight size={18} strokeWidth={2.5} className="shrink-0" />
                      </motion.button>
                    ))}
                  </div>
                </div>
              </div>
            </motion.div>
            ) : (
            <motion.div
              key="quick-search-progress"
              className="quick-search-progress-card mx-auto max-w-[660px] px-5 py-5 max-md:px-4"
              initial={{ opacity: 0, y: 12, scale: 0.985 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, y: -6, scale: 0.99 }}
              transition={{ duration: 0.26, ease: 'easeOut' }}
            >
              <div className="flex items-start justify-between gap-4">
                <div className="min-w-0">
                  <div className="flex items-center gap-2 text-[14px] font-semibold text-text/80">
                    <Loader2 size={15} className="animate-spin text-accent/70" />
                    <span>{text('quickSearch.searching')}</span>
                  </div>
                  <p className="mt-2 text-[12px] leading-relaxed text-muted/70 truncate">
                    {progressMsg || text('quickSearch.preparing')}
                  </p>
                </div>
                <button
                  onClick={handleStop}
                  className="h-9 px-4 rounded-full bg-signal/10 text-signal text-[12px] font-semibold hover:bg-signal/18 active:scale-95 transition-all flex items-center gap-1.5 shrink-0"
                >
                  <X size={13} /> {text('common.stop')}
                </button>
              </div>

		              <div className="mt-5">
		                <div className="quick-search-progress-meter">
		                  <div className="quick-search-progress-track">
		                    <motion.div
		                      className="quick-search-progress-fill"
		                      initial={{ width: '6%' }}
		                      animate={{ width: `${visibleProgress}%` }}
		                      transition={{ duration: 0.85, ease: 'linear' }}
		                    />
		                  </div>
		                  <span className="quick-search-progress-percent">{Math.round(clampedProgress)}%</span>
		                </div>
		                <div ref={progressLinesRef} className="quick-search-progress-lines">
		                  <AnimatePresence initial={false}>
		                    {visibleProgressMessages.map((msg, index) => {
		                      const isCurrent = index === visibleProgressMessages.length - 1
		                      return (
		                        <motion.div
		                          key={`${index}-${msg}`}
		                          initial={{ opacity: 0, y: 8 }}
		                          animate={{ opacity: 1, y: 0 }}
		                          exit={{ opacity: 0, y: -4 }}
		                          transition={{ duration: 0.24, ease: 'easeOut' }}
		                          className={`quick-search-progress-line ${isCurrent ? 'quick-search-progress-line--current' : ''}`}
		                        >
		                          <span />
		                          {isCurrent ? (
		                            <span className="quick-search-progress-shimmer">{msg}</span>
		                          ) : (
		                            <em>{msg}</em>
		                          )}
		                        </motion.div>
		                      )
		                    })}
		                  </AnimatePresence>
		                </div>
		                <p className="quick-search-progress-query">{query.trim()}</p>
              </div>
            </motion.div>
            )}
          </AnimatePresence>
        </div>
      )}

      {/* Main content */}
      {showingResults && (
      <div className="flex-1 min-h-0 overflow-y-auto px-6 max-md:px-4 space-y-5 scrollbar-auto pt-2 pb-6">
        {/* Sensor Tower market signal */}
        {marketSignal && <MarketAnalysisPanel signal={marketSignal} />}

        {/* AI Summary */}
        {summary && !isMarketOnlyResult && (
	          <motion.div
	            ref={summaryRef}
	            initial={{ opacity: 0, y: 8 }}
	            animate={{ opacity: 1, y: 0 }}
	            className="quick-search-analysis-card"
		          >
                {structuredSummary ? (
                  <QuickSearchStructuredSummary
                    structured={structuredSummary}
                    posts={posts}
                    totalSearched={totalSearched}
                    supplementExpanded={summarySupplementExpanded}
                    onToggleSupplement={() => setSummarySupplementExpanded(!summarySupplementExpanded)}
                  />
	                ) : (
	                  <>
	                    <div className="quick-search-analysis">
	                      <QuickSearchSummaryContent text={summaryParts.main} posts={posts} />
	                    </div>
	                    {summaryParts.supplement && (
	                      <div className="quick-search-supplement">
	                        <button
	                          type="button"
	                          className="quick-search-supplement-toggle"
	                          onClick={() => setSummarySupplementExpanded(!summarySupplementExpanded)}
	                        >
	                          {summarySupplementExpanded ? <ChevronUp size={13} /> : <ChevronDown size={13} />}
	                          {summarySupplementExpanded ? text('quickSearch.collapseSupplement') : text('quickSearch.moreSupplement')}
	                        </button>
	                        <AnimatePresence initial={false}>
	                          {summarySupplementExpanded && (
	                            <motion.div
	                              initial={{ height: 0, opacity: 0 }}
	                              animate={{ height: 'auto', opacity: 1 }}
	                              exit={{ height: 0, opacity: 0 }}
	                              transition={{ duration: 0.2, ease: 'easeOut' }}
	                              className="quick-search-supplement-body"
	                            >
	                              <div className="quick-search-analysis">
	                                <Markdown remarkPlugins={[remarkGfm]}>{summaryParts.supplement}</Markdown>
	                              </div>
	                            </motion.div>
	                          )}
	                        </AnimatePresence>
	                      </div>
	                    )}
	                  </>
	                )}
	          </motion.div>
        )}

        {/* Error */}
        {error && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            className="bg-red-50 rounded-xl p-4 text-sm text-red-600"
          >
            {error}
          </motion.div>
        )}

        {/* Posts list */}
	        {((!structuredSummary && posts.length > 0) || (structuredSummary && extraEvidencePosts.length > 0)) && (
	          <div className="quick-search-evidence-section">
	            <div className="flex items-center gap-2 mb-3">
	              <TrendingUp size={13} className="text-accent/50" />
	              <span className="text-xs font-medium text-text/70">
                  {structuredSummary
                    ? text('quickSearch.moreEvidencePosts')
                    : text(isWorkflowResult ? 'quickSearch.workflowEvidence' : 'quickSearch.hotspotEvidence')}
                </span>
	              <span className="text-[11px] text-muted/40 tabular-nums">
                  {structuredSummary ? extraEvidencePosts.length : posts.length} {text('quickSearch.evidenceCountSuffix')}
                </span>
	            </div>
	            <div className="space-y-2">
	              {(structuredSummary ? visibleExtraPosts : visiblePosts).map((post, i) => (
	                <PostCard key={post.url + i} post={post} index={i} />
	              ))}
	            </div>
	            {(structuredSummary ? extraEvidencePosts.length : posts.length) > 2 && (
	              <button
	                type="button"
	                onClick={() => setPostsExpanded(!postsExpanded)}
	                className="quick-search-evidence-toggle"
	              >
	                {postsExpanded ? (
	                  <>
	                    <ChevronUp size={13} />
	                    {text('quickSearch.collapseEvidence')}
	                  </>
	                ) : (
	                  <>
	                    <ChevronDown size={13} />
	                    {text('quickSearch.expandMoreEvidence').replace('{count}', String((structuredSummary ? extraEvidencePosts.length : posts.length) - 2))}
	                  </>
	                )}
	              </button>
	            )}
	          </div>
	        )}
      </div>
      )}
      <AnimatePresence>
        {composerNotice && !showingResults && (
          <motion.div
            key="quick-search-composer-notice"
            className="quick-search-composer-notice"
            initial={{ opacity: 0, y: 10, x: '-50%' }}
            animate={{ opacity: 1, y: 0, x: '-50%' }}
            exit={{ opacity: 0, y: 8, x: '-50%' }}
            transition={{ duration: 0.2, ease: 'easeOut' }}
          >
            {composerNotice}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}

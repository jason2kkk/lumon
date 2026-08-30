import { motion, type Variants } from 'framer-motion'
import { RefreshCw } from 'lucide-react'
import { LogosCarousel } from './animations'
import { useI18n } from '../i18n'

// ============================================================
// Markdown 报告 → 文档排版渲染
// ============================================================

interface DocBlock {
  type: 'h1' | 'h2' | 'h3' | 'h4' | 'paragraph' | 'blockquote' | 'list' | 'table' | 'code' | 'hr' | 'meta'
  content: string
  items?: string[]
  ordered?: boolean
  rows?: string[][]
  header?: string[]
}

function parseMarkdownToBlocks(md: string): DocBlock[] {
  const lines = md.split('\n')
  const blocks: DocBlock[] = []
  let i = 0

  while (i < lines.length) {
    const line = lines[i]

    if (line.trim() === '') { i++; continue }

    if (line.trim() === '---') {
      blocks.push({ type: 'hr', content: '' })
      i++; continue
    }

    if (line.startsWith('# ')) {
      blocks.push({ type: 'h1', content: line.slice(2).trim() })
      i++; continue
    }
    if (line.startsWith('## ')) {
      blocks.push({ type: 'h2', content: line.slice(3).trim() })
      i++; continue
    }
    if (line.startsWith('#### ')) {
      blocks.push({ type: 'h4', content: line.slice(5).trim() })
      i++; continue
    }
    if (line.startsWith('### ')) {
      blocks.push({ type: 'h3', content: line.slice(4).trim() })
      i++; continue
    }

    if (line.startsWith('```')) {
      const codeLang = line.slice(3).trim()
      const codeLines: string[] = []
      i++
      while (i < lines.length && !lines[i].startsWith('```')) {
        codeLines.push(lines[i])
        i++
      }
      i++
      blocks.push({ type: 'code', content: codeLines.join('\n'), items: codeLang ? [codeLang] : undefined })
      continue
    }

    if (line.startsWith('|') && line.includes('|', 1)) {
      const tableLines: string[] = [line]
      i++
      while (i < lines.length && lines[i].startsWith('|')) {
        tableLines.push(lines[i])
        i++
      }
      const parseRow = (r: string) => r.split('|').slice(1, -1).map(c => c.trim())
      const header = parseRow(tableLines[0])
      const dataLines = tableLines.filter((_, idx) => {
        if (idx === 0) return false
        if (idx === 1 && /^\|[\s\-:|]+\|$/.test(tableLines[idx])) return false
        return true
      })
      blocks.push({ type: 'table', content: '', header, rows: dataLines.map(parseRow) })
      continue
    }

    if (line.startsWith('> ')) {
      const quoteLines: string[] = []
      while (i < lines.length && (lines[i].startsWith('> ') || lines[i].startsWith('>'))) {
        quoteLines.push(lines[i].replace(/^>\s?/, ''))
        i++
      }
      blocks.push({ type: 'blockquote', content: quoteLines.join('\n') })
      continue
    }

    if (/^[-*]\s/.test(line) || /^\d+\.\s/.test(line)) {
      const ordered = /^\d+\./.test(line)
      const items: string[] = []
      while (i < lines.length && (/^[-*]\s/.test(lines[i]) || /^\d+\.\s/.test(lines[i]))) {
        items.push(lines[i].replace(/^[-*]\s+/, '').replace(/^\d+\.\s+/, ''))
        i++
      }
      blocks.push({ type: 'list', content: '', items, ordered })
      continue
    }

    if (line.startsWith('**') && line.includes(':**')) {
      const metaMatch = line.match(/^\*\*(.+?)\*\*\s*(.*)$/)
      if (metaMatch) {
        blocks.push({ type: 'meta', content: line })
        i++; continue
      }
    }

    const paraLines: string[] = [line]
    i++
    while (i < lines.length && lines[i].trim() !== '' && !lines[i].startsWith('#') && !lines[i].startsWith('|') && !lines[i].startsWith('>') && !lines[i].startsWith('```') && lines[i].trim() !== '---' && !/^[-*]\s/.test(lines[i]) && !/^\d+\.\s/.test(lines[i])) {
      paraLines.push(lines[i])
      i++
    }
    blocks.push({ type: 'paragraph', content: paraLines.join(' ') })
  }

  return blocks
}

function safeReportUrl(raw: string): string | null {
  try {
    const parsed = new URL(raw.trim(), window.location.origin)
    return parsed.protocol === 'http:' || parsed.protocol === 'https:' ? parsed.href : null
  } catch {
    return null
  }
}

function InlineText({ text }: { text: string }) {
  const { text: t } = useI18n()
  const parts = text.split(/(\*\*[^*]+\*\*|\*[^*]+\*|`[^`]+`|\[[^\]]+\]\([^)]+\)|https?:\/\/[^\s)<>]+|📎|📊|👤|📅|🟢|🟡|🔴)/)
  return (
    <>
      {parts.map((part, i) => {
        if (!part) return null
        if (part.startsWith('**') && part.endsWith('**'))
          return <strong key={i} className="font-semibold text-text">{part.slice(2, -2)}</strong>
        if (part.startsWith('*') && part.endsWith('*'))
          return <em key={i} className="italic text-text/70">{part.slice(1, -1)}</em>
        if (part.startsWith('`') && part.endsWith('`'))
          return <code key={i} className="text-[12px] bg-accent/8 px-1.5 py-0.5 rounded font-mono text-accent">{part.slice(1, -1)}</code>
        const linkMatch = part.match(/^\[([^\]]+)\]\(([^)]+)\)$/)
        if (linkMatch) {
          const label = linkMatch[1]
          const url = linkMatch[2]
          const safeUrl = safeReportUrl(url)
          if (!safeUrl) return <span key={i}>{label}</span>
          const iconOnly = /^(↗|🔗|icon)$/i.test(label.trim())
          if (iconOnly) {
            return (
              <a key={i} href={safeUrl} target="_blank" rel="noopener noreferrer" className="inline-flex items-center justify-center w-6 h-6 rounded-full text-blue-600 hover:bg-blue-50 transition-colors" aria-label={t('common.openLink')}>
                <img src="/link_3_line.png" alt="" className="w-3.5 h-3.5 opacity-70 shrink-0" />
              </a>
            )
          }
          const isGenericLabel = /^(官网|App Store|Play Store|链接|link|website)$/i.test(label)
          const displayText = isGenericLabel ? url : label
          return <a key={i} href={safeUrl} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-0.5 text-blue-600 hover:underline break-all"><img src="/link_3_line.png" alt="" className="inline w-[1em] h-[1em] opacity-60 shrink-0" />{displayText}</a>
        }
        if (/^https?:\/\//.test(part)) {
          const safeUrl = safeReportUrl(part)
          if (!safeUrl) return <span key={i}>{part}</span>
          return <a key={i} href={safeUrl} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-0.5 text-blue-600 hover:underline break-all"><img src="/link_3_line.png" alt="" className="inline w-[1em] h-[1em] opacity-60 shrink-0" />{part}</a>
        }
        return <span key={i}>{part}</span>
      })}
    </>
  )
}

const TEXT_LOGOS = [
  'Reddit', 'Claude', 'OpenAI', 'FEMWC', 'JTBD', 'HackerNews', 'Tavily',
  'Reddit', 'Claude', 'OpenAI', 'FEMWC', 'JTBD', 'HackerNews', 'Tavily',
]

function MarkdownReportView({
  content,
  onRefreshCompetitors,
  refreshingCompetitors,
  competitorRefreshMessage,
}: {
  content: string
  onRefreshCompetitors?: () => void
  refreshingCompetitors?: boolean
  competitorRefreshMessage?: string
}) {
  const { text } = useI18n()
  const blocks = parseMarkdownToBlocks(content)

  const blockVariants: Variants = {
    hidden: { opacity: 0, y: 14 },
    visible: (i: number) => ({
      opacity: 1,
      y: 0,
      transition: {
        duration: 0.35,
        delay: Math.min(i * 0.04, 1.2),
        ease: [0.25, 0.1, 0.25, 1] as [number, number, number, number],
      },
    }),
  }

  return (
    <div className="doc-report">
      {blocks.map((block, i) => {
        const el = (() => {
        switch (block.type) {
          case 'h1':
            return (
              <div key={i} className="mb-6 mt-2">
                <h1 className="text-[22px] max-md:text-[18px] font-bold text-text tracking-tight">{block.content}</h1>
                <div className="mt-2 h-[2px] bg-gradient-to-r from-accent/40 to-transparent rounded-full" />
              </div>
            )
          case 'h2':
            return (
              <div key={i} className="mt-8 mb-3">
                <div className="flex items-center gap-2.5">
                  <div className="w-1 h-5 bg-accent rounded-full shrink-0" />
                  <h2 className="text-[16px] font-bold text-text">{block.content}</h2>
                </div>
              </div>
            )
          case 'h3':
            return <h3 key={i} className="text-[14px] font-semibold text-text/90 mt-5 mb-2">{block.content}</h3>
          case 'h4':
            return <h4 key={i} className="text-[13px] font-semibold text-text/80 mt-4 mb-1.5">{block.content}</h4>
          case 'hr':
            return <hr key={i} className="my-6 border-border/20" />
          case 'meta':
            return (
              <div key={i} className="text-[13px] text-text/70 leading-relaxed mb-1">
                <InlineText text={block.content} />
              </div>
            )
          case 'paragraph':
            return (
              <p key={i} className="text-[13px] text-text/75 leading-[1.8] mb-3">
                <InlineText text={block.content} />
              </p>
            )
          case 'blockquote':
            return (
              <blockquote key={i} className="my-3 pl-4 border-l-[3px] border-accent/30 bg-accent/[0.03] rounded-r-xl py-2.5 pr-4">
                <div className="text-[13px] text-text/70 italic leading-relaxed">
                  <InlineText text={block.content} />
                </div>
              </blockquote>
            )
          case 'list':
            if (block.ordered) {
              return (
                <ol key={i} className="mb-4 space-y-1.5 pl-5">
                  {block.items?.map((item, j) => (
                    <li key={j} className="flex gap-2 text-[13px] text-text/75 leading-relaxed">
                      <span className="text-accent/60 font-medium shrink-0 w-4 text-right">{j + 1}.</span>
                      <span className="flex-1 min-w-0"><InlineText text={item} /></span>
                    </li>
                  ))}
                </ol>
              )
            }
            return (
              <ul key={i} className="mb-4 space-y-1.5 pl-5">
                {block.items?.map((item, j) => {
                  const emojiMatch = item.match(/^(🟢|🟡|🔴|📎|📊|👤|📅|✅|❌|⚠️)\s*/)
                  const hasEmoji = !!emojiMatch
                  const emoji = emojiMatch?.[1]
                  const rest = hasEmoji ? item.slice(emojiMatch![0].length) : item
                  return (
                    <li key={j} className="flex gap-2 text-[13px] text-text/75 leading-relaxed">
                      <span className="shrink-0 w-4 text-center">{hasEmoji ? emoji : <span className="text-accent/50 inline-block mt-[5px] text-[8px]">●</span>}</span>
                      <span className="flex-1 min-w-0"><InlineText text={rest} /></span>
                    </li>
                  )
                })}
              </ul>
            )
          case 'table': {
            const isCompetitorTable = Boolean(
              block.header
              && (
                (
                  block.header.includes('竞品名称')
                  && block.header.includes('近30天收入')
                  && block.header.includes('SensorTower链接')
                )
                || (
                  block.header.includes('Competitor')
                  && block.header.includes('Last 30D Revenue')
                  && block.header.includes('SensorTower')
                )
              ),
            )
            return (
              <div key={i} className="my-4 rounded-xl border border-border/30 overflow-hidden">
                {isCompetitorTable && onRefreshCompetitors && (
                  <div className="flex items-center justify-end gap-2 border-b border-border/20 bg-bg/40 px-3 py-2">
                    {competitorRefreshMessage && (
                      <span className={`text-[11px] ${competitorRefreshMessage.includes('完成') || competitorRefreshMessage.toLowerCase().includes('complete') ? 'text-emerald-600' : 'text-signal'}`}>
                        {competitorRefreshMessage}
                      </span>
                    )}
                    <button
                      type="button"
                      onClick={onRefreshCompetitors}
                      disabled={refreshingCompetitors}
                      className="inline-flex items-center gap-1.5 h-7 px-2.5 rounded-lg text-[11px] font-medium text-text/65 bg-white/70 border border-border/35 hover:border-accent/35 hover:text-accent disabled:opacity-60 transition-all"
                    >
                      <RefreshCw size={12} className={refreshingCompetitors ? 'animate-spin' : ''} />
                      {refreshingCompetitors ? text('reportView.querying') : text('reportView.refreshCompetitors')}
                    </button>
                  </div>
                )}
                <div className="overflow-x-auto">
                  <table className="w-max min-w-full text-[12px]">
                    {block.header && (
                      <thead>
                        <tr className="bg-[#f7f8f9]">
                          {block.header.map((h, j) => (
                            <th key={j} className="text-left py-2.5 px-4 font-semibold text-text/80 border-b border-border/30 whitespace-nowrap">
                              {h}
                            </th>
                          ))}
                        </tr>
                      </thead>
                    )}
                    <tbody>
                      {block.rows?.map((row, j) => (
                        <tr key={j} className="border-b border-border/15 last:border-0 hover:bg-accent/[0.02] transition-colors">
                          {row.map((cell, k) => (
                            <td key={k} className="py-2 px-4 text-text/70">
                              <InlineText text={cell} />
                            </td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )
          }
          case 'code':
            return (
              <div key={i} className="my-4 rounded-xl bg-[#f7f8f9] border border-border/20 p-4 overflow-x-auto">
                <pre className="text-[12px] text-text/70 font-mono leading-relaxed whitespace-pre-wrap">{block.content}</pre>
              </div>
            )
          default:
            return null
        }
        })()
        if (!el) return null
        return (
          <motion.div
            key={i}
            custom={i}
            initial="hidden"
            animate="visible"
            variants={blockVariants}
          >
            {el}
          </motion.div>
        )
      })}

      {/* 底部 Powered by logos */}
      <div className="mt-16 mb-8 pt-8 border-t border-border/15">
        <p className="text-[11px] text-muted/40 text-center mb-6 tracking-[0.2em] uppercase">Powered by</p>
        <LogosCarousel count={7} stagger={0.1} interval={3000} className="gap-10">
          {TEXT_LOGOS.map((name, i) => (
            <span key={i} className="text-[15px] font-semibold text-text/70 tracking-tight select-none whitespace-nowrap" style={{ fontFamily: "'Sora', system-ui, sans-serif" }}>
              {name}
            </span>
          ))}
        </LogosCarousel>
      </div>
    </div>
  )
}

// ============================================================
// JSON 报告渲染（保留旧格式兼容）
// ============================================================

interface ReportJson {
  verdict?: string
  verdict_reason?: string
  product_track?: string
  one_liner?: string
  background?: string
  would_start_today?: string
  requirement?: string
  jtbd?: string
  pain_point?: string
  root_cause?: string
  target_users?: string
  user_mindset?: {
    emotional_phase?: string
    struggling_moment?: string
    current_workarounds?: string[]
    identity_connection?: string
  }
  why_now?: string
  is_real_need?: boolean
  feasibility?: string
  ai_fit?: string
  ai_fit_reason?: string
  non_ai_alternative?: string
  femwc_scores?: Record<string, number>
  femwc_before?: Record<string, number>
  femwc_after?: Record<string, number>
  score_changes?: string
  market_size?: string
  business_model?: string
  verbatim_quotes?: { quote: string; source: string; evidence_type?: string }[]
  competitors?: {
    name: string
    description?: string
    revenue_display?: string
    downloads_display?: string
    strengths?: string[]
    weaknesses?: string[]
    store_url?: string
  }[]
  competitor_gap_matrix?: {
    pain_point: string
    competitors: Record<string, string>
    unserved: boolean
  }[]
  key_debates?: {
    topic: string
    analyst_position: string
    user_position: string
    resolution: string
  }[]
  suggested_features?: {
    must_have?: string[]
    nice_to_have?: string[]
    v1_core?: string[]
    v1_nice?: string[]
    v2_future?: string[]
  }
  smoke_tests?: {
    type: string
    where: string
    success_threshold: string
    what_if_fail?: string
  }[]
  risks?: string[]
  growth_potential?: string
  next_angles?: {
    angle: string
    reasoning: string
    supporting_quote?: string
  }[]
  next_steps?: string[]
  debate_summary?: string
}

function repairTruncatedJson(text: string): string {
  let s = text.trim()
  let openBraces = 0, openBrackets = 0, inString = false, escape = false
  for (let i = 0; i < s.length; i++) {
    const c = s[i]
    if (escape) { escape = false; continue }
    if (c === '\\') { escape = true; continue }
    if (c === '"') { inString = !inString; continue }
    if (inString) continue
    if (c === '{') openBraces++
    else if (c === '}') openBraces--
    else if (c === '[') openBrackets++
    else if (c === ']') openBrackets--
  }
  if (inString) s += '"'
  while (openBrackets > 0) { s += ']'; openBrackets-- }
  while (openBraces > 0) { s += '}'; openBraces-- }
  return s
}

function extractJson(text: string): ReportJson | null {
  if (!text) return null
  try {
    return JSON.parse(text.trim())
  } catch { /* continue */ }

  const m = text.match(/```(?:json)?\s*\n?([\s\S]*?)\n?\s*```/)
  if (m) {
    try { return JSON.parse(m[1].trim()) } catch { /* continue */ }
  }

  const first = text.indexOf('{')
  const last = text.lastIndexOf('}')
  if (first !== -1 && last > first) {
    try { return JSON.parse(text.slice(first, last + 1)) } catch { /* continue */ }
  }

  if (first !== -1) {
    try {
      const repaired = repairTruncatedJson(text.slice(first))
      return JSON.parse(repaired)
    } catch { /* continue */ }
  }

  return null
}

const VERDICT_COLOR: Record<string, string> = {
  GO: 'bg-green-100 text-green-800 border-green-200',
  'NO-GO': 'bg-red-100 text-red-800 border-red-200',
  CONDITIONAL: 'bg-gray-100 text-gray-700 border-gray-200',
}
const DIMS: [string, string][] = [
  ['F', 'fetch.frequency'],
  ['E', 'fetch.emotion'],
  ['M', 'fetch.market'],
  ['W', 'fetch.payment'],
  ['C', 'fetch.competition'],
]

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <p className="text-sm font-semibold mb-2">{title}</p>
      {children}
    </div>
  )
}

function JsonReportView({ report }: { report: string | Record<string, unknown> }) {
  const { text } = useI18n()
  const rj: ReportJson | null =
    typeof report === 'string' ? extractJson(report) : (report as ReportJson)

  if (!rj) {
    return (
      <div className="text-sm whitespace-pre-wrap">
        {typeof report === 'string' ? report : JSON.stringify(report, null, 2)}
      </div>
    )
  }

  const verdictKey = (rj.verdict || '').toUpperCase()
  const feasMap: Record<string, string> = {
    high: text('reportView.high'),
    medium: text('reportView.medium'),
    low: text('reportView.low'),
  }
  const aiFitMap: Record<string, string> = {
    strong: text('reportView.strong'),
    moderate: text('reportView.moderate'),
    weak: text('reportView.weak'),
  }
  const gapValueText = (value: string) => {
    if (value === 'Unmet') return text('reportView.unmet')
    if (value === 'Partial') return text('reportView.partial')
    return value
  }

  return (
    <div className="space-y-5">
      <div>
        <div className="flex items-center gap-3 mb-2">
          {verdictKey && (
            <span className={`text-xs font-bold px-3 py-1 rounded-full border ${VERDICT_COLOR[verdictKey] || 'bg-gray-100 text-gray-700 border-gray-200'}`}>
              {verdictKey}
            </span>
          )}
          <h3 className="text-lg max-md:text-base font-bold flex-1">{rj.requirement || text('reportView.summaryFallback')}</h3>
        </div>
        {rj.verdict_reason && (
          <p className="text-xs text-muted">{rj.verdict_reason}</p>
        )}
      </div>

      <div className="grid grid-cols-4 max-md:grid-cols-2 gap-3">
        <div className="bg-bg rounded-xl p-3.5 text-center">
          <p className="text-[10px] text-muted font-medium">{text('reportView.needAuthenticity')}</p>
          <p className="text-sm font-bold mt-1">
            {rj.is_real_need === true ? text('reportView.realNeed') : rj.is_real_need === false ? text('reportView.pseudoNeed') : text('reportView.pending')}
          </p>
        </div>
        <div className="bg-bg rounded-xl p-3.5 text-center">
          <p className="text-[10px] text-muted font-medium">{text('reportView.feasibility')}</p>
          <p className="text-sm font-bold mt-1">
            {feasMap[rj.feasibility || ''] || rj.feasibility || 'N/A'}
          </p>
        </div>
        <div className="bg-bg rounded-xl p-3.5 text-center">
          <p className="text-[10px] text-muted font-medium">{text('reportView.aiFit')}</p>
          <p className="text-sm font-bold mt-1">
            {aiFitMap[rj.ai_fit || ''] || rj.ai_fit || 'N/A'}
          </p>
        </div>
        <div className="bg-bg rounded-xl p-3.5 text-center">
          <p className="text-[10px] text-muted font-medium">FEMWC</p>
          <p className="text-sm font-bold mt-1">
            {(rj.femwc_scores?.total ?? rj.femwc_after?.total) != null
              ? `${(rj.femwc_scores?.total ?? rj.femwc_after?.total)?.toFixed(2)}/5.00`
              : '?/5.00'}
          </p>
        </div>
      </div>

      {rj.background && (
        <Section title={text('reportView.marketBackground')}>
          <p className="text-sm text-text/80">{rj.background}</p>
        </Section>
      )}

      {rj.would_start_today && (
        <Section title={text('reportView.startToday')}>
          <p className="text-sm text-text/80">{rj.would_start_today}</p>
        </Section>
      )}

      {rj.jtbd && (
        <p className="text-sm">
          <span className="font-semibold">JTBD：</span> {rj.jtbd}
        </p>
      )}

      {rj.pain_point && (
        <p className="text-sm">
          <span className="font-semibold">{text('reportView.corePain')}</span> {rj.pain_point}
        </p>
      )}

      {rj.root_cause && (
        <p className="text-sm">
          <span className="font-semibold">{text('reportView.rootCause')}</span> {rj.root_cause}
        </p>
      )}

      {rj.target_users && (
        <p className="text-sm">
          <span className="font-semibold">{text('reportView.targetUsers')}</span> {rj.target_users}
        </p>
      )}

      {rj.user_mindset && (
        <Section title={text('reportView.userMindset')}>
          <div className="grid grid-cols-2 max-md:grid-cols-1 gap-3">
            {rj.user_mindset.emotional_phase && (
              <div className="bg-bg rounded-xl p-3">
                <p className="text-[10px] text-muted">{text('reportView.emotionalPhase')}</p>
                <p className="text-sm mt-0.5">{rj.user_mindset.emotional_phase}</p>
              </div>
            )}
            {rj.user_mindset.struggling_moment && (
              <div className="bg-bg rounded-xl p-3">
                <p className="text-[10px] text-muted">{text('reportView.strugglingMoment')}</p>
                <p className="text-sm mt-0.5">{rj.user_mindset.struggling_moment}</p>
              </div>
            )}
            {rj.user_mindset.identity_connection && (
              <div className="bg-bg rounded-xl p-3 col-span-2">
                <p className="text-[10px] text-muted">{text('reportView.identityConnection')}</p>
                <p className="text-sm mt-0.5">{rj.user_mindset.identity_connection}</p>
              </div>
            )}
          </div>
          {rj.user_mindset.current_workarounds && rj.user_mindset.current_workarounds.length > 0 && (
            <div className="mt-2">
              <p className="text-[10px] text-muted mb-1">{text('reportView.currentWorkarounds')}</p>
              <ul className="text-sm space-y-0.5">
                {rj.user_mindset.current_workarounds.map((w, i) => (
                  <li key={i}>• {w}</li>
                ))}
              </ul>
            </div>
          )}
        </Section>
      )}

      {rj.why_now && (
        <p className="text-sm">
          <span className="font-semibold">{text('reportView.whyNow')}</span> {rj.why_now}
        </p>
      )}

      {(rj.femwc_scores || (rj.femwc_before && rj.femwc_after)) && (
        <Section title={rj.femwc_before && rj.femwc_after ? text('reportView.femwcChange') : text('reportView.femwcScore')}>
          <div className="grid grid-cols-5 max-md:grid-cols-3 gap-2">
            {DIMS.map(([key, labelKey]) => {
              const scores = rj.femwc_scores || rj.femwc_after
              const before = rj.femwc_before
              const val = scores?.[key] ?? 0
              const hasBefore = before != null
              const delta = hasBefore ? val - (before?.[key] ?? 0) : 0
              return (
                <div key={key} className="bg-white rounded-xl p-3 text-center border border-border">
                  <p className="text-[10px] text-muted">{text(labelKey)}</p>
                  <p className="text-lg font-bold">{val}/5</p>
                  {hasBefore && delta !== 0 && (
                    <p className={`text-[10px] font-medium ${delta > 0 ? 'text-green-600' : 'text-red-500'}`}>
                      {delta > 0 ? '+' : ''}{delta}
                    </p>
                  )}
                </div>
              )
            })}
          </div>
          {rj.score_changes && (
            <p className="text-xs text-muted mt-2">{text('reportView.changeReason')}{rj.score_changes}</p>
          )}
        </Section>
      )}

      {rj.ai_fit_reason && (
        <Section title={text('reportView.lumonFit')}>
          <p className="text-sm text-text/80">{rj.ai_fit_reason}</p>
          {rj.non_ai_alternative && (
            <p className="text-sm text-gray-700 mt-1">{text('reportView.alternative')}{rj.non_ai_alternative}</p>
          )}
        </Section>
      )}

      {rj.verbatim_quotes && rj.verbatim_quotes.length > 0 && (
        <Section title={text('reportView.keyQuotes')}>
          {rj.verbatim_quotes.slice(0, 6).map((q, i) => (
            <div key={i} className="mb-2">
              <blockquote className="text-sm italic border-l-3 border-signal pl-3 text-text/80">
                &quot;{q.quote}&quot;
              </blockquote>
              <p className="text-[10px] text-muted ml-3">
                — {q.source}
                {q.evidence_type && <span className="ml-2 text-accent">#{q.evidence_type}</span>}
              </p>
            </div>
          ))}
        </Section>
      )}

      {rj.competitor_gap_matrix && rj.competitor_gap_matrix.length > 0 && (
        <Section title={text('reportView.competitorGap')}>
          <div className="rounded-xl border border-border/30 overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-max min-w-full text-xs border-collapse">
              <thead>
                <tr className="border-b border-border">
                  <th className="text-left py-2 px-2">{text('reportView.painPoint')}</th>
                  {Object.keys(rj.competitor_gap_matrix[0]?.competitors || {}).map((name) => (
                    <th key={name} className="text-left py-2 px-2">{name}</th>
                  ))}
                  <th className="text-left py-2 px-2">{text('reportView.unresolved')}</th>
                </tr>
              </thead>
              <tbody>
                {rj.competitor_gap_matrix.map((row, i) => (
                  <tr key={i} className="border-b border-border/50">
                    <td className="py-2 px-2 font-medium">{row.pain_point}</td>
                    {Object.values(row.competitors || {}).map((val, j) => (
                      <td key={j} className={`py-2 px-2 ${val === 'Unmet' ? 'text-red-600 font-medium' : val === 'Partial' ? 'text-gray-600' : 'text-green-600'}`}>
                        {gapValueText(val)}
                      </td>
                    ))}
                    <td className="py-2 px-2">
                      {row.unserved ? <span className="text-red-600 font-bold">{text('reportView.yes')}</span> : text('reportView.no')}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          </div>
        </Section>
      )}

      {rj.key_debates && rj.key_debates.length > 0 && (
        <Section title={text('reportView.keyDebates')}>
          <div className="rounded-xl border border-border/30 overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-max min-w-full text-xs border-collapse">
              <thead>
                <tr className="border-b border-border">
                  <th className="text-left py-2 px-2">{text('reportView.debate')}</th>
                  <th className="text-left py-2 px-2">{text('reportView.analyst')}</th>
                  <th className="text-left py-2 px-2">{text('reportView.user')}</th>
                  <th className="text-left py-2 px-2">{text('reportView.conclusion')}</th>
                </tr>
              </thead>
              <tbody>
                {rj.key_debates.map((d, i) => (
                  <tr key={i} className="border-b border-border/50">
                    <td className="py-2 px-2">{d.topic}</td>
                    <td className="py-2 px-2">{d.analyst_position}</td>
                    <td className="py-2 px-2">{d.user_position}</td>
                    <td className="py-2 px-2">{d.resolution}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          </div>
        </Section>
      )}

      {rj.suggested_features && (
        <Section title={text('reportView.roadmap')}>
          <div className="grid grid-cols-3 max-md:grid-cols-1 gap-3">
            {(rj.suggested_features.v1_core || rj.suggested_features.must_have) && (
              <div>
                <p className="text-xs font-medium text-green-700 mb-1">{text('reportView.v1Core')}</p>
                <ul className="text-sm space-y-0.5">
                  {(rj.suggested_features.v1_core || rj.suggested_features.must_have || []).map((f, i) => (
                    <li key={i}>• {f}</li>
                  ))}
                </ul>
              </div>
            )}
            {(rj.suggested_features.v1_nice || rj.suggested_features.nice_to_have) && (
              <div>
                <p className="text-xs font-medium text-blue-700 mb-1">{text('reportView.v1Nice')}</p>
                <ul className="text-sm space-y-0.5">
                  {(rj.suggested_features.v1_nice || rj.suggested_features.nice_to_have || []).map((f, i) => (
                    <li key={i}>• {f}</li>
                  ))}
                </ul>
              </div>
            )}
            {rj.suggested_features.v2_future && rj.suggested_features.v2_future.length > 0 && (
              <div>
                <p className="text-xs font-medium text-purple-700 mb-1">{text('reportView.v2Future')}</p>
                <ul className="text-sm space-y-0.5">
                  {rj.suggested_features.v2_future.map((f, i) => (
                    <li key={i}>• {f}</li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        </Section>
      )}

      {rj.smoke_tests && rj.smoke_tests.length > 0 && (
        <Section title={text('reportView.smokeTest')}>
          <div className="space-y-2">
            {rj.smoke_tests.map((t, i) => (
              <div key={i} className="bg-bg rounded-xl p-3">
                <p className="text-sm font-medium">{t.type}</p>
                <p className="text-xs text-muted mt-0.5">{text('reportView.channel')}{t.where}</p>
                <p className="text-xs text-muted">{text('reportView.successCriteria')}{t.success_threshold}</p>
                {t.what_if_fail && (
                  <p className="text-xs text-gray-700 mt-0.5">{text('reportView.afterFailure')}{t.what_if_fail}</p>
                )}
              </div>
            ))}
          </div>
        </Section>
      )}

      {rj.risks && rj.risks.length > 0 && (
        <Section title={text('reportView.risks')}>
          <ul className="text-sm space-y-0.5">
            {rj.risks.map((r, i) => (
              <li key={i}>• {r}</li>
            ))}
          </ul>
        </Section>
      )}

      {rj.growth_potential && (
        <p className="text-sm">
          <span className="font-semibold">{text('reportView.growthPotential')}</span> {rj.growth_potential}
        </p>
      )}

      {rj.next_angles && rj.next_angles.length > 0 && (
        <Section title={text('reportView.nextAngles')}>
          <div className="space-y-2">
            {rj.next_angles.map((a, i) => (
              <div key={i} className="bg-bg rounded-xl p-3">
                <p className="text-sm font-medium">{a.angle}</p>
                <p className="text-xs text-muted mt-0.5">{a.reasoning}</p>
                {a.supporting_quote && (
                  <blockquote className="text-xs italic border-l-2 border-signal pl-2 mt-1 text-text/70">
                    &quot;{a.supporting_quote}&quot;
                  </blockquote>
                )}
              </div>
            ))}
          </div>
        </Section>
      )}

      {rj.debate_summary && (
        <p className="text-sm">
          <span className="font-semibold">{text('reportView.debateSummary')}</span> {rj.debate_summary}
        </p>
      )}
    </div>
  )
}

// ============================================================
// 统一入口：自动检测内容格式
// ============================================================

function detectFormat(report: string | Record<string, unknown>, hint?: string): 'markdown' | 'json' {
  if (hint === 'markdown') return 'markdown'
  if (hint === 'json') return 'json'
  if (typeof report !== 'string') return 'json'
  const trimmed = report.trim()
  if (trimmed.startsWith('#') || trimmed.startsWith('---\n')) return 'markdown'
  if (trimmed.startsWith('{')) return 'json'
  return 'markdown'
}

export default function ReportView({
  report,
  format,
  onRefreshCompetitors,
  refreshingCompetitors,
  competitorRefreshMessage,
}: {
  report: string | Record<string, unknown>
  format?: string
  onRefreshCompetitors?: () => void
  refreshingCompetitors?: boolean
  competitorRefreshMessage?: string
}) {
  const detected = detectFormat(report, format)
  if (detected === 'markdown' && typeof report === 'string') {
    return (
      <MarkdownReportView
        content={report}
        onRefreshCompetitors={onRefreshCompetitors}
        refreshingCompetitors={refreshingCompetitors}
        competitorRefreshMessage={competitorRefreshMessage}
      />
    )
  }
  return <JsonReportView report={report} />
}

export { MarkdownReportView }

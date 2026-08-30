import { useState, useEffect, useCallback, useRef } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { ArrowLeft, Loader2, MessageSquare, Download, FileDown, X, ExternalLink, CheckCircle2, XCircle, ChevronRight } from 'lucide-react'
import {
  listReports, getReport, deleteReport, exportToFeishu, getFeishuStatus,
  extractOpportunities, runPocEvaluation, getPocEvalResult,
  refreshReportCompetitors, trackAnalyticsEvent,
  type OpportunityPoint, type PocEvalInput, type PocEvalResult, type PocEvalDimension,
} from '../api/client'
import { HelpButton, REPORT_HELP } from './HelpDialog'
import ReportView from './ReportView'
import type { ReportSummary, ReportData } from '../types'
import { useAppStore } from '../stores/app'
import { LocalizedText, useI18n } from '../i18n'

function escHtml(s: string) {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

function safeReportUrl(raw: string): string | null {
  try {
    const parsed = new URL(raw.trim(), window.location.origin)
    return parsed.protocol === 'http:' || parsed.protocol === 'https:' ? parsed.href : null
  } catch {
    return null
  }
}

function inlineMarkdown(text: string): string {
  let s = escHtml(text)
  s = s.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
  s = s.replace(/\*(.+?)\*/g, '<em>$1</em>')
  s = s.replace(/`([^`]+)`/g, '<code>$1</code>')
  s = s.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (_m, label, url) => {
    const iconOnly = /^(↗|🔗|icon)$/i.test(String(label).trim())
    const safeUrl = safeReportUrl(String(url))
    return safeUrl
      ? `<a href="${escHtml(safeUrl)}" target="_blank" rel="noopener noreferrer">${iconOnly ? '↗' : label}</a>`
      : String(label)
  })
  s = s.replace(/(https?:\/\/[^\s<]+)/g, (m) => {
    if (m.includes('</a>') || m.includes('"')) return m
    const safeUrl = safeReportUrl(m)
    return safeUrl
      ? `<a href="${escHtml(safeUrl)}" target="_blank" rel="noopener noreferrer">${escHtml(m)}</a>`
      : escHtml(m)
  })
  return s
}

function markdownToSimpleHtml(md: string): string {
  const lines = md.split('\n')
  const out: string[] = []
  let i = 0

  while (i < lines.length) {
    const line = lines[i]
    if (line.trim() === '') { i++; continue }
    if (line.trim() === '---') { out.push('<hr>'); i++; continue }
    if (line.startsWith('# ')) { out.push(`<h1>${inlineMarkdown(line.slice(2).trim())}</h1>`); i++; continue }
    if (line.startsWith('## ')) { out.push(`<h2>${inlineMarkdown(line.slice(3).trim())}</h2>`); i++; continue }
    if (line.startsWith('#### ')) { out.push(`<h4>${inlineMarkdown(line.slice(5).trim())}</h4>`); i++; continue }
    if (line.startsWith('### ')) { out.push(`<h3>${inlineMarkdown(line.slice(4).trim())}</h3>`); i++; continue }

    if (line.startsWith('```')) {
      const codeLines: string[] = []
      i++
      while (i < lines.length && !lines[i].startsWith('```')) { codeLines.push(escHtml(lines[i])); i++ }
      i++
      out.push(`<pre><code>${codeLines.join('\n')}</code></pre>`)
      continue
    }

    if (line.startsWith('|') && line.includes('|', 1)) {
      const tableLines: string[] = [line]
      i++
      while (i < lines.length && lines[i].startsWith('|')) { tableLines.push(lines[i]); i++ }
      const parseRow = (r: string) => r.split('|').slice(1, -1).map(c => c.trim())
      const header = parseRow(tableLines[0])
      const dataRows = tableLines.filter((_, idx) => {
        if (idx === 0) return false
        if (idx === 1 && /^\|[\s\-:|]+\|$/.test(tableLines[idx])) return false
        return true
      })
      let table = '<table><thead><tr>' + header.map(h => `<th>${inlineMarkdown(h)}</th>`).join('') + '</tr></thead><tbody>'
      for (const row of dataRows) {
        table += '<tr>' + parseRow(row).map(c => `<td>${inlineMarkdown(c)}</td>`).join('') + '</tr>'
      }
      table += '</tbody></table>'
      out.push(table)
      continue
    }

    if (line.startsWith('> ')) {
      const qLines: string[] = []
      while (i < lines.length && (lines[i].startsWith('> ') || lines[i].startsWith('>'))) {
        qLines.push(lines[i].replace(/^>\s?/, ''))
        i++
      }
      out.push(`<blockquote>${inlineMarkdown(qLines.join(' '))}</blockquote>`)
      continue
    }

    if (/^(\d+)\.\s/.test(line)) {
      const items: string[] = []
      while (i < lines.length && /^\d+\.\s/.test(lines[i])) {
        items.push(lines[i].replace(/^\d+\.\s+/, ''))
        i++
      }
      out.push('<ol>' + items.map(it => `<li>${inlineMarkdown(it)}</li>`).join('') + '</ol>')
      continue
    }

    if (/^[-*]\s/.test(line)) {
      const items: string[] = []
      while (i < lines.length && /^[-*]\s/.test(lines[i])) {
        items.push(lines[i].replace(/^[-*]\s+/, ''))
        i++
      }
      out.push('<ul>' + items.map(it => `<li>${inlineMarkdown(it)}</li>`).join('') + '</ul>')
      continue
    }

    const paraLines: string[] = [line]
    i++
    while (i < lines.length && lines[i].trim() !== '' && !lines[i].startsWith('#') && !lines[i].startsWith('|') && !lines[i].startsWith('>') && !lines[i].startsWith('```') && lines[i].trim() !== '---' && !/^[-*]\s/.test(lines[i]) && !/^\d+\.\s/.test(lines[i])) {
      paraLines.push(lines[i])
      i++
    }
    out.push(`<p>${inlineMarkdown(paraLines.join(' '))}</p>`)
  }

  return out.join('\n')
}

function formatDate(iso: string) {
  if (!iso) return ''
  try {
    const d = new Date(iso)
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
  } catch { return iso }
}

export default function ReportsView() {
  const { text, format } = useI18n()
  const [reports, setReports] = useState<ReportSummary[]>([])
  const [selectedReport, setSelectedReport] = useState<ReportData | null>(null)
  const [selectedTitle, setSelectedTitle] = useState('')
  const [selectedFilename, setSelectedFilename] = useState('')
  const [loading, setLoading] = useState(true)
  const [viewLoading, setViewLoading] = useState<string | null>(null)
  const [competitorRefreshing, setCompetitorRefreshing] = useState(false)
  const [competitorRefreshMessage, setCompetitorRefreshMessage] = useState('')
  const [feishuConfigured, setFeishuConfigured] = useState(false)
  const [feishuExporting, setFeishuExporting] = useState(false)
  const [feishuResult, setFeishuResult] = useState<{ url: string } | null>(null)
  const [feishuError, setFeishuError] = useState('')

  const loadReports = () => {
    setLoading(true)
    listReports().then((r) => setReports(r.reports)).catch(() => {}).finally(() => setLoading(false))
  }

  const pendingFileRef = useRef(useAppStore.getState().pendingReportFile)

  useEffect(() => {
    if (pendingFileRef.current) {
      useAppStore.getState().setPendingReportFile(null)
    }
    loadReports()
    getFeishuStatus().then(r => setFeishuConfigured(r.configured)).catch(() => {})
  }, [])

  useEffect(() => {
    const pf = pendingFileRef.current
    if (!pf || reports.length === 0 || loading) return
    pendingFileRef.current = null
    const match = reports.find(r => r.filename === pf)
    if (match) {
      handleViewDirect(match.filename, match.title)
    }
  }, [reports, loading])

  const handleViewDirect = async (filename: string, title: string) => {
    try {
      const data = await getReport(filename)
      setSelectedReport(data)
      setSelectedTitle(title)
      setSelectedFilename(filename)
      setFeishuResult(data.feishu ? { url: data.feishu.url } : null)
      setFeishuError('')
      setCompetitorRefreshMessage('')
      trackAnalyticsEvent('report.view', { has_feishu: Boolean(data.feishu), title_length: title.length })
    } catch { /* ignore */ }
  }

  const handleView = async (filename: string, title: string) => {
    setViewLoading(filename)
    try {
      const data = await getReport(filename)
      setSelectedReport(data)
      setSelectedTitle(title)
      setSelectedFilename(filename)
      setFeishuResult(data.feishu ? { url: data.feishu.url } : null)
      setFeishuError('')
      setCompetitorRefreshMessage('')
    } catch { /* ignore */ }
    setViewLoading(null)
  }

  const handleDownloadMd = () => {
    if (!selectedReport) return
    const content = typeof selectedReport.final_report === 'string'
      ? selectedReport.final_report
      : JSON.stringify(selectedReport.final_report, null, 2)
    const blob = new Blob([content], { type: 'text/markdown;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${selectedTitle || text('reports.reportFilenameFallback')}.md`
    a.click()
    URL.revokeObjectURL(url)
    trackAnalyticsEvent('report.download_md', { has_selected_doc: true })
  }

  const [pdfExporting, setPdfExporting] = useState(false)

  const handleDownloadPdf = useCallback(() => {
    if (!selectedReport || pdfExporting) return
    setPdfExporting(true)

    try {
      const raw = typeof selectedReport.final_report === 'string'
        ? selectedReport.final_report
        : JSON.stringify(selectedReport.final_report, null, 2)

      const htmlBody = markdownToSimpleHtml(raw)
      const title = selectedTitle || text('reports.reportFilenameFallback')

      const printWindow = window.open('', '_blank')
      if (!printWindow) {
        setPdfExporting(false)
        return
      }

      printWindow.document.write(`<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>${title}</title>
<style>
  @page { size: A4; margin: 20mm 18mm; }
  body { font-family: -apple-system, "Helvetica Neue", "PingFang SC", "Microsoft YaHei", sans-serif;
    font-size: 13px; line-height: 1.8; color: #1a1a1a; max-width: 100%; padding: 0; margin: 0; }
  h1 { font-size: 22px; font-weight: 700; margin: 0 0 16px; padding-bottom: 8px; border-bottom: 2px solid #e5e5e5; }
  h2 { font-size: 16px; font-weight: 700; margin: 28px 0 10px; padding-left: 10px; border-left: 3px solid #6366f1; }
  h3 { font-size: 14px; font-weight: 600; margin: 20px 0 8px; }
  h4 { font-size: 13px; font-weight: 600; margin: 16px 0 6px; }
  p { margin: 0 0 10px; }
  blockquote { margin: 10px 0; padding: 8px 14px; border-left: 3px solid #c7d2fe; background: #f8f8ff; font-style: italic; color: #555; }
  ul, ol { margin: 0 0 12px; padding-left: 22px; }
  li { margin-bottom: 4px; }
  table { width: 100%; border-collapse: collapse; margin: 12px 0; font-size: 12px; }
  th { background: #f5f6f7; font-weight: 600; text-align: left; padding: 8px 10px; border: 1px solid #e5e5e5; }
  td { padding: 6px 10px; border: 1px solid #e5e5e5; }
  tr:nth-child(even) { background: #f7f8f9; }
  code { background: #f3f4f6; padding: 1px 5px; border-radius: 3px; font-size: 12px; }
  pre { background: #f5f6f7; padding: 12px; border-radius: 6px; overflow-x: auto; font-size: 12px; margin: 10px 0; }
  a { color: #4f46e5; text-decoration: none; }
  hr { border: none; border-top: 1px solid #e5e5e5; margin: 20px 0; }
  strong { font-weight: 600; }
  @media print { body { -webkit-print-color-adjust: exact; print-color-adjust: exact; } }
</style></head><body>${htmlBody}</body></html>`)

      printWindow.document.close()
      setTimeout(() => {
        printWindow.print()
        printWindow.close()
        setPdfExporting(false)
        trackAnalyticsEvent('report.download_pdf', { has_selected_doc: true })
      }, 300)
    } catch (e) {
      console.error('PDF export failed:', e)
      setPdfExporting(false)
      trackAnalyticsEvent('report.download_pdf_error', { has_selected_doc: Boolean(selectedReport) })
    }
  }, [selectedTitle, selectedReport, pdfExporting, text])

  const handleExportFeishu = useCallback(async () => {
    if (!selectedFilename || feishuExporting) return
    const startedAt = Date.now()
    setFeishuExporting(true)
    setFeishuError('')
    setFeishuResult(null)
    trackAnalyticsEvent('report.export_feishu_start', { configured: feishuConfigured })
    try {
      const result = await exportToFeishu(selectedFilename)
      setFeishuResult({ url: result.url })
      trackAnalyticsEvent('report.export_feishu_done', { duration_ms: Date.now() - startedAt })
    } catch (e) {
      setFeishuError(e instanceof Error ? e.message : text('reports.exportFailed'))
      trackAnalyticsEvent('report.export_feishu_error', { duration_ms: Date.now() - startedAt })
    }
    setFeishuExporting(false)
  }, [selectedFilename, feishuExporting, feishuConfigured, text])

  const handleRefreshCompetitors = useCallback(async () => {
    if (!selectedFilename || competitorRefreshing) return
    setCompetitorRefreshing(true)
    setCompetitorRefreshMessage('')
    trackAnalyticsEvent('report.refresh_competitors_start', { has_selected_doc: true })
    try {
      const result = await refreshReportCompetitors(selectedFilename)
      if (!result.ok) {
        const message = result.error || text('reports.refreshFailed')
        setCompetitorRefreshMessage(message)
        trackAnalyticsEvent('report.refresh_competitors_error', { reason: message })
        return
      }
      if (result.report) {
        setSelectedReport(result.report)
      }
      setCompetitorRefreshMessage(format('reports.refreshDone', { matched: result.matched ?? 0, queried: result.queried ?? 0 }))
      trackAnalyticsEvent('report.refresh_competitors_done', {
        queried: result.queried ?? 0,
        matched: result.matched ?? 0,
      })
    } catch (e) {
      const message = e instanceof Error ? e.message : text('reports.refreshFailed')
      setCompetitorRefreshMessage(message)
      trackAnalyticsEvent('report.refresh_competitors_error', { reason: message })
    } finally {
      setCompetitorRefreshing(false)
    }
  }, [selectedFilename, competitorRefreshing, format, text])

  // 产品机会与 POC 验证
  const [opportunities, setOpportunities] = useState<OpportunityPoint[]>([])
  const [oppsLoading, setOppsLoading] = useState(false)
  const [evalFormOpen, setEvalFormOpen] = useState(false)
  const [evalFormData, setEvalFormData] = useState<PocEvalInput>({
    idea_name: '', idea_brief: '', target_users: '', pain_points: '', simple_product: '',
  })
  const [evalRunning, setEvalRunning] = useState(false)
  const [evalResult, setEvalResult] = useState<PocEvalResult | null>(null)
  const [evalError, setEvalError] = useState('')
  const [evalOppIndex, setEvalOppIndex] = useState(-1)
  const [evalLoading, setEvalLoading] = useState(false)

  useEffect(() => {
    if (!selectedReport) {
      setOpportunities([])
      setEvalResult(null)
      return
    }
    setOppsLoading(true)
    extractOpportunities(selectedReport.final_report, selectedFilename)
      .then(r => setOpportunities(r.opportunities))
      .catch(() => {})
      .finally(() => setOppsLoading(false))
  }, [selectedReport, selectedFilename])

  const handleSelectOpportunity = async (opp: OpportunityPoint, index: number) => {
    setEvalFormData({
      idea_name: opp.title,
      idea_brief: opp.description || opp.features.join('；'),
      target_users: opp.target_users || '',
      pain_points: opp.pain_points || '',
      simple_product: opp.simple_product || opp.features.join('\n') || '',
    })
    setEvalOppIndex(index)
    setEvalError('')
    setEvalFormOpen(true)

    // 如果有历史评价，加载它
    if (opp.eval_id) {
      setEvalLoading(true)
      try {
        const prev = await getPocEvalResult(opp.eval_id)
        setEvalResult(prev)
      } catch {
        setEvalResult(null)
      }
      setEvalLoading(false)
    } else {
      setEvalResult(null)
    }
  }

  const handleSubmitEval = async () => {
    if (evalRunning) return
    const { idea_name, target_users, pain_points, simple_product } = evalFormData
    if (!idea_name?.trim() || !target_users?.trim() || !pain_points?.trim() || !simple_product?.trim()) {
      setEvalError(text('reports.requiredFields'))
      return
    }
    setEvalRunning(true)
    setEvalError('')
    const startedAt = Date.now()
    trackAnalyticsEvent('poc_eval.start', { opportunity_index: evalOppIndex })
    try {
      const result = await runPocEvaluation({
        ...evalFormData,
        report_filename: selectedFilename,
        opportunity_index: evalOppIndex,
      })
      setEvalResult(result)
      // 更新本地 opportunities 的 eval_id
      if (evalOppIndex >= 0) {
        setOpportunities(prev => prev.map((o, i) => i === evalOppIndex ? { ...o, eval_id: result.id } : o))
      }
      trackAnalyticsEvent('poc_eval.done', {
        opportunity_index: evalOppIndex,
        duration_ms: Date.now() - startedAt,
      })
    } catch (e) {
      setEvalError(e instanceof Error ? e.message : text('reports.evalFailed'))
      trackAnalyticsEvent('poc_eval.error', {
        opportunity_index: evalOppIndex,
        duration_ms: Date.now() - startedAt,
      })
    }
    setEvalRunning(false)
  }

  const reportDetailView = selectedReport ? (() => {
    const reportFormat = selectedReport.report_format || 'json'
    return (
      <motion.div
        key="report-detail"
        initial={{ opacity: 0, scale: 0.88, y: 30 }}
        animate={{ opacity: 1, scale: 1, y: 0 }}
        exit={{ opacity: 0, scale: 0.92, y: 20 }}
        transition={{ duration: 0.4, ease: [0.16, 1, 0.3, 1] }}
        className="h-full flex flex-col"
      >
        <div className="shrink-0 px-6 max-md:px-4 pt-5 pb-3 flex items-start justify-between max-md:flex-col max-md:gap-3">
          <div>
            <button
              onClick={() => { setSelectedReport(null); setFeishuError(''); setEvalResult(null); loadReports() }}
              className="flex items-center gap-1.5 text-xs text-muted hover:text-text transition-colors mb-2"
            >
              <ArrowLeft size={13} /> {text('reports.backToList')}
            </button>
            <h1 className="text-lg max-md:text-base font-bold">{selectedTitle || text('reports.detailTitle')}</h1>
            <p className="text-xs text-muted mt-0.5">
              {formatDate(selectedReport.created_at)}
              {selectedReport.debate_rounds > 0 && ` · ${selectedReport.debate_rounds} ${text('reports.debateRounds')}`}
            </p>
          </div>
          <div className="flex items-center gap-2 shrink-0 mt-6 max-md:mt-0 max-md:flex-wrap">
            {feishuResult ? (
              <a
                href={feishuResult.url}
                target="_blank"
                rel="noopener noreferrer"
                className="flex items-center gap-1.5 text-[11px] font-medium text-white bg-emerald-500 h-8 px-3.5 rounded-lg hover:opacity-90 transition-opacity"
              >
                <CheckCircle2 size={12} /> {text('reports.feishuCreated')}
                <ExternalLink size={10} className="opacity-70" />
              </a>
            ) : (
              <button
                onClick={handleExportFeishu}
                disabled={feishuExporting || !feishuConfigured}
                title={feishuConfigured ? text('reports.publishFeishuTitle') : text('reports.configureFeishuTitle')}
                className="flex items-center gap-1.5 text-[11px] font-medium text-white bg-[#3370ff] h-8 px-3.5 rounded-lg hover:opacity-90 disabled:opacity-50 transition-opacity"
              >
                {feishuExporting ? <Loader2 size={12} className="animate-spin" /> : <ExternalLink size={12} />}
                <LocalizedText>{feishuExporting ? text('reports.publishing') : text('reports.publishFeishu')}</LocalizedText>
              </button>
            )}
            <button
              onClick={handleDownloadPdf}
              disabled={pdfExporting}
              className="flex items-center gap-1.5 text-[11px] font-medium text-muted border border-border/40 h-8 px-3.5 rounded-lg hover:border-accent/40 hover:text-accent transition-colors"
            >
              {pdfExporting ? <Loader2 size={12} className="animate-spin" /> : <FileDown size={12} />}
              <LocalizedText>{pdfExporting ? text('reports.exporting') : text('reports.downloadPdf')}</LocalizedText>
            </button>
            <button
              onClick={handleDownloadMd}
              className="flex items-center gap-1.5 text-[11px] font-medium text-muted border border-border/40 h-8 px-3.5 rounded-lg hover:border-accent/40 hover:text-accent transition-colors"
            >
              <Download size={12} /> Markdown
            </button>
          </div>
        </div>

        {!oppsLoading && opportunities.length > 0 && (
          <div className="shrink-0 px-6 max-md:px-4 pb-3">
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-[11px] text-muted font-medium flex items-center gap-1">
                <img src="/fire_line.png" alt="" className="w-4 h-4 opacity-70" /> <LocalizedText>{text('reports.productIdeas')}</LocalizedText>
              </span>
              {opportunities.map((opp, i) => (
                <button
                  key={i}
                  onClick={() => handleSelectOpportunity(opp, i)}
                  className="group flex items-center gap-1.5 text-[11px] font-medium px-3 py-1.5 rounded-full border border-accent/20 bg-accent/[0.04] text-accent hover:bg-accent/10 hover:border-accent/40 transition-all"
                >
                  <span className="w-4 h-4 rounded-full bg-accent/15 flex items-center justify-center text-[9px] font-bold shrink-0">
                    {i + 1}
                  </span>
                  <span className="truncate max-w-[200px]">{opp.title}</span>
                  <ChevronRight size={10} className="text-accent/50 group-hover:translate-x-0.5 transition-transform" />
                </button>
              ))}
            </div>
          </div>
        )}

        {feishuError && (
          <div className="mx-6 max-md:mx-4 mb-2 bg-red-50 border border-red-200 rounded-xl px-4 py-2.5">
            <span className="text-[12px] text-red-600">{feishuError}</span>
          </div>
        )}

        <div className="flex-1 overflow-y-auto scrollbar-auto px-6 max-md:px-4 py-4">
          <ReportView
            report={selectedReport.final_report}
            format={reportFormat}
            onRefreshCompetitors={handleRefreshCompetitors}
            refreshingCompetitors={competitorRefreshing}
            competitorRefreshMessage={competitorRefreshMessage}
          />
        </div>

        <AnimatePresence>
          {evalFormOpen && (
            <PocEvalFormDialog
              data={evalFormData}
              onChange={setEvalFormData}
              onSubmit={handleSubmitEval}
              onClose={() => { setEvalFormOpen(false); setEvalResult(null) }}
              running={evalRunning}
              error={evalError}
              evalResult={evalResult}
              evalLoading={evalLoading}
            />
          )}
        </AnimatePresence>
      </motion.div>
    )
  })() : null

  return (
    <div className="h-full flex flex-col">
      <AnimatePresence mode="wait">
        {selectedReport ? (
          reportDetailView
        ) : (
          <motion.div
            key="report-list"
            initial={{ opacity: 0, scale: 0.96, y: 10 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.94, y: -15, transition: { duration: 0.25, ease: [0.4, 0, 1, 1] } }}
            transition={{ duration: 0.3, ease: [0.16, 1, 0.3, 1] }}
            className="h-full flex flex-col"
          >
            <div className="shrink-0 px-6 max-md:px-4 pt-5 pb-2">
              <div className="flex items-center gap-1.5">
                <h1 className="text-base font-bold">{text('reports.title')}</h1>
                <HelpButton {...REPORT_HELP} />
              </div>
              <p className="page-header-subtitle">{text('reports.subtitle')}</p>
            </div>

            <div className="flex-1 overflow-y-auto scrollbar-auto px-6 max-md:px-4 py-4">
              {loading ? (
                <div className="flex items-center justify-center h-full">
                  <Loader2 size={18} className="animate-spin text-muted" />
                </div>
              ) : reports.length === 0 ? (
                <div className="flex flex-col items-center justify-center h-full text-center">
                  <img src="/icon2.png" alt="" className="w-8 h-auto mb-3" />
                  <p className="text-sm font-medium mb-1">{text('reports.emptyTitle')}</p>
                  <p className="text-xs text-muted">{text('reports.emptyDesc')}</p>
                </div>
              ) : (
                <div className="grid grid-cols-2 max-md:grid-cols-1 gap-3">
                  <AnimatePresence>
              {reports.map((r, i) => {
                const isLoading = viewLoading === r.filename
                return (
                  <motion.div
                    key={r.filename}
                    initial={{ opacity: 0, y: 6 }}
                    animate={{ opacity: 1, y: 0 }}
                    exit={{ opacity: 0, scale: 0.95 }}
                    transition={{ delay: i * 0.03 }}
                    onClick={() => !viewLoading && handleView(r.filename, r.title)}
                    className={`group relative flex flex-col border border-border/60 rounded-3xl p-5 cursor-pointer hover:border-accent/30 hover:shadow-sm transition-all ${
                      isLoading ? 'opacity-60' : ''
                    }`}
                  >
                    <button
                      onClick={(e) => {
                        e.stopPropagation()
                        if (!window.confirm(text('reports.deleteConfirm'))) return
                        deleteReport(r.filename).then(() => {
                          setReports(prev => prev.filter(x => x.filename !== r.filename))
                        }).catch(() => {})
                      }}
                      className="absolute top-3 right-3 w-6 h-6 rounded-lg flex items-center justify-center text-muted/40 hover:text-signal hover:bg-signal/10 transition-all opacity-0 group-hover:opacity-100 max-md:opacity-60"
                    >
                      <X size={12} />
                    </button>
                    <div className="flex items-start gap-3 mb-3">
                      <div className="w-9 h-9 bg-accent/8 rounded-xl flex items-center justify-center shrink-0">
                        {isLoading ? (
                          <Loader2 size={16} className="animate-spin text-accent" />
                        ) : (
                          <img src="/book_2_ai_line.png" alt="" className="w-4.5 h-4.5 opacity-70" />
                        )}
                      </div>
                      <div className="flex-1 min-w-0">
                        <p className="text-[13px] font-semibold line-clamp-2 leading-snug">{r.title}</p>
                      </div>
                    </div>
                    {(r.verdict || r.femwc_total != null) && (
                      <div className="flex items-center gap-2 mb-2.5">
                        {r.verdict && (
                          <span className={`text-[9px] font-bold px-2 py-0.5 rounded-full border ${
                            r.verdict.toUpperCase() === 'GO' ? 'bg-green-50 text-green-700 border-green-200' :
                            r.verdict.toUpperCase() === 'NO-GO' ? 'bg-red-50 text-red-700 border-red-200' :
                            'bg-gray-50 text-gray-700 border-gray-200'
                          }`}>
                            {r.verdict.toUpperCase()}
                          </span>
                        )}
                        {r.femwc_total != null && (
                          <span className="text-[10px] font-semibold text-text/60">
                            FEMWC {typeof r.femwc_total === 'number' ? r.femwc_total.toFixed(2) : r.femwc_total}/5
                          </span>
                        )}
                        {r.ai_fit && (
                          <span className={`text-[9px] px-1.5 py-0.5 rounded ${
                            r.ai_fit === 'strong' ? 'bg-emerald-50 text-emerald-600' :
                            r.ai_fit === 'moderate' ? 'bg-gray-100 text-gray-600' :
                            'bg-gray-50 text-gray-500'
                          }`}>
                            AI {r.ai_fit === 'strong' ? text('reports.aiStrong') : r.ai_fit === 'moderate' ? text('reports.aiMedium') : text('reports.aiWeak')}
                          </span>
                        )}
                      </div>
                    )}
                    <div className="flex items-center gap-2.5 text-[10px] text-muted mt-auto">
                      <span>{formatDate(r.created_at)}</span>
                      {r.rounds > 0 && (
                        <span className="flex items-center gap-1">
                          <MessageSquare size={9} /> {r.rounds} {text('reports.debateRounds')}
                        </span>
                      )}
                      <span className={`ml-auto px-1.5 py-0.5 rounded text-[9px] font-medium ${
                        r.rounds > 0 ? 'bg-blue-50 text-blue-600' : 'bg-accent/8 text-accent'
                      }`}>
                        {r.rounds > 0 ? text('reports.debateReport') : text('reports.directReport')}
                      </span>
                    </div>
                  </motion.div>
                )
              })}
            </AnimatePresence>
          </div>
        )}
      </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}


// ============================================================
// POC 验证表单弹窗
// ============================================================

function PocEvalFormDialog({
  data, onChange, onSubmit, onClose, running, error, evalResult, evalLoading,
}: {
  data: PocEvalInput
  onChange: (d: PocEvalInput) => void
  onSubmit: () => void
  onClose: () => void
  running: boolean
  error: string
  evalResult: PocEvalResult | null
  evalLoading: boolean
}) {
  const { text } = useI18n()
  const update = (key: keyof PocEvalInput, val: string) => {
    onChange({ ...data, [key]: val })
  }

  const ev = evalResult?.evaluation
  const verdictColor = ev?.overall_verdict === 'PASS'
    ? 'bg-emerald-50 text-emerald-700 border-emerald-200'
    : ev?.overall_verdict === 'FAIL'
      ? 'bg-red-50 text-red-700 border-red-200'
      : 'bg-gray-50 text-gray-700 border-gray-200'

  const dims = ev ? [
    { key: 'clear_users', label: text('reports.targetUsers'), icon: '/group_2_line.png', data: ev?.clear_users, userInput: evalResult?.input?.target_users ?? '' },
    { key: 'real_needs', label: text('reports.painPoints'), icon: '/question_line.png', data: ev?.real_needs, userInput: evalResult?.input?.pain_points ?? '' },
    { key: 'simple_product', label: text('reports.simpleProduct'), icon: '/earth_4_line.png', data: ev?.simple_product, userInput: evalResult?.input?.simple_product ?? '' },
  ] : []

  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 backdrop-blur-sm"
      onClick={(e) => { if (e.target === e.currentTarget && !running) onClose() }}
    >
      <motion.div
        initial={{ opacity: 0, scale: 0.95, y: 10 }}
        animate={{ opacity: 1, scale: 1, y: 0 }}
        exit={{ opacity: 0, scale: 0.95, y: 10 }}
        transition={{ duration: 0.2, ease: 'easeOut' }}
        className="bg-card rounded-3xl shadow-2xl border border-border/30 w-full max-w-lg max-md:max-w-[calc(100vw-2rem)] max-h-[85vh] overflow-hidden flex flex-col"
      >
        <div className="shrink-0 flex items-center justify-between px-6 max-md:px-4 pt-5 pb-3">
          <div className="flex items-center gap-2">
            <div className="w-8 h-8 bg-accent/10 rounded-xl flex items-center justify-center">
              <img src="/head_ai_line.png" alt="" className="w-4 h-4" />
            </div>
            <div>
              <h2 className="text-[15px] font-bold text-text">{text('reports.pocTitle')}</h2>
              <p className="text-[10px] text-muted">{text('reports.pocSubtitle')}</p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            {ev && (
              <span className={`text-[10px] font-bold px-2.5 py-0.5 rounded-full border ${verdictColor}`}>
                {ev.overall_verdict === 'PASS' ? text('reports.pass') : ev.overall_verdict === 'FAIL' ? text('reports.fail') : text('reports.conditionalPass')}
              </span>
            )}
            <button
              onClick={onClose}
              disabled={running}
              className="w-7 h-7 rounded-lg flex items-center justify-center text-muted hover:text-text hover:bg-bg transition-colors"
            >
              <X size={14} />
            </button>
          </div>
        </div>

        <div className="flex-1 overflow-y-auto scrollbar-auto px-6 max-md:px-4 pb-2">
          <div className="space-y-4">
            <FormField label={text('reports.ideaName')} required>
              <input
                type="text"
                value={data.idea_name}
                onChange={e => update('idea_name', e.target.value)}
                placeholder={text('reports.ideaNamePlaceholder')}
                className="w-full px-3.5 py-2.5 rounded-xl border border-border/40 bg-bg/50 text-[13px] text-text placeholder:text-text/25 focus:outline-none focus:border-accent/40 focus:ring-1 focus:ring-accent/20 transition-all"
              />
            </FormField>

            <FormField label={text('reports.ideaBrief')} required>
              <input
                type="text"
                value={data.idea_brief}
                onChange={e => update('idea_brief', e.target.value)}
                placeholder={text('reports.ideaBriefPlaceholder')}
                className="w-full px-3.5 py-2.5 rounded-xl border border-border/40 bg-bg/50 text-[13px] text-text placeholder:text-text/25 focus:outline-none focus:border-accent/40 focus:ring-1 focus:ring-accent/20 transition-all"
              />
            </FormField>

            <FormField label={text('reports.targetUsers')} required>
              <textarea
                value={data.target_users}
                onChange={e => update('target_users', e.target.value)}
                placeholder={text('reports.targetUsersPlaceholder')}
                rows={3}
                className="w-full px-3.5 py-2.5 rounded-xl border border-border/40 bg-bg/50 text-[13px] text-text placeholder:text-text/25 focus:outline-none focus:border-accent/40 focus:ring-1 focus:ring-accent/20 transition-all resize-y"
              />
            </FormField>

            {ev && (
              <EvalDimensionInline dim={dims[0]} />
            )}

            <FormField label={text('reports.painPoints')} required>
              <textarea
                value={data.pain_points}
                onChange={e => update('pain_points', e.target.value)}
                placeholder={text('reports.painPointsPlaceholder')}
                rows={3}
                className="w-full px-3.5 py-2.5 rounded-xl border border-border/40 bg-bg/50 text-[13px] text-text placeholder:text-text/25 focus:outline-none focus:border-accent/40 focus:ring-1 focus:ring-accent/20 transition-all resize-y"
              />
            </FormField>

            {ev && (
              <EvalDimensionInline dim={dims[1]} />
            )}

            <FormField label={text('reports.simpleProduct')} required>
              <textarea
                value={data.simple_product}
                onChange={e => update('simple_product', e.target.value)}
                placeholder={text('reports.simpleProductPlaceholder')}
                rows={3}
                className="w-full px-3.5 py-2.5 rounded-xl border border-border/40 bg-bg/50 text-[13px] text-text placeholder:text-text/25 focus:outline-none focus:border-accent/40 focus:ring-1 focus:ring-accent/20 transition-all resize-y"
              />
            </FormField>

            {ev && (
              <EvalDimensionInline dim={dims[2]} />
            )}

            {ev?.summary && (
              <div className="bg-bg/50 border border-border/20 rounded-xl px-4 py-3">
                <p className="text-[11px] text-muted leading-relaxed">
                  <span className="font-semibold text-text/60">{text('reports.summary')}</span>{ev.summary}
                </p>
              </div>
            )}

            {evalLoading && (
              <div className="flex items-center justify-center gap-2 py-4 text-muted">
                <Loader2 size={14} className="animate-spin" />
                <span className="text-[12px]">{text('reports.loadingEval')}</span>
              </div>
            )}
          </div>
        </div>

        {error && (
          <div className="mx-6 max-md:mx-4 mb-2 px-3 py-2 rounded-lg bg-signal/8 border border-signal/15 text-[11px] text-signal">
            {error}
          </div>
        )}

        <div className="shrink-0 px-6 max-md:px-4 py-4 border-t border-border/15 flex items-center justify-end gap-3">
          <button
            onClick={onClose}
            disabled={running}
            className="text-[12px] text-muted hover:text-text transition-colors px-4 py-2"
          >
            {ev ? text('common.close') : text('common.cancel')}
          </button>
          <button
            onClick={onSubmit}
            disabled={running || !data.idea_name.trim()}
            className="flex items-center gap-1.5 text-[12px] font-medium text-white bg-accent h-9 px-5 rounded-xl hover:opacity-90 disabled:opacity-50 transition-opacity"
          >
            {running ? (
              <><Loader2 size={12} className="animate-spin" /> {text('reports.evaluating')}</>
            ) : (
              <><img src="/head_ai_line.png" alt="" className="w-3 h-3 brightness-0 invert" /> {ev ? text('reports.reevaluate') : text('reports.startEval')}</>
            )}
          </button>
        </div>
      </motion.div>
    </motion.div>
  )
}

function EvalDimensionInline({ dim }: { dim: { key: string; label: string; icon: string; data: PocEvalDimension; userInput: string } }) {
  const { text } = useI18n()
  const { label, icon, data, userInput } = dim
  return (
    <motion.div
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.25 }}
      className="border border-border/30 rounded-xl overflow-hidden"
    >
      <div className="flex items-center justify-between px-4 py-2.5 bg-bg/30">
        <div className="flex items-center gap-2">
          <img src={icon} alt="" className="w-3.5 h-3.5 opacity-60" />
          <h4 className="text-[12px] font-semibold text-text">{label}</h4>
        </div>
        <div className="flex items-center gap-1.5">
          {data.verdict ? (
            <CheckCircle2 size={13} className="text-emerald-500" />
          ) : (
            <XCircle size={13} className="text-red-400" />
          )}
          <span className={`text-[9px] font-bold px-2 py-0.5 rounded-full ${
            data.verdict
              ? 'bg-emerald-50 text-emerald-700 border border-emerald-200'
              : 'bg-red-50 text-red-600 border border-red-200'
          }`}>
            {data.verdict ? text('reports.verdictPassed') : text('reports.verdictFailed')}
          </span>
        </div>
      </div>

      <div className="px-4 py-2.5 space-y-2">
        {userInput && (
          <div className="bg-bg/50 border border-border/20 rounded-lg px-3 py-2">
            <p className="text-[10px] font-medium text-muted mb-0.5">{text('reports.formOriginal')}</p>
            <p className="text-[11px] text-text/80 leading-relaxed whitespace-pre-line">{userInput}</p>
          </div>
        )}

        <div className="bg-accent/[0.04] border border-accent/10 rounded-lg px-3 py-2">
          <p className="text-[10px] font-medium text-accent/60 mb-0.5">{text('reports.aiReason')}</p>
          <p className="text-[11px] text-text/70 leading-relaxed">
            {(data?.reason ?? '').replace(/^原因[：:]\s*/, '')}
          </p>
        </div>

        <div className="bg-blue-50/50 border border-blue-100 rounded-lg px-3 py-2">
          <p className="text-[10px] font-medium text-blue-500/60 mb-0.5">{text('reports.aiSuggestion')}</p>
          <p className="text-[11px] text-text/70 leading-relaxed">
            {(data?.suggestion ?? '').replace(/^建议[：:]\s*/, '')}
          </p>
        </div>
      </div>
    </motion.div>
  )
}

function FormField({ label, required, children }: { label: string; required?: boolean; children: React.ReactNode }) {
  return (
    <div>
      <label className="block text-[13px] font-semibold text-text mb-1.5">
        {label} {required && <span className="text-signal">*</span>}
      </label>
      {children}
    </div>
  )
}

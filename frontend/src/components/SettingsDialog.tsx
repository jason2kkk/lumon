import { useState, useEffect, useCallback, useRef } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import {
  X, Save, Loader2, CheckCircle2, XCircle,
  ChevronRight, ChevronDown, Eye, EyeOff, RefreshCw,
} from 'lucide-react'
import {
  saveConfig, testConnection, getConfigValues, getEngineStatus, saveRoleNames,
  getServiceUsage, getWebSearchEngine, setWebSearchEngine, testWebSearch,
  getSensorTowerStatus, getTokenStats, resetTokenStats, getGeneralModel,
  setGeneralModel, sessionHeaders,
} from '../api/client'
import type { TokenStats } from '../api/client'
import { useAppStore } from '../stores/app'
import type { SettingsSection } from '../stores/app'
import type { EngineStatus } from '../types'
import { LocalizedText, localizeRoleName, useI18n } from '../i18n'

const BASE = '/api'

interface TestResult { ok: boolean; message: string }

const SECTIONS: { id: SettingsSection; img: string }[] = [
  { id: 'models', img: '/head_ai_line.png' },
  { id: 'cli', img: '/terminal_box_ai_line.png' },
  { id: 'websearch', img: '/earth_4_line.png' },
  { id: 'roles', img: '/group_3_line.png' },
  { id: 'feishu', img: '/link_3_line.png' },
  { id: 'sources', img: '/chart_line_line.png' },
  { id: 'guide', img: '/question_line.png' },
]

function fmtTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`
  return `${n}`
}

function maskMiddle(val: string): string {
  if (!val || val.length < 10) return val
  const showLen = Math.min(5, Math.floor(val.length * 0.2))
  return val.slice(0, showLen) + '****' + val.slice(-showLen)
}

function KeyInput({ value, onChange, placeholder }: {
  value: string; onChange: (v: string) => void; placeholder: string
}) {
  const [visible, setVisible] = useState(false)

  return (
    <div className="relative">
      <input
        type="text"
        value={visible ? value : (value ? maskMiddle(value) : '')}
        onChange={(e) => onChange(e.target.value)}
        onFocus={() => setVisible(true)}
        placeholder={placeholder}
        className="w-full rounded-xl border border-border/60 bg-bg px-3 py-2 pr-9 text-sm font-mono placeholder:text-muted/60 focus:outline-none focus:ring-2 focus:ring-accent/15 focus:border-accent/40 transition-all"
      />
      <button
        type="button"
        onClick={() => setVisible(!visible)}
        className="absolute right-2.5 top-1/2 -translate-y-1/2 text-muted hover:text-text transition-colors"
      >
        {visible ? <EyeOff size={14} /> : <Eye size={14} />}
      </button>
    </div>
  )
}

export default function SettingsDialog() {
  const {
    showSettingsDialog, setShowSettingsDialog, settingsSection, setSettingsSection,
    setConfigReady, dataSources, setDataSources, roleNames, setRoleNames, loadRoleNames,
  } = useAppStore()
  const { text, list, language } = useI18n()

  const activeSection = settingsSection
  const setActiveSection = setSettingsSection
  const sections = SECTIONS
  const sectionText = useCallback((id: SettingsSection, field: 'label' | 'desc') => {
    return text(`settings.sections.${id}.${field}`)
  }, [text])

  const [claudeUrl, setClaudeUrl] = useState('')
  const [claudeKey, setClaudeKey] = useState('')
  const [claudeModel, setClaudeModel] = useState('')
  const [gptUrl, setGptUrl] = useState('')
  const [gptKey, setGptKey] = useState('')
  const [gptModel, setGptModel] = useState('')
  const [gptBuiltin, setGptBuiltin] = useState(false)
  const [tavilyKey, setTavilyKey] = useState('')
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [saveError, setSaveError] = useState('')

  // Connection tests are point-in-time checks. Persisting the result made a
  // previously successful test look current after the gateway went down.
  const [claudeTest, setClaudeTest] = useState<TestResult | null>(null)
  const [gptTest, setGptTest] = useState<TestResult | null>(null)
  const [testingC, setTestingC] = useState(false)
  const [testingG, setTestingG] = useState(false)
  const [roleModels, setRoleModels] = useState<Record<string, string>>({
    director: 'gpt', analyst: 'gpt', critic: 'gpt', investor: 'gpt',
  })
  const [engineStatus, setEngineStatus] = useState<EngineStatus | null>(null)
  const [engineChecking, setEngineChecking] = useState(false)
  const [webSearchEngine, setWebSearchEngineState] = useState<string>('gpt')
  const [openRoleDropdown, setOpenRoleDropdown] = useState<string | null>(null)
  const [editingRoleName, setEditingRoleName] = useState<string | null>(null)
  const [editingNameValue, setEditingNameValue] = useState('')
  const [serviceUsage, setServiceUsage] = useState<Record<string, Record<string, unknown>>>({})
  const [feishuAppId, setFeishuAppId] = useState('')
  const [feishuAppSecret, setFeishuAppSecret] = useState('')
  const [stStatus, setStStatus] = useState<{ installed: boolean; available: boolean; error: string } | null>(null)
  const [stChecking, setStChecking] = useState(false)
  const [wsTestResult, setWsTestResult] = useState<{ ok: boolean; message: string } | null>(null)
  const [wsTesting, setWsTesting] = useState(false)
  const [tokenStats, setTokenStats] = useState<TokenStats | null>(null)
  const [generalModel, setGeneralModelState] = useState<string>('gpt')

  const maskedKeysRef = useRef<Record<string, string>>({})

  const refreshEngine = useCallback(async (force = true) => {
    setEngineChecking(true)
    try { setEngineStatus(await getEngineStatus(force)) }
    catch { /* ignore */ }
    setEngineChecking(false)
  }, [])


  useEffect(() => {
    if (!showSettingsDialog) return

    setClaudeTest(null)
    setGptTest(null)

    // 每次打开都重新拉取配置和引擎状态
    getConfigValues().then((vals) => {
      setClaudeUrl(vals.CLAUDE_BASE_URL || '')
      const maskedCK = vals.CLAUDE_API_KEY_SET ? vals.CLAUDE_API_KEY : ''
      setClaudeKey(maskedCK)
      setClaudeModel(vals.CLAUDE_MODEL || '')
      const isBuiltin = !!(vals as unknown as Record<string, unknown>).GPT_BUILTIN
      setGptBuiltin(isBuiltin)
      setGptUrl(vals.GPT_BASE_URL || '')
      const maskedGK = vals.GPT_API_KEY_SET ? vals.GPT_API_KEY : ''
      setGptKey(maskedGK)
      setGptModel(vals.GPT_MODEL || '')
      if (isBuiltin && !gptTest) {
        setTimeout(() => handleTestGpt(), 300)
      }
      const rm = (vals as unknown as Record<string, unknown>).role_models as Record<string, string> | undefined
      if (rm) setRoleModels(prev => ({ ...prev, ...rm }))
      const tk = ((vals as unknown as Record<string, unknown>).TAVILY_API_KEY as string) || ''
      setTavilyKey(tk)
      const fid = ((vals as unknown as Record<string, unknown>).FEISHU_APP_ID as string) || ''
      setFeishuAppId(fid)
      const fsec = ((vals as unknown as Record<string, unknown>).FEISHU_APP_SECRET as string) || ''
      setFeishuAppSecret(fsec)
      maskedKeysRef.current = { claudeKey: maskedCK, gptKey: maskedGK, tavilyKey: tk, feishuAppSecret: fsec }
    }).catch(() => {})

    refreshEngine()
    getWebSearchEngine().then(r => {
      setWebSearchEngineState(r.engine)
    }).catch(() => {})
    getSensorTowerStatus().then(setStStatus).catch(() => {})
    loadRoleNames()
    getServiceUsage().then(setServiceUsage).catch(() => {})
    getTokenStats().then(setTokenStats).catch(() => {})
    getGeneralModel().then(r => setGeneralModelState(r.model)).catch(() => {})
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showSettingsDialog])

  const handleSave = async () => {
    setSaving(true)
    setSaveError('')
    try {
      const mk = maskedKeysRef.current
      const clearFields = [
        mk.claudeKey && !claudeKey ? 'CLAUDE_API_KEY' : '',
        mk.gptKey && !gptKey ? 'GPT_API_KEY' : '',
        mk.tavilyKey && !tavilyKey ? 'TAVILY_API_KEY' : '',
        mk.feishuAppSecret && !feishuAppSecret ? 'FEISHU_APP_SECRET' : '',
      ].filter(Boolean)
      await saveConfig({
        CLAUDE_BASE_URL: claudeUrl,
        CLAUDE_API_KEY: claudeKey !== mk.claudeKey ? claudeKey : '',
        CLAUDE_MODEL: claudeModel,
        GPT_BASE_URL: gptUrl,
        GPT_API_KEY: gptKey !== mk.gptKey ? gptKey : '',
        GPT_MODEL: gptModel,
        TAVILY_API_KEY: tavilyKey !== mk.tavilyKey ? tavilyKey : '',
        FEISHU_APP_ID: feishuAppId,
        FEISHU_APP_SECRET: feishuAppSecret !== mk.feishuAppSecret ? feishuAppSecret : '',
        CLEAR_FIELDS: clearFields,
      })
      await fetch(BASE + '/config/role-models', {
        method: 'POST',
        headers: sessionHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify(roleModels),
      })
      await setGeneralModel(generalModel)
      setSaved(true)
      setConfigReady(true)
      setTimeout(() => {
        setSaved(false)
        setShowSettingsDialog(false)
      }, 600)
    } catch (e) {
      setSaveError(e instanceof Error ? e.message : text('common.saveFailed'))
    }
    setSaving(false)
  }

  const handleTestClaude = async () => {
    setTestingC(true); setClaudeTest(null)
    try {
      const mk = maskedKeysRef.current
      const r = await testConnection('CLAUDE', {
        base_url: claudeUrl,
        api_key: claudeKey !== mk.claudeKey ? claudeKey : undefined,
        model: claudeModel,
      })
      setClaudeTest(r)
    } catch {
      const r = { ok: false, message: text('common.modelUnavailable') }
      setClaudeTest(r)
    }
    setTestingC(false)
  }

  const handleTestGpt = async () => {
    setTestingG(true); setGptTest(null)
    try {
      const mk = maskedKeysRef.current
      const r = await testConnection('GPT', {
        base_url: gptUrl,
        api_key: gptKey !== mk.gptKey ? gptKey : undefined,
        model: gptModel,
      })
      setGptTest(r)
    } catch {
      const r = { ok: false, message: text('common.modelUnavailable') }
      setGptTest(r)
    }
    setTestingG(false)
  }

  const handleClose = () => {
    setShowSettingsDialog(false)
  }

  useEffect(() => {
    if (!showSettingsDialog) return
    const interval = setInterval(() => {
      getTokenStats().then(setTokenStats).catch(() => {})
    }, 5000)
    return () => clearInterval(interval)
  }, [showSettingsDialog])

  useEffect(() => {
    if (!showSettingsDialog) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setShowSettingsDialog(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [showSettingsDialog, setShowSettingsDialog])

  const inputCls = 'w-full rounded-xl border border-border/60 bg-bg px-3 py-2 text-sm placeholder:text-muted/60 focus:outline-none focus:ring-2 focus:ring-accent/15 focus:border-accent/40 transition-all'

  const StatusDot = ({ ok }: { ok: boolean | null }) => {
    if (ok === null) return <span className="w-2 h-2 rounded-full bg-muted/30 shrink-0" />
    return <span className={`w-2 h-2 rounded-full shrink-0 ${ok ? 'bg-emerald-500' : 'bg-signal'}`} />
  }

  const handleGeneralModelChange = (model: string) => {
    setGeneralModelState(model)
    setGeneralModel(model).catch(() => {})
  }

  const [claudeExpanded, setClaudeExpanded] = useState(false)

  const renderModels = () => (
    <div className="space-y-5">
      <div className="space-y-2">
        <p className="text-xs text-muted">
          {text('settings.modelUsageDesc')}
        </p>
        <div className="flex gap-2">
          {(['gpt', 'claude'] as const).map((id) => {
            const label = id === 'claude' ? 'Claude' : 'GPT'
            return (
              <button
                key={id}
                type="button"
                onClick={() => handleGeneralModelChange(id)}
                className={`settings-selectable-option flex-1 text-center text-[14px] font-medium py-3 rounded-xl transition-all ${
                  generalModel === id
                    ? 'settings-selected-option bg-zinc-900 text-white shadow-sm'
                    : 'bg-bg/60 text-muted ring-1 ring-border/40 hover:ring-zinc-400/25'
                }`}
              >
                {label}
              </button>
            )
          })}
        </div>
      </div>

      {/* GPT 配置 */}
      <div className="bg-bg/60 rounded-xl p-4">
        <div className="flex items-center justify-between mb-3">
          <div className="flex items-center gap-2">
            <img src="/openai_line.png" alt="" className="w-4 h-4" />
            <h4 className="text-sm font-semibold">GPT</h4>
            {gptBuiltin && (
              <span className="text-[10px] text-emerald-600 font-medium bg-emerald-50 px-2 py-0.5 rounded-full">{text('settings.systemBuiltin')}</span>
            )}
            {testingG ? (
              <Loader2 size={12} className="text-accent animate-spin" />
            ) : gptTest ? (
              <div className="flex items-center gap-1">
                <StatusDot ok={gptTest.ok} />
                <span className={`text-[10px] font-medium ${gptTest.ok ? 'text-emerald-600' : 'text-signal'}`}>
                  {gptTest.ok ? text('common.connected') : text('common.contactAdmin')}
                </span>
              </div>
            ) : null}
          </div>
          <button onClick={handleTestGpt} disabled={testingG}
            className="text-[11px] text-accent hover:underline disabled:opacity-50">
            {text('common.testConnection')}
          </button>
        </div>

        {gptBuiltin ? (
          <p className="text-[11px] text-muted leading-relaxed">
            {text('settings.modelBuiltinDesc')}
          </p>
        ) : (
          <div className="space-y-2">
            <div>
              <label className="block text-[11px] text-muted font-medium mb-1">Base URL</label>
              <input type="text" value={gptUrl} onChange={(e) => setGptUrl(e.target.value)}
                placeholder="https://api.example.com" className={inputCls} />
            </div>
            <div>
              <label className="block text-[11px] text-muted font-medium mb-1">API Key</label>
              <KeyInput value={gptKey} onChange={setGptKey} placeholder="sk-..." />
            </div>
            <div>
              <label className="block text-[11px] text-muted font-medium mb-1">{text('settings.modelName')}</label>
              <input type="text" value={gptModel} onChange={(e) => setGptModel(e.target.value)}
                placeholder="gpt-5.4" className={inputCls} />
            </div>
          </div>
        )}

        {gptTest && !gptTest.ok && (
          <p className="flex items-center gap-1 text-[11px] text-signal mt-2">
            <XCircle size={11} /> {gptTest.message}
          </p>
        )}
        {!gptBuiltin && serviceUsage.gpt && 'balance_usd' in serviceUsage.gpt && (
          <div className="flex items-center gap-1.5 mt-2 text-[11px]">
            <span className="text-muted">{text('common.balance')}</span>
            <span className="font-semibold text-emerald-600">${String(serviceUsage.gpt.balance_usd)}</span>
          </div>
        )}
        {tokenStats && (
          <div className="mt-2.5 pt-2.5 border-t border-border/20">
            <div className="flex items-center justify-between">
              <span className="text-[10px] text-muted font-medium">{text('common.tokenUsage')}</span>
              {tokenStats.gpt.calls > 0 && (
                <button
                  onClick={() => { resetTokenStats().then(() => getTokenStats().then(setTokenStats)) }}
                  className="text-[10px] text-muted hover:text-accent transition-colors"
                >{text('common.reset')}</button>
              )}
            </div>
            <p className="text-[10px] text-muted/80 mt-1">
              {text('common.input')} <span className="font-bold text-text">{fmtTokens(tokenStats.gpt.input)}</span>
              <span className="mx-1.5">·</span>
              {text('common.output')} <span className="font-bold text-text">{fmtTokens(tokenStats.gpt.output)}</span>
              <span className="mx-1.5">·</span>
              <span className="font-bold text-text">{tokenStats.gpt.calls}</span> {text('common.calls')}
            </p>
          </div>
        )}
      </div>

      {/* Claude 配置 — 可选，折叠 */}
      <div className="rounded-xl border border-border/30 overflow-hidden">
        <button
          onClick={() => setClaudeExpanded(!claudeExpanded)}
          className="w-full flex items-center justify-between px-4 py-3 hover:bg-bg/60 transition-colors"
        >
          <div className="flex items-center gap-2">
            <img src="/claude_line.png" alt="" className="w-4 h-4" />
            <h4 className="text-sm font-semibold">Claude</h4>
            <span className="text-[10px] text-muted font-medium bg-bg px-2 py-0.5 rounded-full">{text('common.optional')}</span>
            {!claudeExpanded && claudeTest ? (
              <div className="flex items-center gap-1">
                <StatusDot ok={claudeTest.ok} />
                <span className={`text-[10px] font-medium ${claudeTest.ok ? 'text-emerald-600' : 'text-signal'}`}>
                  {claudeTest.ok ? text('common.connected') : text('common.contactAdmin')}
                </span>
              </div>
            ) : null}
          </div>
          <ChevronDown size={14} className={`text-muted transition-transform duration-200 ${claudeExpanded ? 'rotate-180' : ''}`} />
        </button>
        {claudeExpanded && (
          <div className="px-4 pb-4 pt-1">
            <div className="flex items-center justify-between mb-3">
              <div className="flex items-center gap-2">
                {testingC ? (
                  <Loader2 size={12} className="text-accent animate-spin" />
                ) : claudeTest ? (
                  <div className="flex items-center gap-1">
                    <StatusDot ok={claudeTest.ok} />
                    <span className={`text-[10px] font-medium ${claudeTest.ok ? 'text-emerald-600' : 'text-signal'}`}>
                      {claudeTest.ok ? text('common.connected') : text('common.contactAdmin')}
                    </span>
                  </div>
                ) : null}
              </div>
              <button onClick={handleTestClaude} disabled={testingC}
                className="text-[11px] text-accent hover:underline disabled:opacity-50">
                {text('common.retest')}
              </button>
            </div>
            <div className="space-y-2">
              <div>
                <label className="block text-[11px] text-muted font-medium mb-1">Base URL</label>
                <input type="text" value={claudeUrl} onChange={(e) => setClaudeUrl(e.target.value)}
                  placeholder="https://api.example.com" className={inputCls} />
              </div>
              <div>
                <label className="block text-[11px] text-muted font-medium mb-1">API Key</label>
                <KeyInput value={claudeKey} onChange={setClaudeKey} placeholder="sk-..." />
              </div>
              <div>
                <label className="block text-[11px] text-muted font-medium mb-1">{text('settings.modelName')}</label>
                <input type="text" value={claudeModel} onChange={(e) => setClaudeModel(e.target.value)}
                  placeholder="claude-opus-4-6" className={inputCls} />
              </div>
            </div>
            {claudeTest && !claudeTest.ok && (
              <p className="flex items-center gap-1 text-[11px] text-signal mt-2">
                <XCircle size={11} /> {claudeTest.message}
              </p>
            )}
            {serviceUsage.claude && 'balance_usd' in serviceUsage.claude && (
              <div className="flex items-center gap-1.5 mt-2 text-[11px]">
                <span className="text-muted">{text('common.balance')}</span>
                <span className="font-semibold text-emerald-600">${String(serviceUsage.claude.balance_usd)}</span>
              </div>
            )}
            {tokenStats && (
              <div className="mt-2.5 pt-2.5 border-t border-border/20">
                <div className="flex items-center justify-between">
                  <span className="text-[10px] text-muted font-medium">{text('common.tokenUsage')}</span>
                  {tokenStats.claude.calls > 0 && (
                    <button
                      onClick={() => { resetTokenStats().then(() => getTokenStats().then(setTokenStats)) }}
                      className="text-[10px] text-muted hover:text-accent transition-colors"
                    >{text('common.reset')}</button>
                  )}
                </div>
                <p className="text-[10px] text-muted/80 mt-1">
                  {text('common.input')} <span className="font-bold text-text">{fmtTokens(tokenStats.claude.input)}</span>
                  <span className="mx-1.5">·</span>
                  {text('common.output')} <span className="font-bold text-text">{fmtTokens(tokenStats.claude.output)}</span>
                  <span className="mx-1.5">·</span>
                  <span className="font-bold text-text">{tokenStats.claude.calls}</span> {text('common.calls')}
                </p>
              </div>
            )}
          </div>
        )}
      </div>

    </div>
  )

  const handleWsTest = async (engine?: string) => {
    const eng = engine || webSearchEngine
    setWsTesting(true)
    setWsTestResult(null)
    try {
      setWsTestResult(await testWebSearch(eng))
    } catch {
      setWsTestResult({ ok: false, message: text('common.requestFailed') })
    }
    setWsTesting(false)
  }

  const renderWebSearch = () => (
    <div className="space-y-4">
      <p className="text-xs text-muted">
        {text('settings.webSearchDesc')}
      </p>

      <div className="flex gap-2">
        {([
          { id: 'gpt', label: 'GPT Web Search', desc: text('settings.builtinSearchDesc') },
          { id: 'claude', label: 'Claude Web Search', desc: text('settings.builtinSearchDesc') },
          { id: 'tavily', label: 'Tavily API', desc: text('settings.paidSearchDesc') },
        ] as const).map(({ id, label, desc }) => (
          <button
            key={id}
            onClick={async () => {
              setWebSearchEngineState(id)
              setWsTestResult(null)
              try { await setWebSearchEngine(id) } catch { /* ignore */ }
              handleWsTest(id)
            }}
            className={`settings-selectable-option flex-1 text-center text-[12px] font-medium py-2.5 rounded-xl transition-all ${
              webSearchEngine === id
                ? 'settings-selected-option bg-accent text-white shadow-sm'
                : 'bg-bg/60 text-muted ring-1 ring-border/40 hover:ring-accent/30'
            }`}
          >
            <div>{label}</div>
            <div className={`text-[10px] font-normal mt-0.5 ${
              webSearchEngine === id ? 'text-white/70' : 'text-muted/60'
            }`}>{desc}</div>
          </button>
        ))}
      </div>

      {/* 可用性检测状态 */}
      <div className="flex items-center gap-2 px-1">
        {wsTesting ? (
          <>
            <Loader2 size={13} className="animate-spin text-muted" />
            <span className="text-[11px] text-muted">{text('settings.detectingAvailability')}</span>
          </>
        ) : wsTestResult ? (
          <>
            {wsTestResult.ok ? (
              <CheckCircle2 size={13} className="text-emerald-500" />
            ) : (
              <XCircle size={13} className="text-red-400" />
            )}
            <span className={`text-[11px] ${wsTestResult.ok ? 'text-emerald-600' : 'text-red-400'}`}>
              {wsTestResult.message}
            </span>
            <button onClick={() => handleWsTest()} className="ml-auto text-muted hover:text-text transition-colors" title={text('common.refresh')}>
              <RefreshCw size={12} />
            </button>
          </>
        ) : (
          <button onClick={() => handleWsTest()} className="flex items-center gap-1.5 text-[11px] text-accent hover:text-accent/80 transition-colors">
            <RefreshCw size={12} />
            {text('settings.detectAvailability')}
          </button>
        )}
      </div>

      {webSearchEngine === 'tavily' && (
        <div className="bg-bg/60 rounded-xl p-4">
          <h4 className="text-sm font-semibold mb-1">{text('settings.tavilyConfig')}</h4>
          <p className="text-[11px] text-muted mb-3">
            {text('settings.tavilyFreePrefix')}{' '}
            <a href="https://tavily.com" target="_blank" rel="noopener" className="text-accent hover:underline">tavily.com</a>
            {' '}{text('settings.tavilyFreeSuffix')}
          </p>
          <div>
            <label className="block text-[11px] text-muted font-medium mb-1">API Key</label>
            <KeyInput value={tavilyKey} onChange={setTavilyKey} placeholder="tvly-..." />
          </div>
          <p className="text-[10px] text-muted/60 mt-2">
            {text('settings.tavilyUsagePrefix')}{' '}
            <a href="https://app.tavily.com" target="_blank" rel="noopener" className="text-accent hover:underline">Tavily dashboard</a>
            {text('settings.tavilyUsageSuffix') ? ` ${text('settings.tavilyUsageSuffix')}` : ''}
          </p>
        </div>
      )}

      {webSearchEngine === 'gpt' && (
        <div className="bg-bg/60 rounded-xl p-4">
          <p className="text-[11px] text-muted">
            {text('settings.gptWebSearchDesc')}
          </p>
        </div>
      )}

      {webSearchEngine === 'claude' && (
        <div className="bg-bg/60 rounded-xl p-4">
          <p className="text-[11px] text-muted">
            {text('settings.claudeWebSearchDesc')}
          </p>
        </div>
      )}
    </div>
  )

  const toggleDataSource = (src: string) => {
    const next = dataSources.includes(src)
      ? dataSources.filter(s => s !== src)
      : [...dataSources, src]
    if (next.length > 0) setDataSources(next)
  }

  const renderSources = () => {
    const activeSources = [
      { id: 'reddit', label: 'Reddit', desc: text('settings.sourceRedditDesc'), img: '/reddit_line.png' },
      { id: 'hackernews', label: 'HackerNews', desc: text('settings.sourceHackerNewsDesc'), img: '/hacker-news.png' },
    ]
    return (
      <div className="space-y-5">
        <div>
          <p className="text-xs text-muted mb-3">
            {text('settings.sourcesDesc')}
          </p>
          <div className="flex gap-2">
            {activeSources.map(({ id, label, desc, img }) => (
              <button
                key={id}
                onClick={() => toggleDataSource(id)}
                className={`settings-source-card flex-1 flex items-center gap-3 px-4 py-4 rounded-xl transition-all text-left ${
                  dataSources.includes(id)
                    ? 'settings-selected-card bg-accent/8 ring-2 ring-accent/40'
                    : 'bg-bg/60 ring-1 ring-border/40 hover:ring-accent/30'
                }`}
              >
                <img src={img} alt={label} className="w-5 h-5 object-contain" />
                <div>
                  <div className={`text-[13px] font-semibold ${dataSources.includes(id) ? 'text-accent' : 'text-text'}`}>{label}</div>
                  <div className="text-[11px] text-muted mt-0.5">{desc}</div>
                </div>
                <div className="ml-auto">
                  <div className={`w-5 h-5 rounded-md flex items-center justify-center transition-all ${
                    dataSources.includes(id) ? 'bg-accent' : 'bg-border/40'
                  }`}>
                    {dataSources.includes(id) && (
                      <svg width="10" height="10" viewBox="0 0 8 8" fill="none"><path d="M1.5 4L3 5.5L6.5 2" stroke="white" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/></svg>
                    )}
                  </div>
                </div>
              </button>
            ))}
          </div>
        </div>
      </div>
    )
  }

  const renderCli = () => (
    <div className="space-y-4">
      <p className="text-xs text-muted">
        {text('settings.cliDesc')}
      </p>

      {/* rdt-cli */}
      <div className={`rounded-xl border p-4 ${
        engineStatus?.rdt_status?.authenticated
          ? 'border-emerald-200 bg-emerald-50/50'
          : 'border-border/40 bg-bg/60'
      }`}>
        <div className="flex items-center justify-between mb-2">
          <div className="flex items-center gap-2.5">
            <img src="/reddit_line.png" alt="rdt-cli" className="w-5 h-5 object-contain" />
            <h4 className="text-sm font-semibold">rdt-cli</h4>
            {engineChecking && !engineStatus ? (
              <Loader2 size={12} className="text-accent animate-spin" />
            ) : engineStatus?.rdt_status?.authenticated ? (
              <span className="text-[10px] text-emerald-600 font-medium bg-emerald-100 px-2 py-0.5 rounded-full">{text('common.authenticated')}</span>
            ) : engineStatus?.rdt_status?.installed ? (
              <span className="text-[10px] text-gray-600 font-medium bg-gray-100 px-2 py-0.5 rounded-full">{text('common.notLoggedIn')}</span>
            ) : engineStatus ? (
              <span className="text-[10px] text-muted font-medium bg-bg px-2 py-0.5 rounded-full ring-1 ring-border/30">{text('common.notInstalled')}</span>
            ) : null}
          </div>
          <button onClick={() => refreshEngine(true)} disabled={engineChecking}
            className="flex items-center gap-1 text-[11px] text-accent hover:underline disabled:opacity-50">
            {engineChecking ? <Loader2 size={10} className="animate-spin" /> : <RefreshCw size={12} />}
            {text('common.refresh')}
          </button>
        </div>
        <p className="text-[11px] text-muted leading-relaxed">
          {text('settings.rdtDesc')}
        </p>
        {!engineStatus?.rdt_status?.authenticated && (
          <div className="text-xs text-text/60 space-y-1.5 mt-3 bg-black/[0.02] rounded-lg p-3">
            <p>{text('settings.cliSetupHelp')}</p>
          </div>
        )}
        {engineStatus?.rdt_status?.authenticated && (
          <p className="text-[10px] text-muted/60 mt-2">
            {text('settings.cliConnectedHelp')}
          </p>
        )}
        {engineStatus?.rdt_status?.version && (
          <p className="text-[10px] text-muted mt-1.5">{text('settings.version')}: {engineStatus.rdt_status.version}</p>
        )}
      </div>

      {/* st-cli */}
      <div className={`rounded-xl border p-4 ${
        stStatus?.available
          ? 'border-emerald-200 bg-emerald-50/50'
          : stStatus?.installed
            ? 'border-gray-200 bg-gray-50/50'
            : 'border-border/40 bg-bg/60'
      }`}>
        <div className="flex items-center justify-between mb-2">
          <div className="flex items-center gap-2.5">
            <img src="/sensortower.png" alt="st-cli" className="w-5 h-5 object-contain" />
            <h4 className="text-sm font-semibold">st-cli</h4>
            {stStatus === null ? (
              <span className="text-[10px] text-muted font-medium">{text('common.checking')}</span>
            ) : stStatus.available ? (
              <span className="text-[10px] text-emerald-600 font-medium bg-emerald-100 px-2 py-0.5 rounded-full">{text('common.connected')}</span>
            ) : stStatus.installed ? (
              <span className="text-[10px] text-gray-600 font-medium bg-gray-100 px-2 py-0.5 rounded-full">{text('common.notLoggedIn')}</span>
            ) : (
              <span className="text-[10px] text-muted font-medium bg-bg px-2 py-0.5 rounded-full ring-1 ring-border/30">{text('common.notInstalled')}</span>
            )}
          </div>
          <button
            onClick={async () => {
              setStChecking(true)
              try { setStStatus(await getSensorTowerStatus()) } catch { /* */ }
              setStChecking(false)
            }}
            disabled={stChecking}
            className="flex items-center gap-1 text-[11px] text-accent hover:underline disabled:opacity-50"
          >
            {stChecking ? <Loader2 size={10} className="animate-spin" /> : <RefreshCw size={12} />}
            {text('common.refresh')}
          </button>
        </div>
        <p className="text-[11px] text-muted leading-relaxed">
          {text('settings.stDesc')}
        </p>
        {!stStatus?.available && (
          <div className="text-xs text-text/60 space-y-1.5 mt-3 bg-black/[0.02] rounded-lg p-3">
            <p>{text('settings.cliSetupHelp')}</p>
          </div>
        )}
        {stStatus?.available && (
          <p className="text-[10px] text-muted/60 mt-2">
            {text('settings.cliConnectedHelp')}
          </p>
        )}
      </div>

    </div>
  )

  const modelOptions = [
    { value: 'claude', label: 'Claude', icon: '/claude_line.png' },
    { value: 'gpt', label: 'GPT', icon: '/openai_line.png' },
  ]

  const handleStartEditName = (key: string) => {
    setEditingRoleName(key)
    setEditingNameValue(roleNames[key] || '')
  }

  const handleFinishEditName = async () => {
    if (!editingRoleName) return
    const trimmed = editingNameValue.trim()
    if (trimmed && trimmed !== roleNames[editingRoleName]) {
      const updated = { ...roleNames, [editingRoleName]: trimmed.slice(0, 10) }
      setRoleNames(updated)
      try { await saveRoleNames(updated) } catch { /* */ }
    }
    setEditingRoleName(null)
  }

  const renderFeishu = () => {
    const feishuSetupSteps = list('settings.feishuSetupSteps')
    const feishuPermissions = list('settings.feishuPermissions')
    return (
    <div className="space-y-4">
      <p className="text-xs text-muted leading-relaxed">
        {text('settings.feishuDesc')}
      </p>
      <div className="bg-bg/60 rounded-xl p-4 space-y-3">
        <div className="flex items-center gap-2 mb-1">
          <img src="/link_3_line.png" alt="" className="w-4 h-4 opacity-60" />
          <h4 className="text-sm font-semibold">{text('settings.feishuPlatform')}</h4>
          {feishuAppId && feishuAppSecret ? (
            <span className="flex items-center gap-1 text-[10px] font-medium text-emerald-600">
              <CheckCircle2 size={10} /> {text('common.configured')}
            </span>
          ) : (
            <span className="flex items-center gap-1 text-[10px] font-medium text-muted">
              <XCircle size={10} /> {text('common.notConfigured')}
            </span>
          )}
        </div>
        <div>
          <label className="block text-[11px] text-muted font-medium mb-1">App ID</label>
          <input type="text" value={feishuAppId} onChange={(e) => setFeishuAppId(e.target.value)}
            placeholder="cli_xxxxxx" className={inputCls} />
        </div>
        <div>
          <label className="block text-[11px] text-muted font-medium mb-1">App Secret</label>
          <KeyInput value={feishuAppSecret} onChange={setFeishuAppSecret} placeholder="xxxxxx" />
        </div>
      </div>
      <div className="bg-bg/60 rounded-xl p-4">
        <h4 className="text-[13px] font-semibold mb-2">{text('settings.setupSteps')}</h4>
        <ol className="text-xs text-text/70 space-y-1.5 list-decimal list-inside">
          <li>
            {text('settings.feishuStepVisitPrefix')}{' '}
            <a href="https://open.feishu.cn" target="_blank" rel="noopener" className="text-accent hover:underline">{text('settings.feishuPlatform')}</a>
            {' '}{text('settings.feishuStepVisitSuffix')}
          </li>
          <li>{feishuSetupSteps[1]}</li>
          <li>{feishuSetupSteps[2]}
            <ul className="ml-4 mt-1 space-y-0.5 text-[11px] text-muted list-disc list-inside">
              {feishuPermissions.map((item) => <li key={item}>{item}</li>)}
            </ul>
          </li>
          <li>{feishuSetupSteps[3]}</li>
        </ol>
      </div>
    </div>
    )
  }

  const [guideOpenSections, setGuideOpenSections] = useState<Record<string, boolean>>({ qa: true })
  const toggleGuideSection = (key: string) => setGuideOpenSections(prev => ({ ...prev, [key]: !prev[key] }))

  const renderGuide = () => {
    const Arrow = () => (
      <div className="flex justify-center py-1">
        <svg width="12" height="18" viewBox="0 0 12 18" fill="none"><path d="M6 0v14M1 10l5 6 5-6" stroke="#bbb" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/></svg>
      </div>
    )

    const qaItems = list('settings.qaItems').map((item) => {
      const [q, a] = item.split('|')
      return { q, a }
    })
    const errorItems = list('settings.errorItems').map((item) => {
      const [error, explanation] = item.split('|')
      return { error, explanation }
    })
    const workflowSteps = list('settings.workflowSteps')
    const workflowModeTags = list('settings.workflowModeTags')
    const workflowSourceDescriptions = list('settings.workflowSourceDescriptions')
    const workflowAiItems = list('settings.workflowAiItems').map((item) => {
      const [label, desc] = item.split('|')
      return { label, desc }
    })
    const reportTags = list('settings.reportTags')

    return (
      <div className="space-y-4">
        {/* Contact info */}
        <div className="bg-accent/5 border border-accent/15 rounded-xl px-4 py-3 text-[12px] text-text/80 leading-relaxed">
          <LocalizedText>
            {text('settings.guideContactPrefix')}{' '}
            <a href="https://github.com/jason2kkk/lumon/issues" target="_blank" rel="noopener noreferrer" className="text-accent font-medium hover:underline">{text('settings.feedbackForm')}</a>
            {text('settings.guideContactEnd')}
          </LocalizedText>
        </div>

        {/* Q&A — collapsible, default open */}
        <div className="rounded-xl border border-border/30 overflow-hidden">
          <button onClick={() => toggleGuideSection('qa')} className="w-full flex items-center justify-between px-4 py-3 text-[13px] font-semibold text-text hover:bg-bg/60 transition-colors">
            <LocalizedText>{text('settings.faq')}</LocalizedText>
            <ChevronDown size={14} className={`text-muted transition-transform duration-200 ${guideOpenSections.qa ? 'rotate-180' : ''}`} />
          </button>
          {guideOpenSections.qa && (
            <div className="px-4 pb-4 pt-1 space-y-3">
              {qaItems.map((item, i) => (
                <div key={i}>
                  <p className="text-[12px] font-semibold text-text mb-1">Q: {item.q}</p>
                  <p className="text-[12px] text-muted leading-relaxed">{item.a}</p>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Workflow — collapsible, default closed */}
        <div className="rounded-xl border border-border/30 overflow-hidden">
          <button onClick={() => toggleGuideSection('flow')} className="w-full flex items-center justify-between px-4 py-3 text-[13px] font-semibold text-text hover:bg-bg/60 transition-colors">
            <LocalizedText>{text('settings.workflow')}</LocalizedText>
            <ChevronDown size={14} className={`text-muted transition-transform duration-200 ${guideOpenSections.flow ? 'rotate-180' : ''}`} />
          </button>
          {guideOpenSections.flow && (
            <div className="px-4 pb-4 pt-1">
              <p className="text-xs text-muted leading-relaxed mb-4">
                {text('settings.workflowIntro')}
              </p>

              <div className="rounded-xl border border-blue-200 bg-blue-50/50 p-3">
                <p className="text-[10px] font-semibold text-blue-500 mb-2 tracking-wide">{workflowSteps[0]}</p>
                <div className="flex gap-2">
                  {workflowModeTags.map(m => (
                    <span key={m} className="flex-1 text-center text-[11px] font-medium py-1.5 rounded-lg bg-white border border-blue-200/60 text-text/80">{m}</span>
                  ))}
                </div>
              </div>

              <Arrow />

              <div className="rounded-xl border border-gray-200 bg-gray-50/60 p-3">
                <p className="text-[10px] font-semibold text-gray-500 mb-2 tracking-wide">{workflowSteps[1]}</p>
                <div className="grid grid-cols-2 gap-2">
                  {[
                    { logo: '/logo-reddit.svg', name: 'Reddit', via: 'rdt-cli', desc: workflowSourceDescriptions[0] },
                    { logo: '/logo-hn.svg', name: 'HackerNews', via: 'Algolia API', desc: workflowSourceDescriptions[1] },
                    { logo: '/logo-tavily.svg', name: 'WebSearch', via: 'GPT / Tavily', desc: workflowSourceDescriptions[2] },
                    { logo: '/sensortower.png', name: 'SensorTower', via: 'st-cli', desc: workflowSourceDescriptions[3] },
                  ].map(s => (
                    <div key={s.name} className="flex items-center gap-2.5 bg-white rounded-lg border border-gray-200/70 px-2.5 py-2">
                      <img src={s.logo} alt="" className="w-5 h-5 shrink-0 rounded" />
                      <div className="min-w-0">
                        <p className="text-[11px] font-semibold leading-tight">{s.name}</p>
                        <p className="text-[9px] text-muted truncate">via {s.via}</p>
                        <p className="text-[9px] text-muted truncate">{s.desc}</p>
                      </div>
                    </div>
                  ))}
                </div>
              </div>

              <Arrow />

              <div className="rounded-xl border border-violet-200 bg-violet-50/40 p-3">
                <p className="text-[10px] font-semibold text-violet-500 mb-2 tracking-wide">{workflowSteps[2]}</p>
                <div className="space-y-1.5">
                  {workflowAiItems.map(s => (
                    <div key={s.label} className="flex items-center gap-2.5 bg-white rounded-lg border border-violet-200/50 px-2.5 py-2">
                      <img src="/logo-claude.svg" alt="" className="w-4 h-4 shrink-0" />
                      <div className="min-w-0">
                        <p className="text-[11px] font-semibold leading-tight">{s.label}</p>
                        <p className="text-[9px] text-muted">{s.desc}</p>
                      </div>
                    </div>
                  ))}
                </div>
              </div>

              <Arrow />

              <div className="rounded-xl border border-emerald-200 bg-emerald-50/40 p-3">
                <p className="text-[10px] font-semibold text-emerald-500 mb-2 tracking-wide">{workflowSteps[3]}</p>
                <div className="grid grid-cols-2 gap-2">
                  <div className="bg-white rounded-lg border border-emerald-200/50 px-2.5 py-2">
                    <div className="flex items-center gap-1.5 mb-1">
                      <img src="/logo-openai.svg" alt="" className="w-3.5 h-3.5" />
                      <p className="text-[11px] font-semibold">{text('settings.competitorSearch')}</p>
                    </div>
                    <p className="text-[9px] text-muted">{text('settings.competitorSearchDesc')}</p>
                  </div>
                  <div className="bg-white rounded-lg border border-emerald-200/50 px-2.5 py-2">
                    <div className="flex items-center gap-1.5 mb-1">
                      <img src="/sensortower.png" alt="" className="w-3.5 h-3.5 rounded" />
                      <p className="text-[11px] font-semibold">{text('settings.marketData')}</p>
                    </div>
                    <p className="text-[9px] text-muted">{text('settings.marketDataDesc')}</p>
                  </div>
                </div>
              </div>

              <Arrow />

              <div className="rounded-xl border border-gray-200 bg-gray-50/60 p-3">
                <p className="text-[10px] font-semibold text-gray-600 mb-2 tracking-wide">{workflowSteps[4]}</p>
                <div className="bg-white rounded-lg border border-gray-200/70 px-3 py-2.5">
                  <div className="flex items-center gap-2 mb-1.5">
                    <img src="/logo-claude.svg" alt="" className="w-4 h-4" />
                    <p className="text-[11px] font-semibold">{text('settings.reportWriting')}</p>
                  </div>
                  <div className="flex flex-wrap gap-1.5">
                    {reportTags.map(t => (
                      <span key={t} className="text-[9px] px-1.5 py-0.5 rounded bg-gray-100 text-gray-700 font-medium">{t}</span>
                    ))}
                  </div>
                </div>
              </div>
            </div>
          )}
        </div>

        {/* Error explanations — collapsible, default closed */}
        <div className="rounded-xl border border-border/30 overflow-hidden">
          <button onClick={() => toggleGuideSection('errors')} className="w-full flex items-center justify-between px-4 py-3 text-[13px] font-semibold text-text hover:bg-bg/60 transition-colors">
            <LocalizedText>{text('settings.errorGuide')}</LocalizedText>
            <ChevronDown size={14} className={`text-muted transition-transform duration-200 ${guideOpenSections.errors ? 'rotate-180' : ''}`} />
          </button>
          {guideOpenSections.errors && (
            <div className="px-4 pb-4 pt-1 space-y-2.5">
              {errorItems.map((item, i) => (
                <div key={i}>
                  <p className="text-[11px] font-semibold text-signal/90 mb-0.5">{item.error}</p>
                  <p className="text-[11px] text-muted leading-relaxed">{item.explanation}</p>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    )
  }

  const renderRoles = () => (
    <div className="space-y-4">
      <p className="text-xs text-muted">
        {text('settings.rolesDesc')}
      </p>
      <div className="space-y-2">
        {[
          { key: 'director', desc: text('settings.roles.director') },
          { key: 'analyst', desc: text('settings.roles.analyst') },
          { key: 'critic', desc: text('settings.roles.critic') },
          { key: 'investor', desc: text('settings.roles.investor') },
        ].map(({ key, desc }) => {
          const current = modelOptions.find(o => o.value === (roleModels[key] || 'claude')) || modelOptions[0]
          const isOpen = openRoleDropdown === key
          const isEditing = editingRoleName === key
          const displayName = localizeRoleName(key, roleNames[key], language)
          return (
            <div key={key} className="flex items-center justify-between bg-bg/60 rounded-xl px-4 py-3">
              <div>
                {isEditing ? (
                  <input
                    autoFocus
                    type="text"
                    value={editingNameValue}
                    onChange={(e) => setEditingNameValue(e.target.value)}
                    onBlur={handleFinishEditName}
                    onKeyDown={(e) => { if (e.key === 'Enter') handleFinishEditName(); if (e.key === 'Escape') setEditingRoleName(null) }}
                    maxLength={10}
                    className="text-sm font-medium bg-white border border-accent/40 rounded-lg px-2 py-0.5 w-24 focus:outline-none focus:ring-2 focus:ring-accent/20"
                  />
                ) : (
                  <p
                    className="text-sm font-medium cursor-pointer hover:text-accent transition-colors"
                    onDoubleClick={() => handleStartEditName(key)}
                    title={text('settings.doubleClickRename')}
                  >
                    {displayName}
                  </p>
                )}
                <p className="text-[11px] text-muted">{desc}</p>
              </div>
              <div className="relative">
                <button
                  type="button"
                  onClick={() => setOpenRoleDropdown(isOpen ? null : key)}
                  onBlur={() => setTimeout(() => setOpenRoleDropdown(null), 150)}
                  className="flex items-center gap-2 text-xs border border-border/60 rounded-xl pl-3 pr-7 py-1.5 bg-card hover:border-accent/30 focus:outline-none focus:ring-2 focus:ring-accent/15 transition-colors"
                >
                  <img src={current.icon} alt="" className="w-3.5 h-3.5" />
                  <span>{current.label}</span>
                </button>
                <ChevronDown size={12} className="absolute right-2.5 top-1/2 -translate-y-1/2 text-muted pointer-events-none" />
                {isOpen && (
                  <div className="absolute right-0 top-full mt-1 bg-card border border-border/60 rounded-xl shadow-lg z-50 overflow-hidden min-w-[110px]">
                    {modelOptions.map((opt) => (
                      <button
                        key={opt.value}
                        type="button"
                        onMouseDown={(e) => {
                          e.preventDefault()
                          setRoleModels(prev => ({ ...prev, [key]: opt.value }))
                          setOpenRoleDropdown(null)
                        }}
                        className={`flex items-center gap-2 w-full px-3 py-2 text-xs hover:bg-accent/8 transition-colors ${
                          opt.value === current.value ? 'bg-accent/5 font-medium' : ''
                        }`}
                      >
                        <img src={opt.icon} alt="" className="w-3.5 h-3.5" />
                        <span>{opt.label}</span>
                      </button>
                    ))}
                  </div>
                )}
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )

  return (
    <AnimatePresence>
      {showSettingsDialog && (
      <motion.div
        key="settings-overlay"
        initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
        transition={{ duration: 0.2 }}
        className="fixed inset-0 z-50 flex items-center justify-center"
      >
        <div className="absolute inset-0 bg-black/25" onClick={handleClose} />
        <motion.div
          initial={{ scale: 0.95, opacity: 0 }}
          animate={{ scale: 1, opacity: 1 }}
          exit={{ scale: 0.97, opacity: 0 }}
          transition={{ duration: 0.2, ease: [0.25, 0.1, 0.25, 1] }}
          className="settings-modal relative bg-card rounded-3xl shadow-xl w-[680px] h-[80vh] max-h-[900px] flex overflow-hidden max-md:w-full max-md:max-w-full max-md:h-full max-md:max-h-full max-md:rounded-none max-md:flex-col"
        >
          <div className="w-[180px] bg-bg/50 py-5 shrink-0 relative after:absolute after:right-0 after:top-4 after:bottom-4 after:w-[1px] after:bg-border/30 max-md:w-full max-md:py-3 max-md:overflow-x-auto max-md:after:hidden">
            <h2 className="text-sm font-bold px-4 mb-4 max-md:hidden"><LocalizedText>{text('settings.title')}</LocalizedText></h2>
            <nav className="space-y-0.5 px-2 max-md:flex max-md:gap-1 max-md:space-y-0 max-md:px-3 max-md:min-w-max">
              {sections.map(({ id, img }) => {
                const isActive = activeSection === id
                let statusIndicator = null
                if (id === 'models') {
                  const cOk = claudeTest?.ok ?? null
                  const gOk = gptTest?.ok ?? null
                  const anyTesting = testingC || testingG
                  if (anyTesting) {
                    statusIndicator = <Loader2 size={10} className="text-accent animate-spin" />
                  } else if (cOk !== null || gOk !== null) {
                    const anyConnected = (cOk === true) || (gOk === true)
                    if (anyConnected) {
                      statusIndicator = <StatusDot ok={true} />
                    } else {
                      statusIndicator = <StatusDot ok={false} />
                    }
                  }
                } else if (id === 'cli') {
                  const rdtOk = engineStatus?.rdt_status?.authenticated ?? false
                  const stOk = stStatus?.available ?? false
                  const rdtLoaded = engineStatus !== null
                  const stLoaded = stStatus !== null
                  if (rdtLoaded && stLoaded) {
                    statusIndicator = <StatusDot ok={rdtOk && stOk} />
                  } else if (engineChecking) {
                    statusIndicator = <Loader2 size={10} className="text-accent animate-spin" />
                  }
                }
                return (
                  <button
                    key={id}
                    onClick={() => setActiveSection(id)}
                    className={`settings-nav-item w-full flex items-center gap-2.5 px-3 py-2.5 rounded-xl text-left transition-all text-[13px] max-md:w-auto max-md:whitespace-nowrap max-md:shrink-0 max-md:px-3 max-md:py-2 ${
                      isActive
                        ? 'settings-nav-item--active bg-card shadow-sm font-semibold text-text'
                        : 'text-muted hover:text-text hover:bg-card/50'
                    }`}
                  >
                    <img src={img} alt="" className={`w-4 h-4 ${isActive ? 'opacity-80' : 'opacity-40'}`} />
                    <LocalizedText className="flex-1 max-md:flex-none">{sectionText(id, 'label')}</LocalizedText>
                    {statusIndicator}
                    {isActive && <ChevronRight size={12} className="text-muted max-md:hidden" />}
                  </button>
                )
              })}
            </nav>
          </div>

          <div className="flex-1 flex flex-col min-h-0">
            <div className="flex items-center justify-between px-5 py-4 shrink-0">
              <div>
                <h3 className="text-sm font-bold"><LocalizedText>{sectionText(activeSection, 'label')}</LocalizedText></h3>
                <LocalizedText as="p" className="text-[11px] text-muted mt-0.5">{sectionText(activeSection, 'desc')}</LocalizedText>
              </div>
              <button onClick={handleClose} className="w-7 h-7 rounded-xl flex items-center justify-center text-muted hover:text-text hover:bg-bg transition-colors">
                <X size={16} />
              </button>
            </div>
            <div className="mx-5 h-[1px] bg-border/30" />

            <div className="flex-1 overflow-y-auto scrollbar-auto px-5 py-4">
              {activeSection === 'models' && renderModels()}
              {activeSection === 'websearch' && renderWebSearch()}
              {activeSection === 'sources' && renderSources()}
              {activeSection === 'cli' && renderCli()}
              {activeSection === 'roles' && renderRoles()}
              {activeSection === 'feishu' && renderFeishu()}
              {activeSection === 'guide' && renderGuide()}
            </div>

            <div className="mx-5 h-[1px] bg-border/30" />
            <div className="shrink-0 px-5 py-3 flex items-center gap-3">
              {saveError && (
                <p className="text-xs text-signal flex items-center gap-1 flex-1">
                  <XCircle size={12} /> {saveError}
                </p>
              )}
              <div className="flex-1" />
              <button onClick={handleSave} disabled={saving}
                className="flex items-center gap-2 bg-accent text-white text-[13px] font-medium h-9 px-5 rounded-xl hover:opacity-90 disabled:opacity-50 transition-opacity">
                {saving ? <Loader2 size={14} className="animate-spin" /> : <Save size={14} />}
                <LocalizedText>{saved ? text('settings.saved') : text('settings.save')}</LocalizedText>
              </button>
            </div>
          </div>
        </motion.div>
      </motion.div>
      )}
    </AnimatePresence>
  )
}

import { useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { X } from 'lucide-react'
import { LocalizedText, useI18n } from '../i18n'

interface HelpStep {
  title: string
  description: string
}

interface HelpDialogProps {
  title?: string
  steps?: HelpStep[]
  titleKey?: string
  stepsKey?: string
}

export function HelpButton({ title, steps, titleKey, stepsKey }: HelpDialogProps) {
  const { text, list, format } = useI18n()
  const [open, setOpen] = useState(false)
  const localizedTitle = titleKey ? text(titleKey) : title ? text(title) : ''
  const localizedSteps = stepsKey
    ? list(stepsKey).map((item) => {
      const [stepTitle, ...descriptionParts] = item.split('|')
      return { title: stepTitle, description: descriptionParts.join('|') }
    })
    : steps ?? []

  return (
    <>
      <button
        onClick={() => setOpen(true)}
        className="w-6 h-6 rounded-full flex items-center justify-center opacity-40 hover:opacity-80 transition-opacity"
        title={format('help.tooltip', { title: localizedTitle })}
      >
        <img src="/question_line.png" alt="" className="w-3.5 h-3.5" />
      </button>
      <AnimatePresence>
        {open && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 backdrop-blur-sm"
            onClick={(e) => { if (e.target === e.currentTarget) setOpen(false) }}
          >
            <motion.div
              initial={{ opacity: 0, scale: 0.95, y: 10 }}
              animate={{ opacity: 1, scale: 1, y: 0 }}
              exit={{ opacity: 0, scale: 0.95, y: 10 }}
              transition={{ duration: 0.2 }}
              className="bg-card rounded-3xl shadow-2xl border border-border/30 w-full max-w-md max-md:max-w-[calc(100vw-2rem)] max-h-[80vh] overflow-hidden flex flex-col"
            >
              <div className="flex items-center justify-between px-6 pt-5 pb-3">
                <div className="flex items-center gap-2">
                  <img src="/question_line.png" alt="" className="w-4 h-4 opacity-60" />
                  <h2 className="text-[15px] font-bold text-text">
                    <LocalizedText>{localizedTitle}</LocalizedText>
                  </h2>
                </div>
                <button
                  onClick={() => setOpen(false)}
                  aria-label={text('common.close')}
                  className="w-7 h-7 rounded-lg flex items-center justify-center text-muted hover:text-text hover:bg-bg transition-colors"
                >
                  <X size={14} />
                </button>
              </div>

              <div className="flex-1 overflow-y-auto px-6 pb-6">
                <div className="space-y-4">
                  {localizedSteps.map((step, i) => (
                    <div key={i} className="flex gap-3">
                      <div className="shrink-0 w-6 h-6 rounded-full bg-accent/10 flex items-center justify-center text-[11px] font-bold text-accent mt-0.5">
                        {i + 1}
                      </div>
                      <div className="flex-1 min-w-0">
                        <h3 className="text-[13px] font-semibold text-text mb-1">
                          <LocalizedText>{step.title}</LocalizedText>
                        </h3>
                        <LocalizedText as="p" className="text-[12px] text-muted leading-relaxed whitespace-pre-line">
                          {step.description}
                        </LocalizedText>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>
    </>
  )
}

// 各模块的帮助内容
export const FETCH_HELP: HelpDialogProps = {
  titleKey: 'help.fetchTitle',
  stepsKey: 'help.fetchSteps',
}

export const DEBATE_HELP: HelpDialogProps = {
  titleKey: 'help.debateTitle',
  stepsKey: 'help.debateSteps',
}

export const REPORT_HELP: HelpDialogProps = {
  titleKey: 'help.reportTitle',
  stepsKey: 'help.reportSteps',
}

import { AnimatePresence, motion } from 'framer-motion'
import { Check, Search, ShieldCheck, UsersRound, X } from 'lucide-react'
import { useAppStore } from '../stores/app'
import { useI18n } from '../i18n'
import { APP_VERSION } from '../version'

export default function WhatsNewModal() {
  const visible = useAppStore((state) => state.whatsNewVisible)
  const setVisible = useAppStore((state) => state.setWhatsNewVisible)
  const { text, list } = useI18n()
  const items = list('whatsNew.items')
  const icons = [Search, ShieldCheck, UsersRound]

  const close = () => {
    setVisible(false)
    try { localStorage.setItem('lumon_whats_new_seen', APP_VERSION) } catch { /* local preference only */ }
  }

  return (
    <AnimatePresence>
      {visible && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          className="fixed inset-0 z-[80] flex items-center justify-center p-4"
          role="dialog"
          aria-modal="true"
          aria-labelledby="whats-new-title"
        >
          <button type="button" className="absolute inset-0 bg-black/30 backdrop-blur-[2px]" onClick={close} aria-label={text('common.close')} />
          <motion.div
            initial={{ opacity: 0, y: 16, scale: 0.97 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: 8, scale: 0.98 }}
            transition={{ duration: 0.2, ease: 'easeOut' }}
            className="relative w-full max-w-[460px] overflow-hidden rounded-lg border border-border/55 bg-card shadow-2xl"
          >
            <div className="flex items-start justify-between gap-4 border-b border-border/40 px-5 py-4">
              <div>
                <div className="mb-1 flex items-center gap-2">
                  <span className="rounded-full bg-accent/10 px-2 py-0.5 text-[10px] font-semibold text-accent">v{APP_VERSION}</span>
                  <span className="text-[10px] text-muted">{text('whatsNew.releaseLabel')}</span>
                </div>
                <h2 id="whats-new-title" className="text-[18px] font-bold text-text">{text('whatsNew.title')}</h2>
                <p className="mt-1 text-[12px] leading-relaxed text-muted">{text('whatsNew.subtitle')}</p>
              </div>
              <button type="button" onClick={close} className="flex size-8 shrink-0 items-center justify-center rounded-md text-muted hover:bg-bg hover:text-text" aria-label={text('common.close')}>
                <X size={16} />
              </button>
            </div>

            <div className="space-y-1 p-3">
              {items.map((item, index) => {
                const Icon = icons[index] || Check
                const [title, description = ''] = item.split('|')
                return (
                  <div key={title} className="flex gap-3 rounded-md px-3 py-3 hover:bg-bg/55">
                    <div className="flex size-9 shrink-0 items-center justify-center rounded-md bg-accent/8 text-accent">
                      <Icon size={17} />
                    </div>
                    <div className="min-w-0">
                      <p className="text-[13px] font-semibold text-text">{title}</p>
                      <p className="mt-0.5 text-[12px] leading-relaxed text-muted">{description}</p>
                    </div>
                  </div>
                )
              })}
            </div>

            <div className="flex justify-end border-t border-border/40 px-5 py-3.5">
              <button type="button" onClick={close} className="inline-flex h-9 items-center gap-1.5 rounded-md bg-text px-4 text-[12px] font-semibold text-bg hover:opacity-90">
                <Check size={14} />
                {text('whatsNew.done')}
              </button>
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  )
}

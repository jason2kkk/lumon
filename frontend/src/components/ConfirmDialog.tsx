import { motion, AnimatePresence } from 'framer-motion'
import { AlertTriangle } from 'lucide-react'
import { useI18n } from '../i18n'

interface Props {
  open: boolean
  title: string
  message: string
  confirmLabel?: string
  cancelLabel?: string
  onConfirm: () => void
  onCancel: () => void
}

export default function ConfirmDialog({
  open, title, message,
  confirmLabel,
  cancelLabel,
  onConfirm, onCancel,
}: Props) {
  const { text } = useI18n()
  if (!open) return null

  return (
    <AnimatePresence>
      <motion.div
        initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
        className="fixed inset-0 z-50 flex items-center justify-center"
      >
        <div className="absolute inset-0 bg-black/25" onClick={onCancel} />
        <motion.div
          initial={{ scale: 0.95, opacity: 0 }}
          animate={{ scale: 1, opacity: 1 }}
          className="relative bg-card rounded-2xl shadow-xl w-[360px] max-w-[calc(100vw-2rem)] p-5"
        >
          <div className="flex items-start gap-3 mb-4">
            <div className="w-8 h-8 rounded-xl bg-gray-100 flex items-center justify-center shrink-0 mt-0.5">
              <AlertTriangle size={16} className="text-gray-500" />
            </div>
            <div>
              <h3 className="text-sm font-semibold mb-1">{title}</h3>
              <p className="text-xs text-muted leading-relaxed">{message}</p>
            </div>
          </div>
          <div className="flex justify-end gap-2">
            <button
              onClick={onCancel}
              className="text-xs font-medium text-muted border border-border/60 px-4 py-2 rounded-xl hover:border-accent/40 transition-colors"
            >
              {cancelLabel || text('common.cancel')}
            </button>
            <button
              onClick={onConfirm}
              className="text-xs font-medium text-white bg-accent px-4 py-2 rounded-xl hover:opacity-90 transition-opacity"
            >
              {confirmLabel || text('common.confirm')}
            </button>
          </div>
        </motion.div>
      </motion.div>
    </AnimatePresence>
  )
}

import { memo, useMemo, useState } from 'react'
import { motion } from 'framer-motion'
import { ChevronDown } from 'lucide-react'
import type { ChatMessage as ChatMessageType } from '../types'
import { localizeRoleName, useI18n } from '../i18n'

export function parseThinkTags(content: string): {
  chatParts: string[]
  thinkParts: string[]
  isThinking: boolean
} {
  if (!content) return { chatParts: [], thinkParts: [], isThinking: false }

  const chatParts: string[] = []
  const thinkParts: string[] = []
  let remaining = content
  let isThinking = false

  while (remaining.length > 0) {
    const openIdx = remaining.indexOf('<think>')
    if (openIdx === -1) {
      if (isThinking) {
        thinkParts.push(remaining)
      } else {
        chatParts.push(remaining)
      }
      break
    }

    if (openIdx > 0) {
      chatParts.push(remaining.slice(0, openIdx))
    }

    const afterOpen = remaining.slice(openIdx + 7)
    const closeIdx = afterOpen.indexOf('</think>')
    if (closeIdx === -1) {
      thinkParts.push(afterOpen)
      isThinking = true
      break
    }

    thinkParts.push(afterOpen.slice(0, closeIdx))
    remaining = afterOpen.slice(closeIdx + 8)
  }

  return { chatParts, thinkParts, isThinking }
}

export function extractChatText(content: string): string {
  const { chatParts } = parseThinkTags(content)
  return chatParts.join('').trim()
}

export function extractDetailText(content: string): string {
  const { thinkParts } = parseThinkTags(content)
  return thinkParts.join('\n\n').trim()
}

function collapseBlankLines(text: string): string {
  return text.replace(/\n{3,}/g, '\n\n').replace(/^\n+/, '').replace(/\n+$/, '')
}

const COLLAPSED_LINES = 7

const ROLE_FALLBACK: Record<string, string> = {
  analyst: '产品经理', critic: '杠精', director: '导演', researcher: '调研员', investor: '投资人',
}

const ROLE_BG: Record<string, string> = {
  analyst: 'bg-emerald-50/40 border border-emerald-100/30',
  critic: 'bg-gray-50/50 border border-gray-200/35',
  director: 'bg-blue-50/30 border border-blue-100/25',
  researcher: 'bg-violet-50/30 border border-violet-100/25',
  investor: 'bg-slate-50/50 border border-slate-200/35',
}

function getRoleIcon(role: string, provider?: 'claude' | 'gpt'): string {
  if (role === 'director') return '/vibe_coding_line.png'
  if (role === 'investor') return '/head_ai_line.png'
  if (provider === 'gpt') return '/openai_line.png'
  return '/claude_line.png'
}

const STREAMING_ICON_ANIMATE = { scale: [1, 0.7, 1], opacity: [0.8, 0.4, 0.8] }
const IDLE_ICON_ANIMATE = { scale: 1, opacity: 0.7 }
const STREAMING_ICON_TRANSITION = { duration: 1.6, repeat: Infinity, ease: 'easeInOut' as const }
const IDLE_ICON_TRANSITION = { duration: 0.3 }
const MSG_ENTER = { initial: { opacity: 0, y: 6 }, animate: { opacity: 1, y: 0 }, transition: { duration: 0.15 } }
const DIVIDER_ENTER = { initial: { opacity: 0, scaleX: 0.6 }, animate: { opacity: 1, scaleX: 1 }, transition: { duration: 0.3 } }

interface Props {
  message: ChatMessageType
  roleNames: Record<string, string>
}

function ChatMessageComponent({ message, roleNames }: Props) {
  const { text, language } = useI18n()
  const [expanded, setExpanded] = useState(false)

  const { chatParts, isThinking } = useMemo(
    () => message.role === 'human'
      ? { chatParts: [message.content], isThinking: false }
      : parseThinkTags(message.content),
    [message.content, message.role],
  )

  if (message.topicDivider) {
    const { index, title, total } = message.topicDivider
    return (
      <motion.div {...DIVIDER_ENTER} className="flex items-center gap-3 py-2">
        <div className="flex-1 h-[1px] bg-border/40" />
        <span className="text-[11px] font-medium text-muted/60 whitespace-nowrap">
          {text('debate.topic')} {index + 1}/{total}: {title}
        </span>
        <div className="flex-1 h-[1px] bg-border/40" />
      </motion.div>
    )
  }

  const chatText = collapseBlankLines(chatParts.join('').replace(/\[(STRUCTURAL|MINOR)\]\s*/gi, ''))
  const hasThinkContent = message.content.includes('<think>')

  const lines = chatText.split('\n')
  const needsCollapse = !message.streaming && lines.length > COLLAPSED_LINES + 2
  const displayText = needsCollapse && !expanded
    ? lines.slice(0, COLLAPSED_LINES).join('\n')
    : chatText

  if (message.role === 'human') {
    return (
      <motion.div {...MSG_ENTER} className="flex justify-end">
        <div className="max-w-[75%] bg-accent text-white rounded-3xl rounded-br-sm px-4 py-2.5 shadow-sm">
          <p className="text-[13px] leading-relaxed whitespace-pre-wrap">{message.content}</p>
        </div>
      </motion.div>
    )
  }

  const roleName = localizeRoleName(message.role, roleNames[message.role] || ROLE_FALLBACK[message.role], language)
  const iconSrc = getRoleIcon(message.role, message.provider)
  const roleBg = ROLE_BG[message.role] || 'bg-gray-50/60 border border-gray-100/40'

  return (
    <motion.div
      {...MSG_ENTER}
      className={`group rounded-2xl p-3 ${roleBg}`}
    >
      <div className="flex items-center gap-2 mb-1.5">
        <motion.img
          src={iconSrc}
          alt=""
          className="w-4 h-4"
          animate={message.streaming ? STREAMING_ICON_ANIMATE : IDLE_ICON_ANIMATE}
          transition={message.streaming ? STREAMING_ICON_TRANSITION : IDLE_ICON_TRANSITION}
        />
        <span className="text-xs font-semibold text-text/70">{roleName}</span>
      </div>

      <div className="pl-6">
        {message.streaming && !displayText && (
          <p className="text-[13px] text-muted/50 animate-pulse">
            {isThinking ? text('debate.deepThinking') : text('debate.thinking')}
          </p>
        )}
        {displayText && (
          <div className="relative">
            <p className="text-[13px] leading-[1.7] whitespace-pre-wrap break-words text-text/90">
              {displayText}
              {message.streaming && !isThinking && (
                <span className="inline-block w-1.5 h-4 bg-accent/40 ml-0.5 animate-pulse rounded-sm align-middle" />
              )}
            </p>

          </div>
        )}

        {/* 兜底：模型只输出了 <think> 内容但对话部分为空 */}
        {!message.streaming && !displayText && hasThinkContent && (
          <p className="text-[13px] text-muted/60 italic">{text('debate.analysisDonePanel')}</p>
        )}

        {/* Expand / Collapse toggle */}
        {needsCollapse && (
          <button
            onClick={(e) => { e.stopPropagation(); setExpanded(!expanded) }}
            className="mt-1 flex items-center gap-1 text-[11px] text-accent/70 hover:text-accent transition-colors"
          >
            <ChevronDown size={11} className={`transition-transform ${expanded ? 'rotate-180' : ''}`} />
            {expanded ? text('debate.collapse') : `${text('debate.expandAll')} (${lines.length} ${text('debate.lines')})`}
          </button>
        )}

        {isThinking && (
          <div className="flex items-center gap-1.5 mt-2 text-muted">
            <img src="/apple_intelligence_line.png" alt="" className="w-3.5 h-3.5 animate-[spin_3s_linear_infinite]" />
            <span className="text-[11px]">{text('debate.thinkingPanel')}</span>
          </div>
        )}

      </div>
    </motion.div>
  )
}

export default memo(ChatMessageComponent, (prev, next) => {
  if (prev.message !== next.message) return false
  if (prev.roleNames !== next.roleNames) return false
  return true
})

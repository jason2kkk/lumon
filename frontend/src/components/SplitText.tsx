// 本地拆字渐入动画组件：用于在输入框提示、推荐文案等轻量场景复刻 React Bits SplitText 的逐字出现效果。
import { type CSSProperties, type ElementType, useEffect, useMemo } from 'react'

type SplitPart = 'chars' | 'words' | 'lines' | 'words, chars'

type SplitTransform = {
  opacity?: number
  y?: number | string
}

type SplitTextProps = {
  text: string
  className?: string
  delay?: number
  duration?: number
  splitType?: SplitPart
  from?: SplitTransform
  to?: SplitTransform
  textAlign?: CSSProperties['textAlign']
  tag?: ElementType
  onLetterAnimationComplete?: () => void
}

function toCssLength(value: number | string | undefined, fallback: string) {
  if (typeof value === 'number') return `${value}px`
  return value ?? fallback
}

export default function SplitText({
  text,
  className = '',
  delay = 24,
  duration = 0.46,
  splitType = 'chars',
  from = { opacity: 0, y: 12 },
  to = { opacity: 1, y: 0 },
  textAlign = 'left',
  tag = 'p',
  onLetterAnimationComplete,
}: SplitTextProps) {
  const units = useMemo(() => {
    if (splitType === 'words') {
      return text.split(/(\s+)/).filter((part) => part.length > 0)
    }
    if (splitType === 'lines') {
      return text.split('\n').flatMap((line, index, arr) => (
        index < arr.length - 1 ? [line, '\n'] : [line]
      ))
    }
    return Array.from(text)
  }, [splitType, text])

  useEffect(() => {
    if (!onLetterAnimationComplete) return
    const totalMs = Math.max(0, units.length - 1) * delay + duration * 1000
    const timer = window.setTimeout(onLetterAnimationComplete, totalMs)
    return () => window.clearTimeout(timer)
  }, [delay, duration, onLetterAnimationComplete, units.length])

  const Tag = tag
  const parentStyle = {
    textAlign,
    '--split-from-y': toCssLength(from.y, '12px'),
    '--split-to-y': toCssLength(to.y, '0px'),
    '--split-from-opacity': from.opacity ?? 0,
    '--split-to-opacity': to.opacity ?? 1,
    '--split-duration': `${duration}s`,
  } as CSSProperties

  return (
    <Tag className={`split-parent ${className}`} style={parentStyle} aria-label={text}>
      {units.map((unit, index) => {
        const isBreak = unit === '\n'
        if (isBreak) return <br key={`${index}-${unit}`} />

        return (
          <span
            key={`${index}-${unit}`}
            aria-hidden="true"
            className="split-char"
            style={{ '--split-delay': `${index * delay}ms` } as CSSProperties}
          >
            {unit === ' ' ? '\u00A0' : unit}
          </span>
        )
      })}
    </Tag>
  )
}

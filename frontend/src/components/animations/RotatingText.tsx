/**
 * RotatingText — 文字轮播动画
 * 基于 React Bits (MIT) 简化版，适配 framer-motion
 * https://reactbits.dev/text-animations/rotating-text
 */
import { useEffect, useState, useMemo, useCallback } from 'react'
import { motion, AnimatePresence, type TargetAndTransition, type Transition } from 'framer-motion'

interface RotatingTextProps {
  texts: string[]
  transition?: Transition
  initial?: TargetAndTransition
  animate?: TargetAndTransition
  exit?: TargetAndTransition
  rotationInterval?: number
  staggerDuration?: number
  staggerFrom?: 'first' | 'last' | 'center' | number
  className?: string
  splitBy?: 'characters' | 'words'
}

export default function RotatingText({
  texts,
  transition = { type: 'spring', damping: 25, stiffness: 300 },
  initial = { y: '100%', opacity: 0 },
  animate = { y: 0, opacity: 1 },
  exit = { y: '-120%', opacity: 0 },
  rotationInterval = 2000,
  staggerDuration = 0,
  staggerFrom = 'first',
  className = '',
  splitBy = 'characters',
}: RotatingTextProps) {
  const [idx, setIdx] = useState(0)

  const next = useCallback(() => {
    setIdx((prev) => (prev + 1) % texts.length)
  }, [texts.length])

  useEffect(() => {
    const id = setInterval(next, rotationInterval)
    return () => clearInterval(id)
  }, [next, rotationInterval])

  const elements = useMemo(() => {
    const current = texts[idx]
    if (splitBy === 'words') {
      return current.split(' ').map((w, i, arr) => ({
        chars: [w],
        needsSpace: i !== arr.length - 1,
      }))
    }
    return current.split(' ').map((w, i, arr) => ({
      chars: w.split(''),
      needsSpace: i !== arr.length - 1,
    }))
  }, [texts, idx, splitBy])

  const getDelay = useCallback(
    (i: number, total: number) => {
      if (staggerFrom === 'first') return i * staggerDuration
      if (staggerFrom === 'last') return (total - 1 - i) * staggerDuration
      if (staggerFrom === 'center') return Math.abs(Math.floor(total / 2) - i) * staggerDuration
      return Math.abs((staggerFrom as number) - i) * staggerDuration
    },
    [staggerFrom, staggerDuration],
  )

  const totalChars = elements.reduce((sum, w) => sum + w.chars.length, 0)

  return (
    <span className={`inline-flex flex-wrap overflow-hidden ${className}`}>
      <AnimatePresence mode="wait" initial={false}>
        <motion.span key={texts[idx]} className="inline-flex flex-wrap">
          {elements.map((wordObj, wi) => {
            const prevCount = elements.slice(0, wi).reduce((s, w) => s + w.chars.length, 0)
            return (
              <span key={wi} className="inline-flex">
                {wordObj.chars.map((char, ci) => (
                  <motion.span
                    key={ci}
                    initial={initial}
                    animate={animate}
                    exit={exit}
                    transition={{ ...transition, delay: getDelay(prevCount + ci, totalChars) }}
                    className="inline-block"
                  >
                    {char}
                  </motion.span>
                ))}
                {wordObj.needsSpace && <span>&nbsp;</span>}
              </span>
            )
          })}
        </motion.span>
      </AnimatePresence>
    </span>
  )
}

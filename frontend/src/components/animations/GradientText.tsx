/**
 * GradientText — 渐变色文字动画
 * 基于 React Bits (MIT) 改写，适配 framer-motion
 * https://reactbits.dev/text-animations/gradient-text
 */
import { type ReactNode, useState, useCallback, useEffect, useRef } from 'react'
import { motion, useMotionValue, useAnimationFrame, useTransform } from 'framer-motion'

interface GradientTextProps {
  children: ReactNode
  className?: string
  colors?: string[]
  animationSpeed?: number
  showBorder?: boolean
  direction?: 'horizontal' | 'vertical' | 'diagonal'
  pauseOnHover?: boolean
}

export default function GradientText({
  children,
  className = '',
  colors = ['#5227FF', '#FF9FFC', '#B497CF'],
  animationSpeed = 8,
  showBorder = false,
  direction = 'horizontal',
  pauseOnHover = false,
}: GradientTextProps) {
  const [isPaused, setIsPaused] = useState(false)
  const progress = useMotionValue(0)
  const elapsedRef = useRef(0)
  const lastTimeRef = useRef<number | null>(null)
  const animationDuration = animationSpeed * 1000

  useAnimationFrame((time) => {
    if (isPaused) { lastTimeRef.current = null; return }
    if (lastTimeRef.current === null) { lastTimeRef.current = time; return }
    const dt = time - lastTimeRef.current
    lastTimeRef.current = time
    elapsedRef.current += dt

    const fullCycle = animationDuration * 2
    const cycleTime = elapsedRef.current % fullCycle
    progress.set(
      cycleTime < animationDuration
        ? (cycleTime / animationDuration) * 100
        : 100 - ((cycleTime - animationDuration) / animationDuration) * 100,
    )
  })

  useEffect(() => {
    elapsedRef.current = 0
    progress.set(0)
  }, [animationSpeed, progress])

  const backgroundPosition = useTransform(progress, (p) => {
    if (direction === 'vertical') return `50% ${p}%`
    return `${p}% 50%`
  })

  const handleEnter = useCallback(() => { if (pauseOnHover) setIsPaused(true) }, [pauseOnHover])
  const handleLeave = useCallback(() => { if (pauseOnHover) setIsPaused(false) }, [pauseOnHover])

  const angle = direction === 'vertical' ? 'to bottom' : direction === 'diagonal' ? 'to bottom right' : 'to right'
  const gradientColors = [...colors, colors[0]].join(', ')
  const bgSize = direction === 'vertical' ? '100% 300%' : '300% 100%'

  return (
    <span className={`relative inline-block ${className}`} onMouseEnter={handleEnter} onMouseLeave={handleLeave}>
      {showBorder && (
        <span className="absolute inset-0 -z-10 rounded-xl p-[1px] overflow-hidden">
          <motion.span
            className="absolute inset-[-20%]"
            style={{
              backgroundImage: `linear-gradient(${angle}, ${gradientColors})`,
              backgroundSize: bgSize,
              backgroundPosition,
            }}
          />
        </span>
      )}
      <motion.span
        className="bg-clip-text text-transparent font-bold"
        style={{
          backgroundImage: `linear-gradient(${angle}, ${gradientColors})`,
          backgroundSize: bgSize,
          backgroundPosition,
        }}
      >
        {children}
      </motion.span>
    </span>
  )
}

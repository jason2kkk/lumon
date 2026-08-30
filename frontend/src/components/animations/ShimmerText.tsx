/**
 * ShimmerText — 微光文字效果
 * 基于 Spell UI 的设计理念，纯 CSS + Tailwind 实现
 * https://spell.sh/docs/shimmer-text
 */
import { type ReactNode, type CSSProperties } from 'react'

interface ShimmerTextProps {
  children: ReactNode
  className?: string
  shimmerColor?: string
  duration?: number
  shimmerSpread?: number
}

export default function ShimmerText({
  children,
  className = '',
  shimmerColor = 'rgba(255,255,255,0.6)',
  duration = 2.5,
  shimmerSpread = 10,
}: ShimmerTextProps) {
  const spread = Math.min(35, Math.max(6, shimmerSpread))
  const style: CSSProperties = {
    backgroundImage: `linear-gradient(
      90deg,
      currentColor 0%,
      currentColor ${50 - spread}%,
      ${shimmerColor} 50%,
      currentColor ${50 + spread}%,
      currentColor 100%
    )`,
    backgroundSize: '200% 100%',
    WebkitBackgroundClip: 'text',
    backgroundClip: 'text',
    WebkitTextFillColor: 'transparent',
    animation: `shimmer ${duration}s ease-in-out infinite`,
  }

  return (
    <>
      <style>{`
        @keyframes shimmer {
          0% { background-position: 200% 0; }
          100% { background-position: -200% 0; }
        }
      `}</style>
      <span className={className} style={style}>
        {children}
      </span>
    </>
  )
}

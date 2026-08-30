import { useState, useEffect, Children, type ReactNode, type CSSProperties } from 'react'

interface LogosCarouselProps {
  children: ReactNode
  stagger?: number
  count?: number
  className?: string
  duration?: number
  interval?: number
  initialDelay?: number
}

export default function LogosCarousel({
  children,
  stagger = 0.12,
  count,
  className = '',
  duration = 600,
  interval = 3000,
  initialDelay = 500,
}: LogosCarouselProps) {
  const [index, setIndex] = useState(0)
  const [animate, setAnimate] = useState(false)

  const childrenArray = Children.toArray(children)
  const logosPerGroup = count || childrenArray.length
  const groups: ReactNode[][] = []

  for (let i = 0; i < childrenArray.length; i += logosPerGroup) {
    groups.push(childrenArray.slice(i, i + logosPerGroup))
  }

  const groupsLength = groups.length

  useEffect(() => {
    const id = setTimeout(() => setAnimate(true), initialDelay)
    return () => clearTimeout(id)
  }, [initialDelay])

  useEffect(() => {
    if (!animate || groupsLength <= 1) return
    const id = setInterval(() => {
      setIndex(prev => (prev + 1) % groupsLength)
    }, interval)
    return () => clearInterval(id)
  }, [animate, interval, groupsLength])

  if (groupsLength === 0) return null

  return (
    <>
      <style>{`
        @keyframes logos-enter {
          0% { transform: translateY(18px); filter: blur(2px); opacity: 0; }
          100% { transform: translateY(0); filter: blur(0); opacity: 1; }
        }
        @keyframes logos-exit {
          0% { transform: translateY(0); filter: blur(0); opacity: 1; }
          100% { transform: translateY(-18px); filter: blur(2px); opacity: 0; }
        }
      `}</style>
      <div className={`relative ${className}`} style={{ height: 36, clipPath: 'inset(-8px 0 -8px 0)' }}>
        {groups.map((group, gIdx) => {
          const isCurrent = gIdx === index
          const isPrev = gIdx === (index - 1 + groupsLength) % groupsLength && animate
          const isVisible = isCurrent || isPrev
          return (
            <div
              key={gIdx}
              className="absolute inset-0 flex items-center justify-center"
              style={{
                display: isVisible ? 'flex' : 'none',
                gap: 'inherit',
              }}
            >
              {group.map((logo, lIdx) => (
                <LogoItem
                  key={lIdx}
                  animate={animate}
                  index={lIdx}
                  state={isCurrent ? 'enter' : 'exit'}
                  stagger={stagger}
                  duration={duration}
                >
                  {logo}
                </LogoItem>
              ))}
            </div>
          )
        })}
      </div>
    </>
  )
}

function LogoItem({
  children,
  animate,
  index,
  state = 'enter',
  stagger = 0.08,
  duration = 400,
}: {
  children: ReactNode
  animate: boolean
  index: number
  state: 'enter' | 'exit'
  stagger: number
  duration: number
}) {
  const delay = index * stagger

  const styles: CSSProperties = {
    animationDelay: `${delay}s`,
    animationDuration: `${duration}ms`,
    animationFillMode: 'both',
  }

  if (!animate) {
    return (
      <div style={state === 'enter' ? { opacity: 1 } : { opacity: 0 }}>
        {children}
      </div>
    )
  }

  return (
    <div style={{ ...styles, animationName: state === 'enter' ? 'logos-enter' : 'logos-exit' }}>
      {children}
    </div>
  )
}

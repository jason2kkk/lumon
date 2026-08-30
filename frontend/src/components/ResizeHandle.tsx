import { useCallback, useRef } from 'react'

interface Props {
  onResize: (delta: number) => void
  className?: string
}

export default function ResizeHandle({ onResize, className = '' }: Props) {
  const dragging = useRef(false)
  const lastX = useRef(0)

  const handleMouseDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault()
    dragging.current = true
    lastX.current = e.clientX
    document.body.style.cursor = 'col-resize'
    document.body.style.userSelect = 'none'

    const handleMouseMove = (ev: MouseEvent) => {
      if (!dragging.current) return
      const delta = ev.clientX - lastX.current
      lastX.current = ev.clientX
      onResize(delta)
    }

    const handleMouseUp = () => {
      dragging.current = false
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
      window.removeEventListener('mousemove', handleMouseMove)
      window.removeEventListener('mouseup', handleMouseUp)
    }

    window.addEventListener('mousemove', handleMouseMove)
    window.addEventListener('mouseup', handleMouseUp)
  }, [onResize])

  const handleTouchStart = useCallback((e: React.TouchEvent) => {
    dragging.current = true
    lastX.current = e.touches[0].clientX

    const handleTouchMove = (ev: TouchEvent) => {
      if (!dragging.current) return
      const delta = ev.touches[0].clientX - lastX.current
      lastX.current = ev.touches[0].clientX
      onResize(delta)
    }

    const handleTouchEnd = () => {
      dragging.current = false
      window.removeEventListener('touchmove', handleTouchMove)
      window.removeEventListener('touchend', handleTouchEnd)
    }

    window.addEventListener('touchmove', handleTouchMove, { passive: true })
    window.addEventListener('touchend', handleTouchEnd)
  }, [onResize])

  return (
    <div
      onMouseDown={handleMouseDown}
      onTouchStart={handleTouchStart}
      className={`w-1.5 shrink-0 cursor-col-resize group flex items-center justify-center ${className}`}
    >
      <div className="w-0.5 h-8 rounded-full bg-border/40 group-hover:bg-accent/40 group-active:bg-accent transition-colors" />
    </div>
  )
}

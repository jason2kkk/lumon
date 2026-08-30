/**
 * 交互式悬停按钮：hover 时背景色滑入覆盖，文字和 icon 切换
 * 无圆点，通过左侧色块扩展实现背景过渡
 */
import type { ReactNode } from 'react'

interface InteractiveHoverButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  icon?: ReactNode
  hoverIcon?: ReactNode
  bgColor?: string
  contentClassName?: string
}

export default function InteractiveHoverButton({
  children,
  className = '',
  icon,
  hoverIcon,
  bgColor,
  contentClassName = '',
  ...props
}: InteractiveHoverButtonProps) {
  return (
    <button
      className={`group relative w-auto cursor-pointer overflow-hidden rounded-lg border border-border/40 bg-[#f7f8f9] p-1.5 px-3.5 text-center text-[11px] font-medium ${className}`}
      {...props}
    >
      {/* 背景滑块：从左侧滑入覆盖整个按钮 */}
      <div
        className="absolute inset-0 -translate-x-full transition-transform duration-300 ease-out group-hover:translate-x-0"
        style={{ backgroundColor: bgColor || 'var(--interactive-hover-bg, var(--color-accent, #6366f1))' }}
      />
      <div className={`relative flex items-center justify-center gap-1.5 ${contentClassName}`}>
        {icon && (
          <span className="transition-all duration-300 group-hover:opacity-0">
            {icon}
          </span>
        )}
        <span className="inline-block transition-all duration-300 group-hover:opacity-0">
          {children}
        </span>
      </div>
      <div className={`absolute top-0 left-0 z-10 flex h-full w-full items-center justify-center gap-1.5 text-white opacity-0 transition-all duration-300 group-hover:opacity-100 ${contentClassName}`}>
        {hoverIcon || icon}
        <span>{children}</span>
      </div>
    </button>
  )
}

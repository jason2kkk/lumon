import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App'

try {
  const theme = localStorage.getItem('lumon_theme_mode')
  document.documentElement.classList.toggle('dark', theme === 'dark')
  document.documentElement.style.colorScheme = theme === 'dark' ? 'dark' : 'light'
} catch {
  // 主题预载失败不影响应用启动。
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)

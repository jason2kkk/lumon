import type { LanguageMode } from './stores/app'

function currentLanguage(): LanguageMode {
  try {
    return localStorage.getItem('lumon_language_mode') === 'en-US' ? 'en-US' : 'zh-CN'
  } catch {
    return 'zh-CN'
  }
}

function isEnglish(): boolean {
  return currentLanguage() === 'en-US'
}

export function fallbackNeedTitle(): string {
  return isEnglish() ? 'Untitled demand' : '未命名需求'
}

export function defaultApiError(status?: number): string {
  if (isEnglish()) {
    return status ? `Request failed (HTTP ${status}). Please try again later.` : 'Request failed. Please try again later.'
  }
  return status ? `请求失败（HTTP ${status}），请稍后重试。` : '请求失败，请稍后重试。'
}

export function normalizedApiErrorMessage(kind: 'auth' | 'forbidden' | 'rateLimit' | 'notFound' | 'quota' | 'server' | 'external' | 'network' | 'timeout' | 'stream'): string {
  const en = isEnglish()
  const messages = {
    auth: en ? 'API key is invalid or sign-in has expired. Check your local settings.' : 'API Key 无效或登录状态已失效，请检查本地配置。',
    forbidden: en ? 'Model or data service access was denied. Check your account permissions.' : '模型或数据服务访问被拒，请检查账号权限。',
    rateLimit: en ? 'Too many requests. Please wait 1-2 minutes and try again.' : '请求太频繁，请等 1-2 分钟再试。',
    notFound: en ? 'The requested content was not found. Refresh the page and try again.' : '请求的内容不存在，请刷新页面后重试。',
    quota: en ? 'Model quota or external service capacity is insufficient. Check your account balance and provider status.' : '模型额度或外部服务暂时不足，请检查账号余额和服务状态。',
    server: en ? 'The local service is temporarily unavailable. Check whether Lumon is still running.' : '本地服务暂时异常，请检查 Lumon 是否仍在运行。',
    external: en ? 'The external data service connection is unstable. Check your local network and service settings.' : '外部数据服务连接不稳定，请检查本机网络和服务配置。',
    network: en ? 'Network connection failed. Check whether the local service is still running.' : '网络连接失败，请检查本地服务是否仍在运行。',
    timeout: en ? 'The request timed out. Please try again later.' : '请求响应超时，请稍后重试。',
    stream: en ? 'Model output was interrupted. Retry, then check the model service and local network.' : '模型输出中断，请重试并检查模型服务和本机网络。',
  }
  return messages[kind]
}

export function streamDisconnectedMessage(): string {
  return isEnglish() ? 'Connection interrupted. Refresh the page and try again.' : '网络连接中断，请刷新页面重试'
}

export function reportInterruptedMessage(): string {
  return isEnglish()
    ? 'Report generation was interrupted. Retry, then check the model service and local network.'
    : '报告生成中断，请重试并检查模型服务和本机网络'
}

export function refreshFailedMessage(): string {
  return isEnglish() ? 'Refresh failed' : '重新查询失败'
}

export function sensorTowerTimeoutMessage(): string {
  return isEnglish()
    ? 'SensorTower query timed out. Retry, then check the local st-cli session and network.'
    : 'SensorTower 查询超时，请重试并检查本机 st-cli 登录状态和网络'
}

export function exportFailedMessage(): string {
  return isEnglish() ? 'Export failed' : '导出失败'
}

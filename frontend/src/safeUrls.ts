/** 将外部数据中的 URL 限制为绝对 HTTP(S) 地址。 */
export function safeExternalUrl(raw: unknown): string | undefined {
  if (typeof raw !== 'string' || !raw.trim()) return undefined
  try {
    const parsed = new URL(raw.trim())
    return parsed.protocol === 'http:' || parsed.protocol === 'https:' ? parsed.href : undefined
  } catch {
    return undefined
  }
}

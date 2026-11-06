/** Return the URL only when it is safe to bind to an href (http or https). */
export function safeExternalUrl(value: string | null | undefined): string {
  const raw = String(value || '').trim()
  if (!raw) return ''
  try {
    const url = new URL(raw)
    return url.protocol === 'http:' || url.protocol === 'https:' ? url.href : ''
  } catch {
    return ''
  }
}

export const HANDLE_RE = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/
export const CHANNEL_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/

export function isHandle(value: string): boolean {
  return HANDLE_RE.test(value)
}

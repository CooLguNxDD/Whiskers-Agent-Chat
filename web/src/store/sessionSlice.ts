import { createSignal } from "solid-js"

export interface SessionSlice {
  /** Shared service token. Memory only — never localStorage or the URL. */
  token: () => string
  tokenRequired: () => boolean
  setToken: (token: string) => void
  markTokenRequired: () => void
}

const [token, setTokenSignal] = createSignal("")
const [tokenRequired, setTokenRequired] = createSignal(false)

export const session: SessionSlice = {
  token,
  tokenRequired,
  setToken(value) {
    setTokenSignal(value)
    setTokenRequired(false)
  },
  markTokenRequired() {
    setTokenRequired(true)
  },
}

export function useSession() {
  return session
}

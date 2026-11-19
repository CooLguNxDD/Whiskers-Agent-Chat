import { createSignal } from "solid-js"
import { getAuthStatus, logout } from "@/api/auth"
import type { AuthKind } from "@/api/authContract.generated"
import type { AuthSession, AuthUser } from "@/api/types"

export type AuthMethod = AuthKind | null

export interface SessionSlice {
  /** In-memory bearer token override (optional). Never persisted to localStorage. */
  token: () => string
  tokenRequired: () => boolean
  setToken: (token: string) => void
  markTokenRequired: () => void
  authReady: () => boolean
  authRequired: () => boolean
  authenticated: () => boolean
  authMethod: () => AuthMethod
  hasUsers: () => boolean
  user: () => AuthUser | null
  currentSession: () => AuthSession | null
  authEpoch: () => number
  refreshAuth: () => Promise<void>
  applyLogin: (res: { user: AuthUser | null; session: AuthSession; hasUsers?: boolean }) => void
  signOut: () => Promise<void>
  markUnauthorized: () => void
}

const [token, setTokenSignal] = createSignal("")
const [tokenRequired, setTokenRequired] = createSignal(false)
const [authReady, setAuthReady] = createSignal(false)
const [authRequired, setAuthRequired] = createSignal(false)
const [authenticated, setAuthenticated] = createSignal(false)
const [authMethod, setAuthMethod] = createSignal<AuthMethod>(null)
const [hasUsers, setHasUsers] = createSignal(false)
const [user, setUser] = createSignal<AuthUser | null>(null)
const [currentSession, setCurrentSession] = createSignal<AuthSession | null>(null)
const [authEpoch, setAuthEpoch] = createSignal(0)

export const session: SessionSlice = {
  token,
  tokenRequired,
  authReady,
  authRequired,
  authenticated,
  authMethod,
  hasUsers,
  user,
  currentSession,
  authEpoch,
  setToken(value) {
    setTokenSignal(value)
    setTokenRequired(false)
    if (value) {
      setAuthenticated(true)
      setAuthMethod("bearer")
      setUser(null)
      setCurrentSession(null)
    } else if (authMethod() === "bearer") {
      setAuthenticated(!authRequired())
      setAuthMethod(authRequired() ? null : "open")
    }
    setAuthEpoch((n) => n + 1)
  },
  markTokenRequired() {
    this.markUnauthorized()
  },
  async refreshAuth() {
    const tokenAtStart = token()
    try {
      const res = await getAuthStatus()
      setAuthRequired(res.auth_required)
      setHasUsers(res.has_users)
      if (token() === tokenAtStart) {
        if (res.authenticated) {
          setAuthenticated(true)
          setAuthMethod(res.auth_method)
          setUser(res.user)
          setCurrentSession(res.session)
          setTokenRequired(false)
        } else {
          setAuthenticated(false)
          setAuthMethod(null)
          setUser(null)
          setCurrentSession(null)
          setTokenRequired(res.auth_required)
        }
      }
    } catch {
      // Leave current auth state as-is so SSE reconnect handles transient outages.
    } finally {
      setAuthReady(true)
    }
  },
  applyLogin(res) {
    setAuthenticated(true)
    setAuthMethod("session")
    setUser(res.user)
    setCurrentSession(res.session)
    if (res.hasUsers || res.user) setHasUsers(true)
    setTokenSignal("")
    setTokenRequired(false)
    setAuthReady(true)
    setAuthEpoch((n) => n + 1)
  },
  async signOut() {
    try {
      await logout()
    } catch {
      // Clear client state even if the hub is momentarily unreachable.
    }
    setAuthenticated(false)
    setAuthMethod(null)
    setUser(null)
    setCurrentSession(null)
    setTokenSignal("")
    setTokenRequired(authRequired())
    setAuthEpoch((n) => n + 1)
  },
  markUnauthorized() {
    if (authenticated() || !tokenRequired()) {
      setAuthenticated(false)
      setAuthMethod(null)
      setUser(null)
      setCurrentSession(null)
      setTokenRequired(true)
    }
  },
}

export function useSession() {
  return session
}

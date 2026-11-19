import { AUTH_ROUTES, authSessionPath, authUserPath } from "./authContract.generated"
import { api } from "./client"
import type { AuthLoginResponse, AuthSession, AuthStatusResponse, AuthUser } from "./types"

export function getAuthStatus() {
  return api<AuthStatusResponse>(AUTH_ROUTES.status)
}

export function loginWithPassword(input: {
  username: string
  password: string
  remember?: boolean
  label?: string
}) {
  return api<AuthLoginResponse>(AUTH_ROUTES.login, {
    method: "POST",
    body: JSON.stringify(input),
  })
}

export function loginWithToken(input: {
  token: string
  remember?: boolean
  label?: string
}) {
  return api<AuthLoginResponse>(AUTH_ROUTES.login, {
    method: "POST",
    body: JSON.stringify(input),
  })
}

export function registerUser(input: {
  username: string
  password: string
  display_name?: string
  token?: string
  remember?: boolean
  sign_in?: boolean
  label?: string
}) {
  return api<{ user: AuthUser; session: AuthSession | null }>(AUTH_ROUTES.register, {
    method: "POST",
    body: JSON.stringify(input),
  })
}

export function logout() {
  return api<{ ok: boolean }>(AUTH_ROUTES.logout, {
    method: "POST",
    body: JSON.stringify({}),
  })
}

export function listSessions() {
  return api<{ sessions: AuthSession[] }>(AUTH_ROUTES.list_sessions)
}

export function revokeSession(sessionId: number) {
  return api<{ revoked: boolean; id: number }>(authSessionPath(sessionId), {
    method: "DELETE",
  })
}

export function revokeOtherSessions() {
  return api<{ revoked_count: number }>(AUTH_ROUTES.revoke_other_sessions, {
    method: "POST",
    body: JSON.stringify({}),
  })
}

export function listUsers() {
  return api<{ users: AuthUser[] }>(AUTH_ROUTES.list_users)
}

export function changePassword(input: {
  current_password: string
  new_password: string
  revoke_others?: boolean
}) {
  return api<{ ok: boolean; revoked_count: number }>(AUTH_ROUTES.change_password, {
    method: "POST",
    body: JSON.stringify(input),
  })
}

export function deleteUser(userId: number) {
  return api<{ deleted: boolean; id: number }>(authUserPath(userId), {
    method: "DELETE",
  })
}

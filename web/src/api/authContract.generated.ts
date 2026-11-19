// GENERATED from cat_fleet_chat/auth_contract.json by npm run gen:auth — do not edit by hand.

export const AUTH_METHODS = ["password","token"] as const
export type AuthMethodName = (typeof AUTH_METHODS)[number]

export const AUTH_KINDS = ["session","bearer","open"] as const
export type AuthKind = (typeof AUTH_KINDS)[number]

export const AUTH_KIND_LABELS: Record<AuthKind, string> = {
  session: "Signed in with Hub token",
  bearer: "Authenticated with bearer token",
  open: "Open local mode",
}

export const AUTH_LIMITS = {
  usernamePattern: "^[a-z0-9][a-z0-9_-]{1,31}$",
  passwordMin: 8,
  passwordMax: 256,
  labelMax: 120,
  sessionTtlDefaultDays: 30,
} as const

export const AUTH_ROUTES = {
  status: "/api/v1/auth/status",
  login: "/api/v1/auth/login",
  register: "/api/v1/auth/register",
  logout: "/api/v1/auth/logout",
  list_sessions: "/api/v1/auth/sessions",
  revoke_other_sessions: "/api/v1/auth/sessions/revoke-others",
  revoke_session: "/api/v1/auth/sessions/{session_id}",
  list_users: "/api/v1/auth/users",
  delete_user: "/api/v1/auth/users/{user_id}",
  change_password: "/api/v1/auth/password",
} as const

export function authSessionPath(sessionId: number): string {
  return `/api/v1/auth/sessions/${sessionId}`
}

export function authUserPath(userId: number): string {
  return `/api/v1/auth/users/${userId}`
}

export const SKIP_UNAUTHORIZED_PATHS: readonly string[] = ["/api/v1/auth/status","/api/v1/auth/login","/api/v1/auth/register","/api/v1/auth/logout","/api/v1/auth/password"]

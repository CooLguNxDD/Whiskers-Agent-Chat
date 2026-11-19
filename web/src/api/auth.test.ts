import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { ApiError, api, setTokenGetter, setUnauthorizedHandler } from "./client"
import { AUTH_ROUTES, SKIP_UNAUTHORIZED_PATHS, authSessionPath, authUserPath } from "./authContract.generated"
import {
  changePassword,
  deleteUser,
  getAuthStatus,
  listSessions,
  listUsers,
  loginWithPassword,
  loginWithToken,
  logout,
  registerUser,
  revokeOtherSessions,
  revokeSession,
} from "./auth"

interface Call {
  url: URL
  method: string
  credentials: RequestCredentials | undefined
  headers: Headers
  body: unknown
}

let calls: Call[] = []
let next: () => Response = () => json({})

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } })
}

const last = () => calls[calls.length - 1]

beforeEach(() => {
  calls = []
  next = () => json({})
  setTokenGetter(() => "")
  setUnauthorizedHandler(null)
  vi.stubGlobal("fetch", vi.fn(async (input: string, init: RequestInit = {}) => {
    calls.push({
      url: new URL(input, "http://hub.test"),
      method: init.method ?? "GET",
      credentials: init.credentials,
      headers: new Headers(init.headers),
      body: typeof init.body === "string" ? JSON.parse(init.body) : undefined,
    })
    return next()
  }))
})

afterEach(() => {
  vi.unstubAllGlobals()
  setTokenGetter(() => "")
  setUnauthorizedHandler(null)
})

describe("auth client", () => {
  it("sends credentials: same-origin and hits generated auth endpoints", async () => {
    await getAuthStatus()
    expect(last()).toMatchObject({ method: "GET", credentials: "same-origin" })
    expect(last().url.pathname).toBe(AUTH_ROUTES.status)

    await loginWithPassword({ username: "ada", password: "secret-password-1", remember: true, label: "Laptop" })
    expect(last()).toMatchObject({
      method: "POST",
      credentials: "same-origin",
      body: { username: "ada", password: "secret-password-1", remember: true, label: "Laptop" },
    })
    expect(last().url.pathname).toBe(AUTH_ROUTES.login)

    await loginWithToken({ token: "hub-secret", remember: false })
    expect(last().url.pathname).toBe(AUTH_ROUTES.login)
    expect(last().body).toEqual({ token: "hub-secret", remember: false })

    await registerUser({ username: "bob", password: "secret-password-2", token: "hub-secret", sign_in: true })
    expect(last().url.pathname).toBe(AUTH_ROUTES.register)

    await logout()
    expect(last()).toMatchObject({ method: "POST", body: {} })
    expect(last().url.pathname).toBe(AUTH_ROUTES.logout)

    await listSessions()
    expect(last().url.pathname).toBe(AUTH_ROUTES.list_sessions)

    await revokeSession(42)
    expect(last().method).toBe("DELETE")
    expect(last().url.pathname).toBe(authSessionPath(42))

    await revokeOtherSessions()
    expect(last().method).toBe("POST")
    expect(last().url.pathname).toBe(AUTH_ROUTES.revoke_other_sessions)

    await listUsers()
    expect(last().url.pathname).toBe(AUTH_ROUTES.list_users)

    await changePassword({ current_password: "old-password", new_password: "new-password", revoke_others: true })
    expect(last().url.pathname).toBe(AUTH_ROUTES.change_password)

    await deleteUser(7)
    expect(last().method).toBe("DELETE")
    expect(last().url.pathname).toBe(authUserPath(7))
  })

  it("fires unauthorizedHandler on protected 401s and skips auth endpoints", async () => {
    const onUnauthorized = vi.fn()
    setUnauthorizedHandler(onUnauthorized)
    next = () => json({ error: { code: "unauthorized", message: "missing or invalid bearer token" } }, 401)

    await expect(api("/api/v1/channels")).rejects.toBeInstanceOf(ApiError)
    expect(onUnauthorized).toHaveBeenCalledTimes(1)

    await expect(listSessions()).rejects.toBeInstanceOf(ApiError)
    expect(onUnauthorized).toHaveBeenCalledTimes(2)

    for (const path of SKIP_UNAUTHORIZED_PATHS) {
      await expect(api(path, { method: "POST", body: "{}" })).rejects.toBeInstanceOf(ApiError)
    }
    expect(onUnauthorized).toHaveBeenCalledTimes(2)
  })
})

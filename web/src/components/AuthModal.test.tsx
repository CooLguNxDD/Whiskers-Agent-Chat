// @vitest-environment jsdom
import { render } from "solid-js/web"
import { QueryClient, QueryClientProvider } from "@tanstack/solid-query"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
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
} from "@/api/auth"
import type { AuthSession, AuthUser } from "@/api/types"
import { prefs } from "@/store/prefsSlice"
import { session } from "@/store/sessionSlice"
import AuthModal from "./AuthModal"

vi.mock("@/api/auth", () => ({
  getAuthStatus: vi.fn(),
  loginWithPassword: vi.fn(),
  loginWithToken: vi.fn(),
  registerUser: vi.fn(),
  logout: vi.fn(),
  listSessions: vi.fn(),
  revokeSession: vi.fn(),
  revokeOtherSessions: vi.fn(),
  listUsers: vi.fn(),
  changePassword: vi.fn(),
  deleteUser: vi.fn(),
}))

function makeUser(overrides: Partial<AuthUser> = {}): AuthUser {
  return {
    id: 1,
    username: "ada",
    display_name: "Ada",
    created_at: "2026-11-18T00:00:00Z",
    updated_at: "2026-11-18T00:00:00Z",
    last_login_at: "2026-11-18T01:00:00Z",
    active_sessions: 2,
    ...overrides,
  }
}

function makeSession(overrides: Partial<AuthSession> = {}): AuthSession {
  return {
    id: 10,
    user_id: 1,
    username: "ada",
    display_name: "Ada",
    auth_method: "password",
    label: "Chrome on Windows",
    user_agent: "Mozilla/5.0",
    ip_address: "127.0.0.1",
    created_at: "2026-11-18T00:00:00Z",
    last_seen_at: "2026-11-18T01:00:00Z",
    expires_at: "2026-12-18T00:00:00Z",
    current: true,
    ...overrides,
  }
}

let container: HTMLDivElement
let dispose: (() => void) | undefined
let onOpenChange: ReturnType<typeof vi.fn<(open: boolean) => void>>
const settle = async () => { await new Promise((resolve) => setTimeout(resolve, 20)) }

const byLabel = (label: string) =>
  container.querySelector(`[aria-label="${label}"]`) as HTMLInputElement
const type = (label: string, value: string) => {
  const input = byLabel(label)
  input.value = value
  input.dispatchEvent(new Event("input", { bubbles: true }))
}
const button = (text: string) =>
  [...container.querySelectorAll("button")].find((item) => item.textContent?.trim() === text) as HTMLButtonElement
const tab = (value: string) =>
  container.querySelector(`[role="tab"][data-value="${value}"]`) as HTMLButtonElement
const submit = () => (container.querySelector("form") as HTMLFormElement).requestSubmit()

function mount(open = true) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  dispose = render(
    () => (
      <QueryClientProvider client={client}>
        <AuthModal open={open} onOpenChange={onOpenChange} />
      </QueryClientProvider>
    ),
    container,
  )
}

beforeEach(async () => {
  onOpenChange = vi.fn()
  vi.mocked(getAuthStatus).mockReset().mockResolvedValue({
    auth_required: true,
    authenticated: false,
    auth_method: null,
    has_users: true,
    user: null,
    session: null,
  })
  vi.mocked(loginWithPassword).mockReset()
  vi.mocked(loginWithToken).mockReset()
  vi.mocked(registerUser).mockReset()
  vi.mocked(logout).mockReset().mockResolvedValue({ ok: true })
  vi.mocked(listSessions).mockReset().mockResolvedValue({ sessions: [] })
  vi.mocked(revokeSession).mockReset().mockResolvedValue({ revoked: true, id: 10 })
  vi.mocked(revokeOtherSessions).mockReset().mockResolvedValue({ revoked_count: 1 })
  vi.mocked(listUsers).mockReset().mockResolvedValue({ users: [] })
  vi.mocked(changePassword).mockReset().mockResolvedValue({ ok: true, revoked_count: 0 })
  vi.mocked(deleteUser).mockReset().mockResolvedValue({ deleted: true, id: 2 })
  await session.refreshAuth()
  prefs.setDisplayName("")
  localStorage.clear()
  container = document.createElement("div")
  document.body.append(container)
})

afterEach(() => {
  dispose?.()
  container.remove()
  prefs.setDisplayName("")
  localStorage.clear()
})

describe("AuthModal", () => {
  it("signs in with username and password and syncs display name", async () => {
    const user = makeUser()
    const sess = makeSession()
    vi.mocked(loginWithPassword).mockResolvedValue({
      authenticated: true,
      auth_method: "session",
      user,
      session: sess,
    })

    mount()
    await settle()
    type("Username", "ada")
    type("Password", "secret-password-1")
    type("Device label", "Work PC")
    submit()
    await settle()

    expect(vi.mocked(loginWithPassword)).toHaveBeenCalledWith({
      username: "ada",
      password: "secret-password-1",
      remember: true,
      label: "Work PC",
    })
    expect(session.authenticated()).toBe(true)
    expect(session.user()?.username).toBe("ada")
    expect(prefs.displayName()).toBe("Ada")
    expect(onOpenChange).toHaveBeenCalledWith(false)
  })

  it("exchanges hub token for an HttpOnly SQLite session without touching localStorage", async () => {
    const sess = makeSession({ user_id: null, username: null, display_name: null, auth_method: "token" })
    vi.mocked(loginWithToken).mockResolvedValue({
      authenticated: true,
      auth_method: "session",
      user: null,
      session: sess,
    })

    mount()
    await settle()
    button("Hub token").click()
    await settle()
    type("Hub token", "hub-secret-token")
    submit()
    await settle()

    expect(vi.mocked(loginWithToken)).toHaveBeenCalledWith({
      token: "hub-secret-token",
      remember: true,
      label: undefined,
    })
    expect(session.authenticated()).toBe(true)
    expect(session.token()).toBe("")
    expect(localStorage.getItem("cat-fleet-prefs")).toBeNull()
    expect(onOpenChange).toHaveBeenCalledWith(false)
  })

  it("validates password confirmation and registers a new account", async () => {
    const user = makeUser({ username: "bob", display_name: "Bob" })
    const sess = makeSession({ username: "bob", display_name: "Bob" })
    vi.mocked(registerUser).mockResolvedValue({ user, session: sess })

    mount()
    await settle()
    button("Create account").click()
    await settle()

    type("Username", "bob")
    type("Password", "secret-password-2")
    type("Confirm password", "mismatch")
    type("Hub token", "hub-secret")
    submit()
    await settle()
    expect(vi.mocked(registerUser)).not.toHaveBeenCalled()
    expect(container.textContent).toContain("Passwords do not match.")

    type("Confirm password", "secret-password-2")
    submit()
    await settle()
    expect(vi.mocked(registerUser)).toHaveBeenCalledWith({
      username: "bob",
      password: "secret-password-2",
      display_name: undefined,
      token: "hub-secret",
      remember: true,
      sign_in: true,
      label: undefined,
    })
    expect(session.user()?.username).toBe("bob")
  })

  it("lists sessions, revokes another session, revokes others, and signs out", async () => {
    const user = makeUser()
    const current = makeSession({ id: 10, label: "Current Browser", current: true })
    const other = makeSession({ id: 11, label: "Old Tab", current: false, auth_method: "token" })
    session.applyLogin({ user, session: current, hasUsers: true })
    vi.mocked(listSessions)
      .mockResolvedValueOnce({ sessions: [current, other] })
      .mockResolvedValueOnce({ sessions: [current] })

    mount()
    await settle()
    expect(container.textContent).toContain("Current Browser")
    expect(container.textContent).toContain("This device")
    expect(container.textContent).toContain("Old Tab")

    const revokeButtons = [...container.querySelectorAll("button")].filter((b) => b.textContent?.trim() === "Revoke")
    expect(revokeButtons).toHaveLength(2)
    revokeButtons[1].click()
    await settle()
    expect(vi.mocked(revokeSession)).toHaveBeenCalledWith(11)
    expect(container.textContent).not.toContain("Old Tab")

    // Re-seed two sessions to test Revoke other sessions button.
    vi.mocked(listSessions).mockResolvedValueOnce({ sessions: [current, other] }).mockResolvedValueOnce({ sessions: [current] })
    tab("accounts").click()
    await settle()
    tab("sessions").click()
    await settle()
    button("Revoke other sessions").click()
    await settle()
    expect(vi.mocked(revokeOtherSessions)).toHaveBeenCalledTimes(1)

    button("Sign out").click()
    await settle()
    expect(vi.mocked(logout)).toHaveBeenCalledTimes(1)
    expect(session.authenticated()).toBe(false)
    expect(container.textContent).toContain("Sign in to Cat Fleet")
  })

  it("changes password and deletes another user after two-step confirmation", async () => {
    const ada = makeUser({ id: 1, username: "ada", display_name: "Ada" })
    const bob = makeUser({ id: 2, username: "bob", display_name: "Bob", active_sessions: 0, last_login_at: null })
    const current = makeSession({ id: 10, current: true })
    session.applyLogin({ user: ada, session: current, hasUsers: true })
    vi.mocked(listUsers)
      .mockResolvedValueOnce({ users: [ada, bob] })
      .mockResolvedValueOnce({ users: [ada] })

    mount()
    await settle()
    tab("accounts").click()
    await settle()

    type("Current password", "old-password-1")
    type("New password", "new-password-2")
    type("Confirm new password", "new-password-2")
    submit()
    await settle()

    expect(vi.mocked(changePassword)).toHaveBeenCalledWith({
      current_password: "old-password-1",
      new_password: "new-password-2",
      revoke_others: false,
    })
    expect(container.textContent).toContain("Password updated.")

    const deleteButtons = [...container.querySelectorAll("button")].filter((b) => b.textContent?.trim() === "Delete")
    expect(deleteButtons).toHaveLength(2)
    deleteButtons[1].click()
    await settle()
    button("Confirm delete").click()
    await settle()

    expect(vi.mocked(deleteUser)).toHaveBeenCalledWith(2)
    expect(container.textContent).not.toContain("@bob")
  })
})

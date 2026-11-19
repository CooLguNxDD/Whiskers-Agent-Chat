import { createEffect, createSignal, For, Show } from "solid-js"
import { useQueryClient } from "@tanstack/solid-query"
import {
  changePassword,
  deleteUser,
  listSessions,
  listUsers,
  loginWithPassword,
  loginWithToken,
  registerUser,
  revokeOtherSessions,
  revokeSession,
} from "@/api/auth"
import { AUTH_KIND_LABELS, AUTH_LIMITS } from "@/api/authContract.generated"
import type { AuthSession, AuthUser } from "@/api/types"
import { Button } from "@/components/ui/button"
import { Modal } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { TabStrip } from "@/components/ui/page"
import { usePrefs, useSession } from "@/store"
import { getErrorMessage } from "@/utils/errors"
import { formatStamp } from "@/utils/time"

type LoginTab = "password" | "token" | "register"
type ManageTab = "sessions" | "accounts"

const LOGIN_TABS: { value: LoginTab; label: string }[] = [
  { value: "password", label: "Sign in" },
  { value: "token", label: "Hub token" },
  { value: "register", label: "Create account" },
]

export default function AuthModal(props: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const session = useSession()
  const prefs = usePrefs()
  const queryClient = useQueryClient()

  const [loginTab, setLoginTab] = createSignal<LoginTab>("token")
  const [userPickedLoginTab, setUserPickedLoginTab] = createSignal(false)
  const [manageTab, setManageTab] = createSignal<ManageTab>("sessions")
  const [showLoginInOpenMode, setShowLoginInOpenMode] = createSignal(false)

  // Login / register form state
  const [username, setUsername] = createSignal("")
  const [displayName, setDisplayName] = createSignal("")
  const [password, setPassword] = createSignal("")
  const [confirmPassword, setConfirmPassword] = createSignal("")
  const [hubToken, setHubToken] = createSignal("")
  const [deviceLabel, setDeviceLabel] = createSignal("")
  const [remember, setRemember] = createSignal(true)
  const [authError, setAuthError] = createSignal("")
  const [busy, setBusy] = createSignal(false)

  // Session & account management state
  const [sessions, setSessions] = createSignal<AuthSession[]>([])
  const [sessionsLoaded, setSessionsLoaded] = createSignal(false)
  const [sessionsBusy, setSessionsBusy] = createSignal(false)
  const [sessionsError, setSessionsError] = createSignal("")

  const [users, setUsers] = createSignal<AuthUser[]>([])
  const [usersLoaded, setUsersLoaded] = createSignal(false)
  const [usersBusy, setUsersBusy] = createSignal(false)
  const [usersError, setUsersError] = createSignal("")
  const [accountsNotice, setAccountsNotice] = createSignal("")
  const [confirmDeleteUserId, setConfirmDeleteUserId] = createSignal<number | null>(null)

  // Change password form state
  const [currentPw, setCurrentPw] = createSignal("")
  const [newPw, setNewPw] = createSignal("")
  const [confirmNewPw, setConfirmNewPw] = createSignal("")
  const [revokeOthersOnPw, setRevokeOthersOnPw] = createSignal(false)
  const [pwBusy, setPwBusy] = createSignal(false)

  // Inline create-account state (when already authenticated)
  const [newUsername, setNewUsername] = createSignal("")
  const [newDisplayName, setNewDisplayName] = createSignal("")
  const [newAccountPw, setNewAccountPw] = createSignal("")
  const [confirmAccountPw, setConfirmAccountPw] = createSignal("")
  const [signInNewAccount, setSignInNewAccount] = createSignal(false)
  const [createUserBusy, setCreateUserBusy] = createSignal(false)

  const isLoggedOutView = () =>
    !session.authenticated() || (session.authRequired() && !session.authenticated()) || showLoginInOpenMode()

  const modalTitle = () => (isLoggedOutView() ? "Sign in to Cat Fleet" : "Account & Sessions")

  const resetLoginForms = () => {
    setPassword("")
    setConfirmPassword("")
    setHubToken("")
    setAuthError("")
  }

  const refreshSessions = async () => {
    setSessionsBusy(true)
    setSessionsError("")
    try {
      const res = await listSessions()
      setSessions(res.sessions)
      setSessionsLoaded(true)
    } catch (err) {
      setSessionsError(getErrorMessage(err, "Could not load sessions."))
    } finally {
      setSessionsBusy(false)
    }
  }

  const refreshUsers = async () => {
    setUsersBusy(true)
    setUsersError("")
    try {
      const res = await listUsers()
      setUsers(res.users)
      setUsersLoaded(true)
    } catch (err) {
      setUsersError(getErrorMessage(err, "Could not load accounts."))
    } finally {
      setUsersBusy(false)
    }
  }

  createEffect(() => {
    if (!props.open) {
      setUserPickedLoginTab(false)
      setShowLoginInOpenMode(false)
      setAuthError("")
      setSessionsError("")
      setUsersError("")
      setAccountsNotice("")
      setConfirmDeleteUserId(null)
      return
    }
    if (!userPickedLoginTab()) {
      setLoginTab(session.hasUsers() ? "password" : "token")
    }
  })

  createEffect(() => {
    if (!props.open || isLoggedOutView()) return
    if (manageTab() === "sessions") {
      void refreshSessions()
    } else {
      void refreshUsers()
    }
  })

  const completeLogin = (res: { user: AuthUser | null; session: AuthSession; hasUsers?: boolean }) => {
    session.applyLogin(res)
    if (res.user) {
      prefs.setDisplayName(res.user.display_name)
    }
    resetLoginForms()
    setShowLoginInOpenMode(false)
    void queryClient.invalidateQueries()
    props.onOpenChange(false)
  }

  const submitPasswordLogin = async (event: Event) => {
    event.preventDefault()
    setAuthError("")
    setBusy(true)
    try {
      const res = await loginWithPassword({
        username: username().trim(),
        password: password(),
        remember: remember(),
        label: deviceLabel().trim() || undefined,
      })
      completeLogin({ user: res.user, session: res.session, hasUsers: true })
    } catch (err) {
      setAuthError(getErrorMessage(err, "Sign in failed."))
    } finally {
      setBusy(false)
    }
  }

  const submitTokenLogin = async (event: Event) => {
    event.preventDefault()
    setAuthError("")
    setBusy(true)
    try {
      const res = await loginWithToken({
        token: hubToken().trim(),
        remember: remember(),
        label: deviceLabel().trim() || undefined,
      })
      completeLogin({ user: res.user, session: res.session })
    } catch (err) {
      setAuthError(getErrorMessage(err, "Invalid hub token."))
    } finally {
      setBusy(false)
    }
  }

  const submitRegister = async (event: Event) => {
    event.preventDefault()
    setAuthError("")
    if (password() !== confirmPassword()) {
      setAuthError("Passwords do not match.")
      return
    }
    setBusy(true)
    try {
      const res = await registerUser({
        username: username().trim(),
        password: password(),
        display_name: displayName().trim() || undefined,
        token: session.authRequired() && !session.authenticated() ? hubToken().trim() : undefined,
        remember: remember(),
        sign_in: true,
        label: deviceLabel().trim() || undefined,
      })
      if (res.session) {
        completeLogin({ user: res.user, session: res.session, hasUsers: true })
      } else {
        prefs.setDisplayName(res.user.display_name)
        resetLoginForms()
        void queryClient.invalidateQueries()
        props.onOpenChange(false)
      }
    } catch (err) {
      setAuthError(getErrorMessage(err, "Could not create account."))
    } finally {
      setBusy(false)
    }
  }

  const handleSignOut = async () => {
    setShowLoginInOpenMode(false)
    await session.signOut()
    queryClient.clear()
  }

  const handleRevokeSession = async (item: AuthSession) => {
    setSessionsError("")
    try {
      await revokeSession(item.id)
      if (item.current) {
        await session.signOut()
        queryClient.clear()
      } else {
        await refreshSessions()
      }
    } catch (err) {
      setSessionsError(getErrorMessage(err, "Could not revoke session."))
    }
  }

  const handleRevokeOtherSessions = async () => {
    setSessionsError("")
    try {
      await revokeOtherSessions()
      await refreshSessions()
    } catch (err) {
      setSessionsError(getErrorMessage(err, "Could not revoke other sessions."))
    }
  }

  const submitChangePassword = async (event: Event) => {
    event.preventDefault()
    setUsersError("")
    setAccountsNotice("")
    if (newPw() !== confirmNewPw()) {
      setUsersError("New passwords do not match.")
      return
    }
    setPwBusy(true)
    try {
      const res = await changePassword({
        current_password: currentPw(),
        new_password: newPw(),
        revoke_others: revokeOthersOnPw(),
      })
      setCurrentPw("")
      setNewPw("")
      setConfirmNewPw("")
      setAccountsNotice(
        res.revoked_count > 0
          ? `Password updated and ${res.revoked_count} other session(s) signed out.`
          : "Password updated.",
      )
      if (revokeOthersOnPw()) {
        void refreshSessions()
      }
    } catch (err) {
      setUsersError(getErrorMessage(err, "Could not update password."))
    } finally {
      setPwBusy(false)
    }
  }

  const handleDeleteUser = async (item: AuthUser) => {
    setUsersError("")
    setAccountsNotice("")
    try {
      await deleteUser(item.id)
      setConfirmDeleteUserId(null)
      if (session.user()?.id === item.id) {
        await session.signOut()
        queryClient.clear()
      } else {
        await refreshUsers()
        void refreshSessions()
      }
    } catch (err) {
      setUsersError(getErrorMessage(err, "Could not delete account."))
    }
  }

  const submitCreateAccountInline = async (event: Event) => {
    event.preventDefault()
    setUsersError("")
    setAccountsNotice("")
    if (newAccountPw() !== confirmAccountPw()) {
      setUsersError("Passwords do not match.")
      return
    }
    setCreateUserBusy(true)
    try {
      const wantsSignIn = !session.user() && signInNewAccount()
      const res = await registerUser({
        username: newUsername().trim(),
        password: newAccountPw(),
        display_name: newDisplayName().trim() || undefined,
        remember: true,
        sign_in: wantsSignIn,
        label: deviceLabel().trim() || undefined,
      })
      setNewUsername("")
      setNewDisplayName("")
      setNewAccountPw("")
      setConfirmAccountPw("")
      setSignInNewAccount(false)
      if (wantsSignIn && res.session) {
        completeLogin({ user: res.user, session: res.session, hasUsers: true })
        return
      }
      setAccountsNotice(`Account @${res.user.username} created.`)
      await refreshUsers()
    } catch (err) {
      setUsersError(getErrorMessage(err, "Could not create account."))
    } finally {
      setCreateUserBusy(false)
    }
  }

  const identitySummary = () => {
    const activeUser = session.user()
    if (activeUser) {
      return activeUser.display_name && activeUser.display_name !== activeUser.username
        ? `Signed in as @${activeUser.username} (${activeUser.display_name})`
        : `Signed in as @${activeUser.username}`
    }
    return AUTH_KIND_LABELS[session.authMethod() ?? "session"]
  }

  return (
    <Modal open={props.open} onOpenChange={props.onOpenChange} title={modalTitle()}>
      <Show
        when={isLoggedOutView()}
        fallback={
          <div class="flex flex-col gap-4">
            <div class="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-line bg-ink-sunken px-3.5 py-3">
              <div class="min-w-0">
                <p class="text-sm font-medium text-cream">{identitySummary()}</p>
                <Show when={session.currentSession()}>
                  {(cs) => (
                    <p class="mt-0.5 text-xs text-cream-dim">
                      {cs().label || "Browser session"} · expires {formatStamp(cs().expires_at)}
                    </p>
                  )}
                </Show>
              </div>
              <div class="flex items-center gap-2">
                <Show when={session.authMethod() === "open"}>
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    onClick={() => {
                      setShowLoginInOpenMode(true)
                      setUserPickedLoginTab(true)
                      setLoginTab(session.hasUsers() ? "password" : "register")
                    }}
                  >
                    Sign in
                  </Button>
                </Show>
                <Show when={session.authMethod() !== "open"}>
                  <Button type="button" size="sm" variant="outline" onClick={() => void handleSignOut()}>
                    Sign out
                  </Button>
                </Show>
              </div>
            </div>

            <TabStrip
              label="Account and session sections"
              value={manageTab()}
              onChange={(value) => setManageTab(value)}
              tabs={[
                { value: "sessions", label: "Sessions", count: sessionsLoaded() ? sessions().length : undefined },
                { value: "accounts", label: "Accounts", count: usersLoaded() ? users().length : undefined },
              ]}
            />

            <Show when={manageTab() === "sessions"}>
              <div class="flex flex-col gap-3">
                <Show when={sessionsError()}>
                  <p role="alert" class="rounded-lg bg-rust/15 px-3 py-2 text-xs text-rust">
                    {sessionsError()}
                  </p>
                </Show>
                <div class="flex items-center justify-between gap-2">
                  <p class="text-xs text-cream-dim">
                    Sessions are stored in SQLite via an HttpOnly cookie.
                  </p>
                  <Show when={sessions().length > 1}>
                    <Button type="button" size="sm" variant="outline" onClick={() => void handleRevokeOtherSessions()}>
                      Revoke other sessions
                    </Button>
                  </Show>
                </div>
                <Show when={!sessionsBusy() || sessionsLoaded()} fallback={<p class="text-sm text-cream-dim">Loading sessions…</p>}>
                  <Show
                    when={sessions().length > 0}
                    fallback={<p class="rounded-lg border border-line px-3 py-4 text-center text-sm text-cream-dim">No active stored sessions.</p>}
                  >
                    <ul class="flex flex-col gap-2">
                      <For each={sessions()}>
                        {(item) => (
                          <li class="flex items-start justify-between gap-3 rounded-lg border border-line bg-ink-sunken/70 px-3 py-2.5">
                            <div class="min-w-0 flex-1">
                              <div class="flex flex-wrap items-center gap-1.5">
                                <span class="text-sm font-medium text-cream">{item.label || "Browser session"}</span>
                                <Show when={item.current}>
                                  <span class="rounded-full bg-moss/15 px-2 py-0.5 text-[11px] font-medium text-moss">
                                    This device
                                  </span>
                                </Show>
                                <span class="rounded border border-line px-1.5 py-0.5 text-[11px] text-cream-dim">
                                  {item.auth_method}
                                </span>
                                <Show when={item.username}>
                                  <span class="text-xs text-cream-dim">@{item.username}</span>
                                </Show>
                              </div>
                              <p class="mt-1 text-xs text-cream-dim">
                                Last active {formatStamp(item.last_seen_at)} · Expires {formatStamp(item.expires_at)}
                                <Show when={item.ip_address}> · {item.ip_address}</Show>
                              </p>
                            </div>
                            <Button
                              type="button"
                              size="sm"
                              variant={item.current ? "danger" : "outline"}
                              onClick={() => void handleRevokeSession(item)}
                            >
                              Revoke
                            </Button>
                          </li>
                        )}
                      </For>
                    </ul>
                  </Show>
                </Show>
              </div>
            </Show>

            <Show when={manageTab() === "accounts"}>
              <div class="flex flex-col gap-4">
                <Show when={usersError()}>
                  <p role="alert" class="rounded-lg bg-rust/15 px-3 py-2 text-xs text-rust">
                    {usersError()}
                  </p>
                </Show>
                <Show when={accountsNotice()}>
                  <p class="rounded-lg bg-moss/15 px-3 py-2 text-xs text-moss">{accountsNotice()}</p>
                </Show>

                <Show when={session.user()}>
                  {(activeUser) => (
                    <form class="flex flex-col gap-2.5 rounded-lg border border-line bg-ink-sunken/60 p-3.5" onSubmit={(e) => void submitChangePassword(e)}>
                      <h3 class="text-sm font-semibold text-cream">Change password (@{activeUser().username})</h3>
                      <label class="flex flex-col gap-1 text-xs text-cream-dim">
                        Current password
                        <Input
                          type="password"
                          aria-label="Current password"
                          autocomplete="current-password"
                          value={currentPw()}
                          onInput={(e) => setCurrentPw(e.currentTarget.value)}
                          required
                        />
                      </label>
                      <div class="grid gap-2.5 sm:grid-cols-2">
                        <label class="flex flex-col gap-1 text-xs text-cream-dim">
                          New password
                          <Input
                            type="password"
                            aria-label="New password"
                            autocomplete="new-password"
                            value={newPw()}
                            onInput={(e) => setNewPw(e.currentTarget.value)}
                            required
                          />
                        </label>
                        <label class="flex flex-col gap-1 text-xs text-cream-dim">
                          Confirm new password
                          <Input
                            type="password"
                            aria-label="Confirm new password"
                            autocomplete="new-password"
                            value={confirmNewPw()}
                            onInput={(e) => setConfirmNewPw(e.currentTarget.value)}
                            required
                          />
                        </label>
                      </div>
                      <label class="flex items-center gap-2 text-xs text-cream-dim">
                        <input
                          type="checkbox"
                          class="accent-brass"
                          checked={revokeOthersOnPw()}
                          onChange={(e) => setRevokeOthersOnPw(e.currentTarget.checked)}
                        />
                        Sign out other devices
                      </label>
                      <div class="flex justify-end">
                        <Button type="submit" size="sm" disabled={pwBusy()}>
                          {pwBusy() ? "Updating…" : "Update password"}
                        </Button>
                      </div>
                    </form>
                  )}
                </Show>

                <div class="flex flex-col gap-2">
                  <h3 class="text-sm font-semibold text-cream">Accounts</h3>
                  <Show when={!usersBusy() || usersLoaded()} fallback={<p class="text-sm text-cream-dim">Loading accounts…</p>}>
                    <Show
                      when={users().length > 0}
                      fallback={<p class="rounded-lg border border-line px-3 py-3 text-xs text-cream-dim">No accounts yet. Create one below so you can sign in with a username and password.</p>}
                    >
                      <ul class="flex flex-col gap-2">
                        <For each={users()}>
                          {(item) => (
                            <li class="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-line bg-ink-sunken/70 px-3 py-2.5">
                              <div class="min-w-0 flex-1">
                                <p class="text-sm font-medium text-cream">
                                  @{item.username}{" "}
                                  <Show when={item.display_name && item.display_name !== item.username}>
                                    <span class="font-normal text-cream-dim">({item.display_name})</span>
                                  </Show>
                                </p>
                                <p class="text-xs text-cream-dim">
                                  {item.active_sessions ?? 0} active session(s) · Last login{" "}
                                  {item.last_login_at ? formatStamp(item.last_login_at) : "never"}
                                </p>
                              </div>
                              <Show
                                when={confirmDeleteUserId() === item.id}
                                fallback={
                                  <Button
                                    type="button"
                                    size="sm"
                                    variant="outline"
                                    onClick={() => setConfirmDeleteUserId(item.id)}
                                  >
                                    Delete
                                  </Button>
                                }
                              >
                                <div class="flex items-center gap-1.5">
                                  <Button
                                    type="button"
                                    size="sm"
                                    variant="danger"
                                    onClick={() => void handleDeleteUser(item)}
                                  >
                                    Confirm delete
                                  </Button>
                                  <Button
                                    type="button"
                                    size="sm"
                                    variant="ghost"
                                    onClick={() => setConfirmDeleteUserId(null)}
                                  >
                                    Cancel
                                  </Button>
                                </div>
                              </Show>
                            </li>
                          )}
                        </For>
                      </ul>
                    </Show>
                  </Show>
                </div>

                <form class="flex flex-col gap-2.5 rounded-lg border border-line bg-ink-sunken/60 p-3.5" onSubmit={(e) => void submitCreateAccountInline(e)}>
                  <h3 class="text-sm font-semibold text-cream">Create account</h3>
                  <div class="grid gap-2.5 sm:grid-cols-2">
                    <label class="flex flex-col gap-1 text-xs text-cream-dim">
                      Username
                      <Input
                        type="text"
                        aria-label="New account username"
                        autocomplete="off"
                        placeholder="andrew"
                        value={newUsername()}
                        onInput={(e) => setNewUsername(e.currentTarget.value)}
                        required
                      />
                    </label>
                    <label class="flex flex-col gap-1 text-xs text-cream-dim">
                      Display name (optional)
                      <Input
                        type="text"
                        aria-label="New account display name"
                        autocomplete="off"
                        placeholder="Andrew"
                        value={newDisplayName()}
                        onInput={(e) => setNewDisplayName(e.currentTarget.value)}
                      />
                    </label>
                  </div>
                  <div class="grid gap-2.5 sm:grid-cols-2">
                    <label class="flex flex-col gap-1 text-xs text-cream-dim">
                      Password (min {AUTH_LIMITS.passwordMin} chars)
                      <Input
                        type="password"
                        aria-label="New account password"
                        autocomplete="new-password"
                        value={newAccountPw()}
                        onInput={(e) => setNewAccountPw(e.currentTarget.value)}
                        required
                      />
                    </label>
                    <label class="flex flex-col gap-1 text-xs text-cream-dim">
                      Confirm password
                      <Input
                        type="password"
                        aria-label="Confirm new account password"
                        autocomplete="new-password"
                        value={confirmAccountPw()}
                        onInput={(e) => setConfirmAccountPw(e.currentTarget.value)}
                        required
                      />
                    </label>
                  </div>
                  <Show when={!session.user()}>
                    <label class="flex items-center gap-2 text-xs text-cream-dim">
                      <input
                        type="checkbox"
                        class="accent-brass"
                        checked={signInNewAccount()}
                        onChange={(e) => setSignInNewAccount(e.currentTarget.checked)}
                      />
                      Sign in as this account now
                    </label>
                  </Show>
                  <div class="flex justify-end">
                    <Button type="submit" size="sm" disabled={createUserBusy()}>
                      {createUserBusy() ? "Creating…" : "Create account"}
                    </Button>
                  </div>
                </form>
              </div>
            </Show>
          </div>
        }
      >
        <div class="flex flex-col gap-4">
          <Show when={showLoginInOpenMode()}>
            <div class="flex items-center justify-between rounded-lg border border-line bg-ink-sunken px-3 py-2 text-xs text-cream-dim">
              <span>Hub is running in open local mode.</span>
              <Button type="button" size="sm" variant="ghost" onClick={() => setShowLoginInOpenMode(false)}>
                Back to sessions
              </Button>
            </div>
          </Show>

          <TabStrip
            label="Sign-in methods"
            value={loginTab()}
            onChange={(value) => {
              setUserPickedLoginTab(true)
              setLoginTab(value)
              setAuthError("")
            }}
            tabs={LOGIN_TABS}
          />

          <Show when={authError()}>
            <p role="alert" class="rounded-lg bg-rust/15 px-3 py-2 text-xs text-rust">
              {authError()}
            </p>
          </Show>

          <Show when={loginTab() === "password"}>
            <form class="flex flex-col gap-3" onSubmit={(e) => void submitPasswordLogin(e)}>
              <label class="flex flex-col gap-1 text-xs text-cream-dim">
                Username
                <Input
                  type="text"
                  aria-label="Username"
                  autocomplete="username"
                  placeholder="andrew"
                  value={username()}
                  onInput={(e) => setUsername(e.currentTarget.value)}
                  required
                />
              </label>
              <label class="flex flex-col gap-1 text-xs text-cream-dim">
                Password
                <Input
                  type="password"
                  aria-label="Password"
                  autocomplete="current-password"
                  value={password()}
                  onInput={(e) => setPassword(e.currentTarget.value)}
                  required
                />
              </label>
              <label class="flex flex-col gap-1 text-xs text-cream-dim">
                Device label (optional)
                <Input
                  type="text"
                  aria-label="Device label"
                  placeholder="Work laptop"
                  value={deviceLabel()}
                  onInput={(e) => setDeviceLabel(e.currentTarget.value)}
                />
              </label>
              <label class="flex items-center gap-2 text-xs text-cream-dim">
                <input
                  type="checkbox"
                  class="accent-brass"
                  checked={remember()}
                  onChange={(e) => setRemember(e.currentTarget.checked)}
                />
                Keep me signed in ({AUTH_LIMITS.sessionTtlDefaultDays} days)
              </label>
              <div class="flex justify-end gap-2 pt-1">
                <Button type="submit" disabled={busy()}>
                  {busy() ? "Signing in…" : "Sign in"}
                </Button>
              </div>
            </form>
          </Show>

          <Show when={loginTab() === "token"}>
            <form class="flex flex-col gap-3" onSubmit={(e) => void submitTokenLogin(e)}>
              <p class="text-xs text-cream-dim">
                Your hub token is exchanged once for an HttpOnly session cookie stored in SQLite—never saved to localStorage.
              </p>
              <label class="flex flex-col gap-1 text-xs text-cream-dim">
                Hub token
                <Input
                  type="password"
                  aria-label="Hub token"
                  autocomplete="off"
                  value={hubToken()}
                  onInput={(e) => setHubToken(e.currentTarget.value)}
                  required
                />
              </label>
              <label class="flex flex-col gap-1 text-xs text-cream-dim">
                Device label (optional)
                <Input
                  type="text"
                  aria-label="Device label"
                  placeholder="Work laptop"
                  value={deviceLabel()}
                  onInput={(e) => setDeviceLabel(e.currentTarget.value)}
                />
              </label>
              <label class="flex items-center gap-2 text-xs text-cream-dim">
                <input
                  type="checkbox"
                  class="accent-brass"
                  checked={remember()}
                  onChange={(e) => setRemember(e.currentTarget.checked)}
                />
                Keep me signed in (store session in SQLite)
              </label>
              <div class="flex justify-end gap-2 pt-1">
                <Button type="submit" disabled={busy()}>
                  {busy() ? "Signing in…" : "Sign in with token"}
                </Button>
              </div>
            </form>
          </Show>

          <Show when={loginTab() === "register"}>
            <form class="flex flex-col gap-3" onSubmit={(e) => void submitRegister(e)}>
              <div class="grid gap-3 sm:grid-cols-2">
                <label class="flex flex-col gap-1 text-xs text-cream-dim">
                  Username
                  <Input
                    type="text"
                    aria-label="Username"
                    autocomplete="username"
                    placeholder="andrew"
                    value={username()}
                    onInput={(e) => setUsername(e.currentTarget.value)}
                    required
                  />
                </label>
                <label class="flex flex-col gap-1 text-xs text-cream-dim">
                  Display name (optional)
                  <Input
                    type="text"
                    aria-label="Display name"
                    autocomplete="off"
                    placeholder="Andrew"
                    value={displayName()}
                    onInput={(e) => setDisplayName(e.currentTarget.value)}
                  />
                </label>
              </div>
              <div class="grid gap-3 sm:grid-cols-2">
                <label class="flex flex-col gap-1 text-xs text-cream-dim">
                  Password (min {AUTH_LIMITS.passwordMin} chars)
                  <Input
                    type="password"
                    aria-label="Password"
                    autocomplete="new-password"
                    value={password()}
                    onInput={(e) => setPassword(e.currentTarget.value)}
                    required
                  />
                </label>
                <label class="flex flex-col gap-1 text-xs text-cream-dim">
                  Confirm password
                  <Input
                    type="password"
                    aria-label="Confirm password"
                    autocomplete="new-password"
                    value={confirmPassword()}
                    onInput={(e) => setConfirmPassword(e.currentTarget.value)}
                    required
                  />
                </label>
              </div>
              <Show when={session.authRequired() && !session.authenticated()}>
                <label class="flex flex-col gap-1 text-xs text-cream-dim">
                  Hub token (required to authorize account creation)
                  <Input
                    type="password"
                    aria-label="Hub token"
                    autocomplete="off"
                    value={hubToken()}
                    onInput={(e) => setHubToken(e.currentTarget.value)}
                    required
                  />
                </label>
              </Show>
              <label class="flex items-center gap-2 text-xs text-cream-dim">
                <input
                  type="checkbox"
                  class="accent-brass"
                  checked={remember()}
                  onChange={(e) => setRemember(e.currentTarget.checked)}
                />
                Keep me signed in ({AUTH_LIMITS.sessionTtlDefaultDays} days)
              </label>
              <div class="flex justify-end gap-2 pt-1">
                <Button type="submit" disabled={busy()}>
                  {busy() ? "Creating…" : "Create account"}
                </Button>
              </div>
            </form>
          </Show>
        </div>
      </Show>
    </Modal>
  )
}

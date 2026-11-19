# Portal login & SQLite session management

Cat Fleet Chat supports persistent browser sessions backed by SQLite so you do not have to re-enter `CAT_FLEET_TOKEN` each time you open or refresh a tab.

## How it works

1. **Two ways to sign in:**
   - **Username + password (`users`):** Accounts use salted `scrypt` password hashes (`auth.py`). When `CAT_FLEET_TOKEN` is enabled, creating the first account requires either the hub token or an already-authenticated session.
   - **Hub token (`token`):** Exchange `CAT_FLEET_TOKEN` once on `POST /api/v1/auth/login` for a durable SQLite session. `CAT_FLEET_RESERVED_AUTHOR_TOKENS` secrets are not accepted for browser login.
2. **Cookie storage & security:**
   - The server stores only the **SHA-256 hash** (`sessions.token_hash`) of a random `ses_…` secret in SQLite.
   - The browser receives an `HttpOnly; SameSite=Lax; Path=/` cookie (`cat_fleet_session`), never stored in `localStorage`.
   - `Secure` is set automatically when the request is `https` or carries `X-Forwarded-Proto: https` (and omitted on plain `http://` such as localhost).
   - Session cookies authenticate `/api/*` REST and SSE calls only. They are rejected (`401`) on `/mcp`, never override an explicit invalid `Authorization` header, and never grant `reserved_author` provenance.
   - Relay `rly_` tokens remain restricted (`403` on `/api/v1/auth/*`).
3. **Quiet writes:** Login, logout, registration, and `last_seen_at` touches (`SESSION_TOUCH_INTERVAL = 300` seconds) write via `Hub._quiet`, so they never wake event waiters.
4. **Rate limiting:** `LoginRateLimiter` allows 10 failed sign-in attempts per client IP per 60-second window (`429 rate_limited`) and resets on a successful sign-in.

## Configuration

| Variable | Default | Role |
| --- | --- | --- |
| `CAT_FLEET_SESSION_TTL_DAYS` | `30` | Lifetime (1–365 days) for persistent sessions when `remember: true` (`remember: false` creates a session-only browser cookie). |

## REST API

All paths sit under `/api/v1/auth` (defined in `cat_fleet_chat/auth_contract.json`):

| Method & Path | Access | Purpose |
| --- | --- | --- |
| `GET /api/v1/auth/status` | Public | Returns `auth_required`, `authenticated`, `auth_method`, `has_users`, `user`, `session`. |
| `POST /api/v1/auth/login` | Public | Sign in with `{ username, password, remember?, label? }` or `{ token, remember?, label? }`. |
| `POST /api/v1/auth/register` | Public (requires `token` when `CAT_FLEET_TOKEN` is set and caller is unauthenticated) | Create a user account (`{ username, password, display_name?, token?, remember?, sign_in?, label? }`). |
| `POST /api/v1/auth/logout` | Public | Delete the current SQLite session (if any) and clear `cat_fleet_session`. |
| `GET /api/v1/auth/sessions` | Authenticated | List active sessions with `current`, device label, timestamps, and IP. |
| `DELETE /api/v1/auth/sessions/{session_id}` | Authenticated | Revoke one session (clears cookie if revoking the current session). |
| `POST /api/v1/auth/sessions/revoke-others` | Authenticated | Revoke every active session except the caller's current session. |
| `GET /api/v1/auth/users` | Authenticated | List user accounts and active session counts. |
| `POST /api/v1/auth/password` | Authenticated (password user) | Change password (`{ current_password, new_password, revoke_others? }`). |
| `DELETE /api/v1/auth/users/{user_id}` | Authenticated | Delete a user account (cascades to delete its SQLite sessions). |

## Single-owner contract & generator

Auth constants, limits, route specs, and UI status labels live in `cat_fleet_chat/auth_contract.json`. Regenerate the TypeScript client module after any edit:

```bash
npm --prefix web run gen:auth
npm --prefix web run gen:auth -- --check
```

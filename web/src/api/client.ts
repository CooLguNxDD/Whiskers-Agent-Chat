import type { ApiErrorBody } from "./types"

let tokenGetter: () => string = () => ""

/** Register the in-memory token reader. Called once from the app shell. */
export function setTokenGetter(getter: () => string) {
  tokenGetter = getter
}

export class ApiError extends Error {
  status: number
  code: string
  details?: Record<string, unknown>

  constructor(status: number, body: ApiErrorBody | null, fallback: string) {
    super(body?.message || fallback)
    this.status = status
    this.code = body?.code || "request_failed"
    this.details = body?.details
  }
}

export function isApiError(err: unknown): err is ApiError {
  return err instanceof ApiError
}

async function readError(response: Response): Promise<ApiErrorBody | null> {
  try {
    const parsed = (await response.json()) as { error?: ApiErrorBody }
    return parsed.error ?? null
  } catch {
    return null
  }
}

async function request(path: string, init: RequestInit): Promise<Response> {
  const headers = new Headers(init.headers)
  const token = tokenGetter()
  if (token) headers.set("Authorization", `Bearer ${token}`)
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json")
  const response = await fetch(path, { ...init, headers })
  if (!response.ok) {
    throw new ApiError(response.status, await readError(response), response.statusText)
  }
  return response
}

/** JSON fetch against the hub. Sends the bearer token when one is in memory. */
export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  return (await (await request(path, init)).json()) as T
}

/** Text fetch (JSONL exports) with the same auth and error mapping as `api`. */
export async function apiText(path: string, init: RequestInit = {}): Promise<string> {
  return (await request(path, init)).text()
}

/** A query string from the set values only. Arrays repeat the key. */
export function queryString(params: Record<string, string | number | boolean | readonly string[] | null | undefined>): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value == null || value === "") continue
    if (Array.isArray(value)) {
      for (const item of value) if (item !== "") search.append(key, item)
    } else {
      search.set(key, String(value))
    }
  }
  const text = search.toString()
  return text ? `?${text}` : ""
}

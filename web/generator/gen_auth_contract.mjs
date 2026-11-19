#!/usr/bin/env node
/**
 * Generates TypeScript auth constants, routes, and labels from cat_fleet_chat/auth_contract.json.
 *
 *   npm run gen:auth
 *   npm run gen:auth -- --check
 *
 * Options:
 *   --check              Fail if the generated file differs from auth_contract.json
 *   --contract <path>    Override source contract JSON path
 *   --out <path>         Override generated TypeScript output path
 */
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs"
import path from "node:path"
import { fileURLToPath, pathToFileURL } from "node:url"

function requireArray(value, name) {
  if (!Array.isArray(value) || value.length === 0) {
    throw new Error(`Invalid auth contract: ${name} must be a non-empty array`)
  }
  return value
}

function requireObject(value, name) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`Invalid auth contract: ${name} must be an object`)
  }
  return value
}

function stripParamTypes(routePath) {
  return String(routePath).replace(/\{([A-Za-z0-9_]+):[^}]+\}/g, "{$1}")
}

function routePrefix(routePath, id) {
  const clean = stripParamTypes(routePath)
  const idx = clean.indexOf("/{")
  if (idx === -1) {
    throw new Error(`Invalid auth contract: route ${id} must include a /{param} segment`)
  }
  return clean.slice(0, idx + 1)
}

/** Render the TypeScript contract module from parsed auth_contract.json. */
export function renderAuthContract(contract) {
  const root = requireObject(contract, "root")
  const sessionTtl = requireObject(root.session_ttl_days, "session_ttl_days")
  const limits = requireObject(root.limits, "limits")
  const authMethods = requireArray(root.auth_methods, "auth_methods")
  const authKinds = requireArray(root.auth_kinds, "auth_kinds")
  const authKindLabels = requireObject(root.auth_kind_labels, "auth_kind_labels")
  const routes = requireArray(root.routes, "routes")

  for (const kind of authKinds) {
    if (typeof authKindLabels[kind] !== "string") {
      throw new Error(`Invalid auth contract: missing auth_kind_labels.${kind}`)
    }
  }

  const routeEntries = routes.map((route) => {
    const r = requireObject(route, "route")
    if (typeof r.id !== "string" || typeof r.path !== "string" || typeof r.method !== "string") {
      throw new Error("Invalid auth contract: each route requires string id, method, and path")
    }
    return r
  })

  const routeMap = Object.fromEntries(
    routeEntries.map((route) => [route.id, stripParamTypes(route.path)]),
  )
  const revokeSessionRoute = routeEntries.find((route) => route.id === "revoke_session")
  const deleteUserRoute = routeEntries.find((route) => route.id === "delete_user")
  if (!revokeSessionRoute || !deleteUserRoute) {
    throw new Error("Invalid auth contract: routes must define revoke_session and delete_user")
  }
  const sessionPrefix = routePrefix(revokeSessionRoute.path, "revoke_session")
  const userPrefix = routePrefix(deleteUserRoute.path, "delete_user")

  const skipPaths = routeEntries
    .filter((route) => Boolean(route.skip_unauthorized_redirect))
    .map((route) => stripParamTypes(route.path))

  const labelEntries = authKinds.map(
    (kind) => `  ${kind}: ${JSON.stringify(authKindLabels[kind])},`,
  )
  const routeLines = routeEntries.map(
    (route) => `  ${route.id}: ${JSON.stringify(routeMap[route.id])},`,
  )

  return [
    "// GENERATED from cat_fleet_chat/auth_contract.json by npm run gen:auth — do not edit by hand.",
    "",
    `export const AUTH_METHODS = ${JSON.stringify(authMethods)} as const`,
    "export type AuthMethodName = (typeof AUTH_METHODS)[number]",
    "",
    `export const AUTH_KINDS = ${JSON.stringify(authKinds)} as const`,
    "export type AuthKind = (typeof AUTH_KINDS)[number]",
    "",
    "export const AUTH_KIND_LABELS: Record<AuthKind, string> = {",
    ...labelEntries,
    "}",
    "",
    "export const AUTH_LIMITS = {",
    `  usernamePattern: ${JSON.stringify(String(limits.username_pattern))},`,
    `  passwordMin: ${Number(limits.password_min)},`,
    `  passwordMax: ${Number(limits.password_max)},`,
    `  labelMax: ${Number(limits.label_max)},`,
    `  sessionTtlDefaultDays: ${Number(sessionTtl.default)},`,
    "} as const",
    "",
    "export const AUTH_ROUTES = {",
    ...routeLines,
    "} as const",
    "",
    "export function authSessionPath(sessionId: number): string {",
    `  return \`${sessionPrefix}\${sessionId}\``,
    "}",
    "",
    "export function authUserPath(userId: number): string {",
    `  return \`${userPrefix}\${userId}\``,
    "}",
    "",
    `export const SKIP_UNAUTHORIZED_PATHS: readonly string[] = ${JSON.stringify(skipPaths)}`,
    "",
  ].join("\n")
}

function parseArgs(argv) {
  const options = { check: false }
  for (let index = 0; index < argv.length; index++) {
    const arg = argv[index]
    if (arg === "--check") options.check = true
    else if (arg === "--contract" || arg === "--out") options[arg.slice(2)] = argv[++index]
    else throw new Error(`Unknown argument: ${arg}`)
  }
  return options
}

function normalizeNewlines(text) {
  return text.replace(/\r\n/g, "\n")
}

/** Generate or drift-check authContract.generated.ts. */
export function generateAuthContract(argv = []) {
  const options = parseArgs(argv)
  const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..")
  const contractPath = path.resolve(
    options.contract ?? path.join(webRoot, "..", "cat_fleet_chat", "auth_contract.json"),
  )
  const outPath = path.resolve(
    options.out ?? path.join(webRoot, "src", "api", "authContract.generated.ts"),
  )
  const contract = JSON.parse(readFileSync(contractPath, "utf8"))
  const rendered = renderAuthContract(contract)

  if (options.check) {
    if (!existsSync(outPath)) {
      throw new Error(`${outPath} is missing; run npm --prefix web run gen:auth`)
    }
    const existing = normalizeNewlines(readFileSync(outPath, "utf8"))
    if (existing !== rendered) {
      throw new Error(`${outPath} is out of date with ${contractPath}; run npm --prefix web run gen:auth`)
    }
    return { checked: true, contractPath, outPath }
  }

  mkdirSync(path.dirname(outPath), { recursive: true })
  writeFileSync(outPath, rendered, "utf8")
  return { checked: false, contractPath, outPath }
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  try {
    const result = generateAuthContract(process.argv.slice(2))
    console.log(result.checked ? `OK: ${result.outPath} matches ${result.contractPath}` : `Generated ${result.outPath}`)
  } catch (error) {
    console.error(error instanceof Error ? error.message : error)
    process.exit(1)
  }
}

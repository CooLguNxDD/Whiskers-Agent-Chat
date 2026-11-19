import { mkdtempSync, readFileSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import path from "node:path"
import { fileURLToPath } from "node:url"
import { describe, expect, it } from "vitest"
// @ts-expect-error -- plain ESM script without type declarations
import { generateAuthContract, renderAuthContract } from "./gen_auth_contract.mjs"

const dir = path.dirname(fileURLToPath(import.meta.url))
const contractPath = path.resolve(dir, "..", "..", "cat_fleet_chat", "auth_contract.json")
const generatedPath = path.resolve(dir, "..", "src", "api", "authContract.generated.ts")

describe("gen:auth", () => {
  it("renders valid TypeScript constants and path builders from a contract object", () => {
    const sample = {
      session_ttl_days: { default: 14, min: 1, max: 90 },
      limits: { username_pattern: "^[a-z]+$", password_min: 10, password_max: 128, label_max: 64 },
      auth_methods: ["password", "token"],
      auth_kinds: ["session", "bearer", "open"],
      auth_kind_labels: {
        session: "Session active",
        bearer: "Bearer active",
        open: "Open mode",
      },
      routes: [
        { id: "status", method: "GET", path: "/api/v1/auth/status", public: true, skip_unauthorized_redirect: true },
        { id: "revoke_session", method: "DELETE", path: "/api/v1/auth/sessions/{session_id:int}", public: false, skip_unauthorized_redirect: false },
        { id: "delete_user", method: "DELETE", path: "/api/v1/auth/users/{user_id:int}", public: false, skip_unauthorized_redirect: false },
      ],
    }
    const rendered = renderAuthContract(sample)
    expect(rendered).toContain("// GENERATED from cat_fleet_chat/auth_contract.json")
    expect(rendered).toContain("passwordMin: 10,")
    expect(rendered).toContain("sessionTtlDefaultDays: 14,")
    expect(rendered).toContain('revoke_session: "/api/v1/auth/sessions/{session_id}",')
    expect(rendered).toContain("return `/api/v1/auth/sessions/${sessionId}`")
    expect(rendered).toContain("return `/api/v1/auth/users/${userId}`")
    expect(rendered).toContain('SKIP_UNAUTHORIZED_PATHS: readonly string[] = ["/api/v1/auth/status"]')
  })

  it("writes files, verifies --check passes when fresh, and detects drift or invalid contracts", () => {
    const tempDir = mkdtempSync(path.join(tmpdir(), "gen-auth-"))
    const tempOut = path.join(tempDir, "authContract.generated.ts")
    const badContract = path.join(tempDir, "bad.json")

    expect(() => generateAuthContract(["--contract", contractPath, "--out", tempOut, "--check"])).toThrow(/missing/)
    generateAuthContract(["--contract", contractPath, "--out", tempOut])
    expect(() => generateAuthContract(["--contract", contractPath, "--out", tempOut, "--check"])).not.toThrow()

    writeFileSync(tempOut, "// stale\n", "utf8")
    expect(() => generateAuthContract(["--contract", contractPath, "--out", tempOut, "--check"])).toThrow(/out of date/)

    writeFileSync(badContract, JSON.stringify({ routes: [] }), "utf8")
    expect(() => generateAuthContract(["--contract", badContract, "--out", tempOut])).toThrow(/Invalid auth contract/)
  })

  it("keeps web/src/api/authContract.generated.ts in sync with cat_fleet_chat/auth_contract.json", () => {
    const contract = JSON.parse(readFileSync(contractPath, "utf8"))
    const expected = renderAuthContract(contract)
    const actual = readFileSync(generatedPath, "utf8").replace(/\r\n/g, "\n")
    expect(actual).toBe(expected)
  })
})

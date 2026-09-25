// @vitest-environment jsdom
import { afterEach, expect, it, vi } from "vitest"
import { render } from "solid-js/web"
import Appearance from "@/components/Appearance"
import { prefs } from "@/store/prefsSlice"
import { applyAppearance, accents } from "./appearance"
import { buildRegistry, themeList, themeRegistry, resolveThemeVars } from "./registry"

let dispose: (() => void) | undefined
afterEach(() => { dispose?.(); document.body.replaceChildren(); vi.restoreAllMocks() })

it("resolves ancestors and computes lightness from tokens", () => {
  const raw = {
    base: { id: "base", label: "Base", vars: { bg: "oklch(0.9 0 0)", fg: "black" } },
    child: { id: "child", label: "Child", extends: "base", vars: { fg: "blue" } },
  }
  expect(resolveThemeVars("child", raw)).toEqual({ bg: "oklch(0.9 0 0)", fg: "blue" })
  expect(buildRegistry(raw).child.isLight).toBe(true)
  vi.spyOn(console, "error").mockImplementation(() => {})
  expect(resolveThemeVars("child", { ...raw, base: { ...raw.base, extends: "child" } })).toEqual({ fg: "blue" })
  expect(resolveThemeVars("child", { child: raw.child })).toEqual({ fg: "blue" })
})

it("applies every theme and accent without overriding base colors", () => {
  for (const theme of themeList) for (const accent of accents) {
    applyAppearance(theme.id, accent)
    expect(document.documentElement.dataset.theme).toBe(theme.id)
    expect(document.documentElement.dataset.light).toBe(String(theme.isLight))
    expect(document.documentElement.dataset.accent).toBe(accent)
    expect(document.documentElement.style.getPropertyValue("--theme-amber")).toBe(theme.vars.amber)
  }
  applyAppearance("missing", "amber")
  expect(document.documentElement.dataset.theme).toBe("mocha")
})

it("persists identity and appearance together and tolerates blocked storage", () => {
  prefs.setDisplayName("ada")
  prefs.setTheme("latte")
  prefs.setAccent("violet")
  expect(JSON.parse(localStorage.getItem("cat-fleet-prefs")!).state).toEqual({ displayName: "ada", theme: "latte", accent: "violet" })
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("blocked") })
  expect(() => prefs.setTheme("paper")).not.toThrow()
  expect(prefs.theme()).toBe("paper")
})

it("removes tokens absent in the next theme", () => {
  themeRegistry.temporary = { id: "temporary", label: "Temporary", isLight: false, vars: { ...themeRegistry.mocha.vars, "temporary-only": "red" } }
  try {
    applyAppearance("temporary", "cyan")
    expect(document.documentElement.style.getPropertyValue("--theme-temporary-only")).toBe("red")
    applyAppearance("mocha", "amber")
    expect(document.documentElement.style.getPropertyValue("--theme-temporary-only")).toBe("")
  } finally { delete themeRegistry.temporary }
})

it("restores legacy names and falls back for invalid saved settings", async () => {
  localStorage.setItem("cat-fleet-prefs", JSON.stringify({ state: { displayName: "legacy", theme: "missing", accent: "missing" }, version: 0 }))
  vi.resetModules()
  const restored = (await import("@/store/prefsSlice")).prefs
  expect(restored.displayName()).toBe("legacy")
  expect(restored.theme()).toBe("mocha")
  expect(restored.accent()).toBe("amber")
  localStorage.setItem("cat-fleet-prefs", "{broken")
  vi.resetModules()
  expect((await import("@/store/prefsSlice")).prefs.theme()).toBe("mocha")
})

it("renders registry choices and applies picker selections", () => {
  const container = document.createElement("div")
  document.body.append(container)
  const close = vi.fn()
  dispose = render(() => <Appearance open={true} onOpenChange={close} />, container)
  expect(container.querySelectorAll("button[aria-pressed]").length).toBe(themeList.length + accents.length)
  const cyan = container.querySelector('[aria-label="cyan accent"]') as HTMLButtonElement
  cyan.click()
  expect(prefs.accent()).toBe("cyan")
  expect(cyan.getAttribute("aria-pressed")).toBe("true")
  window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }))
  expect(close).toHaveBeenCalledWith(false)
})

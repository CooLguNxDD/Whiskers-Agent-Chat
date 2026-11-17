import { mkdtempSync, readFileSync } from "node:fs"
import { tmpdir } from "node:os"
import path from "node:path"
import { describe, expect, it } from "vitest"
// @ts-expect-error -- plain ESM script without type declarations
import { addExport, generatePanel, panelNames, renderPanelSource } from "./gen_panel.mjs"

describe("gen:panel", () => {
  it("derives names from Pascal, kebab or spaced input", () => {
    expect(panelNames("TaskQueuePanel")).toEqual({ pascal: "TaskQueue", camel: "taskQueue", id: "task-queue", title: "Task queue" })
    expect(panelNames("task-queue")?.pascal).toBe("TaskQueue")
    expect(panelNames("9lives")).toBeNull()
    expect(panelNames("")).toBeNull()
  })

  it("renders a PanelDef with optional icon and fixed flag", () => {
    const source = renderPanelSource(panelNames("Notes"), { icon: "list", fixed: true, title: "Cat notes" })
    expect(source).toContain('export const notesPanel: PanelDef = {')
    expect(source).toContain('id: "notes"')
    expect(source).toContain('title: "Cat notes"')
    expect(source).toContain('icon: "list"')
    expect(source).toContain("closable: false")
  })

  it("adds exports once, sorted, under the header", () => {
    const one = addExport("", panelNames("Zebra"))
    const two = addExport(addExport(one, panelNames("Alpha")), panelNames("Alpha"))
    expect(two.split("\n").filter((line) => line.startsWith("export"))).toEqual([
      'export { alphaPanel, AlphaPanelBody } from "./AlphaPanel"',
      'export { zebraPanel, ZebraPanelBody } from "./ZebraPanel"',
    ])
    expect(two.startsWith("/**")).toBe(true)
  })

  it("writes files and refuses to overwrite without --force", () => {
    const root = mkdtempSync(path.join(tmpdir(), "gen-panel-"))
    const { file, index } = generatePanel(["Inbox", "--icon", "inbox", "--root", root])
    expect(readFileSync(file, "utf8")).toContain("InboxPanelBody")
    expect(readFileSync(index, "utf8")).toContain('from "./InboxPanel"')
    expect(() => generatePanel(["Inbox", "--root", root])).toThrow(/exists/)
    expect(() => generatePanel(["Inbox", "--root", root, "--force"])).not.toThrow()
    expect(() => generatePanel(["Inbox", "--icon", "Bad Icon", "--root", root, "--force"])).toThrow(/IconName/)
    expect(() => generatePanel([])).toThrow(/Usage/)
  })
})

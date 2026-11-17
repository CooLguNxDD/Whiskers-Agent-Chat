#!/usr/bin/env node
/**
 * Scaffolds a dockable panel for the panel kit (src/components/ui/dock).
 *
 *   npm run gen:panel -- <Name> [--title "Shown title"] [--icon <IconName>] [--fixed] [--force]
 *
 * Writes src/components/panels/<Name>Panel.tsx (a body component plus a `PanelDef`) and adds an
 * export to src/components/panels/index.ts. Then put the panel in a DockArea's `panels` and its
 * default layout (see docs/panels.md). `--fixed` makes it non-closable; `--root <dir>` targets
 * another web root (tests use it).
 */
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs"
import path from "node:path"
import { fileURLToPath, pathToFileURL } from "node:url"

/** Name forms derived from a PascalCase / kebab / spaced name, or null when it is unusable. */
export function panelNames(raw) {
  const words = String(raw ?? "")
    .replace(/Panel$/, "")
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .split(/[^A-Za-z0-9]+/)
    .filter(Boolean)
  if (!words.length || !/^[A-Za-z]/.test(words[0])) return null
  const pascal = words.map((word) => word[0].toUpperCase() + word.slice(1)).join("")
  return {
    pascal,
    camel: pascal[0].toLowerCase() + pascal.slice(1),
    id: words.map((word) => word.toLowerCase()).join("-"),
    title: words.map((word, index) => (index ? word.toLowerCase() : word[0].toUpperCase() + word.slice(1))).join(" "),
  }
}

/** The panel module source. */
export function renderPanelSource(names, options = {}) {
  const title = options.title ?? names.title
  const lines = [
    `import type { PanelDef } from "@/components/ui/dock"`,
    ``,
    `/** ${title}: body of the \`${names.id}\` dock panel. Fills its stack; scroll inside. */`,
    `export function ${names.pascal}PanelBody() {`,
    `  return (`,
    `    <section class="flex min-h-0 flex-1 flex-col overflow-y-auto p-4" aria-label=${JSON.stringify(title)}>`,
    `      <p class="text-sm text-cream-dim">${title.replace(/[<>{}]/g, "")} panel</p>`,
    `    </section>`,
    `  )`,
    `}`,
    ``,
    `/** Dock registration for {@link ${names.pascal}PanelBody}. Scaffolded by \`npm run gen:panel\`. */`,
    `export const ${names.camel}Panel: PanelDef = {`,
    `  id: ${JSON.stringify(names.id)},`,
    `  title: ${JSON.stringify(title)},`,
  ]
  if (options.icon) lines.push(`  icon: ${JSON.stringify(options.icon)},`)
  if (options.fixed) lines.push(`  closable: false,`)
  lines.push(`  render: () => <${names.pascal}PanelBody />,`, `}`, ``)
  return lines.join("\n")
}

const INDEX_HEADER = `/** Dockable panels shared across pages. Add one with \`npm run gen:panel -- <Name>\`. */\n`

/** `index` with an export line for the panel added once, kept sorted. */
export function addExport(index, names) {
  const line = `export { ${names.camel}Panel, ${names.pascal}PanelBody } from "./${names.pascal}Panel"`
  const body = (index || INDEX_HEADER).split("\n").filter((entry) => entry.trim())
  const header = body.filter((entry) => !entry.startsWith("export "))
  const exports = body.filter((entry) => entry.startsWith("export "))
  if (!exports.includes(line)) exports.push(line)
  exports.sort()
  return [...header, ...exports].join("\n") + "\n"
}

function parseArgs(argv) {
  const options = { force: false, fixed: false }
  const rest = []
  for (let index = 0; index < argv.length; index++) {
    const arg = argv[index]
    if (arg === "--force") options.force = true
    else if (arg === "--fixed") options.fixed = true
    else if (arg === "--title" || arg === "--icon" || arg === "--root") options[arg.slice(2)] = argv[++index]
    else rest.push(arg)
  }
  return { options, name: rest[0] }
}

/** Runs the generator; returns the written paths. Throws with a readable message on bad input. */
export function generatePanel(argv) {
  const { options, name } = parseArgs(argv)
  const names = panelNames(name)
  if (!names) throw new Error("Usage: npm run gen:panel -- <Name> [--title T] [--icon I] [--fixed] [--force]")
  if (options.icon !== undefined && !/^[a-z]+$/.test(options.icon)) throw new Error(`--icon must be an IconName, got ${JSON.stringify(options.icon)}`)
  const root = path.resolve(options.root ?? path.join(path.dirname(fileURLToPath(import.meta.url)), ".."))
  const dir = path.join(root, "src", "components", "panels")
  const file = path.join(dir, `${names.pascal}Panel.tsx`)
  const index = path.join(dir, "index.ts")
  if (existsSync(file) && !options.force) throw new Error(`${path.relative(root, file)} exists; pass --force to overwrite`)
  mkdirSync(dir, { recursive: true })
  writeFileSync(file, renderPanelSource(names, options))
  writeFileSync(index, addExport(existsSync(index) ? readFileSync(index, "utf8") : "", names))
  return { names, file, index }
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  try {
    const { names, file, index } = generatePanel(process.argv.slice(2))
    console.log(`Created ${file}\nUpdated ${index}\n\nUse it:\n  import { ${names.camel}Panel } from "@/components/panels"\n  <DockArea panels={[..., ${names.camel}Panel]} ... />  // and add "${names.id}" to the default layout`)
  } catch (error) {
    console.error(error instanceof Error ? error.message : error)
    process.exit(1)
  }
}

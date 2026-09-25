import { For } from "solid-js"
import { Modal } from "@/components/ui/dialog"
import { usePrefs } from "@/store"
import { themeList } from "@/themes/registry"
import { accents } from "@/themes/appearance"

/** Theme previews and accent swatches with immediate, device-local persistence. */
export default function Appearance(props: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const prefs = usePrefs()
  return <Modal open={props.open} onOpenChange={props.onOpenChange} title="Appearance">
    <fieldset>
      <legend class="mb-3 text-sm text-cream-dim">Theme</legend>
      <div class="grid grid-cols-2 gap-3 sm:grid-cols-3">
        <For each={themeList}>{def => <button type="button" aria-pressed={prefs.theme() === def.id}
          class="theme-choice rounded-lg border border-line p-2 text-left" onClick={() => prefs.setTheme(def.id)}>
          <div class="mb-2 rounded p-3" style={{ background: def.vars.bg }} aria-hidden="true">
            <div class="h-4 rounded" style={{ background: def.vars.card }} />
            <div class="mt-2 h-1.5 w-8 rounded" style={{ background: def.vars.amber }} />
          </div>
          <span class="block text-sm">{def.label}</span>
          <span class="block text-xs text-cream-dim">{def.description}</span>
        </button>}</For>
      </div>
    </fieldset>
    <fieldset class="mt-5">
      <legend class="mb-3 text-sm text-cream-dim">Accent</legend>
      <div class="flex flex-wrap gap-3">
        <For each={accents}>{accent => <button type="button" aria-label={`${accent} accent`}
          aria-pressed={prefs.accent() === accent} onClick={() => prefs.setAccent(accent)}
          class="theme-choice flex flex-col items-center gap-1 rounded-lg border border-line p-2 text-xs capitalize">
          <span class="size-7 rounded-full" style={{ background: `var(--accent-${accent})` }} aria-hidden="true" />
          {accent}
        </button>}</For>
      </div>
    </fieldset>
    <p class="mt-4 text-xs text-cream-dim">Changes apply immediately and are saved on this device.</p>
  </Modal>
}

# Panel kit (portal)

The portal's dockable, resizable panels live in `web/src/components/ui/dock/`. The Chat page
(`FleetPage`) is built from it: **Chat**, **Fleet** and **In progress** are panels you can
resize, re-dock and close. The layout is saved per device.

## Using it

- **Resize**: drag the bar between two panels, or focus it and use the arrow keys. The app
  sidebar's right edge resizes the same way (200–420px).
- **Dock**: drag a panel's tab onto another panel. Drop it in the centre to add it as a tab, or
  near an edge (the outer quarter) to split beside it. A tinted preview shows where it will land.
- **Close / reopen**: use `×` on a tab. Chat's ◧ button toggles Fleet, and a reopened panel docks
  next to its sibling (`PanelHome.near`).
- **Narrow screens**: below 900px of dock width, every open panel collapses into one tab strip.
- **Reset**: Appearance → Layout → *Reset panel layout* clears every saved layout, sidebar width
  and folded section.

## Building blocks

| Piece | File | What it does |
| --- | --- | --- |
| Layout model | `layout.ts` | Pure split/stack tree: `movePanel`, `removePanel`, `dockPanel`, `resizeSplit`, `flatten`, `sanitize` (untrusted JSON → tree). Untouched subtrees keep their identity. |
| State | `dockState.ts` | `createDockState(key, panelIds, defaults, homes)`: a Solid store reconciled by node `id`, saved as `cat-fleet-dock:<key>`. Panels missing from a saved layout's `known` list are docked at their home. |
| Renderer | `DockArea.tsx` | Renders the tree. Each panel body is created once and re-parented between stacks, so drafts, scroll and queries survive a re-dock; a closed panel's body is disposed. |
| Splitter | `ResizeHandle.tsx` | `role="separator"` bar with pointer drag and arrow keys. |
| Long lists | `CollapsibleList.tsx` | A section header that folds it (saved as `cat-fleet-dock:fold:<id>`), with long lists clamped to `limit` rows plus *Show all N*; `foldAbove` folds by default. |
| Hover card | `../hovercard.tsx` | Read-only hover/focus card in a portal (`pointer-events: none` so it never blocks a splitter; Escape closes). |

## Adding a panel

```bash
npm --prefix web run gen:panel -- AgentNotes --title "Agent notes" --icon list
```

This writes `src/components/panels/AgentNotesPanel.tsx` (a body component plus a `PanelDef`)
and exports it from `src/components/panels/index.ts`. Flags: `--fixed` (no close button),
`--force` (overwrite). Then add it to a page:

```tsx
const dock = createDockState("chat", ["chat", "fleet", "agent-notes"], defaults, {
  "agent-notes": { zone: "right", share: 0.24, near: { panel: "fleet", zone: "center" } },
})
<DockArea dock={dock} panels={[chatPanel, fleetPanel, agentNotesPanel]} />
```

Panels that need page state (such as Chat) are defined inline as a `PanelDef` instead.

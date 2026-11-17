import AgentRoster from "@/components/AgentRoster"
import type { PanelDef } from "@/components/ui/dock"

/** Fleet: body of the `fleet` dock panel — the open channel's agent roster. */
export function FleetPanelBody() {
  return <AgentRoster />
}

/** Dock registration for {@link FleetPanelBody}. Scaffolded by `npm run gen:panel`. */
export const fleetPanel: PanelDef = {
  id: "fleet",
  title: "Fleet",
  icon: "board",
  render: () => <FleetPanelBody />,
}

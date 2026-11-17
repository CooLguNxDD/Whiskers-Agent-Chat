import { InProgressTasks } from "@/components/AgentRoster"
import type { PanelDef } from "@/components/ui/dock"

/** In progress: body of the `in-progress` dock panel — the open channel's in-flight tasks. */
export function InProgressPanelBody() {
  return <InProgressTasks />
}

/** Dock registration for {@link InProgressPanelBody}. Scaffolded by `npm run gen:panel`. */
export const inProgressPanel: PanelDef = {
  id: "in-progress",
  title: "In progress",
  icon: "tasks",
  render: () => <InProgressPanelBody />,
}

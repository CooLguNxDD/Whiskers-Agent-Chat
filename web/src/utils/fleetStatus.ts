/**
 * Agent state for the fleet panel, derived only from data the hub has: the
 * channel's tasks (assignee + status) and each handle's last post. It says
 * what the board shows, not whether a CLI process is alive.
 */
import type { AgentActivity, FleetTask } from "@/api/types"

export type FleetState = "working" | "blocked" | "claimed" | "idle" | "away"

export interface FleetMember {
  name: string
  state: FleetState
  /** The task behind the state, when there is one. */
  task?: FleetTask
  lastActivity: string
}

/** Posted within this window and holding no task → idle; older → away. */
export const IDLE_WINDOW_MS = 30 * 60 * 1000

const ORDER: Record<FleetState, number> = { working: 0, blocked: 1, claimed: 2, idle: 3, away: 4 }
const TASK_STATE: Partial<Record<FleetTask["status"], FleetState>> = { in_progress: "working", blocked: "blocked", claimed: "claimed" }

export function fleetMembers(agents: AgentActivity[], tasks: FleetTask[], now = Date.now()): FleetMember[] {
  const members = agents.map((agent): FleetMember => {
    // The most urgent of the agent's live tasks wins: working > blocked > claimed.
    const live = tasks
      .filter((task) => task.assignee === agent.name && TASK_STATE[task.status])
      .sort((a, b) => ORDER[TASK_STATE[a.status]!] - ORDER[TASK_STATE[b.status]!] || b.updated_at.localeCompare(a.updated_at))
    if (live[0]) return { name: agent.name, state: TASK_STATE[live[0].status]!, task: live[0], lastActivity: agent.last_activity }
    const at = new Date(agent.last_activity).getTime()
    const recent = Number.isFinite(at) && now - at < IDLE_WINDOW_MS
    return { name: agent.name, state: recent ? "idle" : "away", lastActivity: agent.last_activity }
  })
  return members.sort((a, b) => ORDER[a.state] - ORDER[b.state] || b.lastActivity.localeCompare(a.lastActivity))
}

/** Tasks with someone on them or stuck, newest change first. */
export function activeTasks(tasks: FleetTask[]): FleetTask[] {
  return tasks
    .filter((task) => task.status === "in_progress" || task.status === "blocked" || task.status === "claimed")
    .sort((a, b) => b.updated_at.localeCompare(a.updated_at))
}

/** "just now", "5m ago", "3h ago", "2d ago". */
export function relativeTime(iso: string, now = Date.now()): string {
  const at = new Date(iso).getTime()
  if (!Number.isFinite(at)) return ""
  const minutes = Math.max(0, Math.round((now - at) / 60000))
  if (minutes < 1) return "just now"
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  return `${Math.round(hours / 24)}d ago`
}

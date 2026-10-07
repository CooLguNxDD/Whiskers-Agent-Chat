import { createRenderEffect, lazy } from "solid-js"
import { prefs } from "@/store/prefsSlice"
import { applyAppearance } from "@/themes/appearance"
import { render } from "solid-js/web"
import { Router, Route } from "@solidjs/router"
import { QueryClient, QueryClientProvider } from "@tanstack/solid-query"
import { FleetStreamProvider } from "@/hooks/useFleetStream"
import { RootLayout } from "@/routes/__root"
import FleetPage from "@/components/FleetPage"
import "./index.css"

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: 1, refetchOnWindowFocus: false },
  },
})

const TasksPage = lazy(() => import("@/components/TaskBoard"))
const WebhooksPage = lazy(() => import("@/components/Webhooks"))
const root = document.getElementById("root")
if (!root) throw new Error("root element missing")
applyAppearance(prefs.theme(), prefs.accent())

render(
  () => {
    createRenderEffect(() => applyAppearance(prefs.theme(), prefs.accent()))
    return (
    <QueryClientProvider client={queryClient}>
      <FleetStreamProvider>
        <Router root={RootLayout}>
          <Route path="/" component={FleetPage} />
          <Route path="/tasks" component={TasksPage} />
          <Route path="/webhooks" component={WebhooksPage} />
        </Router>
      </FleetStreamProvider>
    </QueryClientProvider>
    )
  },
  root,
)

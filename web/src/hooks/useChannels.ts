import { createMutation, createQuery, useQueryClient } from "@tanstack/solid-query"
import { createChannel, listChannels, setChannelState } from "@/api/fleet"
import type { Channel } from "@/api/types"
import { useUIStore } from "@/store"

export function useChannels() {
  return createQuery(() => ({
    queryKey: ["channels"],
    queryFn: listChannels,
  }))
}

export function useCreateChannel() {
  const queryClient = useQueryClient()
  return createMutation(() => ({
    mutationFn: ({ name, topic }: { name: string; topic: string }) => createChannel(name, topic),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["channels"] })
    },
  }))
}

/** The selected channel's row, or undefined while channels load. */
export function useActiveChannel(): () => Channel | undefined {
  const channels = useChannels()
  const ui = useUIStore()
  return () => channels.data?.channels.find((channel) => channel.name === ui.activeChannel())
}

/** Change a channel's state. Refreshes channels and tasks (a forced archive cancels tasks). */
export function useSetChannelState() {
  const queryClient = useQueryClient()
  return createMutation(() => ({
    mutationFn: setChannelState,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["channels"] })
      void queryClient.invalidateQueries({ queryKey: ["tasks"] })
    },
  }))
}

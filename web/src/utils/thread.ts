import type { ThreadNode, ThreadPage } from "@/api/types"

/** Levels one thread read shows. Mirrors feed.THREAD_NEST_MAX on the hub. */
export const THREAD_NEST_MAX = 16

/**
 * Merge the pages of one thread read into a single reply tree.
 *
 * The hub nests each page on its own, so a reply whose parent came on an
 * earlier page arrives as a top-level node. This flattens every page, keeps the
 * newest copy of each post, and rebuilds the tree from `parent_id`, siblings in
 * id (chronological) order. A post whose parent is not in the pages stays at
 * the top. Iterative throughout: no recursion at any depth. The hub's window
 * already bounds the depth, so nothing is re-parented to cap it.
 */
export function mergeThread(pages: readonly Pick<ThreadPage, "posts">[]): ThreadNode[] {
  const flat = new Map<number, ThreadNode>()
  for (const page of pages) {
    const stack = [...page.posts]
    while (stack.length > 0) {
      const node = stack.pop()!
      flat.set(node.id, { ...node, replies: [] })
      for (const reply of node.replies ?? []) stack.push(reply)
    }
  }
  const ordered = [...flat.values()].sort((a, b) => a.id - b.id)
  const roots: ThreadNode[] = []
  for (const node of ordered) {
    const parent = node.parent_id != null ? flat.get(node.parent_id) : undefined
    if (parent && parent !== node) parent.replies.push(node)
    else roots.push(node)
  }
  return roots
}

/** Every node of a tree, depth first, without recursion. */
export function walkThread(roots: readonly ThreadNode[]): ThreadNode[] {
  const out: ThreadNode[] = []
  const stack = [...roots].reverse()
  while (stack.length > 0) {
    const node = stack.pop()!
    out.push(node)
    for (let index = node.replies.length - 1; index >= 0; index--) stack.push(node.replies[index])
  }
  return out
}

/** Deepest nesting level of a tree (a lone root is 1). */
export function threadDepth(roots: readonly ThreadNode[]): number {
  let deepest = 0
  const stack: [ThreadNode, number][] = roots.map((node) => [node, 1])
  while (stack.length > 0) {
    const [node, level] = stack.pop()!
    deepest = Math.max(deepest, level)
    for (const reply of node.replies) stack.push([reply, level + 1])
  }
  return deepest
}

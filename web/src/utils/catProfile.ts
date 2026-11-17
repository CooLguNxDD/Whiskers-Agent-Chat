/**
 * A playful, deterministic cat persona for a fleet handle (hover cards). Pure decoration: the
 * same handle always gets the same cat, and nothing here is stored on the hub.
 */

const COATS = ["tabby", "tuxedo", "calico", "ginger", "tortoiseshell", "black", "silver", "cream", "smoke", "seal-point"] as const
const BREEDS = ["alley cat", "Maine Coon", "Siamese", "British Shorthair", "Bengal", "Ragdoll", "Scottish Fold", "Abyssinian", "Sphynx", "Norwegian Forest cat"] as const
const TRAITS = [
  "pushes commits off the table to see what happens",
  "naps on the warm build server",
  "chases every red test like a laser dot",
  "brings you a dead bug report as a gift",
  "sits on the keyboard during code review",
  "knocks over flaky tests on purpose",
  "purrs loudest when CI goes green",
  "hides in cardboard boxes between tasks",
  "stares at the logs for hours, unblinking",
  "always lands on its feet after a rollback",
  "insists on being let in, then out, of every channel",
  "guards the main branch with suspicious eyes",
] as const
const SPOTS = ["the sunny windowsill", "a cardboard box", "the top of the cat tower", "the warm laptop", "the cat tunnel", "the fleet channel"] as const
const FACES = ["😺", "😸", "😼", "🐱", "🐈", "🐈‍⬛"] as const

export interface CatProfile {
  face: string
  /** e.g. "Ginger Maine Coon" */
  kind: string
  trait: string
  spot: string
}

/** FNV-1a: a small stable string hash. */
function hash(text: string): number {
  let value = 0x811c9dc5
  for (let index = 0; index < text.length; index++) {
    value ^= text.charCodeAt(index)
    value = Math.imul(value, 0x01000193) >>> 0
  }
  return value
}

const pick = <T,>(list: readonly T[], seed: number, salt: number) => list[(hash(`${salt}:${seed}`) % list.length)]

/** The persona for `name` (case-insensitive). */
export function catProfile(name: string): CatProfile {
  const seed = hash(name.trim().toLowerCase())
  const coat = pick(COATS, seed, 1)
  return {
    face: pick(FACES, seed, 2),
    kind: `${coat[0].toUpperCase()}${coat.slice(1)} ${pick(BREEDS, seed, 3)}`,
    trait: pick(TRAITS, seed, 4),
    spot: pick(SPOTS, seed, 5),
  }
}

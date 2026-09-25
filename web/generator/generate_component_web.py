#!/usr/bin/env python3
"""
generate_component_web.py

Scaffolds a React (web) component for the Cat Tunnel Operator Console stack.
Uses a Finite State Machine to drive the generation pipeline.

Usage:
    python scripts/generate_component_web.py <Name> [options]

Options:
    --dir <path>   Subdirectory under src/ (default: components)
    --motion       Add Framer Motion motion.div + AnimatePresence
    --store        Add Zustand useUIStore import stub
    --events       Add mitt event bus import + typed listener stub
    --query        Add TanStack Query useQuery stub
    --page         Alias for --motion --store

Examples:
    python scripts/generate_component_web.py ModCard --motion --store
    python scripts/generate_component_web.py AuthPill --motion --events
    python scripts/generate_component_web.py StatsStrip --query
    python scripts/generate_component_web.py ConsolePage --page --dir pages
    python scripts/generate_component_web.py Badge
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path


# ── FSM states ────────────────────────────────────────────────────────────────

class State(Enum):
    """
    State representation for component generation.
    """
    IDLE        = auto()   # initial — nothing has happened yet
    PARSED      = auto()   # CLI args validated and stored in context
    PATHS_READY = auto()   # target paths computed, collision check passed
    BUILT       = auto()   # file content strings assembled in memory
    WRITTEN     = auto()   # files flushed to disk
    DONE        = auto()   # success message printed — terminal state
    ERROR       = auto()   # terminal error state


# ── Transitions ───────────────────────────────────────────────────────────────

TRANSITIONS: dict[tuple[State, str], State] = {
    (State.IDLE,        "parse"):   State.PARSED,
    (State.PARSED,      "resolve"): State.PATHS_READY,
    (State.PATHS_READY, "build"):   State.BUILT,
    (State.BUILT,       "write"):   State.WRITTEN,
    (State.WRITTEN,     "finish"):  State.DONE,
    # any state → ERROR
    (State.IDLE,        "fail"):    State.ERROR,
    (State.PARSED,      "fail"):    State.ERROR,
    (State.PATHS_READY, "fail"):    State.ERROR,
    (State.BUILT,       "fail"):    State.ERROR,
    (State.WRITTEN,     "fail"):    State.ERROR,
}


# ── Context ───────────────────────────────────────────────────────────────────

@dataclass
class Context:
    """
    Context class for component generation.
    """
    # populated by PARSE
    name:        str  = ""
    pascal:      str  = ""
    sub_dir:     str  = "components"
    with_motion: bool = False
    with_store:  bool = False
    with_events: bool = False
    with_query:  bool = False

    # populated by RESOLVE_PATHS
    root:           Path = field(default_factory=Path)
    target_dir:     Path = field(default_factory=Path)
    component_file: Path = field(default_factory=Path)
    index_file:     Path = field(default_factory=Path)

    # populated by BUILD
    component_src: str = ""
    index_src:     str = ""

    # set on error
    error_msg: str = ""


# ── FSM ───────────────────────────────────────────────────────────────────────

class GeneratorFSM:
    """
    FSM for component generator.
    """
    def __init__(self) -> None:
        self.state   = State.IDLE
        self.context = Context()

    def transition(self, event: str) -> None:
        key = (self.state, event)
        if key not in TRANSITIONS:
            raise RuntimeError(f"No transition from {self.state.name} on event '{event}'")
        self.state = TRANSITIONS[key]

    def run(self, argv: list[str]) -> int:
        steps = [
            ("parse",   self._do_parse),
            ("resolve", self._do_resolve_paths),
            ("build",   self._do_build),
            ("write",   self._do_write),
            ("finish",  self._do_finish),
        ]
        for event, action in steps:
            try:
                action(argv)
                self.transition(event)
            except SystemExit as exc:
                return int(exc.code) if exc.code is not None else 0
            except Exception as exc:  # noqa: BLE001
                self.context.error_msg = str(exc)
                self.transition("fail")
                print(f"✗ Error [{self.state.name}]: {self.context.error_msg}", file=sys.stderr)
                return 1
        return 0

    # ── State actions ─────────────────────────────────────────────────────────

    def _do_parse(self, argv: list[str]) -> None:
        assert self.state == State.IDLE
        parser = argparse.ArgumentParser(
            prog="generate_component_web.py",
            description="Scaffold a React (web) component for the Cat Tunnel stack.",
        )
        parser.add_argument("name",    help="Component name (any casing — normalised to PascalCase)")
        parser.add_argument("--dir",   default="components", dest="sub_dir", metavar="PATH",
                            help="Subdirectory under src/ (default: components)")
        parser.add_argument("--motion", action="store_true", help="Add Framer Motion wrapper")
        parser.add_argument("--store",  action="store_true", help="Add Zustand store stub")
        parser.add_argument("--events", action="store_true", help="Add mitt event bus stub")
        parser.add_argument("--query",  action="store_true", help="Add TanStack Query stub")
        parser.add_argument("--page",   action="store_true",
                            help="Alias for --motion --store (page-level component)")
        ns = parser.parse_args(argv)

        ctx             = self.context
        ctx.name        = ns.name
        ctx.pascal      = ns.name[0].upper() + ns.name[1:]
        ctx.sub_dir     = ns.sub_dir
        ctx.with_motion = ns.motion or ns.page
        ctx.with_store  = ns.store  or ns.page
        ctx.with_events = ns.events
        ctx.with_query  = ns.query

    def _do_resolve_paths(self, _argv: list[str]) -> None:
        assert self.state == State.PARSED
        ctx = self.context
        ctx.root           = Path(__file__).resolve().parent.parent
        ctx.target_dir     = ctx.root / "src" / ctx.sub_dir / ctx.pascal
        ctx.component_file = ctx.target_dir / f"{ctx.pascal}.tsx"
        ctx.index_file     = ctx.target_dir / "index.ts"
        for f in (ctx.component_file, ctx.index_file):
            if f.exists():
                raise FileExistsError(f"File already exists: {f}")

    def _do_build(self, _argv: list[str]) -> None:
        assert self.state == State.PATHS_READY
        ctx = self.context
        ctx.component_src = build_component(ctx)
        ctx.index_src     = build_index(ctx)

    def _do_write(self, _argv: list[str]) -> None:
        assert self.state == State.BUILT
        ctx = self.context
        ctx.target_dir.mkdir(parents=True, exist_ok=True)
        ctx.component_file.write_text(ctx.component_src, encoding="utf-8")
        print(f"  Created: {ctx.component_file.relative_to(ctx.root)}")
        ctx.index_file.write_text(ctx.index_src, encoding="utf-8")
        print(f"  Created: {ctx.index_file.relative_to(ctx.root)}")

    def _do_finish(self, _argv: list[str]) -> None:
        assert self.state == State.WRITTEN
        ctx   = self.context
        flags = " ".join(filter(None, [
            "--motion" if ctx.with_motion else None,
            "--store"  if ctx.with_store  else None,
            "--events" if ctx.with_events else None,
            "--query"  if ctx.with_query  else None,
        ])) or "(none)"
        p = ctx.pascal
        d = ctx.sub_dir
        print(f"""
✓ {p} scaffolded  [flags: {flags}]

Next steps:
  1. Import: import {p} from "@/{d}/{p}"
  2. Extend {p}Props with real props.
  3. Replace // TODO stubs with real query keys / store selectors / event names.
  4. Style with Tailwind utilities — add classes to the cn() call or child elements.
""")


# ── Template builders ─────────────────────────────────────────────────────────

def build_imports(ctx: Context) -> list[str]:
    """
    Builds import statements for a component.
    """
    lines = [
        'import type { FC, ReactNode } from "react"',
        'import { cn } from "@/lib/utils"',
    ]
    if ctx.with_events:
        lines.append('import { useEffect } from "react"')
    if ctx.with_motion:
        lines.append('import { motion, AnimatePresence } from "framer-motion"')
    if ctx.with_store:
        lines.append('import { useUIStore } from "@/store"')
    if ctx.with_events:
        lines.append('import { bus } from "@/events/bus"')
    if ctx.with_query:
        lines.append('import { useQuery } from "@tanstack/react-query"')
    return lines


def build_stubs(ctx: Context) -> list[str]:
    """Return individual stub blocks (each already indented 2 spaces)."""
    stubs: list[str] = []

    if ctx.with_query:
        stubs.append(
            f'  // TODO: replace with real query key + fetcher\n'
            f'  const {{ data, isPending }} = useQuery({{\n'
            f'    queryKey: ["{ctx.pascal.lower()}"],\n'
            f'    queryFn: () => Promise.resolve(null),\n'
            f'  }})'
        )

    if ctx.with_store:
        stubs.append(
            '  // TODO: select only the slices you need\n'
            '  const expandedModId = useUIStore(s => s.expandedModId)'
        )

    if ctx.with_events:
        stubs.append(
            '  useEffect(() => {\n'
            '    // TODO: replace event name + handler\n'
            '    const handler = () => {}\n'
            '    bus.on("mod:toggled", handler)\n'
            '    return () => bus.off("mod:toggled", handler)\n'
            '  }, [])'
        )

    return stubs


def build_jsx(ctx: Context) -> list[str]:
    """Return JSX lines indented for inside `return (`."""
    if ctx.with_motion:
        return [
            '    <AnimatePresence>',
            '      <motion.div',
            '        className={cn("flex flex-col", className)}',
            '        initial={{ opacity: 0, y: 8 }}',
            '        animate={{ opacity: 1, y: 0 }}',
            '        exit={{ opacity: 0, y: -4 }}',
            '        transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}',
            '      >',
            '        {children}',
            '      </motion.div>',
            '    </AnimatePresence>',
        ]
    return [
        '    <div className={cn("flex flex-col", className)}>',
        '      {children}',
        '    </div>',
    ]


def build_component(ctx: Context) -> str:
    """
    Builds the main component body.
    """
    p = ctx.pascal

    imports = "\n".join(build_imports(ctx))
    stubs   = build_stubs(ctx)
    jsx     = "\n".join(build_jsx(ctx))

    # Body: blank line before each stub block if any exist
    body = ""
    if stubs:
        body = "\n" + "\n\n".join(stubs) + "\n"

    return (
        f"{imports}\n"
        f"\n"
        f"export interface {p}Props {{\n"
        f"  /** Content rendered inside the component. */\n"
        f"  children?: ReactNode\n"
        f"  /** Additional Tailwind class names merged onto the root element. */\n"
        f"  className?: string\n"
        f"}}\n"
        f"\n"
        f"const {p}: FC<{p}Props> = ({{ children, className }}) => {{{body}\n"
        f"  return (\n"
        f"{jsx}\n"
        f"  )\n"
        f"}}\n"
        f"\n"
        f"export default {p}\n"
    )


def build_index(ctx: Context) -> str:
    """
    Builds the index file for a component.
    """
    p = ctx.pascal
    return (
        f'export {{ default }} from "./{p}"\n'
        f'export type {{ {p}Props }} from "./{p}"\n'
    )


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    """
    Main entrypoint for web component generation.
    """
    sys.exit(GeneratorFSM().run(sys.argv[1:]))


if __name__ == "__main__":
    main()

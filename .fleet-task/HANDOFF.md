# S05-docs handoff

## Built
- Added `docs/proposals.md` covering proposal statuses/transitions and refusals, REST and MCP surfaces, human commands/state inspection, feed reactions, Discord reaction configuration/reference persistence/ignored cases, relevant environment variables, and schema upgrades v10–v13.
- Added the README proposals/evolve feature link and updated migration/tool summaries.
- Updated the project index with proposal/reaction modules, feature suites, environment variables, and schema versions.

## Files changed
- `docs/proposals.md` — operator guide for proposals, reactions and evolve/Discord controls.
- `README.md` — short feature section/link plus schema and MCP overview updates.
- `CLAUDE.md` — modules, env vars, tests/schema versions and guide index.

## Verify
- `bash .fleet-task/CHECK.sh` — passed.
- `git diff --check` — passed.
- No product code or tests changed; no focused runtime tests were applicable to this docs-only change. Per worker contract, did not run the full fleet verify command.

## Unfinished
- Nothing known.

## Dependencies
- None added.

## Learned
- Proposal IDs (`P<n>`) and associated feed `post_id` values are separate identifiers; reaction endpoints require the feed post ID.
- Discord reaction actions require both relay-side user authorization and, for evolve commands, the hub-side handle allowlist.

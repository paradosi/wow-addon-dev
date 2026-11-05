# wow-api-mcp — notes for Claude

## What this is

An MCP server (stdio, registered user-scope as `wow-api`) plus two CLIs, answering
"does this WoW API exist, and with what signature, in each client an addon targets?"
for **Retail (`live`), TBC Anniversary (`classic_anniversary`), Classic Era
(`classic_era`) and WoW Forever (`forever`)**. It is the reference layer for the addons
in `~/projects/wowaddons`.

`wow-api-build` clones upstream into `~/data/wow-api/upstream/` and builds
`~/data/wow-api/index.sqlite`; `wow-api-scan` checks an addon directory against it.

## Stack

`python` + `mcp` 2.x (`MCPServer`, not v1's `FastMCP`). `lua5.1` and `luac5.1` are
runtime dependencies: the first evaluates Blizzard's data tables, the second compiles
addon and Blizzard Lua to bytecode listings for the scanner. `just refresh` / `just
build` / `just scan <dir>` / `just test`.

## Conventions

- Flavor keys **are** the upstream branch names (wow-ui-source and
  BlizzardInterfaceResources share them). Adding a flavor is one `Flavor(...)` entry in
  `config.py` + `just refresh`.
- Presence of a row in `api` (kind, name, flavor) *is* availability. Blizzard-Lua
  definitions live in `def`, with `deprecated` / `lod` flags.

## Gotchas

- **Blizzard's UI tree for one client contains files for other clients.** Which files
  load is decided by TOC suffix (`_Classic.toc` covers every Classic client) and
  `[AllowLoadGameType ...]` per line. `loaded_files()` follows that; scanning the tree
  blindly credits retail with Classic's deprecation shims.
- Blizzard creates globals inside function bodies (`DEFAULT_CHAT_FRAME = ChatFrame1`)
  and via `_G.X = ...`. Only bytecode (`SETGLOBAL`) sees all of them — don't go back to
  regex for plain globals.
- Forever's game type `camelot` is inferred from its TOCs, not documented.
- GlobalStrings is parsed line by line in Python: upstream has shipped a malformed line.
- Ketho annotations are retail-oriented. Classic signatures can differ; confirm in source.
- The scanner's "guarded" means *inside a feature test of a global*. A local flag
  (`if isClassic then`) is not recognised, so such references can show as MISSING.

## Secrets

None. Everything read is public upstream data.

## Host

Runs on `deathstar` (see `~/CLAUDE.md`). 4 vCPU / 16 GB RAM / ~83 GB free disk.
Concurrency is what hurts before disk does — a parallel build plus a couple of
containers — but the budget is comfortable, and the host it sits on (`avatar`,
20 threads / 62 GB) has room to grow the VM if something genuinely needs it.
Not a reason to refuse work.

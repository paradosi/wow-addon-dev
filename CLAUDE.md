# wow-addon-dev — notes for Claude

## What this is

The WoW addon development environment for `~/projects/wowaddons`: an API index of
four clients (Retail `live`, TBC Anniversary `classic_anniversary`, Classic Era
`classic_era`, WoW Forever `forever`), a cross-flavor scanner, the `./check` gate,
release tooling (bump / interface / package) and an MCP server (user scope, name
`wow`, 13 read-only tools). One CLI, `wowdev`, on PATH via `~/bin/wowdev`.

Data lives in `~/data/wowdev/`: `upstream/` (shallow clones), `index.sqlite`,
`tools/` (LuaLS, BigWigs packager, pinned), `releases/` (local package builds).

## Stack

Python + `mcp` 2.x (`MCPServer`, not v1's `FastMCP`). Runtime deps outside Python:
`lua5.1`, `luac5.1`, `luacheck`, `/usr/bin/rg`, `zip`. `just test`, `just lint`,
`just refresh`, `just check [ADDON]`.

## Conventions

- Flavor keys **are** the upstream branch names (wow-ui-source, BlizzardInterfaceResources
  and FramexmlAnnotations share them). A new client is one `Flavor(...)` in `config.py`.
- Presence of an `api` row (kind, name, flavor) *is* availability. Blizzard-Lua
  definitions live in `def` with `deprecated` / `lod` flags.
- Anything that writes to an addon repo (`bump`, `interface`) prints a diff and needs
  `-w`. Those stay CLI-only; the MCP tools are all read-only.
- **The live loop is `game.py`** (`wowdev deploy|restore|errors`, MCP `wow_game_errors`).
  Everything is one `ssh northpc powershell -EncodedCommand` per step, files come back
  base64. Deploy only replaces an existing install by default, and backs up the user's
  own copy once (outside `AddOns`). BugGrabber only writes at logout or `/reload`.
- **The index is shared across threads** (the MCP server runs tools on anyio workers):
  `Index` keeps one SQLite connection per thread. Don't go back to a single connection.
- **No Claude attribution** in commits here or in the addon repos (jim, 2026-11-05): no
  `Co-Authored-By: Claude`, no `Claude-Session:`. `.claude/settings.json` blanks it.
- The weekly refresh is `systemd/wowdev-refresh.{service,timer}` (Tue 06:00), installed with
  `just install-timer`. `wow_status` still reports staleness if it ever stops.

## Gotchas

- **Blizzard's UI tree for one client contains other clients' files.** Which load is
  decided by TOC suffix (`_Classic.toc` = every Classic client) and `AllowLoadGameType`.
  `loaded_files()` follows that; walking the tree credits retail with Classic shims.
- Blizzard creates globals inside function bodies and via `_G.X = …`. Only bytecode
  (`SETGLOBAL`) sees them all — don't go back to regex for plain globals.
- Guard detection (`scan._guarded_pcs`) sees globals, flavor-flag fields
  (`Postal.WOWRetail`) and version probes (`GetBuildInfo`). Plain locals are invisible.
- luac5.1 rejects a UTF-8 BOM the client accepts; both scan and check strip it.
- Forever's game type `camelot` is inferred from its TOCs, not documented.
- GlobalStrings is parsed line by line: upstream has shipped a malformed line.
- Ketho annotations are retail-oriented. That is why LuaLS `undefined-global` is off
  and the types pass is advisory.
- `uv run --project`, never `--directory`, in wrappers: the latter changes cwd.
- **The packager without `manual-changelog:` ships a git-log CHANGELOG** — every addon
  here did on 2026-11-05, Claude-Session trailers included. `package.inspect` flags it.

## Not built (deliberately, 2026-11-05)


## Secrets

None. Everything read is public upstream data.

## Host

Runs on `deathstar` (see `~/CLAUDE.md`). 4 vCPU / 16 GB RAM / ~83 GB free disk.
Concurrency is what hurts before disk does — a parallel build plus a couple of
containers — but the budget is comfortable, and the host it sits on (`avatar`,
20 threads / 62 GB) has room to grow the VM if something genuinely needs it.
Not a reason to refuse work.

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
- **`wowdev taint` / `wow_game_taint`** read `<client>\Logs\taint.log`. It exists only
  after `/console taintLog 1` in game plus a /reload. The parser was written to Blizzard's
  documented shape (timestamped headline, indented stack) and tested on a sample:
  **no real taint.log had been read when it was written (2026-11-05)**, so check the
  first real one against `parse_taint` and fix the parser if they differ.
- **`wowdev status --game` / `wow_game_clients`** read the launcher's `.build.info` on the
  game PC (product codes = `Flavor.wago_product`). Check it before an in-game test: on
  2026-11-05 every client on northpc was still on its June/July build.
- **Spells are `spells.py`** (`wowdev spell`, MCP `wow_spell`): wago.tools DB2 CSVs of the
  index's own builds. Products: `wow`, `wow_anniversary`, `wow_classic_era`, `wowf`
  (`Flavor.wago_product`). A wago failure warns and keeps the cache; never make it fatal.
- **`wowdev unused` is advisory, never in the gate.** Every finding needs a human: `_G[...]`
  lookups, XML `inherits` and other addons are invisible to it, and FloLib functions one Flo
  addon doesn't call may be the other's (FloLib is a shared global library, highest
  `FLOLIB_VERSION` wins). Locale "missing" is the exception: with an enUS base, AceLocale
  raises "Missing entry" through the error handler, so treat it as a bug.
- **`analyse()` tracks registers holding `_G`**; any opcode in `_WRITES_A` clears one. Without
  that, `bar.x = …` after a reused register read as a global definition (2026-11-05).
- **WoW Forever is a separate install on northpc**: `C:\Program Files (x86)\World of Warcraft\_forever_`
  (`FOREVER_ROOT`, `CLIENT_ROOTS` in `game.py`), with its own `.build.info`. That tree also has
  fresh, unused `_anniversary_`/`_classic_era_` clients; jim's real ones (settings, ~145 addons) are
  under `WOW_ROOT`, so `installed_builds()` reads only `wowf` from the Forever tree (2026-11-06).
- **Hook names are references**: `_hooked_name()` reads the string literal loaded into the
  first argument register of `hooksecurefunc` / `:Hook` / `:SecureHook` / `:RawHook`. Names removed
  from *every* client land in "unknown in every client", not in errors: the index has no
  history, so check that list after a Classic patch.
- **Retail's tooltip `Set*` methods are built in Lua** (`TooltipDataHandler.lua`'s accessor
  table, `AddTooltipDataAccessor`), so the widget dump misses them; `_tooltip_accessors()`
  adds them as `GameTooltip` methods. Without it `tooltip:SetBagItem()` failed Postal's gate.
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
- **TOC line tags: `[AllowLoad… x]`, `[ExcludeLoad… x]` and a bare `[Bootstrap]`.** A tag the
  parser does not strip leaves the filename unmatched and the file silently vanishes. Until
  2026-11-05 Exclude and Bootstrap were not stripped: ~190 Retail functions (every
  `*_LoadUI`, `AuraUtil.*`) and TBC's `MiniMapTracking` were "absent". `[Bootstrap]` files
  load at startup even in a LoadOnDemand addon. **Measure after any loader change**: each
  client's FrameXML dump (`_res(f, "FrameXML")`) vs `collect_defs` — 99.1% Retail, 99.8%
  TBC/Era, 99.0% Forever that day.
- Blizzard creates globals inside function bodies and via `_G.X = …`. Only bytecode
  (`SETGLOBAL`) sees them all — don't go back to regex for plain globals.
- Guard detection (`scan._guarded_pcs`) sees globals, flavor-flag fields
  (`Postal.WOWRetail`) and version probes (`GetBuildInfo`). Plain locals are invisible.
- luac5.1 rejects a UTF-8 BOM the client accepts; both scan and check strip it.
- **Forever is a Mainline-family client whose game is Camelot**: `[Family]`=Mainline,
  `[Game]`=Camelot, game types `camelot`+`mainline`+`standard`. Inferred from its TOCs and
  checked against the client's FrameXML dump (95% found, the same as Retail). The earlier
  guess (both folders for both tokens, no `mainline` type) found 78% and wrongly reported
  about 830 Blizzard functions as absent on Forever. Re-measure before changing it.
- GlobalStrings is parsed line by line: upstream has shipped a malformed line.
- Ketho annotations are retail-oriented. That is why LuaLS `undefined-global` is off
  and the types pass is advisory.
- `uv run --project`, never `--directory`, in wrappers: the latter changes cwd.
- **`HasRestrictions` means different things per client.** On Retail/Forever it travels
  with the secret-value system (CLEU no longer reaches addons). On TBC/Era it marks 182
  APIs that addons use freely. `Index.restricted()` returns `{}` for a client with no
  `Secret*` markers, so the scan never warns on Classic. Keep it that way.
- **A spell name matches helper spells too** (a totem's attack, a trap's trigger, a
  physical-school rank-1 duplicate). The castable rank is the one with a duration and
  the right school. SpellMisc has per-difficulty rows; only `DifficultyID 0` is used.
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

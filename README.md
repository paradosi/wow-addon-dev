# wow-addon-dev

A development environment for World of Warcraft addons that target several game
clients at once. It indexes every client's API, checks addon code against each
client the addon's TOCs claim to support, and runs the release steps locally
before anything is tagged. All of it is exposed both as a CLI (`wowdev`) and as an
MCP server, so Claude can check any API it is about to use.

Built for the addons in `~/projects/wowaddons` (FarmMode, iChat, FloAspectBar,
FloTotemBar, Postal, xCT+), which ship one codebase to clients that are years apart.

## Table of contents

- [Overview](#overview)
- [Requirements](#requirements)
- [Installation](#installation)
- [Usage](#usage)
- [MCP server](#mcp-server)
- [Configuration](#configuration)
- [Development](#development)
- [Project structure](#project-structure)
- [How it works](#how-it-works)
- [Design decisions](#design-decisions)
- [Troubleshooting](#troubleshooting)
- [License and data sources](#license-and-data-sources)

## Overview

The largest source of bugs in multi-client addons is calling something that does
not exist in one of the clients. Retail moved most flat globals into `C_*`
namespaces, the Classic clients keep some of them only as deprecation shims, and
Blizzard's own UI code differs per client. No tool tells you "this line breaks on
Classic Era" before a player hits it, and there may be no game client to try it in.
That tooling is what this project provides.

### Features

- **An API index of four clients**: Retail (`live`), TBC Anniversary
  (`classic_anniversary`), Classic Era (`classic_era`) and WoW Forever (`forever`).
  It holds every C function, event and payload, enum with values, struct,
  widget method and script handler, CVar, FrameXML global, mixin, template and
  enUS GlobalString, with typed signatures wherever Blizzard documents them.
- **A five-way availability status per client**: `native`, `blizzard_ui`,
  `load_on_demand`, `deprecated_shim` and `absent`. "Exists only because
  `Blizzard_Deprecated` still ships a fallback" is a separate state from "exists".
- **Secret values and restricted APIs (Retail 12.x, Forever).** Blizzard's
  `SecretArguments`, `SecretReturns`, `SecretWhen…Restricted`,
  `SecretInChatMessagingLockdown`, `RequiresUnitAuraAccess`, `HasRestrictions`
  and per-field `NeverSecret` are kept on every function, event and parameter.
  `wow_lookup` shows them, and the scan warns where an addon registers a
  restricted event (string constants count) or calls a restricted function.
- **A cross-flavor addon scan.** It reads each TOC's `## Interface:`, follows the
  TOC to the files that client loads, and checks every global the bytecode reads
  against that client. Feature tests such as `if C_X then`,
  `C_X and C_X.F or F` and `if Postal.WOWRetail then` are recognised as
  deliberate, so the house compat idiom is not flagged.
- **A single gate (`wowdev check`, or `./check` in `~/projects/wowaddons`)**:
  luac syntax, luacheck, TOC lint and the API scan, with optional
  lua-language-server typing and a packager dry run.
- **Release tooling.** `bump` updates every TOC, the hardcoded `local VERSION`
  and the changelog heading. `interface` raises TOC interface numbers to the
  current clients. `package` builds the real release zip locally with
  BigWigsMods/packager and inspects it.
- **Spell records per client**: id, name, rank, duration, school and
  description from the DB2 tables of the same build (via wago.tools), so an
  addon's spell tables can be checked: does this id exist on TBC, which rank is
  it, how long does the totem last.
- **A dead-weight and duplication report (`wowdev unused`)**: files shipped
  but never loaded, media nothing names, globals nothing reads, unused locals
  and unreachable code, libraries nothing uses, stale locale keys (and keys
  looked up but missing from enUS, which AceLocale reports as an error), and
  blocks of identical code with every place they occur. Advisory, never part
  of the gate: each line is a candidate to check before deleting.
- **Blizzard's UI source per client**, searchable and readable, so you can see
  how Blizzard calls an API or how a deprecated function was reimplemented.

### Status

Working, and in daily use for the six addons above. Verified 2026-11-05 against
live builds 12.1.5 (retail), 2.5.6 (TBC Anniversary), 1.15.9 (Classic Era) and
1.60.1 (Forever).

> [!NOTE]
> **The live-game loop runs against `northpc`** (since 2026-11-05): `wowdev deploy`
> installs a build into the clients that already have the addon, and `wowdev errors`
> reads back what BugGrabber caught. The static gate still runs first; the game is
> where combat lockdown, taint and timing get confirmed.

## Requirements

| Tool | Why | On `deathstar` |
|---|---|---|
| Python ≥ 3.12 + `uv` | runs everything | yes |
| `lua5.1`, `luac5.1` | evaluate Blizzard's data tables; compile Lua to bytecode for the scan | apt `lua5.1` |
| `luacheck` | lint pass | apt `lua-check` |
| `git`, `zip`, `curl`, `bash` | upstream fetch; BigWigs packager | yes |
| `rg` (ripgrep) | `wow_source_search` | `/usr/bin/rg` |
| lua-language-server 3.19.2 | optional type pass | installed by `wowdev setup` |
| BigWigsMods/packager v2.7.0 | `package` | installed by `wowdev setup` |

About 400 MB of disk under `~/data/wowdev`: the upstream clones (~250 MB), the
index (~67 MB) and the tools.

## Installation

```sh
cd ~/projects/wow-addon-dev
just setup        # uv sync, install LuaLS + packager, clone upstream, build index (~1 min)
```

`~/bin/wowdev` is the on-PATH wrapper (`uv run --project ~/projects/wow-addon-dev
wowdev "$@"`), and `~/projects/wowaddons/check` calls `wowdev check`.

Register the MCP server with Claude Code. It is already done on `deathstar`, at user
scope, under the name `wow`:

```sh
claude mcp add -s user wow -- /home/jim/bin/wowdev mcp
```

## Usage

```text
wowdev setup                     install tools + fetch upstream + build index
wowdev refresh [--offline]       update upstream and rebuild the index
wowdev status [--game]           index age, each client's version/interface
                                 (--game: vs the game PC's installed builds)
wowdev api NAME [NAME...]        availability matrix (and full detail for one name)
wowdev check [ADDON...]          the full static gate (default: every addon)
wowdev scan ADDON...             cross-flavor API check only
wowdev lint ADDON...             TOC / packaging lint only
wowdev types ADDON [--flavor F]  lua-language-server type check (advisory)
wowdev package ADDON             local packager dry run, inspect the zip
wowdev bump ADDON X.Y.Z [-w]     version everywhere + changelog heading
wowdev interface ADDON... [-w]   raise TOC Interface to current clients
wowdev deploy ADDON [--flavor F] build and install into the game PC's clients
wowdev restore ADDON [--flavor F] put back the install the first deploy replaced
wowdev errors [ADDON] [--flavor F] [--sessions N|--all] [-v] [--json]
                                 Lua errors BugGrabber saved on the game PC
wowdev taint [ADDON] [--flavor F] [-v] the client's taint.log, grouped, for one addon
wowdev spell ID|NAME [--flavor F] spell id, rank, duration and school per client
wowdev unused [ADDON...] [-v] [--json]  dead files, media, globals, locals, libs,
                                 locale keys and duplicated code (advisory)
wowdev mcp                       run the MCP server on stdio
```

`ADDON` is a path or a directory name under `~/projects/wowaddons`. Flavor names
also accept aliases: `retail`, `tbc`, `vanilla`, `era`, `camelot`.

### Examples

**Is it safe to call this on every client I ship to?**

```console
$ wowdev api UnitAura C_UnitAuras.GetAuraDataByIndex GetAddOnMetadata
                                live              classic_annivers  classic_era       forever
UnitAura                        absent            deprecated_shim   deprecated_shim   absent
C_UnitAuras.GetAuraDataByIndex  native            native            native            native
GetAddOnMetadata                absent            absent            absent            absent    see ['C_AddOns.GetAddOnMetadata']
```

**The pre-commit gate for one addon:**

```console
$ cd ~/projects/wowaddons && ./check FarmMode
=== FarmMode ===
  syntax   ok    0 errors, 0 warnings  (1 files parsed)
  lint     warn  0 errors, 4 warnings
  toc      warn  0 errors, 4 warnings
      ! CHANGELOG.md: newest entry is 1.3.1, TOCs say 1.4.2
      ! FarmMode.toc: Interface 20505 < current client 20506 (shown as out of date)
  api      ok    0 errors, 0 warnings

gate: pass
```

**A cross-flavor finding with its location:**

```console
$ wowdev scan FloTotemBar-Classic
## Classic Era (classic_era) — client interface 11509
~ DEPRECATED UnitBuff at FloLib.lua:783 — only a Blizzard_Deprecated shim
  ok guarded, absent here (expected compat idiom): BOOKTYPE_SPELL, LibStub, …
```

**Try a build in game, then read what broke:**

```console
$ wowdev deploy FloTotemBar-Classic
# deploy FloTotemBar-Classic (installs as FloTotemBar) to northpc
zip: FloTotemBar-v1.2.3-2-g1d0c802.zip
  classic_anniversary  installed 1.2.4
  classic_era          skipped: not installed in this client (pass --flavor to add it)
$ wowdev errors iChat --all          # after a /reload or logout in game
# iChat errors on classic_anniversary -- 1 of 13 in file, session 250 (last all), saved 2026-05-10T21:42:47
x2  session 243  2026/02/23 13:40:01
  Interface/AddOns/iChat/portraits.lua:123: attempt to call global 'FindUnitByName' (a nil value)
```

Deploy only replaces an existing install unless `--flavor` says otherwise. The
first time it replaces your own copy, that copy is moved to
`%USERPROFILE%\wowdev-backups\<flavor>-<time>\` (outside `AddOns`, so the
client never loads it) and `wowdev restore` puts it back. Later deploys carry a
`.wowdev-deploy` marker and are simply replaced.

**A release, previewed and then written:**

```sh
wowdev bump iChat 1.4.11          # diff of every TOC, local VERSION, CHANGELOG heading
wowdev bump iChat 1.4.11 -w       # write it; then fill in the changelog bullets
wowdev interface iChat -w         # 20505 -> 20506 etc., only for indexed clients
./check iChat --package           # gate + build the real zip locally and inspect it
git -C iChat commit -am "v1.4.11: …" && git -C iChat tag v1.4.11 && git -C iChat push --tags
```

## MCP server

Eighteen tools, all read-only (annotated `readOnlyHint`):

| Tool | Answers |
|---|---|
| `wow_status` | index age; each client's version, build and current TOC interface number |
| `wow_lookup` | everything about one name: per-client status, signature / payload / enum values / struct fields, wiki link, where Blizzard defines it. A bare `GetSpellInfo` also surfaces `C_Spell.GetSpellInfo` |
| `wow_compat` | availability matrix for a list of names. Matching is exact, so flat `GetAddOnMetadata` is not answered with `C_AddOns.GetAddOnMetadata` |
| `wow_search` | full-text search over names, systems, descriptions and argument names, with camelCase split |
| `wow_widget` | a widget's methods and handlers per ancestor, in one client |
| `wow_system` | every function, event, enum and struct in a namespace or system |
| `wow_diff` | names in one client but not another (porting) |
| `wow_source_search` / `wow_source_read` | ripgrep over, and reading of, Blizzard's UI source for one client |
| `wow_strings` | enUS GlobalStrings by key or text |
| `wow_spell` | a spell by id or name, per client: rank, duration, school, description. A name returns every rank's id |
| `wow_check_addon` | the cross-flavor scan of an addon directory |
| `wow_check_gate` | the whole `./check` gate for one addon |
| `wow_unused` | the `wowdev unused` report for one addon: dead files, media, globals, locals, libs, locale keys, duplicated blocks |
| `wow_addons` | the addons under `~/projects/wowaddons`: package-as name, version, targeted clients |
| `wow_game_taint` | the client's `Logs/taint.log` on the game PC, grouped into headline + stack, filtered to one addon, repeats counted, blocked actions totalled (`openWorldHint`) |
| `wow_game_clients` | the game PC's installed builds (launcher `.build.info`) next to the index's: says whether the client needs patching or the index is a build behind (`openWorldHint`) |
| `wow_game_errors` | Lua errors BugGrabber saved in a client on the game PC, filtered to one addon and the last N sessions (reaches `northpc` over ssh, so `openWorldHint`) |

Release, packaging and `deploy` stay CLI-only, so any change to addon files is a
deliberate command rather than a tool call.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `WOWDEV_DATA` | `~/data/wowdev` | upstream clones, `index.sqlite`, `tools/`, `releases/` |
| `WOWDEV_LUA` | `lua5.1` | interpreter used to evaluate Blizzard's data tables |
| `WOWDEV_GAME_HOST` | `northpc` | ssh host of the Windows PC that runs WoW (key auth, `BatchMode`) |
| `WOWDEV_WOW_ROOT` | `C:\Program Files (x86)\Battle.net Games\World of Warcraft` | the WoW install on it; client folders are mapped in `game.CLIENT_DIRS` |

Nothing is secret: every input is a public repository, and the game PC is reached with the existing ssh key. Clients are configured in
`src/wowdev/config.py` (`FLAVORS`). Each entry's key **is** the upstream branch
name, so adding a client (say `classic` for Mists) is one entry plus `wowdev refresh`.

## Development

| Recipe | Does |
|---|---|
| `just setup` | deps, tools, upstream, index |
| `just test` | pytest. Index-backed tests skip if no index is built |
| `just lint` / `just fmt` | ruff check + format |
| `just refresh` / `just build` | update upstream and rebuild / rebuild offline |
| `just install-timer` | install the weekly refresh as a system timer (Tue 06:00; needs sudo) |
| `just check [ADDON] [--types] [--package]` | the gate |
| `just wowdev …` | any subcommand |
| `just run` | the MCP server on stdio |

## Project structure

```text
src/wowdev/
  config.py    flavors (branch names, aliases, TOC suffixes, game types), paths
  dump.lua     evaluates Blizzard/Ketho Lua data tables in a sandbox -> JSON
  build.py     fetch upstream; build index.sqlite (api, def, annotation, gstring, spell, fts)
  index.py     read side: status classification, bulk sets for the scanner
  scan.py      TOC -> loaded files -> luac bytecode -> per-client findings
  toc.py       TOC lint, version bump, interface update (dry-run diffs)
  luals.py     headless lua-language-server with per-flavor FrameXML types
  package.py   BigWigsMods/packager dry run + zip inspection
  check.py     the gate: syntax, luacheck, toc, api (+ types, package)
  unused.py    advisory dead-weight / duplication report (not part of the gate)
  spells.py    wago.tools DB2 exports (SpellName, Spell, SpellMisc, SpellDuration) per build
  game.py      live loop over ssh + PowerShell: deploy / restore / BugGrabber errors
  server.py    MCP server (mcp 2.x MCPServer), 18 read-only tools
  cli.py       `wowdev` subcommands; `setup` installs pinned tools
tests/test_core.py
```

## How it works

**Sources, per client.** Each source has a branch per client:

| Source | Gives |
|---|---|
| [Gethe/wow-ui-source](https://github.com/Gethe/wow-ui-source) | Blizzard's UI as shipped, including `Blizzard_APIDocumentationGenerated` (typed functions, events, tables) |
| [Ketho/BlizzardInterfaceResources](https://github.com/Ketho/BlizzardInterfaceResources) | dumps from the running client: every global function, event, enum value, widget method, CVar, frame, template, GlobalString |
| [NumyAddon/FramexmlAnnotations](https://github.com/NumyAddon/FramexmlAnnotations) | LuaLS annotations of FrameXML (types pass) |
| [Ketho/vscode-wow-api](https://github.com/Ketho/vscode-wow-api) | wiki-derived signatures and doc links (single branch, retail-oriented) |
| [wago.tools](https://wago.tools) | CSV exports of each build's DB2 tables; four spell tables per client (`spells.py`) |

Spell tables are fetched for the exact `version.build` the UI source describes, or
the newest build wago.tools has for that product if the exact one is not up yet.
They are cached under `upstream/spells-<flavor>/<build>/` and re-downloaded only
when the build changes. A wago.tools outage only warns: the refresh keeps the
cached tables. Retail is most of the size: 416k spells, about 67 MB of the
136 MB index.

**Building the index.** Every data file is a Lua table, so `dump.lua` evaluates
it with the real `lua5.1` in a sandbox (unknown globals resolve to a placeholder)
and prints JSON. Nothing is parsed with regex.

The harder question is which globals Blizzard's own Lua defines *in this client*.
One client's UI tree contains other clients' files: the retail tree includes
`Blizzard_Deprecated/Classic/`. So `loaded_files()` follows each Blizzard addon's
TOC the way the client does. It picks the suffix TOC first (`_Classic.toc` covers
every Classic client), honours `## AllowLoadGameType:` and per-line
`[AllowLoadGameType …]`, and follows XML `<Script>`/`<Include>`. Each loaded Lua
file is compiled with `luac5.1 -l`, and its `SETGLOBAL` and `_G.X =` instructions
are the definitions, at any nesting depth. That is how `DEFAULT_CHAT_FRAME`
(assigned inside a function) and `UnitAura` (a shim inside a `do` block,
Classic-only) get attributed correctly.

**Scanning an addon.** Each TOC's `## Interface:` numbers map to clients. A
family bucket of `interface // 100` (or `// 10000` for 12.x) means 11508 and
11509 both count as Classic Era. The TOC's files are compiled to bytecode, and
every `GETGLOBAL` (plus one `GETTABLE` level for `C_X.F` and `Enum.X`) becomes a
reference with a line number.
A function named by a string literal is a reference too, when it is the
first argument of `hooksecurefunc("Name", …)` or Ace's `:Hook("Name", …)`, `:SecureHook("Name", …)` /
`:RawHook("Name", …)`. Hooking a function the client no longer has errors at load,
which is how xCT+ broke on TBC 2.5.6. The object form, `hooksecurefunc(frame, "Method", …)`,
is left alone.

A reference is *guarded* when it sits inside the jump region of a feature test,
which is a `TEST`/`EQ`/`LT`/`LE` followed by `JMP` whose operand, on the same
line, came from one of three things:

- **A global's truthiness**: `if C_X then`, and `C_X and C_X.F or F`.
- **A field named like a flavor flag**: `Postal.WOWRetail`, `ns.isClassic`.
- **A comparison involving a version probe**: `GetBuildInfo`, `WOW_PROJECT_ID`.

Flavor flags and version probes guard both arms for every name. A global's
truthiness is narrower (since 2026-11-06): the arm where the global *exists*
(`if C_Spell then … end`) covers only that global and its members, so
`if C_Spell then GetSpellInfo(1) end` still reports `GetSpellInfo`; the arm
where it is *missing* (`if not C_X then`, the `or F` in `C_X and C_X.F or F`)
is a fallback and covers everything. Closures created inside a guard inherit it.
Three things look like tests and are not: a comparison with a global
(`self == FloBarEARTH`), a global the file assigns itself
(`if not FLOLIB_VERSION then`), and a global whose register was reused before
the test (`not MainMenuBar:IsShown()` tests `IsShown`'s result).
Guarded references to absent names are reported as the expected compat idiom
rather than as errors.

> [!WARNING]
> **Before 2026-11-06 the scanner over-trusted guards.** Any global test on a line
> guarded everything in both arms, so FloTotemBar 1.2.4 and FloAspectBar 1.2.3
> shipped to WoW Forever calling `GetSpellInfo` and indexing `MainMenuBar`, both
> absent there, and `./check` passed them.

**The gate.** Errors fail it: syntax, luacheck errors, W113 "accessing undefined
variable" (every remaining one has been a real bug), TOC errors (missing listed
files, mismatched versions, texture paths that don't match `package-as`), and
unguarded references to globals absent in a targeted client. Warnings are
reported but don't fail it: deprecated shims, outdated interface numbers and
changelog drift.

**Restrictions.** `api.detail.restrictions` holds every marker as Blizzard
wrote it, with `Enum.SecretAspect` bits mapped back to names. The `restriction`
table holds the notable ones per client; `SecretArguments` and
`SecretArgumentsAddAspect` are left out, because about 3,600 Retail functions
carry them and they only say "don't pass secret values in". The scan reports
restrictions only on clients that have secret markers at all. TBC and Era mark
182 functions `HasRestrictions`, `COMBAT_LOG_EVENT_UNFILTERED` among them, and
addons use those freely there, so on Classic that marker is shown but never
warned on.

## Design decisions

- **Bytecode, not regex, for globals and guards.** Regex missed globals assigned
  inside function bodies and could not tell `if C_X then … else F() end` from an
  unguarded call. The first regex version reported dozens of false positives on
  these addons; the bytecode version reports real ones.
- **LuaLS `undefined-global` is off.** Ketho's annotations describe retail, so
  LuaLS would call every Classic-only global undefined. Availability belongs to
  the flavor-aware scanner; LuaLS is kept for types, and is advisory for the same
  reason. `cast-local-type`, `inject-field` and luacheck-duplicated diagnostics
  are off too.
- **One repo, not an API server plus a separate toolkit** (decided 2026-11-05).
  The gate, the release tools and the MCP server all need the same index and the
  same TOC model.
- **mcp 2.x (`MCPServer`), not pinned to v1.** The resolver picked 2.3 and the
  port was a rename. `omada-mcp` is still on v1.
- **No WoW API emulator.** Executing addon code headlessly (as wowless does)
  would test more, but needs a far larger stub of the client. It is not built.
- **Spell data from wago.tools, not a scraped site or a client extraction.** It
  serves every client's DB2 tables by exact build, so a spell row and the API
  index describe the same patch. Wowhead was rejected as a scraping target, and
  extracting CASC locally needs a client on this box.
- **Rejected: deriving availability from Ketho's annotations alone.** They are a
  single retail-oriented branch. The per-client dumps are authoritative for presence.

## Troubleshooting

> [!IMPORTANT]
> **A TOC interface number "older than the current client" means the AddOns list
> shows it as out of date**, but only once the player's client is patched. On
> 2026-11-05 the installed clients on `northpc` were older than live (2.5.5,
> 1.15.8, 12.0.7), which is why the TOCs drifted unnoticed. Raise them with
> `wowdev interface <addon> -w` after confirming the live build.

- **`no index at …`**: run `wowdev refresh` (or `just setup` on a fresh host).
- **The index is stale**: `wowdev status` shows its age, and `wow_status` reports
  `stale` after 8 days. `just install-timer` sets up the weekly refresh; `systemctl status wowdev-refresh` shows the last run.
- **A reference is reported MISSING but it is guarded**: the scanner sees guards
  on globals, on flavor-flag fields and on version probes, but not on plain locals
  (`local isClassic = …; if isClassic then`). Function calls
  (`if IsClassicClient() then`) are not guards either. Use a field, or test the global
  directly: `if IsClassicClient() and SomeFunc then`.
- **`unknown in every client`**: other addons' globals (Titan Panel, ElvUI),
  frame names built at runtime (`StaticPopup1EditBox`), or APIs removed from every
  client. The last group is real.
- **`:Method() not on any widget here`**: the receiver's type is unknown to
  bytecode, so this is a hint. Confirm with `wow_widget`.
- **Ketho signatures are retail.** When a Classic signature matters, check it with
  `wowdev` MCP `wow_source_search` against that client's source.
- **The game install on `northpc`** is at
  `C:\Program Files (x86)\Battle.net Games\World of Warcraft` (`_retail_`,
  `_anniversary_`, `_classic_era_`, `_classic_`). The Battle.net uninstall
  registry entries still say `G:\…`, which no longer exists. Reach it with
  `ssh northpc` and `powershell -EncodedCommand`, because cmd → PowerShell
  quoting does not survive plain strings.

## License and data sources

The code here is unlicensed and private. The indexed data comes from the
repositories above. Ketho/vscode-wow-api and NumyAddon/FramexmlAnnotations are
MIT; the Blizzard UI source is Blizzard's, mirrored by Gethe. The data is
downloaded locally and is not redistributed by this repo.

"""stdio MCP server over the WoW API index."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from .config import FLAVORS, resolve_flavor
from .index import STATUS_DOC, Index, flavor_keys
from .scan import format_report, scan

INSTRUCTIONS = """\
World of Warcraft addon API reference for four clients, built from Blizzard's
own UI source (Gethe/wow-ui-source) and dumps of each live client
(Ketho/BlizzardInterfaceResources): Retail `live`, TBC Anniversary
`classic_anniversary`, Classic Era `classic_era`, WoW Forever `forever`.
Flavor arguments also accept aliases: retail, tbc, vanilla, era, camelot.

Availability is the point: one addon codebase runs on clients years apart.
Before using any API in addon code, check it with wow_compat or wow_lookup
for every flavor the addon's TOCs target. Statuses: native (C API),
blizzard_ui (defined by Blizzard Lua/XML), load_on_demand,
deprecated_shim (Blizzard_Deprecated fallback; will be removed), absent.
wow_check_addon runs the whole-addon version of that check; wow_check_gate
runs every static pass (syntax, luacheck, TOC lint, API scan) and is the
pre-commit gate for the addons in ~/projects/wowaddons (see wow_addons).
Release and packaging steps are CLI-only (`wowdev bump|interface|package`),
so changes to addon files stay deliberate.

Signatures come from Blizzard's APIDocumentation where it exists (typed,
mostly C_* namespaces), else from Ketho's wiki-derived annotations, which
are retail-oriented. When a signature matters on Classic, confirm it in that
flavor's source with wow_source_search.
"""

mcp = MCPServer("wow-api", instructions=INSTRUCTIONS)
# every tool only reads the local index / source tree
READ_ONLY = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
idx = Index()


def _fmt_params(ps: list[dict]) -> str:
    out = []
    for p in ps:
        t = p.get("type") or "?"
        if p.get("inner_type"):
            t = f"{t}<{p['inner_type']}>"
        s = f"{p.get('name')}: {t}{'?' if p.get('nilable') else ''}"
        if "default" in p:
            s += f" = {json.dumps(p['default'])}"
        out.append(s)
    return ", ".join(out)


def _signature(name: str, d: dict) -> str | None:
    if "args" not in d and "returns" not in d:
        return None
    ret = _fmt_params(d.get("returns") or [])
    return f"{name}({_fmt_params(d.get('args') or [])})" + (f" -> {ret}" if ret else "")


def _detail_view(kind: str, name: str, d: dict) -> dict:
    v: dict = {}
    if sig := _signature(name, d):
        v["signature"] = sig
    for k in (
        "doc",
        "flags",
        "restrictions",
        "payload",
        "synchronous",
        "values",
        "fields",
        "inherits",
        "default",
        "category",
        "account",
        "character",
        "secure",
        "value",
        "load_on_demand",
        "type",
        "source",
    ):
        if d.get(k) not in (None, [], {}):
            v[k] = d[k]
    if kind == "event":
        v["payload"] = _fmt_params(d.get("payload") or []) or "(none)"
    secret_info = {
        p["name"]: ("never secret" if p.get("never_secret") else p["secret"])
        for p in (d.get("payload") or []) + (d.get("args") or []) + (d.get("returns") or [])
        if p.get("never_secret") or p.get("secret")
    }
    if secret_info:
        v["secret_fields"] = secret_info
    if "fields" in v:
        v["fields"] = _fmt_params(v["fields"])
    return v


@mcp.tool(annotations=READ_ONLY)
def wow_status() -> dict:
    """Index build time, and each flavor's client version, build and interface
    number (the value a TOC's `## Interface:` should carry to not be marked
    out of date)."""
    m = idx.meta()
    built = m.get("built_at", "?")
    age_h = (time.time() - idx.path.stat().st_mtime) / 3600
    return {
        "built_at": built,
        "age_hours": round(age_h, 1),
        "stale": age_h > 24 * 8,
        "refresh": "just refresh  (in ~/projects/wow-addon-dev)",
        "flavors": {
            k: {
                "label": f["label"],
                "version": f["version"],
                "build": f["build"],
                "interface": f["interface"],
                "ui_source_date": f["ui_date"],
                "aliases": list(FLAVORS[k].aliases),
            }
            for k, f in idx.flavors.items()
        },
        "statuses": STATUS_DOC,
    }


@mcp.tool(annotations=READ_ONLY)
def wow_lookup(name: str, flavor: str | None = None) -> dict:
    """Everything known about one API name: per-flavor availability, typed
    signature / event payload / enum values / struct fields, wiki link, and
    where Blizzard's Lua defines it. Accepts full names (`C_Container.GetBagName`,
    `Slider:SetObeyStepOnDrag`, `BAG_UPDATE`, `Enum.ItemQuality`) or a bare
    function name, which also finds namespaced versions (`GetSpellInfo` →
    also `C_Spell.GetSpellInfo`). Give `flavor` to see only that client's detail."""
    names = idx.canonical(name)
    bare = name.split(".")[-1].split(":")[-1]
    also = [
        r["name"]
        for r in idx.q(
            "SELECT DISTINCT name FROM api WHERE (name LIKE ? OR name LIKE ?) AND name != ?"
            " LIMIT 8",
            f"%.{bare}",
            f"%:{bare}",
            name,
        )
    ]
    if not names:
        hits = wow_search(name, limit=8)["results"]
        return {"found": False, "query": name, "did_you_mean": [h["name"] for h in hits]}
    fl = [resolve_flavor(flavor)] if flavor else list(idx.flavors)
    out = []
    for n in names[:5]:
        entry: dict = {"name": n, "availability": idx.availability(n)}
        by_kind: dict[str, dict] = {}
        for r in idx.rows(n):
            if r["flavor"] not in fl:
                continue
            d = json.loads(r["detail"]) if r["detail"] else {}
            view = _detail_view(r["kind"], n, d)
            k = by_kind.setdefault(r["kind"], {"system": r["system"], "per_flavor": {}})
            k["per_flavor"][r["flavor"]] = view
        # collapse identical per-flavor detail so the common case is short
        for k in by_kind.values():
            views = list(k["per_flavor"].values())
            if views and all(v == views[0] for v in views):
                k["detail"] = views[0]
                k["flavors"] = list(k["per_flavor"])
                del k["per_flavor"]
        entry["kinds"] = by_kind
        if ann := idx.annotation(n):
            entry["annotation"] = {
                k: ann[k]
                for k in ("signature", "params", "returns", "description", "url")
                if ann[k]
            }
        defs = idx.q(
            "SELECT flavor, path, line, deprecated, lod FROM def WHERE name=? ORDER BY flavor", n
        )
        if defs:
            entry["defined_in"] = [
                {
                    "flavor": d["flavor"],
                    "at": f"{d['path']}:{d['line']}",
                    **({"deprecated": True} if d["deprecated"] else {}),
                    **({"load_on_demand": True} if d["lod"] else {}),
                }
                for d in defs
                if d["flavor"] in fl
            ][:12]
        out.append(entry)
    res: dict = {"found": True, "results": out}
    if extra := [a for a in also if a not in names]:
        res["related_names"] = extra
    return res


@mcp.tool(annotations=READ_ONLY)
def wow_compat(names: list[str], flavors: list[str] | None = None) -> dict:
    """Availability matrix: for each name, its status in each flavor
    (native / blizzard_ui / load_on_demand / deprecated_shim / absent).
    The quick check before writing a call into multi-flavor addon code."""
    fl = flavor_keys(flavors)
    matrix = {}
    for n in names:
        # exact names only: a compat answer for C_X.F is not one for flat F
        row: dict = {f: idx.status(n, f) for f in fl}
        if all(v == "absent" for v in row.values()):
            bare = n.split(".")[-1].split(":")[-1]
            alt = [
                r["name"]
                for r in idx.q(
                    "SELECT DISTINCT name FROM api WHERE name LIKE ? OR name LIKE ?"
                    " OR name = ? COLLATE NOCASE LIMIT 6",
                    f"%.{bare}",
                    f"%:{bare}",
                    n,
                )
                if r["name"] != n
            ]
            if alt:
                row["_see_also"] = alt
        matrix[n] = row
    return {"flavors": fl, "matrix": matrix, "legend": STATUS_DOC}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=True))
def wow_game_errors(
    addon: str | None = None, flavor: str = "classic_anniversary", sessions: int | None = 1
) -> dict:
    """Lua errors BugGrabber saved in a client on the game PC (over ssh), newest
    first. `addon` is an installed folder name or a repo under ~/projects/wowaddons;
    only errors whose message or stack mention that addon are kept. `sessions`:
    the most recent N login/reload sessions (None = all). BugGrabber writes its
    file at logout or /reload, so ask for a /reload after testing. Installing a
    build is CLI-only: `wowdev deploy <addon>`."""
    from . import game
    from .config import resolve_flavor

    try:
        return game.errors(addon, resolve_flavor(flavor), sessions)
    except game.GameHostError as e:
        return {"error": str(e)}


@mcp.tool(annotations=READ_ONLY)
def wow_search(
    query: str, kind: str | None = None, flavor: str | None = None, limit: int = 20
) -> dict:
    """Full-text search across names, systems, descriptions and argument names.
    camelCase is split, so `free bag slots` finds
    C_Container.CalculateTotalNumberOfFreeBagSlots. `kind` filters to one of:
    function, event, enum, struct, constants, callback, widget, widget_method,
    script_handler, cvar, cvar_command, framexml, mixin, frame, template, lua,
    global_const. `flavor` keeps only names that exist in that client."""
    terms = [t for t in re.findall(r"[\w]+", query) if t]
    if not terms:
        return {"results": []}
    match = " ".join(f'"{t}"*' for t in terms)
    sql = (
        "SELECT name, kind, system, bm25(fts, 10.0, 1.0, 2.0, 5.0, 1.0) AS score FROM fts"
        " WHERE fts MATCH ?"
    )
    args: list = [match]
    if kind:
        sql += " AND kind = ?"
        args.append(kind)
    sql += " ORDER BY score LIMIT ?"
    args.append(max(limit * 4, 50))
    rows = idx.q(sql, *args)
    fl = resolve_flavor(flavor) if flavor else None
    out = []
    for r in rows:
        flavors = [
            x["flavor"]
            for x in idx.q("SELECT flavor FROM api WHERE kind=? AND name=?", r["kind"], r["name"])
        ]
        if fl and fl not in flavors:
            continue
        out.append(
            {"name": r["name"], "kind": r["kind"], "system": r["system"], "flavors": flavors}
        )
        if len(out) >= limit:
            break
    return {"query": query, "results": out}


@mcp.tool(annotations=READ_ONLY)
def wow_widget(widget: str, flavor: str = "live", inherited: bool = True) -> dict:
    """A widget type's methods and script handlers in one client, grouped by
    the ancestor that provides them (Slider → Frame → Region → ScriptRegion →
    Object → FrameScriptObject). Use to answer "can I call X on a Y here?"."""
    f = resolve_flavor(flavor)
    chain, seen, queue = [], set(), [widget]
    while queue:
        w = queue.pop(0)
        if w in seen:
            continue
        seen.add(w)
        r = idx.q("SELECT detail FROM api WHERE kind='widget' AND name=? AND flavor=?", w, f)
        if not r:
            if w == widget:
                types = [
                    x["name"]
                    for x in idx.q(
                        "SELECT name FROM api WHERE kind='widget' AND flavor=? ORDER BY name", f
                    )
                ]
                return {"found": False, "flavor": f, "widget_types": types}
            continue
        d = json.loads(r[0]["detail"] or "{}")
        chain.append(w)
        if inherited:
            queue.extend(d.get("inherits") or [])
    out = {}
    for w in chain:
        methods = [
            m["name"].split(":", 1)[1]
            for m in idx.q(
                "SELECT name FROM api WHERE kind='widget_method' AND system=? AND flavor=?"
                " ORDER BY name",
                w,
                f,
            )
        ]
        handlers = [
            m["name"].split(":", 1)[1]
            for m in idx.q(
                "SELECT name FROM api WHERE kind='script_handler' AND system=? AND flavor=?"
                " ORDER BY name",
                w,
                f,
            )
        ]
        out[w] = {"methods": methods, **({"handlers": handlers} if handlers else {})}
    return {"widget": widget, "flavor": f, "chain": chain, "by_type": out}


@mcp.tool(annotations=READ_ONLY)
def wow_system(name: str, flavor: str = "live") -> dict:
    """Every function, event, enum and struct in one API system or namespace in
    one client: `C_Container`, `Container`, `UnitAura`, `C_Spell`... Systems
    are Blizzard's APIDocumentation grouping, so this lists a feature area's
    events alongside its functions."""
    f = resolve_flavor(flavor)
    rows = idx.q(
        "SELECT kind, name, detail FROM api WHERE flavor=? AND (system=? COLLATE NOCASE"
        " OR name LIKE ?) ORDER BY kind, name",
        f,
        name,
        f"{name}.%",
    )
    systems = {
        r["system"]
        for r in idx.q(
            "SELECT DISTINCT system FROM api WHERE flavor=? AND name LIKE ?", f, f"{name}.%"
        )
    }
    if systems - {name, None}:
        rows += idx.q(
            f"SELECT kind, name, detail FROM api WHERE flavor=? AND kind != 'function' AND"
            f" system IN ({','.join('?' * len(systems))}) ORDER BY kind, name",
            f,
            *systems,
        )
    out: dict[str, list] = {}
    seen = set()
    for r in rows:
        if (r["kind"], r["name"]) in seen:
            continue
        seen.add((r["kind"], r["name"]))
        d = json.loads(r["detail"]) if r["detail"] else {}
        if r["kind"] == "function":
            item = _signature(r["name"], d) or r["name"] + "(…)"
        elif r["kind"] == "event":
            item = f"{r['name']}: {_fmt_params(d.get('payload') or []) or '(no payload)'}"
        else:
            item = r["name"]
        out.setdefault(r["kind"], []).append(item)
    if not out:
        return {"found": False, "hint": "try wow_search to find the system name"}
    return {"system": name, "flavor": f, **out}


@mcp.tool(annotations=READ_ONLY)
def wow_diff(
    from_flavor: str,
    to_flavor: str,
    kind: str = "function",
    prefix: str | None = None,
    limit: int = 300,
) -> dict:
    """Names of one kind that exist in `from_flavor` but not `to_flavor`, and
    vice versa — what you lose and gain porting between clients. Narrow with
    `prefix` (e.g. `C_Spell.`, `Unit`, `Enum.`)."""
    a, b = resolve_flavor(from_flavor), resolve_flavor(to_flavor)
    like = f"{prefix}%" if prefix else "%"

    def names(fl: str) -> set[str]:
        return {
            r["name"]
            for r in idx.q(
                "SELECT name FROM api WHERE flavor=? AND kind=? AND name LIKE ?", fl, kind, like
            )
        }

    na, nb = names(a), names(b)
    only_a, only_b = sorted(na - nb), sorted(nb - na)
    return {
        "kind": kind,
        f"only_in_{a}": only_a[:limit],
        f"only_in_{b}": only_b[:limit],
        "counts": {a: len(na), b: len(nb), f"only_{a}": len(only_a), f"only_{b}": len(only_b)},
    }


RG = shutil.which("rg", path="/usr/bin:/usr/local/bin:/bin") or "rg"


@mcp.tool(annotations=READ_ONLY)
def wow_source_search(
    pattern: str, flavor: str = "live", glob: str | None = None, limit: int = 60
) -> dict:
    """Ripgrep Blizzard's UI source for one client (FrameXML, every Blizzard_*
    addon, Lua and XML). The way to see how Blizzard itself calls an API, which
    template a frame inherits, or how a deprecated function was reimplemented.
    `pattern` is a Rust regex; `glob` narrows files (`*.xml`, `**/Blizzard_Deprecated*/**`)."""
    f = FLAVORS[resolve_flavor(flavor)]
    root = f.ui_dir / "Interface"
    cmd = [RG, "-n", "--no-heading", "--max-columns", "240", "-M", "240", "-e", pattern]
    if glob:
        cmd += ["-g", glob]
    p = subprocess.run(
        cmd + ["."], cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if p.returncode == 2:
        return {"error": p.stderr.strip()[:500]}
    lines = p.stdout.splitlines()
    return {
        "flavor": f.key,
        "root": "Interface/",
        "total_matches": len(lines),
        "truncated": len(lines) > limit,
        "matches": [ln.removeprefix("./") for ln in lines[:limit]],
    }


@mcp.tool(annotations=READ_ONLY)
def wow_source_read(
    path: str, flavor: str = "live", start: int = 1, end: int | None = None
) -> dict:
    """Read lines from a file in Blizzard's UI source (paths as returned by
    wow_source_search or wow_lookup's defined_in, relative to Interface/)."""
    f = FLAVORS[resolve_flavor(flavor)]
    root = (f.ui_dir / "Interface").resolve()
    p = (root / path.removeprefix("Interface/")).resolve()
    if not p.is_relative_to(root) or not p.is_file():
        return {"error": f"no such file under {f.key}'s Interface/: {path}"}
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    end = min(end or start + 199, len(lines), start + 999)
    body = "\n".join(f"{i:>5}  {lines[i - 1]}" for i in range(max(start, 1), end + 1))
    return {"flavor": f.key, "path": path, "lines": f"{start}-{end} of {len(lines)}", "text": body}


@mcp.tool(annotations=READ_ONLY)
def wow_strings(query: str, flavor: str = "live", limit: int = 25) -> dict:
    """Search Blizzard's enUS GlobalStrings by key or text, e.g. to reuse
    `ERR_NOT_IN_COMBAT` instead of hardcoding English. Keys exist as globals."""
    f = resolve_flavor(flavor)
    rows = idx.q(
        "SELECT key, value FROM gstring WHERE flavor=? AND (key LIKE ? OR value LIKE ?)"
        " ORDER BY length(key) LIMIT ?",
        f,
        f"%{query}%",
        f"%{query}%",
        limit,
    )
    return {"flavor": f, "results": {r["key"]: r["value"] for r in rows}}


_SCHOOLS = (
    (1, "physical"),
    (2, "holy"),
    (4, "fire"),
    (8, "nature"),
    (16, "frost"),
    (32, "shadow"),
    (64, "arcane"),
)


def _dur(ms: int | None) -> str | None:
    if ms is None:
        return None
    if ms < 0:
        return "until cancelled"
    if ms == 0:
        return "instant"
    s = ms / 1000
    return f"{s:g} s" if s < 120 else f"{s / 60:g} min"


def _spell_view(r, describe: bool) -> dict:
    d = {"id": r["id"], "name": r["name"], "rank": r["rank"]}
    if r["duration_ms"] is not None:
        d["duration"] = _dur(r["duration_ms"])
        if r["max_duration_ms"] not in (None, r["duration_ms"]):
            d["max_duration"] = _dur(r["max_duration_ms"])
    if r["school"]:
        d["school"] = "+".join(n for b, n in _SCHOOLS if r["school"] & b) or str(r["school"])
    if describe and r["description"]:
        d["description"] = r["description"][:300]
    return d


@mcp.tool(annotations=READ_ONLY)
def wow_spell(query: str, flavors: list[str] | None = None, limit: int = 40) -> dict:
    """Spell records per client from the DB2 tables of the same build as the
    API index (wago.tools): id, name, rank, duration, school, description.

    `query` is a spell id ("3599") or a name. A name matches exactly first
    (case-insensitive), then as a substring. Use it to check an addon's spell
    tables: does this id exist in TBC, which rank is it, how long does the
    totem/trap/aura last on this client. Durations are the client's own data,
    not the tooltip, so talents and set bonuses are not applied. A name
    also returns non-castable helper spells that share it (a totem's own
    attack, a trap's trigger): the castable rank is usually the one with a
    duration and the expected school."""
    fl = flavor_keys(flavors)
    ph = ",".join("?" * len(fl))
    cols = "flavor, id, name, rank, duration_ms, max_duration_ms, school, description"
    q = query.strip()
    if q.isdigit():
        rows = idx.q(f"SELECT {cols} FROM spell WHERE id=? AND flavor IN ({ph})", int(q), *fl)
        match = "id"
    else:
        rows = idx.q(
            f"SELECT {cols} FROM spell WHERE name=? COLLATE NOCASE AND flavor IN ({ph})"
            " ORDER BY id",
            q,
            *fl,
        )
        match = "exact"
        if not rows:
            rows = idx.q(
                f"SELECT {cols} FROM spell WHERE name LIKE ? AND flavor IN ({ph})"
                " ORDER BY length(name), id LIMIT ?",
                f"%{q}%",
                *fl,
                limit * len(fl),
            )
            match = "substring"
    out: dict[str, list] = {f: [] for f in fl}
    for r in rows:
        if len(out[r["flavor"]]) < limit:
            out[r["flavor"]].append(_spell_view(r, describe=match == "id" or len(rows) <= 12))
    meta = idx.meta()
    return {
        "query": q,
        "match": match,
        "builds": {f: meta.get(f"spells_build_{f}") for f in fl},
        "spells": {f: v or "absent" for f, v in out.items()},
    }


@mcp.tool(annotations=READ_ONLY)
def wow_check_addon(path: str, flavors: list[str] | None = None, include_libs: bool = False) -> str:
    """Scan an addon directory for API references that don't exist (or exist
    only as deprecated shims) in a client its TOCs target, plus TOC interface
    numbers older than the current client. Reads each TOC's `## Interface:`
    to decide which clients apply, compiles the files it loads with luac, and
    treats references inside feature tests (`if C_X then`, `C_X and C_X.F or F`)
    as deliberate. Vendored Libs/ are skipped unless include_libs."""
    try:
        return format_report(scan(Path(path), flavors, include_libs))
    except (FileNotFoundError, ValueError) as e:
        return f"error: {e}"


@mcp.tool(annotations=READ_ONLY)
def wow_check_gate(addon: str, types: bool = False) -> str:
    """The full static gate for one addon — what `./check` runs in
    ~/projects/wowaddons: syntax (luac), luacheck (undefined-variable is an
    error), TOC lint (version consistency, listed files, interface numbers,
    texture paths vs package-as) and the cross-flavor API scan. `types` adds
    a lua-language-server pass (advisory: annotations are retail-oriented).
    `addon` is a path or a directory name under ~/projects/wowaddons."""
    from .check import format_report, resolve_addon, run

    try:
        rep = run(resolve_addon(addon), types=types)
    except (FileNotFoundError, ValueError) as e:
        return f"error: {e}"
    verdict = "FAIL" if rep.errors else "pass"
    return format_report(rep, verbose=True, limit=60) + f"\n\ngate: {verdict}"


@mcp.tool(annotations=READ_ONLY)
def wow_addons() -> dict:
    """The addons under ~/projects/wowaddons: directory, installed folder name
    (package-as), version, and the clients each TOC targets."""
    from .check import discover
    from .scan import read_toc as scan_toc
    from .toc import package_as, tocs

    out = {}
    for a in discover():
        ts = tocs(a)
        out[a.name] = {
            "package_as": package_as(a),
            "version": next(
                (t.headers.get("version") for t in ts if t.headers.get("version")), None
            ),
            "tocs": {
                t.path.name: {"interface": t.interfaces, "flavors": scan_toc(t.path, idx).flavors}
                for t in ts
            },
        }
    return out


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()

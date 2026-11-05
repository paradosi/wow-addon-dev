"""Fetch upstream data and build the SQLite index.

    uv run wow-api-build            # fetch/update upstream, then rebuild
    uv run wow-api-build --offline  # rebuild from what is already on disk

The index is written to a temp file and renamed into place, so a running
server never sees a half-built database (it reopens on the next call).
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

from .config import (
    BIR_URL,
    DB_PATH,
    DUMP_LUA,
    FLAVORS,
    KETHO_URL,
    LUA,
    UI_SOURCE_URL,
    UPSTREAM_DIR,
    Flavor,
    interface_from_version,
)

SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE flavor(
    key TEXT PRIMARY KEY, label TEXT, version TEXT, build TEXT, interface INTEGER,
    ui_commit TEXT, ui_date TEXT, bir_commit TEXT, bir_date TEXT);
-- one row per (kind, name, flavor): presence of a row IS availability
CREATE TABLE api(
    kind TEXT NOT NULL, name TEXT NOT NULL, flavor TEXT NOT NULL,
    system TEXT, detail TEXT,
    PRIMARY KEY(kind, name, flavor));
CREATE INDEX api_name ON api(name COLLATE NOCASE);
CREATE INDEX api_flavor_kind ON api(flavor, kind);
-- Ketho/vscode-wow-api annotations: wiki-derived signatures, flavor-agnostic
CREATE TABLE annotation(
    name TEXT PRIMARY KEY, signature TEXT, params TEXT, returns TEXT,
    description TEXT, url TEXT, file TEXT);
-- where Blizzard's own Lua defines a global (FrameXML source)
CREATE TABLE def(
    flavor TEXT, name TEXT, path TEXT, line INTEGER, deprecated INTEGER, lod INTEGER);
CREATE INDEX def_name ON def(name);
CREATE TABLE gstring(flavor TEXT, key TEXT, value TEXT, PRIMARY KEY(flavor, key));
CREATE VIRTUAL TABLE fts USING fts5(name, kind, system, words, text, tokenize='unicode61');
"""


# --------------------------------------------------------------------------- fetch


def _git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _sync(url: str, branch: str, dest: Path, sparse: str | None = None) -> None:
    if not dest.exists():
        print(f"  clone {url}@{branch}", file=sys.stderr)
        extra = ["--filter=blob:none", "--sparse"] if sparse else []
        _git("clone", "-q", "--depth", "1", "--single-branch", "-b", branch, *extra, url, str(dest))
        if sparse:
            _git("sparse-checkout", "set", sparse, cwd=dest)
        return
    before = _git("rev-parse", "HEAD", cwd=dest)
    _git("fetch", "-q", "--depth", "1", "origin", branch, cwd=dest)
    _git("reset", "-q", "--hard", "FETCH_HEAD", cwd=dest)
    after = _git("rev-parse", "HEAD", cwd=dest)
    state = "unchanged" if before == after else f"{before[:8]} -> {after[:8]}"
    print(f"  {dest.name}: {state}", file=sys.stderr)


def fetch() -> None:
    UPSTREAM_DIR.mkdir(parents=True, exist_ok=True)
    for f in FLAVORS.values():
        _sync(UI_SOURCE_URL, f.key, f.ui_dir, sparse="Interface")
        _sync(BIR_URL, f.key, f.bir_dir)
    _sync(KETHO_URL, "master", UPSTREAM_DIR / "ketho-annotations")


# --------------------------------------------------------------------------- parse


def _lua_json(*args: str) -> object:
    p = subprocess.run([LUA, str(DUMP_LUA), *args], capture_output=True, text=True, check=True)
    for line in p.stderr.splitlines():
        print(f"    lua: {line}", file=sys.stderr)
    return json.loads(p.stdout)


def _res(f: Flavor, name: str) -> dict:
    path = f.bir_dir / "Resources" / f"{name}.lua"
    return _lua_json("res", str(path)) if path.exists() else {}


def _apidoc(f: Flavor) -> list[dict]:
    d = f.ui_dir / "Interface" / "AddOns" / "Blizzard_APIDocumentationGenerated"
    files = sorted(str(p) for p in d.glob("*.lua"))
    enum = f.bir_dir / "Resources" / "LuaEnum.lua"
    return _lua_json("doc", str(enum) if enum.exists() else "-", *files) if files else []


_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")


def words(name: str) -> str:
    """'C_Container.GetContainerNumSlots' -> 'c container get container num slots'."""
    return " ".join(w.lower() for w in _CAMEL.findall(name))


def _params(items: list | None) -> list[dict]:
    out = []
    for a in items or []:
        p = {"name": a.get("Name"), "type": a.get("Type")}
        if a.get("Nilable"):
            p["nilable"] = True
        if "Default" in a:
            p["default"] = a["Default"]
        if a.get("InnerType"):
            p["inner_type"] = a["InnerType"]
        if a.get("Mixin"):
            p["mixin"] = a["Mixin"]
        if a.get("Documentation"):
            p["doc"] = " ".join(a["Documentation"])
        if a.get("StrideIndex"):
            p["stride"] = a["StrideIndex"]
        out.append(p)
    return out


_FUNC_SKIP = {"Name", "Type", "Arguments", "Returns", "Documentation", "Namespace"}


def _func_detail(fn: dict, table: dict) -> dict:
    d: dict = {"args": _params(fn.get("Arguments")), "returns": _params(fn.get("Returns"))}
    if fn.get("Documentation"):
        d["doc"] = " ".join(fn["Documentation"])
    flags = sorted(k for k, v in fn.items() if k not in _FUNC_SKIP and v is True)
    if flags:
        d["flags"] = flags
    d["source"] = f"Blizzard_APIDocumentationGenerated/{table.get('_file')}"
    return d


def _widget_for_scriptobject(table_name: str) -> str:
    """'SimpleSliderAPI' -> 'Slider', 'FrameAPICooldown' -> 'Cooldown'."""
    n = table_name
    for prefix in ("Simple", "FrameAPI"):
        if n.startswith(prefix):
            n = n[len(prefix) :]
    if n.endswith("API"):
        n = n[:-3]
    return {"Checkbox": "CheckButton", "Anim": "Animation"}.get(n, n)


def collect_flavor(f: Flavor) -> dict:
    """Everything known about one flavor, as {kind: {name: (system, detail)}}."""
    rows: dict[str, dict[str, tuple[str | None, dict]]] = defaultdict(dict)

    def put(kind: str, name: str, system: str | None, detail: dict | None = None) -> None:
        old = rows[kind].get(name)
        if old:  # merge: keep the richer detail, fill a missing system
            merged = {**(detail or {}), **old[1]}
            rows[kind][name] = (old[0] or system, merged)
        else:
            rows[kind][name] = (system, dict(detail or {}))

    # --- Blizzard's structured docs (typed, but only the C_* / documented subset)
    widget_sigs: dict[tuple[str, str], dict] = {}
    for t in _apidoc(f):
        system = t.get("Name")
        ns = t.get("Namespace")
        is_so = t.get("Type") == "ScriptObject"
        for fn in t.get("Functions") or []:
            if is_so:
                widget_sigs[(_widget_for_scriptobject(system), fn["Name"])] = _func_detail(fn, t)
                continue
            name = f"{ns}.{fn['Name']}" if ns else fn["Name"]
            put("function", name, system, _func_detail(fn, t))
        for ev in t.get("Events") or []:
            d = {"payload": _params(ev.get("Payload"))}
            if ev.get("Documentation"):
                d["doc"] = " ".join(ev["Documentation"])
            if ev.get("SynchronousEvent"):
                d["synchronous"] = True
            if ev.get("UniqueEvent"):
                d["unique"] = True
            d["source"] = f"Blizzard_APIDocumentationGenerated/{t.get('_file')}"
            put("event", ev.get("LiteralName") or ev["Name"], system, d)
        for tb in t.get("Tables") or []:
            kind = {
                "Enumeration": "enum",
                "Structure": "struct",
                "Constants": "constants",
                "CallbackType": "callback",
            }.get(tb.get("Type"), "struct")
            d = {"fields": _params(tb.get("Fields")) if kind != "enum" else None}
            if kind == "enum":
                d["values"] = {x["Name"]: x.get("EnumValue") for x in tb.get("Fields") or []}
            if kind == "constants":
                d["values"] = {x["Name"]: x.get("Value") for x in tb.get("Values") or []}
            if kind == "callback":
                d["args"] = _params(tb.get("Arguments"))
            if tb.get("Documentation"):
                d["doc"] = " ".join(tb["Documentation"])
            d = {k: v for k, v in d.items() if v is not None}
            name = f"Enum.{tb['Name']}" if kind == "enum" else tb["Name"]
            put(kind, name, system, d)

    # --- Ketho's dumps of the live client: authoritative presence, untyped
    g = _res(f, "GlobalAPI")
    for name in g.get("GlobalAPI", []):
        put("function", name, name.split(".")[0] if "." in name else None)
    for name in g.get("LuaAPI", []):
        put("lua", name, "Lua")

    for system, evs in (_res(f, "Events").get("Events") or {}).items():
        for ev in evs:
            put("event", ev, system)

    le = _res(f, "LuaEnum")
    for ename, values in (le.get("Enum") or {}).items():
        put("enum", f"Enum.{ename}", None, {"values": values})
    for cname, values in (le.get("Constants") or {}).items():
        put("constants", cname, None, {"values": values})
    for k, v in le.items():
        if k not in ("Enum", "Constants") and not isinstance(v, dict | list):
            put("global_const", k, "LuaEnum", {"value": v})

    for wtype, w in (_res(f, "WidgetAPI").get("WidgetAPI") or {}).items():
        put("widget", wtype, "Widget", {"inherits": w.get("inherits") or []})
        for m in w.get("methods") or []:
            put("widget_method", f"{wtype}:{m}", wtype, widget_sigs.get((wtype, m)))
        for h in w.get("handlers") or []:
            put("script_handler", f"{wtype}:{h}", wtype)
    for so, methods in (_res(f, "ScriptObjectAPI").get("ScriptObjectAPI") or {}).items():
        base = so[:-3] if so.endswith("API") else so
        put("widget", base, "ScriptObject", {"inherits": [], "script_object": True})
        for m in methods:
            put("widget_method", f"{base}:{m}", base, widget_sigs.get((base, m)))

    cv = _res(f, "CVars").get("CVars") or {}
    for name, v in (cv.get("var") or {}).items():
        default, category, account, character, secure, help_ = (list(v) + [None] * 6)[:6]
        put(
            "cvar",
            name,
            "CVar",
            {
                "default": default,
                "category": category,
                "account": account,
                "character": character,
                "secure": secure,
                "doc": help_ or None,
            },
        )
    for name, v in (cv.get("command") or {}).items():
        put("cvar_command", name, "CVar", {"category": v[0], "doc": (v[1] or None)})

    fx = _res(f, "FrameXML")
    for name in fx.get("FrameXML", []):
        put("framexml", name, "FrameXML")
    for name in fx.get("LoadOnDemand", []):
        put("framexml", name, "FrameXML", {"load_on_demand": True})
    for name in _res(f, "Mixins").get("Mixins", []):
        put("mixin", name, "FrameXML")
    fr = _res(f, "Frames")
    for name in fr.get("Frames", []):
        put("frame", name, "FrameXML")
    for name in fr.get("LoadOnDemand", []):
        put("frame", name, "FrameXML", {"load_on_demand": True})
    for name, v in (_res(f, "Templates").get("Templates") or {}).items():
        put("template", name, "FrameXML", {"type": v.get("type") if isinstance(v, dict) else None})

    return rows


_DEF_TOP = re.compile(r"^(?:function\s+([A-Za-z_][\w.:]*)\s*\(|([A-Za-z_][\w.]*)\s*=[^=])")
# inside the `do ... end` blocks of Blizzard_Deprecated* files, shims are indented
_DEF_INDENTED = re.compile(
    r"^\s+(?:function\s+([A-Za-z_][\w.]*)\s*\(|([A-Za-z_]\w*)\s*=\s*function\b)"
)
_TOC_COND = re.compile(r"\[(AllowLoad[A-Za-z]*)\s+([^\]]+)\]")
_XML_REF = re.compile(r'<(Script|Include)\s+file="([^"]+)"', re.I)


def _ci_path(base: Path, rel: str) -> Path | None:
    """Resolve a TOC/XML path the way Windows would: backslashes, any case."""
    cur = base
    for part in rel.replace("\\", "/").split("/"):
        if part in ("", "."):
            continue
        nxt = cur / part
        if not nxt.exists():
            try:
                low = part.lower()
                nxt = next(c for c in cur.iterdir() if c.name.lower() == low)
            except (StopIteration, OSError):
                return None
        cur = nxt
    return cur


def _cond_ok(f: Flavor, cond: str, value: str) -> bool:
    vals = {v.strip().lower() for v in value.replace(",", " ").split()}
    if cond.lower() == "allowloadgametype":
        return bool(vals & f.game_types)
    if cond.lower() == "allowload":  # Game / Glue / Both
        return bool(vals & {"game", "both"})
    return True  # AllowLoadEnvironment etc.: not a flavor question


def _addon_toc(f: Flavor, addon_dir: Path) -> Path | None:
    for suf in f.toc_suffixes:
        for sep in ("_", "-"):
            p = addon_dir / f"{addon_dir.name}{sep}{suf}.toc"
            if p.exists():
                return p
    p = addon_dir / f"{addon_dir.name}.toc"
    return p if p.exists() else None


def loaded_files(f: Flavor) -> list[tuple[Path, bool]]:
    """Every Lua and XML file this client loads from Blizzard's UI, following
    TOCs and XML <Script>/<Include> the way the client does: (path, lod)."""
    out: list[tuple[Path, bool]] = []
    seen: set[Path] = set()

    def walk_xml(xml: Path, lod: bool) -> None:
        if xml in seen:
            return
        seen.add(xml)
        out.append((xml, lod))
        try:
            text = xml.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        for _kind, ref in _XML_REF.findall(text):
            p = _ci_path(xml.parent, ref)
            if not p:
                continue
            if p.suffix.lower() == ".lua" and p not in seen:
                seen.add(p)
                out.append((p, lod))
            elif p.suffix.lower() == ".xml":
                walk_xml(p, lod)

    for addon in sorted((f.ui_dir / "Interface" / "AddOns").iterdir()):
        if not addon.is_dir() or addon.name == "Blizzard_APIDocumentationGenerated":
            continue
        toc = _addon_toc(f, addon)
        if not toc:
            continue
        lines = toc.read_text(encoding="utf-8", errors="replace").splitlines()
        headers = {}
        for ln in lines:
            m = re.match(r"^##\s*([\w-]+)\s*:\s*(.*)$", ln)
            if m:
                headers[m.group(1).lower()] = m.group(2).strip()
        if "allowloadgametype" in headers and not _cond_ok(
            f, "AllowLoadGameType", headers["allowloadgametype"]
        ):
            continue
        if headers.get("allowload", "both").lower() == "glue":
            continue
        lod = headers.get("loadondemand", "0").strip() == "1"
        for ln in lines:
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            conds = _TOC_COND.findall(ln)
            if not all(_cond_ok(f, c, v) for c, v in conds):
                continue
            ref = _TOC_COND.sub("", ln).strip()
            p = _ci_path(addon, ref)
            if not p:
                continue
            if p.suffix.lower() == ".lua" and p not in seen:
                seen.add(p)
                out.append((p, lod))
            elif p.suffix.lower() == ".xml":
                walk_xml(p, lod)
    return out


_LUAC_SET = re.compile(
    r"^\s*\d+\s+\[(\d+)\]\s+(SETGLOBAL|GETGLOBAL|SETTABLE)\s+(-?\d+)[^;]*;\s*(\S+)"
)
_XML_NAME = re.compile(r'<(\w+)\b[^>]*?\bname="([A-Za-z_]\w*)"')


def _luac_globals(path: Path) -> list[tuple[str, int]] | None:
    """Every global a Lua file assigns, from its bytecode: SETGLOBAL anywhere
    (function bodies included) and `_G.X = ...` / `_G["X"] = ...`."""
    p = subprocess.run(
        ["luac5.1", "-l", "-p", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if p.returncode:
        return None
    out = []
    g_reg: set[str] = set()
    for line in p.stdout.splitlines():
        m = _LUAC_SET.match(line)
        if not m:
            continue
        ln, op, reg, name = int(m.group(1)), m.group(2), m.group(3), m.group(4)
        if op == "SETGLOBAL":
            out.append((name, ln))
        elif op == "GETGLOBAL":
            if name == "_G":
                g_reg.add(reg)
            else:
                g_reg.discard(reg)
        elif op == "SETTABLE" and reg in g_reg and name.startswith('"'):
            out.append((name.strip('"'), ln))
    return out


def collect_defs(f: Flavor) -> list[tuple]:
    """Globals defined by Blizzard Lua/XML that this client actually loads:
    (flavor, name, path, line, deprecated, load_on_demand).

    Plain globals come from bytecode (exact, any nesting depth); dotted and
    method names (`function FooMixin:Bar`) from a top-level regex, which is
    all that is needed for those; XML contributes named frames/templates."""
    from concurrent.futures import ThreadPoolExecutor

    root = f.ui_dir / "Interface"
    files = loaded_files(f)
    lua = [(p, lod) for p, lod in files if p.suffix.lower() == ".lua"]
    with ThreadPoolExecutor(max_workers=8) as ex:
        compiled = list(ex.map(lambda x: _luac_globals(x[0]), lua))
    out = []
    for (p, lod), globs in zip(lua, compiled, strict=True):
        rel = str(p.relative_to(root))
        deprecated = "deprecated" in rel.lower()
        text = p.read_text(encoding="utf-8", errors="replace")
        seen = set()
        for name, ln in globs or []:
            seen.add(name)
            out.append((f.key, name, rel, ln, int(deprecated), int(lod)))
        for i, line in enumerate(text.splitlines(), 1):
            m = _DEF_TOP.match(line) or (deprecated and _DEF_INDENTED.match(line))
            if not m:
                continue
            name = m.group(1) or m.group(2)
            if name not in seen and (globs is None or "." in name or ":" in name):
                out.append((f.key, name, rel, i, int(deprecated), int(lod)))
    for p, lod in files:
        if p.suffix.lower() != ".xml":
            continue
        rel = str(p.relative_to(root))
        text = p.read_text(encoding="utf-8", errors="replace")
        for m in _XML_NAME.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            out.append((f.key, m.group(2), rel, line, 0, int(lod)))
    return out


_ANN_FUNC = re.compile(r"^function\s+([A-Za-z_][\w.:]*)\s*\(([^)]*)\)")


def collect_annotations() -> dict[str, dict]:
    root = UPSTREAM_DIR / "ketho-annotations" / "Annotations"
    out: dict[str, dict] = {}
    for p in sorted(root.rglob("*.lua")):
        block: list[str] = []
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("---"):
                block.append(line[3:].strip())
                continue
            m = _ANN_FUNC.match(line)
            if m and m.group(1) not in out:
                url, params, returns, desc = None, [], [], []
                for b in block:
                    if b.startswith("[Documentation]("):
                        url = b[len("[Documentation](") : -1]
                    elif b.startswith("@param"):
                        params.append(b[6:].strip())
                    elif b.startswith("@return"):
                        returns.append(b[7:].strip())
                    elif b and not b.startswith("@") and not b.startswith("#"):
                        desc.append(b)
                out[m.group(1)] = {
                    "signature": f"{m.group(1)}({m.group(2)})",
                    "params": params,
                    "returns": returns,
                    "description": " ".join(desc) or None,
                    "url": url,
                    "file": str(p.relative_to(root)),
                }
            block = []
    return out


_GSTR = re.compile(r'^(?:_G\["([^"]+)"\]|([A-Za-z_]\w*))\s*=\s*"((?:[^"\\]|\\.)*)";?\s*$')
_UNESC = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "'": "'"}


def _gstrings(f: Flavor) -> dict[str, str]:
    """enUS GlobalStrings. Parsed line by line: upstream files have had the odd
    malformed line, which would make a whole-file Lua load fail."""
    path = f.bir_dir / "Resources" / "GlobalStrings" / "enUS.lua"
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = _GSTR.match(line)
        if m:
            val = re.sub(r"\\(.)", lambda x: _UNESC.get(x.group(1), x.group(1)), m.group(3))
            out[m.group(1) or m.group(2)] = val
    return out


# --------------------------------------------------------------------------- write


def _version(f: Flavor) -> tuple[str, str]:
    """('2.5.6', '70577') from the wow-ui-source commit subject '2.5.6 (70577)'."""
    subj = _git("log", "-1", "--format=%s", cwd=f.ui_dir)
    m = re.match(r"\s*([\d.]+)\s*\((\d+)\)", subj)
    return (m.group(1), m.group(2)) if m else (subj, "")


def build() -> Path:
    t0 = time.time()
    tmp = DB_PATH.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    db = sqlite3.connect(tmp)
    db.executescript(SCHEMA)

    fts_rows: dict[tuple[str, str], dict] = {}
    for f in FLAVORS.values():
        if not f.ui_dir.exists() or not f.bir_dir.exists():
            print(f"  {f.key}: no upstream data, skipped", file=sys.stderr)
            continue
        version, build_no = _version(f)
        db.execute(
            "INSERT INTO flavor VALUES(?,?,?,?,?,?,?,?,?)",
            (
                f.key,
                f.label,
                version,
                build_no,
                interface_from_version(version),
                _git("rev-parse", "HEAD", cwd=f.ui_dir),
                _git("log", "-1", "--format=%cI", cwd=f.ui_dir),
                _git("rev-parse", "HEAD", cwd=f.bir_dir),
                _git("log", "-1", "--format=%cI", cwd=f.bir_dir),
            ),
        )
        rows = collect_flavor(f)
        n = 0
        for kind, items in rows.items():
            batch = []
            for name, (system, detail) in items.items():
                batch.append((kind, name, f.key, system, json.dumps(detail) if detail else None))
                key = (kind, name)
                e = fts_rows.setdefault(key, {"system": system, "text": set()})
                e["system"] = e["system"] or system
                if detail:
                    for k in ("doc",):
                        if detail.get(k):
                            e["text"].add(detail[k])
                    for p in detail.get("args", []) + detail.get("payload", []):
                        e["text"].add(p.get("name") or "")
            db.executemany("INSERT INTO api VALUES(?,?,?,?,?)", batch)
            n += len(batch)
        defs = collect_defs(f)
        db.executemany("INSERT INTO def VALUES(?,?,?,?,?,?)", defs)
        gs = _gstrings(f)
        db.executemany("INSERT INTO gstring VALUES(?,?,?)", [(f.key, k, v) for k, v in gs.items()])
        print(
            f"  {f.key}: {version} ({build_no}) — {n} api rows, {len(defs)} defs, "
            f"{len(gs)} strings",
            file=sys.stderr,
        )

    ann = collect_annotations()
    db.executemany(
        "INSERT INTO annotation VALUES(?,?,?,?,?,?,?)",
        [
            (
                k,
                v["signature"],
                json.dumps(v["params"]),
                json.dumps(v["returns"]),
                v["description"],
                v["url"],
                v["file"],
            )
            for k, v in ann.items()
        ],
    )
    for name, a in ann.items():
        for kind in ("function", "widget_method"):
            if (kind, name) in fts_rows and a["description"]:
                fts_rows[(kind, name)]["text"].add(a["description"])

    db.executemany(
        "INSERT INTO fts(name, kind, system, words, text) VALUES(?,?,?,?,?)",
        [
            (name, kind, e["system"] or "", words(name), " ".join(sorted(e["text"])))
            for (kind, name), e in fts_rows.items()
        ],
    )
    db.execute("INSERT INTO meta VALUES('built_at', ?)", (time.strftime("%Y-%m-%dT%H:%M:%S%z"),))
    ketho = UPSTREAM_DIR / "ketho-annotations"
    if ketho.exists():
        db.execute(
            "INSERT INTO meta VALUES('ketho_commit', ?)", (_git("rev-parse", "HEAD", cwd=ketho),)
        )
    db.commit()
    db.execute("VACUUM")
    db.close()
    tmp.replace(DB_PATH)
    print(
        f"  wrote {DB_PATH} ({DB_PATH.stat().st_size / 1e6:.1f} MB, "
        f"{len(ann)} annotations) in {time.time() - t0:.1f}s",
        file=sys.stderr,
    )
    return DB_PATH


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="wow-api-build", description=__doc__.splitlines()[0])
    ap.add_argument("--offline", action="store_true", help="skip git fetch, rebuild only")
    args = ap.parse_args(argv)
    if not args.offline:
        print("fetching upstream:", file=sys.stderr)
        fetch()
    print("building index:", file=sys.stderr)
    build()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

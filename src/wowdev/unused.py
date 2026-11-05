"""Dead weight in an addon: what ships but is never loaded or used, and code
written twice. Advisory only, never part of the gate: every finding is a
candidate a person has to judge, because WoW code reaches things in ways no
static pass sees (`_G["Name"..i]`, XML `inherits`, other addons).

Passes:

    unloaded    .lua/.xml files no TOC or included XML loads, yet not stripped
                by .pkgmeta — shipped to every player for nothing
    media       textures, fonts and sounds whose name appears in no loaded file
    globals     globals the addon defines that nothing in it reads (bytecode
                SETGLOBAL with no GETGLOBAL, no string mention, no XML mention)
    locals      luacheck: unused local functions and variables, values never
                read, unreachable code, empty branches
    libs        vendored libraries nothing outside the library names
    locale      locale keys defined but never looked up
    duplicates  runs of ≥ MIN_RUN identical (normalised) lines in two places
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from collections import defaultdict
from pathlib import Path

from .build import _XML_REF, _ci_path
from .scan import analyse

MIN_RUN = 8  # lines a duplicate has to span before it is worth reporting
_MIN_CHARS = 160  # ...and enough real code that it is not a run of `end`s
_LIB = re.compile(r"(^|/)(libs?|libraries)/", re.I)
_MEDIA = {".tga", ".blp", ".png", ".jpg", ".jpeg", ".ttf", ".otf", ".ogg", ".mp3", ".wav"}
_LUACHECK = re.compile(r"^(.+?):(\d+):(\d+): \((\w)(\d+)\) (.*)$")
_LOCALE_DEF = re.compile(
    r'\bL\s*\[\s*(?:"((?:[^"\\]|\\.)*)"|\'((?:[^\'\\]|\\.)*)\'|\[(=*)\[(.*?)\]\3\])\s*\]\s*=',
    re.S,
)
_LOCALE_USE = re.compile(
    r'\bL\s*\[\s*(?:"((?:[^"\\]|\\.)*)"|\'((?:[^\'\\]|\\.)*)\'|\[(=*)\[(.*?)\]\3\])\s*\]|\bL\.(\w+)',
    re.S,
)
_LOCALE_DYNAMIC = re.compile(r"\bL\s*\[\s*[A-Za-z_(]")
_LIB_NAME = re.compile(
    r"""(?:NewLibrary\s*\(\s*|MAJOR(?:_VERSION)?\s*(?:,\s*\w+\s*)?=\s*)["']([^"']+)["']"""
)
_NEVER_DEAD = re.compile(r"^(SLASH_|BINDING_|BINDING_HEADER_|BINDING_NAME_)")
_LUACHECK_CODES = {
    "211": "unused",
    "221": "never set",
    "231": "never read",
    "311": "value never read",
    "511": "unreachable code",
    "512": "loop runs at most once",
    "521": "unused label",
    "541": "empty do..end",
    "542": "empty if branch",
}


# ------------------------------------------------------------------ files


def pkgmeta_ignores(addon: Path) -> list[str]:
    pm = addon / ".pkgmeta"
    if not pm.exists():
        return []
    out, inside = [], False
    for ln in pm.read_text(errors="replace").splitlines():
        if re.match(r"^ignore\s*:", ln):
            inside = True
            continue
        if inside:
            m = re.match(r"^\s+-\s*(.+?)\s*$", ln)
            if m:
                out.append(m.group(1).strip("'\"").rstrip("/"))
            elif ln.strip() and not ln.startswith((" ", "\t")):
                inside = False
    return out


def _ignored(rel: str, ignores: list[str]) -> bool:
    return any(rel == i or rel.startswith(i + "/") for i in ignores)


def shipped(addon: Path) -> list[Path]:
    """Every file the packager would put in the zip."""
    ign = pkgmeta_ignores(addon)
    out = []
    for p in sorted(addon.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(addon).as_posix()
        if rel.startswith((".git/", ".release/", ".github/")) or rel.startswith("."):
            continue
        if any(part.startswith(".") for part in p.relative_to(addon).parts):
            continue
        if _ignored(rel, ign) or p.suffix.lower() == ".md":
            continue
        out.append(p)
    return out


def loaded(addon: Path) -> list[Path]:
    """Lua and XML files the client loads, from every TOC, following XML includes."""
    seen: dict[Path, None] = {}

    def add(p: Path) -> None:
        if p in seen:
            return
        seen[p] = None
        if p.suffix.lower() == ".xml":
            for _, ref in _XML_REF.findall(p.read_text(encoding="utf-8", errors="replace")):
                if (q := _ci_path(p.parent, ref)) is not None:
                    add(q)

    for toc in sorted(addon.glob("*.toc")):
        for ln in toc.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            ln = re.sub(r"\[[^\]]*\]", "", ln).strip()
            if ln and not ln.startswith("#") and (p := _ci_path(addon, ln)) is not None:
                add(p)
    if (b := _ci_path(addon, "Bindings.xml")) is not None:
        add(b)  # the client loads it without a TOC line
    return list(seen)


def _text(p: Path) -> str:
    return p.read_text(encoding="utf-8-sig", errors="replace")


# ------------------------------------------------------------------ passes


def _unloaded(addon: Path, ship: list[Path], load: set[Path]) -> list[str]:
    return [
        p.relative_to(addon).as_posix()
        for p in ship
        if p.suffix.lower() in (".lua", ".xml") and p not in load
    ]


def _media(addon: Path, ship: list[Path], corpus: str) -> list[str]:
    low = corpus.lower().replace("\\\\", "/").replace("\\", "/")
    out = []
    for p in ship:
        if p.suffix.lower() not in _MEDIA:
            continue
        stem = p.stem.lower()
        if stem not in low:
            out.append(p.relative_to(addon).as_posix())
    return out


def _globals(addon: Path, code: list[Path], load: list[Path], saved: set[str]) -> list[dict]:
    defs: dict[str, tuple[str, int]] = {}
    reads: set[str] = set()
    for p in load:
        if p.suffix.lower() != ".lua":
            continue
        rel = p.relative_to(addon).as_posix()
        try:
            refs, defines, _ = analyse(p, rel, set())
        except SyntaxError:
            continue
        reads |= {r.name for r in refs}
        if p in code:
            src = _text(p).splitlines()
            for name in defines:
                if name in defs:
                    continue
                line = next(
                    (
                        i + 1
                        for i, s in enumerate(src)
                        if re.search(
                            rf"(?<![\w.:]){re.escape(name)}\s*=|function\s+{re.escape(name)}\b", s
                        )
                    ),
                    0,
                )
                defs[name] = (rel, line)
    strings = " ".join(_text(p) for p in load)
    xmltoc = " ".join(_text(p) for p in load if p.suffix.lower() == ".xml") + " ".join(
        _text(t) for t in addon.glob("*.toc")
    )
    out = []
    for name, (rel, line) in sorted(defs.items()):
        if name in reads or name in saved or _NEVER_DEAD.match(name):
            continue
        if re.search(rf"\b{re.escape(name)}\b", xmltoc):
            continue
        if re.search(rf"""["']{re.escape(name)}["']""", strings):
            continue  # hooksecurefunc("Name"), _G["Name"], RawHook("Name")
        out.append({"name": name, "file": rel, "line": line})
    return out


def _locals(addon: Path, code: list[Path]) -> list[dict]:
    if not code:
        return []
    p = subprocess.run(
        [
            "luacheck", "--no-config", "--formatter", "plain", "--codes", "--no-color",
            "--only", *_LUACHECK_CODES, "--",
            *[c.relative_to(addon).as_posix() for c in code],
        ],
        cwd=addon, capture_output=True, text=True, errors="replace",
    )  # fmt: skip
    out = []
    lines: dict[str, list[str]] = {}
    for ln in p.stdout.splitlines():
        m = _LUACHECK.match(ln)
        if not m:
            continue
        msg = m.group(6)
        # an unused `_` or unpacked return in a select-style list is noise
        if re.search(r"variable '_\w*'|value assigned to variable '_", msg):
            continue
        if m.group(5) == "211" and msg.startswith("unused variable"):
            src = lines.setdefault(m.group(1), _text(addon / m.group(1)).splitlines())
            decl = src[int(m.group(2)) - 1] if int(m.group(2)) <= len(src) else ""
            # one of several names unpacked from a call: naming the returns is
            # documentation, not dead code
            if re.match(r"^\s*(local\s+)?[\w\s]+,[\w\s,]*=", decl) or re.match(r"^\s*for\b", decl):
                continue
        out.append(
            {"file": m.group(1), "line": int(m.group(2)), "code": m.group(5), "message": msg}
        )
    return out


def _libs(addon: Path, load: list[Path]) -> list[dict]:
    """A library counts as used if anything outside it names it in a string
    (LibStub("X"), NewAddon(..., "AceEvent-3.0")), reads a global it defines
    (LibStub), or calls a distinctive key it sets (string.utf8sub)."""
    roots: dict[Path, set[str]] = defaultdict(set)
    provides: dict[Path, set[str]] = defaultdict(set)
    for p in load:
        rel = p.relative_to(addon).as_posix()
        m = re.match(r"^((?:[^/]*/)*?(?:libs?|libraries))/([^/]+)/", rel, re.I)
        if not m:
            continue
        root = addon / m.group(1) / m.group(2)
        roots.setdefault(root, set())
        if p.suffix.lower() == ".lua":
            src = _text(p)
            roots[root] |= set(_LIB_NAME.findall(src))
            try:
                _, defines, keys = analyse(p, rel, set())
            except SyntaxError:
                defines, keys = set(), set()
            provides[root] |= defines | {
                k for k in keys if len(k) >= 6 and re.search(r"[A-Z0-9_]|^utf8", k)
            }
    out = []
    for root, names in sorted(roots.items()):
        names.add(root.name)
        others = " ".join(_text(p) for p in load if root not in p.parents)
        if any(re.search(rf"""["']{re.escape(n)}["']""", others) for n in names):
            continue
        if any(re.search(rf"(?<![\w]){re.escape(k)}\b", others) for k in provides[root]):
            continue
        out.append({"lib": root.relative_to(addon).as_posix(), "names": sorted(names)})
    return out


def _key(m: re.Match) -> str:
    return next(g for g in (m.group(1), m.group(2), m.group(4), m.groups()[-1]) if g is not None)


def _locale(addon: Path, load: list[Path]) -> dict:
    lua = [
        p
        for p in load
        if p.suffix.lower() == ".lua" and not _LIB.search(p.relative_to(addon).as_posix())
    ]
    defining = [p for p in lua if _LOCALE_DEF.search(_text(p))]
    if not defining:
        return {"unused": [], "missing": [], "dynamic": False}
    users = [p for p in lua if p not in defining]
    used: set[str] = set()
    dynamic = False
    for p in users:
        t = _text(p)
        used |= {_key(m) for m in _LOCALE_USE.finditer(t)}
        dynamic |= bool(_LOCALE_DYNAMIC.search(t))
    # the base locale: the file that marks itself default (or is enUS), else the
    # first. With no enUS file the code's own English strings are the keys, and
    # an unused key is a stale translation.
    base = next(
        (p for p in defining if re.search(r"NewLocale\([^)]*,\s*true|enUS", _text(p))),
        defining[0],
    )
    defined = [_key(m) for m in _LOCALE_DEF.finditer(_text(base))]
    unused = sorted({k for k in defined if k not in used})
    # the reverse, and a real bug when the base locale is enUS: AceLocale
    # reports "Missing entry" through the error handler on every lookup
    has_enus = bool(re.search(r"enUS", _text(base)))
    missing = sorted({k for k in used if k not in set(defined)}) if has_enus else []
    return {
        "base": base.relative_to(addon).as_posix(),
        "defined": len(set(defined)),
        "unused": unused,
        "missing": missing,
        "dynamic": dynamic,
    }


def _norm(line: str) -> str:
    s = re.sub(r"--.*$", "", line) if not re.search(r"""["'][^"']*--""", line) else line
    return re.sub(r"\s+", " ", s).strip()


_FILES: list[str] = []


def _span(loc: str) -> tuple[int, int, int]:
    f, rng = loc.rsplit(":", 1)
    a, b = rng.split("-")
    return _FILES.index(f), int(a), int(b)


def _sortkey(loc: str) -> tuple[str, int]:
    f, rng = loc.rsplit(":", 1)
    return f, int(rng.split("-")[0])


def _duplicates(addon: Path, code: list[Path]) -> list[dict]:
    global _FILES
    seqs: list[tuple[str, list[tuple[int, str]]]] = []
    for p in code:
        rows = [(i + 1, n) for i, ln in enumerate(_text(p).splitlines()) if (n := _norm(ln))]
        seqs.append((p.relative_to(addon).as_posix(), rows))
    _FILES = [s[0] for s in seqs]
    windows: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for fi, (_, rows) in enumerate(seqs):
        for k in range(len(rows) - MIN_RUN + 1):
            chunk = [r[1] for r in rows[k : k + MIN_RUN]]
            if sum(len(re.sub(r"\W", "", c)) for c in chunk) < _MIN_CHARS:
                continue
            h = hashlib.blake2b("\n".join(chunk).encode(), digest_size=12).hexdigest()
            windows[h].append((fi, k))
    pairs: set[tuple[tuple[int, int], tuple[int, int]]] = set()
    for locs in windows.values():
        if 1 < len(locs) <= 12:
            for a in range(len(locs)):
                for b in range(a + 1, len(locs)):
                    pairs.add((locs[a], locs[b]))
    runs = []
    for (fa, ka), (fb, kb) in pairs:
        if fa == fb and abs(ka - kb) < MIN_RUN:
            continue  # a window overlapping itself, e.g. a long run of similar lines
        if ((fa, ka - 1), (fb, kb - 1)) in pairs:
            continue  # not the start of the run
        n = 0
        while ((fa, ka + n), (fb, kb + n)) in pairs:
            n += 1
        ra, rb = seqs[fa][1], seqs[fb][1]
        length = n + MIN_RUN - 1
        runs.append(
            {
                "lines": length,
                "a": f"{seqs[fa][0]}:{ra[ka][0]}-{ra[ka + length - 1][0]}",
                "b": f"{seqs[fb][0]}:{rb[kb][0]}-{rb[kb + length - 1][0]}",
                "first": ra[ka][1][:80],
            }
        )
    # one entry per repeated block, with every place it occurs; drop blocks
    # whose every occurrence sits inside an occurrence of a longer block
    groups: dict[str, dict] = {}
    for r in runs:
        fa, a0, a1 = _span(r["a"])
        text = "\n".join(n for ln, n in seqs[fa][1] if a0 <= ln <= a1)
        h = hashlib.blake2b(text.encode(), digest_size=12).hexdigest()
        g = groups.setdefault(h, {"lines": r["lines"], "first": r["first"], "at": set()})
        g["at"] |= {r["a"], r["b"]}
    ordered = sorted(groups.values(), key=lambda g: (-g["lines"] * len(g["at"]), -g["lines"]))
    kept: list[dict] = []
    for g in ordered:
        spans = [_span(x) for x in g["at"]]
        inside = all(
            any(
                s[0] == o[0] and o[1] <= s[1] and s[2] <= o[2]
                for k in kept
                for o in map(_span, k["at"])
            )
            for s in spans
        )
        if not inside:
            kept.append(g)
    return [
        {
            "lines": g["lines"],
            "count": len(g["at"]),
            "at": sorted(g["at"], key=_sortkey),
            "first": g["first"],
        }
        for g in kept
    ]


# ------------------------------------------------------------------ entry


def unused(addon: Path) -> dict:
    addon = addon.expanduser().resolve()
    ship = shipped(addon)
    load = loaded(addon)
    lset = set(load)
    code = [
        p
        for p in load
        if p.suffix.lower() == ".lua" and not _LIB.search(p.relative_to(addon).as_posix())
    ]
    corpus = " ".join(_text(p) for p in load) + " ".join(_text(t) for t in addon.glob("*.toc"))
    saved: set[str] = set()
    for t in addon.glob("*.toc"):
        for m in re.finditer(
            r"^##\s*SavedVariables(?:PerCharacter)?\s*:\s*(.*)$", _text(t), re.M | re.I
        ):
            saved |= {v.strip() for v in m.group(1).split(",") if v.strip()}
    return {
        "addon": addon.name,
        "unloaded": _unloaded(addon, ship, lset),
        "media": _media(addon, ship, corpus),
        "globals": _globals(addon, code, load, saved),
        "locals": _locals(addon, code),
        "libs": _libs(addon, load),
        "locale": _locale(addon, load),
        "duplicates": _duplicates(addon, code),
    }


def format_report(r: dict, verbose: bool = False, limit: int = 15) -> str:
    out = [f"# unused / duplicated: {r['addon']}  (candidates — check each before removing)"]

    def section(title: str, rows: list[str], note: str = "") -> None:
        out.append(f"  {title:<11} {len(rows)}{('  ' + note) if note else ''}")
        for row in rows if verbose else rows[:limit]:
            out.append(f"      {row}")
        if not verbose and len(rows) > limit:
            out.append(f"      … {len(rows) - limit} more (-v)")

    section("unloaded", r["unloaded"], "shipped, never loaded")
    section("media", r["media"], "name appears in no loaded file")
    section(
        "globals",
        [f"{g['file']}:{g['line']} {g['name']}" for g in r["globals"]],
        "defined, never read here",
    )
    section("locals", [f"{x['file']}:{x['line']} W{x['code']} {x['message']}" for x in r["locals"]])
    section(
        "libs",
        [f"{x['lib']}  ({', '.join(x['names'])})" for x in r["libs"]],
        "nothing outside it names it",
    )
    loc = r["locale"]
    note = f"of {loc.get('defined', 0)} in {loc.get('base', '-')}"
    if loc.get("dynamic"):
        note += "; L[var] lookups exist, so some are used dynamically"
    section("locale", loc["unused"], note)
    section(
        "missing",
        loc.get("missing", []),
        "looked up, not in the enUS locale: AceLocale raises an error",
    )
    section(
        "duplicates",
        [f"{d['lines']:>3} lines ×{d['count']}  {', '.join(d['at'])}" for d in r["duplicates"]],
        f"blocks of ≥{MIN_RUN} identical lines",
    )
    return "\n".join(out)

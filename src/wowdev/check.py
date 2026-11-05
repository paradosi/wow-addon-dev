"""The pre-flight gate: every static pass there is, for one addon or all.

    syntax    luac5.1 -p          — a failure means the addon will not load at all
    lint      luacheck            — W113 "accessing undefined variable" is an error
                                     (after config tuning every one has been a real bug)
    toc       toc.lint            — versions, listed files, interface, texture paths
    api       scan.scan           — references missing / deprecated in a targeted client
    types     luals.check         — advisory: types are retail-oriented (opt-in, --types)
    package   package.build       — local packager dry run (opt-in, --package)

Errors fail the gate; warnings are reported and don't.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import luals, package, scan, toc
from .index import Index

ADDONS_DIR = Path.home() / "projects" / "wowaddons"
_LIB = re.compile(r"(^|/)(libs?|libraries)/", re.I)
_LUACHECK = re.compile(r"^(.+?):(\d+):(\d+): \((\w)(\d+)\) (.*)$")


@dataclass
class Section:
    name: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    skipped: str | None = None


@dataclass
class Report:
    addon: str
    sections: list[Section] = field(default_factory=list)

    @property
    def errors(self) -> int:
        return sum(len(s.errors) for s in self.sections)

    @property
    def warnings(self) -> int:
        return sum(len(s.warnings) for s in self.sections)


def discover() -> list[Path]:
    return sorted(p.parent for p in ADDONS_DIR.glob("*/.pkgmeta"))


def resolve_addon(name: str) -> Path:
    p = Path(name).expanduser()
    if p.is_dir():
        return p.resolve()
    q = ADDONS_DIR / name
    if q.is_dir():
        return q.resolve()
    raise FileNotFoundError(f"no addon {name!r} (looked in . and {ADDONS_DIR})")


def _syntax(addon: Path) -> Section:
    s = Section("syntax")
    files = toc.lua_files(addon)
    with tempfile.NamedTemporaryFile("wb", suffix=".lua") as tmp:
        for f in files:
            data = f.read_bytes()
            # luac5.1 rejects a UTF-8 BOM that the client tolerates
            tmp.seek(0)
            tmp.truncate()
            tmp.write(data.removeprefix(b"\xef\xbb\xbf"))
            tmp.flush()
            p = subprocess.run(
                ["luac5.1", "-p", tmp.name], capture_output=True, text=True, errors="replace"
            )
            if p.returncode:
                s.errors.append(p.stderr.strip().replace(tmp.name, str(f.relative_to(addon))))
    s.notes.append(f"{len(files)} files parsed")
    return s


def _luacheck(addon: Path) -> Section:
    s = Section("lint")
    files = [
        str(f.relative_to(addon))
        for f in toc.lua_files(addon)
        if not _LIB.search(str(f.relative_to(addon)))
    ]
    if not files:
        s.skipped = "no Lua outside Libs/"
        return s
    # run inside the addon so a repo-local .luacheckrc wins over ../.luacheckrc
    p = subprocess.run(
        ["luacheck", "--formatter", "plain", "--codes", "--no-color", *files],
        cwd=addon,
        capture_output=True,
        text=True,
        errors="replace",
    )
    for line in p.stdout.splitlines():
        m = _LUACHECK.match(line)
        if not m:
            continue
        msg = f"{m.group(1)}:{m.group(2)} {m.group(4)}{m.group(5)} {m.group(6)}"
        if m.group(4) == "E" or m.group(5) == "113":
            s.errors.append(msg)
        else:
            s.warnings.append(msg)
    return s


def _toc(addon: Path, idx: Index) -> Section:
    s = Section("toc")
    for f in toc.lint(addon, idx.current_interfaces()):
        {"error": s.errors, "warn": s.warnings, "info": s.notes}[f.level].append(
            f"{f.where}: {f.message}"
        )
    return s


def _api(addon: Path) -> Section:
    s = Section("api")
    r = scan.scan(addon)
    s.errors += [f"PARSE {e}" for e in r["parse_errors"]]
    for flavor, fr in r["flavors"].items():
        for m in fr["missing"]:
            s.errors.append(
                f"[{flavor}] {m['at']} {m['name']} does not exist here "
                f"(exists in {', '.join(m['exists_in'])})"
            )
        for m in fr["deprecated_shim"]:
            s.warnings.append(f"[{flavor}] {m['at']} {m['name']} is only a deprecation shim")
        for m in fr["methods_missing"]:
            s.notes.append(
                f"[{flavor}] {m['at']} :{m['method']}() not on any widget here (receiver unknown)"
            )
        if fr.get("guarded_deprecated"):
            s.notes.append(
                f"[{flavor}] guarded fallback to deprecated: {', '.join(sorted(fr['guarded_deprecated']))}"
            )
        if fr["unknown"]:
            s.notes.append(f"[{flavor}] unknown everywhere: {', '.join(fr['unknown'])}")
    return s


def _types(addon: Path, flavors: list[str]) -> Section:
    s = Section("types")
    if not luals.available():
        s.skipped = "lua-language-server not installed (wowdev setup)"
        return s
    flavor = flavors[0] if flavors else "live"
    for d in luals.check(addon, flavor):
        s.warnings.append(f"{d.file}:{d.line} [{d.code}] {d.message[:180]}")
    s.notes.append(f"FrameXML types from {flavor}; advisory only")
    return s


def _package(addon: Path) -> Section:
    s = Section("package")
    r = package.build(addon)
    for f in r.findings:
        {"error": s.errors, "warn": s.warnings, "info": s.notes}[f.level].append(
            f"{f.where}: {f.message}"
        )
    if r.zip:
        s.notes.append(f"{r.zip.name}: {len(r.files)} files")
    return s


def run(addon: Path, types: bool = False, pkg: bool = False) -> Report:
    idx = Index()
    rep = Report(addon.name)
    rep.sections.append(_syntax(addon))
    rep.sections.append(_luacheck(addon))
    rep.sections.append(_toc(addon, idx))
    rep.sections.append(_api(addon))
    if types:
        flavors = sorted({f for t in scan.scan(addon)["tocs"] for f in t["flavors"]})
        rep.sections.append(_types(addon, flavors))
    if pkg:
        rep.sections.append(_package(addon))
    return rep


def format_report(rep: Report, verbose: bool = False, limit: int = 25) -> str:
    out = [f"=== {rep.addon} ==="]
    for s in rep.sections:
        if s.skipped:
            out.append(f"  {s.name:8} skipped: {s.skipped}")
            continue
        status = "FAIL" if s.errors else ("warn" if s.warnings else "ok")
        out.append(
            f"  {s.name:8} {status:4}  {len(s.errors)} errors, {len(s.warnings)} warnings"
            + (f"  ({'; '.join(s.notes[:1])})" if s.notes and not verbose else "")
        )
        for e in s.errors[:limit]:
            out.append(f"      x {e}")
        if len(s.errors) > limit:
            out.append(f"      … {len(s.errors) - limit} more errors")
        shown = s.warnings if verbose else s.warnings[: min(limit, 8)]
        for w in shown:
            out.append(f"      ! {w}")
        if len(s.warnings) > len(shown):
            out.append(f"      … {len(s.warnings) - len(shown)} more warnings (-v)")
        if verbose:
            out += [f"      i {n}" for n in s.notes]
    return "\n".join(out)

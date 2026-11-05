"""TOC-level tooling for an addon repo: lint, version bump, interface update.

An addon here is a git repo whose top level holds one or more TOCs
(`<Name>.toc`, `<Name>_<Flavor>.toc`, `<Name>-<Flavor>.toc`) and a `.pkgmeta`
whose `package-as:` is the folder name the client installs it under.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from pathlib import Path

from .build import _ci_path

SEMVER = re.compile(r"\d+\.\d+\.\d+")
_HEADER = re.compile(r"^##\s*([\w-]+)\s*:\s*(.*?)\s*$")
_LUA_VERSION = re.compile(r'^(\s*local\s+VERSION\s*=\s*")([^"]*)(")', re.M)
# Interface\AddOns\<Folder>\<path>, ending at a quote, whitespace, or the
# ":size" / "|t" of an inline |T...|t texture escape
_ADDON_PATH = re.compile(r"Interface[\\/]+AddOns[\\/]+([^\\/\"'\s|:]+)[\\/]+([^\"'\s\]|:]+)", re.I)
_TEXTURE_EXTS = ("", ".tga", ".blp", ".png", ".jpg")


@dataclass
class Finding:
    level: str  # error | warn | info
    where: str
    message: str

    def __str__(self) -> str:
        mark = {"error": "x", "warn": "!", "info": "i"}[self.level]
        return f"{mark} {self.where}: {self.message}"


@dataclass
class Toc:
    path: Path
    headers: dict[str, str]
    files: list[tuple[int, str]]  # (line number, listed path) — conditions stripped
    lines: list[str] = field(repr=False, default_factory=list)

    @property
    def interfaces(self) -> list[int]:
        return [int(x) for x in re.findall(r"\d+", self.headers.get("interface", ""))]


def read_toc(path: Path) -> Toc:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    lines = text.splitlines()
    headers, files = {}, []
    for i, ln in enumerate(lines, 1):
        if m := _HEADER.match(ln):
            headers[m.group(1).lower()] = m.group(2)
        elif ln.strip() and not ln.lstrip().startswith("#"):
            files.append((i, re.sub(r"\s*\[[^\]]*\]", "", ln).strip()))
    return Toc(path, headers, files, lines)


def package_as(addon: Path) -> str:
    pm = addon / ".pkgmeta"
    if pm.exists():
        m = re.search(r"^package-as:\s*(\S+)", pm.read_text(errors="replace"), re.M)
        if m:
            return m.group(1)
    return addon.name


def tocs(addon: Path) -> list[Toc]:
    return [read_toc(p) for p in sorted(addon.glob("*.toc"))]


def lua_files(addon: Path) -> list[Path]:
    return sorted(
        p for p in addon.rglob("*.lua") if ".git" not in p.parts and ".release" not in p.parts
    )


def changelog_version(addon: Path) -> str | None:
    cl = addon / "CHANGELOG.md"
    if not cl.exists():
        return None
    for ln in cl.read_text(errors="replace").splitlines():
        if ln.startswith("## "):
            m = SEMVER.search(ln)
            return m.group(0) if m else None
    return None


# ------------------------------------------------------------------ lint


def lint(addon: Path, current_interfaces: dict[int, int] | None = None) -> list[Finding]:
    """Static checks on TOCs and packaging that nothing else here covers.

    current_interfaces maps a TOC's interface "bucket" to the client's current
    number (see scan._flavor_key_for_interface); None skips that check."""
    out: list[Finding] = []
    addon = addon.resolve()
    pkg = package_as(addon)
    ts = tocs(addon)
    if not ts:
        return [Finding("error", addon.name, "no .toc files")]

    # one version everywhere
    versions = {t.path.name: t.headers.get("version") for t in ts}
    distinct = {v for v in versions.values()}
    if None in distinct:
        for n, v in versions.items():
            if v is None:
                out.append(Finding("error", n, "no ## Version"))
    if len(distinct - {None}) > 1:
        out.append(Finding("error", "TOCs", f"## Version differs: {versions}"))
    version = next((v for v in versions.values() if v), None)

    for p in lua_files(addon):
        text = p.read_text(encoding="utf-8", errors="replace")
        for m in _LUA_VERSION.finditer(text):
            if version and m.group(2) != version:
                line = text.count("\n", 0, m.start()) + 1
                out.append(
                    Finding(
                        "error",
                        f"{p.relative_to(addon)}:{line}",
                        f'local VERSION = "{m.group(2)}" but TOCs say {version}',
                    )
                )

    cv = changelog_version(addon)
    if version and (addon / "CHANGELOG.md").exists() and cv != version:
        out.append(
            Finding(
                "warn",
                "CHANGELOG.md",
                f"newest entry is {cv or '(no version)'}, TOCs say {version}",
            )
        )

    # IDs used by the release pipeline
    for key in ("x-curse-project-id", "x-wago-id"):
        vals = {t.headers.get(key) for t in ts}
        if None in vals and len(vals) > 1:
            missing = [t.path.name for t in ts if key not in t.headers]
            out.append(Finding("warn", "TOCs", f"## {key} missing from {missing}"))
        elif len(vals - {None}) > 1:
            out.append(Finding("error", "TOCs", f"## {key} differs between TOCs: {vals}"))

    file_sets = {}
    for t in ts:
        name = t.path.name
        if not t.interfaces:
            out.append(Finding("error", name, "no ## Interface"))
        if current_interfaces is not None:
            from .scan import bucket

            for i in t.interfaces:
                cur = current_interfaces.get(bucket(i))
                if cur and i < cur:
                    out.append(
                        Finding(
                            "warn",
                            name,
                            f"Interface {i} < current client {cur} (shown as out of date)",
                        )
                    )
        listed = set()
        for ln, f in t.files:
            listed.add(f.replace("\\", "/").lower())
            if f.lower().endswith("bindings.xml"):
                out.append(
                    Finding(
                        "warn",
                        f"{name}:{ln}",
                        "Bindings.xml is loaded automatically; listing it loads it twice",
                    )
                )
            p = _ci_path(addon, f)
            if p is None:
                out.append(Finding("error", f"{name}:{ln}", f"listed file does not exist: {f}"))
            elif str(p.relative_to(addon)).replace("\\", "/") != f.replace("\\", "/"):
                out.append(
                    Finding(
                        "warn",
                        f"{name}:{ln}",
                        f"case differs from disk ({p.relative_to(addon)}); fine on "
                        "Windows, breaks on case-sensitive installs",
                    )
                )
        file_sets[name] = listed
        icon = t.headers.get("icontexture")
        if icon and not icon.isdigit():
            out += _check_addon_path(addon, pkg, icon, f"{name} IconTexture")

    if len({frozenset(s) for s in file_sets.values()}) > 1:
        base = max(file_sets.values(), key=len)
        for n, s in file_sets.items():
            if s != base and (missing := sorted(base - s)):
                out.append(
                    Finding(
                        "info",
                        n,
                        f"lists {len(s)} files vs {len(base)} in another TOC; "
                        f"missing {missing[:5]}{'…' if len(missing) > 5 else ''}",
                    )
                )

    # Interface\AddOns\<Name>\... paths in Lua/XML must use package-as
    for p in [*lua_files(addon), *sorted(addon.rglob("*.xml"))]:
        if ".git" in p.parts or re.search(r"(^|/)libs?/", str(p.relative_to(addon)), re.I):
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        for m in _ADDON_PATH.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            out += _check_addon_path(addon, pkg, m.group(0), f"{p.relative_to(addon)}:{line}")
    return out


def _check_addon_path(addon: Path, pkg: str, ref: str, where: str) -> list[Finding]:
    m = _ADDON_PATH.search(ref)
    if not m:
        return []
    folder, rest = m.group(1), re.sub(r"\\+", "/", m.group(2).rstrip(";,)"))
    if folder != pkg:
        if folder.lower() == addon.name.lower() or folder.lower() == pkg.lower().rstrip("+"):
            return [
                Finding(
                    "error",
                    where,
                    f"path uses folder '{folder}' but the addon installs as '{pkg}' "
                    "(.pkgmeta package-as) — this file will not be found",
                )
            ]
        return []  # another addon's or Blizzard's path: not ours to check
    if "%" in rest or ".." in rest or "{" in rest:
        return []  # built at runtime
    for ext in _TEXTURE_EXTS:
        if _ci_path(addon, rest + ext):
            return []
    return [Finding("error", where, f"{rest} does not exist in the repo")]


# ------------------------------------------------------------------ rewrite helpers


@dataclass
class Edit:
    path: Path
    old: str
    new: str

    def diff(self, root: Path) -> str:
        rel = str(self.path.relative_to(root))
        return "".join(
            difflib.unified_diff(
                self.old.splitlines(True), self.new.splitlines(True), f"a/{rel}", f"b/{rel}", n=0
            )
        )


def _set_header(text: str, key: str, value: str) -> str:
    pat = re.compile(rf"^(##\s*{re.escape(key)}\s*:\s*).*?(\s*)$", re.M | re.I)
    if pat.search(text):
        return pat.sub(lambda m: f"{m.group(1)}{value}{m.group(2)}", text, count=1)
    return f"## {key}: {value}\n{text}"


def plan_bump(addon: Path, version: str) -> list[Edit]:
    """Set ## Version in every TOC, every `local VERSION = "…"`, and add a
    CHANGELOG heading in the file's existing style if the version is new."""
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[\w.]+)?", version):
        raise ValueError(f"not a version: {version!r} (want X.Y.Z)")
    addon = addon.resolve()
    edits = []
    for t in tocs(addon):
        old = t.path.read_text(encoding="utf-8-sig")
        new = _set_header(old, "Version", version)
        if new != old:
            edits.append(Edit(t.path, old, new))
    for p in lua_files(addon):
        old = p.read_text(encoding="utf-8", errors="surrogateescape")
        new = _LUA_VERSION.sub(lambda m: f"{m.group(1)}{version}{m.group(3)}", old)
        if new != old:
            edits.append(Edit(p, old, new))
    cl = addon / "CHANGELOG.md"
    if cl.exists() and changelog_version(addon) != version:
        old = cl.read_text()
        lines = old.splitlines(True)
        idx = next((i for i, ln in enumerate(lines) if ln.startswith("## ")), len(lines))
        prev = lines[idx].rstrip("\n") if idx < len(lines) else f"## v{version}"
        heading = _heading_like(prev, version)
        lines[idx:idx] = [heading + "\n", "\n", "- \n", "\n"]
        edits.append(Edit(cl, old, "".join(lines)))
    return edits


def _heading_like(prev: str, version: str) -> str:
    """New changelog heading in the same style as the previous one."""
    import datetime

    today = datetime.date.today().isoformat()
    old = SEMVER.search(prev)
    if not old:
        return f"## v{version}"
    h = prev[: old.start()] + version + prev[old.end() :]
    h = re.sub(r"\d{4}-\d{2}-\d{2}", today, h)
    h = h.replace(f"/{old.group(0)})", f"/{version})")  # link targets like tree/1.4.10
    return h


def plan_interface(addon: Path, current: dict[int, int]) -> list[Edit]:
    """Raise each TOC's ## Interface to the current client number of the same
    flavor. Numbers for clients the index doesn't cover are left alone."""
    from .scan import bucket

    edits = []
    for t in tocs(addon.resolve()):
        old = t.path.read_text(encoding="utf-8-sig")
        new_ifs = [str(max(i, current.get(bucket(i), i))) for i in t.interfaces]
        if new_ifs != [str(i) for i in t.interfaces]:
            new = _set_header(old, "Interface", ", ".join(new_ifs))
            edits.append(Edit(t.path, old, new))
    return edits


def apply(edits: list[Edit]) -> None:
    for e in edits:
        # write through the same encoding we read with: never re-encode Lua sources
        e.path.write_text(e.new, encoding="utf-8", errors="surrogateescape")

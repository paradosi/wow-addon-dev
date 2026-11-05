"""Build an addon's release zip locally with BigWigsMods/packager, never uploading.

The same release.sh the GitHub workflow runs (pinned to a tag, under
~/data/wowdev/tools/packager), run with -d so nothing reaches CurseForge or
Wago. The result is inspected for what would otherwise only be seen after a
tag: missing TOC-listed files, files .pkgmeta meant to strip, and a changelog
generated from git log instead of the maintained CHANGELOG.md.
"""

from __future__ import annotations

import re
import subprocess
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .build import _ci_path
from .config import DATA_DIR
from .toc import Finding, package_as, tocs

PACKAGER_TAG = "v2.7.0"
PACKAGER = DATA_DIR / "tools" / "packager" / "release.sh"
RELEASES = DATA_DIR / "releases"
# never belongs in a shipped zip
_FORBIDDEN = re.compile(
    r"(^|/)(CLAUDE\.md|\.env(\..*)?|\.luacheckrc|\.luarc\.json|\.vscode/.*|.*\.(pem|key))$", re.I
)


@dataclass
class PackageResult:
    addon: str
    zip: Path | None
    dir: Path | None
    version: str | None
    files: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    log_tail: str = ""


def _pkgmeta(addon: Path) -> str:
    p = addon / ".pkgmeta"
    return p.read_text(errors="replace") if p.exists() else ""


def build(addon: Path, out: Path | None = None) -> PackageResult:
    if not PACKAGER.exists():
        raise FileNotFoundError(f"packager not installed at {PACKAGER} — run `wowdev setup`")
    addon = addon.resolve()
    out = (out or RELEASES / addon.name).resolve()
    out.mkdir(parents=True, exist_ok=True)
    for old in out.iterdir():  # one build per directory: stale zips mislead
        if old.is_dir():
            subprocess.run(["rm", "-rf", str(old)], check=True)
        else:
            old.unlink()
    p = subprocess.run(
        ["bash", str(PACKAGER), "-d", "-t", str(addon), "-r", str(out)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    pkg = package_as(addon)
    zips = sorted(out.glob("*.zip"))
    res = PackageResult(
        addon=addon.name,
        zip=zips[0] if zips else None,
        dir=out / pkg if (out / pkg).is_dir() else None,
        version=None,
        log_tail="\n".join(p.stdout.splitlines()[-15:] + p.stderr.splitlines()[-10:]),
    )
    if p.returncode or not res.zip:
        res.findings.append(
            Finding("error", "packager", f"exit {p.returncode}: {p.stderr.strip()[-400:]}")
        )
        return res
    m = re.search(r"Creating archive: \S+ \(([^)]+)\)", p.stdout)
    res.version = m.group(1) if m else None
    with zipfile.ZipFile(res.zip) as z:
        res.files = sorted(n for n in z.namelist() if not n.endswith("/"))
    res.findings = inspect(addon, res)
    return res


def inspect(addon: Path, res: PackageResult) -> list[Finding]:
    out: list[Finding] = []
    pkg = package_as(addon)
    tops = {f.split("/", 1)[0] for f in res.files}
    if tops != {pkg}:
        out.append(
            Finding("error", "zip", f"top-level folders {sorted(tops)}, expected only {pkg}/")
        )
    inside = {f.split("/", 1)[1] for f in res.files if "/" in f}
    lower = {f.lower() for f in inside}

    for t in tocs(addon):
        if f"{t.path.name}".lower() not in lower:
            out.append(Finding("error", "zip", f"{t.path.name} is not in the package"))
        for ln, f in t.files:
            if f.replace("\\", "/").lower() not in lower and _ci_path(addon, f) is not None:
                out.append(
                    Finding(
                        "error",
                        f"{t.path.name}:{ln}",
                        f"{f} exists in the repo but was stripped from the package",
                    )
                )

    meta = _pkgmeta(addon)
    ignored = (
        re.findall(r"^\s*-\s*(\S+)", meta.split("ignore:", 1)[1], re.M) if "ignore:" in meta else []
    )
    for ig in ignored:
        ig = ig.strip("\"'").rstrip("/")
        if any(f == ig or f.startswith(ig + "/") for f in inside) and ig != "CHANGELOG.md":
            out.append(Finding("warn", ".pkgmeta", f"ignore: {ig} is still in the package"))
    for f in inside:
        if _FORBIDDEN.search(f):
            out.append(Finding("error", "zip", f"{f} must not ship"))

    if "manual-changelog" not in meta and "CHANGELOG.md" in inside:
        text = (res.dir / "CHANGELOG.md").read_text(errors="replace") if res.dir else ""
        leaks = [s for s in ("Claude-Session:", "Co-Authored-By:", "claude.ai/code") if s in text]
        out.append(
            Finding(
                "warn",
                ".pkgmeta",
                "no manual-changelog: the packager generated CHANGELOG.md from git log, and that is "
                "what CurseForge/Wago show as release notes"
                + (f" — it contains {', '.join(leaks)} trailers" if leaks else "")
                + ". Add `manual-changelog: CHANGELOG.md` to use the maintained one.",
            )
        )
    return out


def format_result(r: PackageResult) -> str:
    lines = [f"# {r.addon} — package {r.version or '?'}"]
    if r.zip:
        lines.append(f"zip: {r.zip} ({r.zip.stat().st_size / 1024:.1f} KiB, {len(r.files)} files)")
    if r.dir:
        lines.append(f"dir: {r.dir}")
    lines += [str(f) for f in r.findings] or ["clean: package matches the TOCs and .pkgmeta"]
    if any(f.level == "error" for f in r.findings) and r.log_tail:
        lines += ["", "packager output (tail):", r.log_tail]
    return "\n".join(lines)

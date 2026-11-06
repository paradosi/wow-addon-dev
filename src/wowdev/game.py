"""The live-game loop on the Windows PC that runs WoW: install a build into a
client's AddOns folder, and read the Lua errors BugGrabber saved back out.

Everything goes over `ssh <host> powershell -EncodedCommand` (the script as
base64 UTF-16LE, so no quoting survives to bite), and files come back as
base64 so the Windows console code page never touches them.

    deploy   package.build() the addon, upload the zip, back up whatever was
             installed (only the first time: later deploys carry a marker and
             are simply replaced), expand it into each targeted client
    restore  put that first backup back
    errors   newest !BugGrabber.lua for a client -> errors, filtered to an addon

BugGrabber writes its SavedVariables at logout or /reload, like any addon, so
errors from a session still running are not visible until then.
"""

from __future__ import annotations

import base64
import datetime
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .config import DUMP_LUA, LUA

HOST = os.environ.get("WOWDEV_GAME_HOST", "northpc")
WOW_ROOT = os.environ.get(
    "WOWDEV_WOW_ROOT", r"C:\Program Files (x86)\Battle.net Games\World of Warcraft"
)
# WoW Forever installs as its own product with its own launcher, in a
# separate tree (found on northpc 2026-11-06). That tree also holds fresh
# _anniversary_/_classic_era_ clients, but the ones with jim's settings and
# addons are under WOW_ROOT, so only Forever is read from here.
FOREVER_ROOT = os.environ.get("WOWDEV_FOREVER_ROOT", r"C:\Program Files (x86)\World of Warcraft")
# flavor key -> client folder name, under CLIENT_ROOTS[flavor] (default WOW_ROOT)
CLIENT_DIRS = {
    "live": "_retail_",
    "classic_anniversary": "_anniversary_",
    "classic_era": "_classic_era_",
    "forever": "_forever_",
}
CLIENT_ROOTS = {"forever": FOREVER_ROOT}


def client_dir(flavor: str) -> str:
    return rf"{CLIENT_ROOTS.get(flavor, WOW_ROOT)}\{CLIENT_DIRS[flavor]}"
MARKER = ".wowdev-deploy"  # written into every folder deploy installs
_NOISE = re.compile(
    r"post-quantum|store now, decrypt later|server may need to be upgraded|openssh\.com/pq"
)


class GameHostError(RuntimeError):
    pass


def ps(script: str, timeout: int = 120) -> str:
    """Run a PowerShell script on HOST and return its stdout."""
    full = "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'\n" + script
    enc = base64.b64encode(full.encode("utf-16-le")).decode()
    p = subprocess.run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            HOST,
            f"powershell -NoProfile -NonInteractive -EncodedCommand {enc}",
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
    )
    out = "\n".join(
        ln
        for ln in p.stdout.replace("\r", "").splitlines()
        if not ln.startswith(("#< CLIXML", "<Objs"))
    )
    if p.returncode:
        err = "\n".join(ln for ln in p.stderr.splitlines() if not _NOISE.search(ln))
        raise GameHostError(f"{HOST}: exit {p.returncode}: {err.strip()[-800:] or out[-800:]}")
    return out


def _q(s: str) -> str:
    """A PowerShell single-quoted string literal."""
    return "'" + s.replace("'", "''") + "'"


def _addons_dir(flavor: str) -> str:
    return rf"{client_dir(flavor)}\Interface\AddOns"


def installed_clients() -> list[str]:
    script = "; ".join(
        f"if (Test-Path {_q(client_dir(k))}) {{ {_q(k)} }}" for k in CLIENT_DIRS
    )
    return [ln.strip() for ln in ps(script).splitlines() if ln.strip() in CLIENT_DIRS]


_ROOT_PRODUCTS = {FOREVER_ROOT: ("wowf",)}


def installed_builds() -> dict[str, str]:
    """{product: version} from the install's .build.info, the launcher's own
    record of what each client is patched to (e.g. wow_anniversary: 2.5.6.70577)."""
    out: dict[str, str] = {}
    for root in dict.fromkeys([WOW_ROOT, *CLIENT_ROOTS.values()]):
        p = _q(root + r"\.build.info")
        raw = ps(f"if (Test-Path {p}) {{ Get-Content -Raw {p} }}")
        lines = [ln for ln in raw.splitlines() if "|" in ln]
        if not lines:
            continue
        head = [h.split("!")[0] for h in lines[0].split("|")]
        for ln in lines[1:]:
            row = dict(zip(head, ln.split("|"), strict=False))
            product = row.get("Product")
            if not product or not row.get("Version"):
                continue
            # a product in a root that does not supply that flavor's client
            # (Forever's tree has its own TBC/Era) must not hide the real one
            if root != WOW_ROOT and product not in _ROOT_PRODUCTS.get(root, ()):
                continue
            out[product] = row["Version"]
    return out


def client_builds() -> list[dict]:
    """Each installed client's build next to the build the index describes."""
    from .config import FLAVORS
    from .index import Index

    fl = Index().flavors
    by_product = {f.wago_product: f.key for f in FLAVORS.values() if f.wago_product}
    out = []
    for product, version in sorted(installed_builds().items()):
        key = by_product.get(product)
        row = {"product": product, "installed": version, "flavor": key}
        if key and key in fl:
            row["indexed"] = f"{fl[key]['version']}.{fl[key]['build']}"
            row["current"] = row["indexed"] == version
            if not row["current"]:
                v = lambda s: tuple(int(x) for x in s.split(".") if x.isdigit())  # noqa: E731
                row["behind"] = "client" if v(version) < v(row["indexed"]) else "index"
        out.append(row)
    return out


def format_client_builds(rows: list[dict]) -> str:
    out = [f"# game clients on {HOST}"]
    for r in rows:
        if not r["flavor"]:
            out.append(f"  {r['product']:18} {r['installed']:16} not indexed")
        elif r["current"]:
            out.append(f"  {r['product']:18} {r['installed']:16} = index ({r['flavor']})")
        elif r["behind"] == "client":
            out.append(
                f"! {r['product']:18} {r['installed']:16} older than the live build "
                f"{r['indexed']} ({r['flavor']}): patch the client in Battle.net before testing"
            )
        else:
            out.append(
                f"! {r['product']:18} {r['installed']:16} newer than the index's "
                f"{r['indexed']} ({r['flavor']}): run `wowdev refresh` once upstream has it"
            )
    return "\n".join(out)


def present_in(pkg: str, flavors: list[str]) -> set[str]:
    """Flavors among `flavors` whose client already has Interface/AddOns/<pkg>."""
    script = "; ".join(
        f"if (Test-Path (Join-Path {_q(_addons_dir(f))} {_q(pkg)})) {{ {_q(f)} }}"
        for f in flavors
        if f in CLIENT_DIRS
    )
    return {ln.strip() for ln in ps(script).splitlines()} & set(flavors) if script else set()


def running_clients() -> list[str]:
    out = ps(
        "Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -like 'Wow*' } | ForEach-Object { $_.ProcessName }"
    )
    return sorted({ln.strip() for ln in out.splitlines() if ln.strip()})


def addon_flavors(addon: Path) -> list[str]:
    from .index import Index
    from .scan import read_toc

    idx = Index()
    found = {f for t in sorted(addon.glob("*.toc")) for f in read_toc(t, idx).flavors}
    return [f for f in CLIENT_DIRS if f in found]


# ------------------------------------------------------------------ deploy


@dataclass
class DeployResult:
    addon: str
    package_as: str
    zip: Path | None = None
    installed: dict[str, str] = field(default_factory=dict)  # flavor -> version installed
    backups: dict[str, str] = field(default_factory=dict)  # flavor -> backup path (first deploy)
    skipped: dict[str, str] = field(default_factory=dict)  # flavor -> why
    running: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def deploy(addon: Path, flavors: list[str] | None = None) -> DeployResult:
    from . import package

    addon = addon.resolve()
    pkg = package.package_as(addon)
    res = DeployResult(addon.name, pkg)
    built = package.build(addon)
    bad = [str(f) for f in built.findings if f.level == "error"]
    if bad or not built.zip:
        res.errors = bad or ["packager produced no zip"]
        return res
    res.zip = built.zip

    have = set(installed_clients())
    if flavors:
        want = flavors
    else:
        # by default only replace an existing install: putting an addon into a
        # client that never had it is a choice, made with --flavor
        targeted = addon_flavors(addon)
        want = sorted(
            present_in(pkg, [f for f in targeted if f in have]), key=list(CLIENT_DIRS).index
        )
        for f in targeted:
            if f not in want:
                res.skipped[f] = "not installed in this client (pass --flavor to add it)"
    targets = []
    for f in want:
        if f not in CLIENT_DIRS:
            res.skipped[f] = "no client folder known for this flavor (game.CLIENT_DIRS)"
        elif f not in have:
            res.skipped[f] = f"{CLIENT_DIRS[f]} is not installed on {HOST}"
        else:
            targets.append(f)
    if not targets:
        return res

    remote_zip = f"wowdev-{pkg}.zip"
    up = subprocess.run(
        ["scp", "-q", "-o", "BatchMode=yes", str(built.zip), f"{HOST}:{remote_zip}"],
        capture_output=True,
        text=True,
    )
    if up.returncode:
        res.errors.append(f"upload failed: {up.stderr.strip()[-400:]}")
        return res

    for f in targets:
        a = _addons_dir(f)
        script = f"""
$a = {_q(a)}; $pkg = {_q(pkg)}; $dst = Join-Path $a $pkg
$zip = Join-Path $HOME {_q(remote_zip)}
if (Test-Path $dst) {{
  if (Test-Path (Join-Path $dst {_q(MARKER)})) {{ Remove-Item -Recurse -Force $dst }}
  else {{
    $bk = Join-Path $HOME ('wowdev-backups\\' + {_q(f)} + '-' + (Get-Date -Format yyyyMMdd-HHmmss))
    New-Item -ItemType Directory -Force $bk | Out-Null
    Move-Item $dst (Join-Path $bk $pkg)
    'BACKUP ' + (Join-Path $bk $pkg)
  }}
}}
Expand-Archive -Path $zip -DestinationPath $a -Force
Set-Content -Path (Join-Path $dst {_q(MARKER)}) -Value ({_q(built.version or "?")} + ' ' + (Get-Date -Format s))
$toc = Join-Path $dst ($pkg + '.toc')
if (Test-Path $toc) {{ 'VERSION ' + ((Select-String -Path $toc -Pattern '^## Version:\\s*(.*)').Matches[0].Groups[1].Value) }}
"""
        try:
            for ln in ps(script).splitlines():
                if ln.startswith("BACKUP "):
                    res.backups[f] = ln[7:].strip()
                elif ln.startswith("VERSION "):
                    res.installed[f] = ln[8:].strip()
            res.installed.setdefault(f, built.version or "?")
        except GameHostError as e:
            res.errors.append(f"[{f}] {e}")
    try:
        res.running = running_clients()
    except GameHostError:
        pass
    return res


def restore(addon: Path, flavors: list[str] | None = None) -> dict[str, str]:
    """Put back the install that the first deploy backed up."""
    from .package import package_as

    pkg = package_as(addon.resolve())
    out = {}
    for f in flavors or addon_flavors(addon):
        if f not in CLIENT_DIRS:
            continue
        script = f"""
$a = {_q(_addons_dir(f))}; $pkg = {_q(pkg)}; $dst = Join-Path $a $pkg
$bk = Get-ChildItem (Join-Path $HOME 'wowdev-backups') -Directory -Filter ({_q(f)} + '-*') -ErrorAction SilentlyContinue |
  Where-Object {{ Test-Path (Join-Path $_.FullName $pkg) }} | Sort-Object Name -Descending | Select-Object -First 1
if (-not $bk) {{ 'NONE'; return }}
if (Test-Path $dst) {{
  if (-not (Test-Path (Join-Path $dst {_q(MARKER)}))) {{ 'NOTOURS'; return }}
  Remove-Item -Recurse -Force $dst
}}
Move-Item (Join-Path $bk.FullName $pkg) $dst
'RESTORED ' + $bk.FullName
"""
        r = ps(script).strip().splitlines()
        last = r[-1] if r else ""
        out[f] = {
            "NONE": "no backup to restore",
            "NOTOURS": "installed copy is not a wowdev deploy; left alone",
        }.get(last, last.replace("RESTORED ", "restored from "))
    return out


def format_deploy(r: DeployResult) -> str:
    lines = [f"# deploy {r.addon} (installs as {r.package_as}) to {HOST}"]
    if r.zip:
        lines.append(f"zip: {r.zip.name}")
    for f, v in r.installed.items():
        lines.append(f"  {f:20} installed {v}")
        if f in r.backups:
            lines.append(f"  {'':20} previous install kept at {r.backups[f]}")
    for f, why in r.skipped.items():
        lines.append(f"  {f:20} skipped: {why}")
    lines += [f"  x {e}" for e in r.errors]
    if r.running and r.installed:
        lines.append(f"running: {', '.join(r.running)} -- /reload to load the new build")
    return "\n".join(lines)


# ------------------------------------------------------------------ errors


def _bug_grabber(flavor: str) -> tuple[str, str, dict]:
    """(path, last write time, BugGrabberDB) for the newest account's file."""
    wtf = rf"{client_dir(flavor)}\WTF\Account"
    script = f"""
$f = Get-ChildItem {_q(wtf)} -Recurse -Filter '!BugGrabber.lua' -ErrorAction SilentlyContinue |
  Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $f) {{ 'NONE'; return }}
'PATH ' + $f.FullName
'TIME ' + $f.LastWriteTime.ToString('s')
[Convert]::ToBase64String([IO.File]::ReadAllBytes($f.FullName))
"""
    lines = ps(script, timeout=180).splitlines()
    if not lines or lines[0].strip() == "NONE":
        raise GameHostError(
            f"no !BugGrabber.lua under {wtf} (is BugGrabber installed and has it saved?)"
        )
    path = lines[0][5:].strip()
    when = lines[1][5:].strip()
    data = base64.b64decode("".join(lines[2:]))
    with tempfile.TemporaryDirectory() as td:
        lf = Path(td) / "bg.lua"
        lf.write_bytes(data)
        p = subprocess.run(
            [LUA, str(DUMP_LUA), "res", str(lf)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    db = json.loads(p.stdout or "{}").get("BugGrabberDB") or {}
    return path, when, db


def _pkg(addon: str | None) -> str | None:
    """Installed folder name for a repo dir / name under ~/projects/wowaddons."""
    if not addon:
        return None
    from .check import ADDONS_DIR
    from .package import package_as

    d = Path(addon).expanduser()
    d = d if d.is_dir() else ADDONS_DIR / addon
    return package_as(d) if d.is_dir() else addon


# taint.log: "11/5 20:31:02.123  <headline>" then indented stack lines with the
# same timestamp prefix
_TAINT_LINE = re.compile(r"^\d+/\d+ \d+:\d+:\d+\.\d+  ?(.*)$")
_TAINT_NUM = re.compile(r"\b0x[0-9a-f]+\b|\b\d+\b")


def parse_taint(text: str) -> list[dict]:
    """Group a taint.log into entries: {time, headline, stack}."""
    out: list[dict] = []
    for raw in text.splitlines():
        m = _TAINT_LINE.match(raw)
        body = m.group(1) if m else raw
        if not body.strip():
            continue
        if body[:1].isspace() and out:
            out[-1]["stack"].append(body.strip())
        else:
            out.append(
                {"time": raw.split("  ")[0] if m else "", "headline": body.strip(), "stack": []}
            )
    return out


def taint(addon: str | None = None, flavor: str = "classic_anniversary", tail: int = 20000) -> dict:
    """The client's Logs/taint.log (last `tail` lines), grouped, filtered to
    entries naming `addon`, with repeats counted. Also reports whether the
    taintLog CVar is saved in Config.wtf."""
    if flavor not in CLIENT_DIRS:
        raise GameHostError(f"no client folder known for {flavor}")
    pkg = _pkg(addon)
    base = client_dir(flavor)
    script = f"""
$c = {_q(base + r"\WTF\Config.wtf")}
if (Test-Path $c) {{ Select-String -Path $c -Pattern '^SET taintLog' | % {{ 'CVAR ' + $_.Line }} }}
$f = {_q(base + r"\Logs\taint.log")}
if (-not (Test-Path $f)) {{ 'NONE'; return }}
'TIME ' + (Get-Item $f).LastWriteTime.ToString('s')
$t = (Get-Content $f -Tail {int(tail)} -Encoding UTF8) -join "`n"
[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($t))
"""
    lines = ps(script, timeout=180).splitlines()
    cvar = next((ln[5:].strip() for ln in lines if ln.startswith("CVAR ")), None)
    rest = [ln for ln in lines if not ln.startswith("CVAR ")]
    res: dict = {"flavor": flavor, "addon": pkg, "taintLog_cvar": cvar, "entries": []}
    if not rest or rest[0].strip() == "NONE":
        res["log"] = None
        return res
    res["log"] = base + r"\Logs\taint.log"
    res["written"] = rest[0][5:].strip()
    entries = parse_taint(base64.b64decode("".join(rest[1:])).decode("utf-8", "replace"))
    res["total"] = len(entries)
    needle = (
        re.compile(rf"(\b{re.escape(pkg)}\b|AddOns[\\/]{re.escape(pkg)}[\\/])", re.I)
        if pkg
        else None
    )
    groups: dict[str, dict] = {}
    for e in entries:
        if needle and not needle.search(e["headline"] + "\n" + "\n".join(e["stack"])):
            continue
        key = _TAINT_NUM.sub("#", e["headline"])
        g = groups.setdefault(key, {**e, "count": 0})
        g["count"] += 1
        g["last"] = e["time"]
    res["entries"] = sorted(groups.values(), key=lambda g: -g["count"])
    res["blocked"] = sum(g["count"] for g in groups.values() if "blocked" in g["headline"].lower())
    return res


def format_taint(r: dict, verbose: bool = False) -> str:
    who = r["addon"] or "all addons"
    out = [f"# taint.log, {r['flavor']}, {who}"]
    cv = r["taintLog_cvar"]
    if r["log"] is None:
        out.append("no Logs\\taint.log in this client.")
        out.append("Turn logging on in game: /console taintLog 1  (2 = also every tainted read),")
        out.append("then /reload, reproduce, and /reload or log out to flush it.")
        return "\n".join(out)
    out.append(f"{r['log']}  written {r['written']}  ({r['total']} entries in the tail read)")
    out.append(f"taintLog in Config.wtf: {cv or 'not saved (may still be on for this session)'}")
    if not r["entries"]:
        out.append(f"  nothing that names {who}")
    for g in r["entries"]:
        out.append(f"  {g['count']:>4}x  {g['headline'][:200]}   (last {g['last']})")
        for s in g["stack"][: (12 if verbose else 3)]:
            out.append(f"          {s[:200]}")
    return "\n".join(out)


def errors(
    addon: str | None = None, flavor: str = "classic_anniversary", sessions: int | None = 1
) -> dict:
    """Errors BugGrabber saved for `flavor`, newest first. `addon` is a folder
    name as installed (package-as) or a repo directory; errors whose message or
    stack mention Interface/AddOns/<it>/ are kept. sessions=None: every session."""

    if flavor not in CLIENT_DIRS:
        raise GameHostError(f"no client folder known for {flavor}")
    pkg = _pkg(addon)
    path, when, db = _bug_grabber(flavor)
    current = int(db.get("session") or 0)
    errs = db.get("errors") or []
    if isinstance(errs, dict):  # sparse arrays come back as objects
        errs = [errs[k] for k in sorted(errs, key=lambda k: int(k))]
    keep = []
    needle = re.compile(rf"AddOns[\\/]{re.escape(pkg)}[\\/]", re.I) if pkg else None
    for e in errs:
        if not isinstance(e, dict):
            continue
        s = int(e.get("session") or 0)
        if sessions is not None and s <= current - sessions:
            continue
        text = f"{e.get('message', '')}\n{e.get('stack', '')}"
        if needle and not needle.search(text):
            continue
        when_e = e.get("time", "")
        if isinstance(when_e, (int, float)):  # newer BugGrabber stores epoch seconds
            when_e = datetime.datetime.fromtimestamp(when_e).strftime("%Y/%m/%d %H:%M:%S")
        keep.append(
            {
                "message": e.get("message", ""),
                "count": e.get("counter", 1),
                "time": str(when_e),
                "session": s,
                "stack": e.get("stack", ""),
                "locals": e.get("locals", ""),
            }
        )
    keep.sort(key=lambda e: (e["session"], e["time"]), reverse=True)
    return {
        "flavor": flavor,
        "file": path,
        "saved_at": when,
        "current_session": current,
        "sessions": "all" if sessions is None else sessions,
        "addon": pkg,
        "total_in_file": len(errs),
        "errors": keep,
        "note": "BugGrabber saves at logout or /reload; errors from a session still running are not in the file yet",
    }


def format_errors(r: dict, verbose: bool = False) -> str:
    scope = f"{r['addon']} " if r["addon"] else ""
    lines = [
        f"# {scope}errors on {r['flavor']} -- {len(r['errors'])} of {r['total_in_file']} in file, "
        f"session {r['current_session']} (last {r['sessions']}), saved {r['saved_at']}"
    ]
    for e in r["errors"]:
        lines.append(f"\nx{e['count']}  session {e['session']}  {e['time']}\n  {e['message']}")
        stack = e["stack"].splitlines()
        for s in stack if verbose else stack[:6]:
            lines.append(f"    {s}")
        if verbose and e["locals"]:
            lines += ["  locals:"] + [f"    {s}" for s in e["locals"].splitlines()[:40]]
    if not r["errors"]:
        lines.append("no matching errors" + ("" if r["sessions"] == "all" else " (try --all)"))
    return "\n".join(lines)

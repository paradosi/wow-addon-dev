"""wowdev — World of Warcraft addon development CLI.

    wowdev setup                     install tools + fetch upstream + build index
    wowdev refresh                   update upstream and rebuild the index
    wowdev status [--game]           index age, each client's version/interface
                                     (--game: vs the game PC's installed builds)
    wowdev api NAME [NAME...]        availability matrix (and detail for one name)
    wowdev check [ADDON...]          the full static gate (default: every addon)
    wowdev scan ADDON...             cross-flavor API check only
    wowdev lint ADDON...             TOC / packaging lint only
    wowdev types ADDON [--flavor F]  lua-language-server type check (advisory)
    wowdev package ADDON             local packager dry run, inspect the zip
    wowdev bump ADDON X.Y.Z [-w]     version everywhere + changelog heading
    wowdev interface ADDON... [-w]   raise TOC Interface to current clients
    wowdev deploy ADDON [--flavor F] build and install into the game PC's clients
    wowdev restore ADDON [--flavor F] put back the install the first deploy replaced
    wowdev errors [ADDON] [--flavor F] [--sessions N|--all] [-v]
                                     Lua errors BugGrabber saved on the game PC
    wowdev taint [ADDON] [--flavor F] [-v]  the client's taint.log, grouped, for one addon
    wowdev spell ID|NAME [--flavor F] spell id, rank, duration and school per client
    wowdev unused [ADDON...] [-v]    dead files, media, globals, locals, libs,
                                     locale keys and duplicated code (advisory)
    wowdev mcp                       run the MCP server on stdio

ADDON is a path or a directory name under $WOWDEV_ADDONS (~/projects/wowaddons). `bump` and
`interface` print a diff and change nothing without -w/--write.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

from . import build as build_mod
from .config import LUALS_VERSION, TOOLS_DIR


def _addons(names: list[str]) -> list[Path]:
    from .check import discover, resolve_addon

    return [resolve_addon(n) for n in names] if names else discover()


def _download(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "wowdev-setup"})
    with urllib.request.urlopen(req, timeout=120) as r, dest.open("wb") as f:
        shutil.copyfileobj(r, f)


def install_tools() -> None:
    """lua-language-server and BigWigsMods/packager, pinned, under TOOLS_DIR."""
    from .package import PACKAGER_TAG

    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    luals_dir = TOOLS_DIR / f"luals-{LUALS_VERSION}"
    if not (luals_dir / "bin" / "lua-language-server").exists():
        print(f"  installing lua-language-server {LUALS_VERSION}", file=sys.stderr)
        url = (
            "https://github.com/LuaLS/lua-language-server/releases/download/"
            f"{LUALS_VERSION}/lua-language-server-{LUALS_VERSION}-linux-x64.tar.gz"
        )
        with tempfile.TemporaryDirectory() as tmp:
            tgz = Path(tmp) / "luals.tgz"
            _download(url, tgz)
            luals_dir.mkdir(parents=True, exist_ok=True)
            with tarfile.open(tgz) as t:
                t.extractall(luals_dir, filter="data")
    _relink(TOOLS_DIR / "luals", luals_dir.name)

    pk_dir = TOOLS_DIR / f"packager-{PACKAGER_TAG}"
    if not (pk_dir / "release.sh").exists():
        print(f"  installing BigWigsMods/packager {PACKAGER_TAG}", file=sys.stderr)
        pk_dir.mkdir(parents=True, exist_ok=True)
        _download(
            f"https://raw.githubusercontent.com/BigWigsMods/packager/{PACKAGER_TAG}/release.sh",
            pk_dir / "release.sh",
        )
    _relink(TOOLS_DIR / "packager", pk_dir.name)
    for tool in ("lua5.1", "luac5.1", "luacheck", "git", "zip", "rg"):
        if not shutil.which(tool):
            print(f"  ! {tool} not on PATH", file=sys.stderr)


def _relink(link: Path, target: str) -> None:
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(target)


def _edits(kind: str, addon: Path, edits, write: bool) -> int:
    from .toc import apply

    if not edits:
        print(f"{addon.name}: nothing to change")
        return 0
    for e in edits:
        print(e.diff(addon), end="")
    if write:
        apply(edits)
        print(f"{addon.name}: {kind} — wrote {len(edits)} files")
    else:
        print(f"{addon.name}: {kind} — dry run, {len(edits)} files would change (-w to write)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="wowdev",
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="\n".join(__doc__.splitlines()[2:]),
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup")
    r = sub.add_parser("refresh")
    r.add_argument("--offline", action="store_true", help="rebuild without fetching")
    stp = sub.add_parser("status")
    stp.add_argument(
        "--game", action="store_true", help="also compare the game PC's installed builds"
    )
    a = sub.add_parser("api")
    a.add_argument("names", nargs="+")
    a.add_argument("--flavor", action="append")
    c = sub.add_parser("check")
    c.add_argument("addons", nargs="*")
    c.add_argument("--types", action="store_true", help="add the LuaLS pass")
    c.add_argument("--package", action="store_true", help="add the packager dry run")
    c.add_argument("-v", "--verbose", action="store_true")
    s = sub.add_parser("scan")
    s.add_argument("addons", nargs="+")
    s.add_argument("--flavor", action="append")
    s.add_argument("--include-libs", action="store_true")
    s.add_argument("--json", action="store_true")
    li = sub.add_parser("lint")
    li.add_argument("addons", nargs="*")
    t = sub.add_parser("types")
    t.add_argument("addons", nargs="+")
    t.add_argument("--flavor", default=None)
    pk = sub.add_parser("package")
    pk.add_argument("addons", nargs="+")
    b = sub.add_parser("bump")
    b.add_argument("addon")
    b.add_argument("version")
    b.add_argument("-w", "--write", action="store_true")
    i = sub.add_parser("interface")
    i.add_argument("addons", nargs="*")
    i.add_argument("-w", "--write", action="store_true")
    dp = sub.add_parser("deploy")
    dp.add_argument("addon")
    dp.add_argument(
        "--flavor", action="append", help="repeatable; default: every flavor the TOCs target"
    )
    rs = sub.add_parser("restore")
    rs.add_argument("addon")
    rs.add_argument("--flavor", action="append")
    er = sub.add_parser("errors")
    er.add_argument("addon", nargs="?")
    er.add_argument("--flavor", default="classic_anniversary")
    er.add_argument("--sessions", type=int, default=1, help="most recent N sessions (default 1)")
    er.add_argument("--all", action="store_true", help="every session in the file")
    er.add_argument("-v", "--verbose", action="store_true", help="full stacks and locals")
    er.add_argument("--json", action="store_true")
    tn = sub.add_parser("taint")
    tn.add_argument("addon", nargs="?")
    tn.add_argument("--flavor", default="classic_anniversary")
    tn.add_argument("-v", "--verbose", action="store_true", help="longer stacks")
    tn.add_argument("--json", action="store_true")
    sp = sub.add_parser("spell")
    sp.add_argument("query", help="spell id or name")
    sp.add_argument("--flavor", action="append")
    sp.add_argument("--json", action="store_true")
    un = sub.add_parser("unused")
    un.add_argument("addons", nargs="*")
    un.add_argument("-v", "--verbose", action="store_true")
    un.add_argument("--json", action="store_true")
    sub.add_parser("mcp")
    args = ap.parse_args(argv)

    if args.cmd == "setup":
        install_tools()
        build_mod.main([])
        return 0
    if args.cmd == "refresh":
        return build_mod.main(["--offline"] if args.offline else [])
    if args.cmd == "mcp":
        from .server import main as serve

        serve()
        return 0
    if args.cmd == "status":
        from .server import wow_status

        st = wow_status()
        print(
            f"index built {st['built_at']} ({st['age_hours']} h ago)"
            + ("  — STALE, run wowdev refresh" if st["stale"] else "")
        )
        for k, f in st["flavors"].items():
            print(
                f"  {k:20} {f['label']:16} {f['version']:8} build {f['build']:6} "
                f"interface {f['interface']}"
            )
        if args.game:
            from . import game

            print(game.format_client_builds(game.client_builds()))
        return 0
    if args.cmd == "unused":
        from . import unused as unused_mod
        from .check import discover, resolve_addon

        dirs = [resolve_addon(a) for a in args.addons] if args.addons else discover()
        reports = [unused_mod.unused(d) for d in dirs]
        if args.json:
            print(json.dumps(reports, indent=1))
        else:
            print("\n\n".join(unused_mod.format_report(r, args.verbose) for r in reports))
        return 0
    if args.cmd == "spell":
        from .server import wow_spell

        res = wow_spell(args.query, args.flavor)
        if args.json:
            print(json.dumps(res, indent=1))
            return 0
        for fl, rows in res["spells"].items():
            print(f"{fl}  ({res['builds'].get(fl) or 'no spell data'})")
            if rows == "absent":
                print("    absent")
                continue
            for s in rows:
                extra = "  ".join(x for x in (s.get("duration"), s.get("school")) if x)
                print(f"  {s['id']:>7}  {s['name']}  {s['rank'] or ''}  {extra}".rstrip())
        return 0
    if args.cmd == "api":
        from .server import wow_compat, wow_lookup

        if len(args.names) == 1 and not args.flavor:
            print(json.dumps(wow_lookup(args.names[0]), indent=1))
        else:
            m = wow_compat(args.names, args.flavor)
            fl = m["flavors"]
            w = max(len(n) for n in m["matrix"]) + 2
            print("".ljust(w) + "".join(f[:16].ljust(18) for f in fl))
            for n, row in m["matrix"].items():
                print(
                    n.ljust(w)
                    + "".join(row[f].ljust(18) for f in fl)
                    + (f"  see {row['_see_also']}" if "_see_also" in row else "")
                )
        return 0
    if args.cmd == "check":
        from .check import format_report, run

        worst = 0
        for addon in _addons(args.addons):
            rep = run(addon, types=args.types, pkg=args.package)
            print(format_report(rep, verbose=args.verbose))
            worst = max(worst, 1 if rep.errors else 0)
        print("\ngate: " + ("FAIL — errors above" if worst else "pass"))
        return worst
    if args.cmd == "scan":
        from . import scan

        rc = 0
        for addon in _addons(args.addons):
            res = scan.scan(addon, args.flavor, args.include_libs)
            print(json.dumps(res, indent=1) if args.json else scan.format_report(res))
            rc = rc or (1 if scan.problems(res) else 0)
        return rc
    if args.cmd == "lint":
        from .index import Index
        from .toc import lint

        cur = Index().current_interfaces()
        rc = 0
        for addon in _addons(args.addons):
            fs = lint(addon, cur)
            print(f"# {addon.name}")
            print("\n".join(str(f) for f in fs) or "clean")
            rc = rc or (1 if any(f.level == "error" for f in fs) else 0)
        return rc
    if args.cmd == "types":
        from . import luals

        for addon in _addons(args.addons):
            flavor = args.flavor or "live"
            ds = luals.check(addon, flavor)
            print(f"# {addon.name} (types against {flavor})")
            print("\n".join(str(d) for d in ds) or "clean")
        return 0
    if args.cmd == "package":
        from . import package

        rc = 0
        for addon in _addons(args.addons):
            res = package.build(addon)
            print(package.format_result(res))
            rc = rc or (1 if any(f.level == "error" for f in res.findings) else 0)
        return rc
    if args.cmd in ("deploy", "restore", "errors", "taint"):
        from . import game
        from .config import resolve_flavor

        flavors = (
            [resolve_flavor(f) for f in (getattr(args, "flavor", None) or [])]
            if args.cmd not in ("errors", "taint")
            else None
        )
        try:
            if args.cmd == "deploy":
                r = game.deploy(_addons([args.addon])[0], flavors)
                print(game.format_deploy(r))
                return 1 if r.errors else 0
            if args.cmd == "restore":
                for f, msg in game.restore(_addons([args.addon])[0], flavors).items():
                    print(f"  {f:20} {msg}")
                return 0
            if args.cmd == "taint":
                r = game.taint(args.addon, resolve_flavor(args.flavor))
                print(json.dumps(r, indent=1) if args.json else game.format_taint(r, args.verbose))
                return 1 if r.get("blocked") else 0
            r = game.errors(
                args.addon, resolve_flavor(args.flavor), None if args.all else args.sessions
            )
            print(json.dumps(r, indent=1) if args.json else game.format_errors(r, args.verbose))
            return 1 if r["errors"] else 0
        except game.GameHostError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
    if args.cmd == "bump":
        from .toc import plan_bump

        addon = _addons([args.addon])[0]
        return _edits(f"bump to {args.version}", addon, plan_bump(addon, args.version), args.write)
    if args.cmd == "interface":
        from .index import Index
        from .toc import plan_interface

        cur = Index().current_interfaces()
        for addon in _addons(args.addons):
            _edits("interface", addon, plan_interface(addon, cur), args.write)
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())

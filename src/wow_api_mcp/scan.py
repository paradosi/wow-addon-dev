"""Cross-flavor compatibility scanner for an addon directory.

    uv run wow-api-scan ~/projects/wowaddons/FarmMode
    uv run wow-api-scan ~/projects/wowaddons/iChat --flavor classic_era --json

Reads the addon's TOCs to learn which clients it targets, follows each TOC
(and the XML it includes) to the Lua files that client would load, compiles
them with `luac5.1 -l`, and checks every global the bytecode reads against the
index for each targeted flavor.

Working from bytecode rather than regex means every global read is explicit
(GETGLOBAL) with its line number, and guards are visible too: a global loaded
and immediately TESTed is a truthiness check, which is how the
`(C_X and C_X.F) or F` idiom is recognised as deliberate rather than a bug.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .build import _XML_REF, _ci_path
from .config import FLAVORS, resolve_flavor
from .index import Index

LUAC = "luac5.1"
_INSTR = re.compile(r"^\s*(\d+)\s+\[(\d+)\]\s+([A-Z]+)\s+([-\d ]+?)\s*(?:;\s*(.*))?$")
_FUNC = re.compile(r"^(?:main|function) <(.+):(\d+),(\d+)>.* at (0x[0-9a-f]+)\)")
_TEST_OPS = {"TEST", "TESTSET", "EQ", "LT", "LE"}
_LIB_DIR = re.compile(r"(^|/)(libs?|libraries)(/|$)", re.I)
# globals every addon may read that no data file lists. LibStub is created by
# `_G[LIBSTUB_MAJOR] = ...`, a computed key no static pass can see.
_ALWAYS = {"_G", "self", "...", "arg", "LibStub"}


@dataclass
class Ref:
    name: str
    file: str
    line: int
    guarded: bool
    method: bool = False


@dataclass
class TocInfo:
    path: Path
    interfaces: list[int]
    flavors: list[str]
    files: list[Path] = field(default_factory=list)
    saved_vars: set[str] = field(default_factory=set)
    xml_globals: set[str] = field(default_factory=set)


# ------------------------------------------------------------------ TOC handling


def _flavor_key_for_interface(iface: int, idx: Index) -> str | None:
    def bucket(n: int) -> int:
        return n // 10000 * 100 if n >= 100000 else n // 100

    target = bucket(iface)
    for key, f in idx.flavors.items():
        if f["interface"] and bucket(f["interface"]) == target:
            return key
    return None


def _suffix_flavor(toc: Path, addon: str) -> str | None:
    stem = toc.stem
    if stem == addon:
        return None
    suffix = stem[len(addon) + 1 :] if stem.startswith(addon) else ""
    for f in FLAVORS.values():
        if suffix.lower() in (s.lower() for s in f.toc_suffixes):
            return f.key
    return None


def read_toc(toc: Path, idx: Index) -> TocInfo:
    lines = toc.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    headers: dict[str, str] = {}
    for ln in lines:
        m = re.match(r"^##\s*([\w-]+)\s*:\s*(.*)$", ln)
        if m:
            headers[m.group(1).lower()] = m.group(2).strip()
    interfaces = [int(x) for x in re.findall(r"\d+", headers.get("interface", ""))]
    flavors = [k for i in interfaces if (k := _flavor_key_for_interface(i, idx))]
    if not flavors and (s := _suffix_flavor(toc, toc.parent.name)):
        flavors = [s]
    info = TocInfo(toc, interfaces, sorted(set(flavors), key=list(FLAVORS).index))
    for key in ("savedvariables", "savedvariablespercharacter"):
        info.saved_vars |= {v.strip() for v in headers.get(key, "").split(",") if v.strip()}

    seen: set[Path] = set()

    def add(p: Path) -> None:
        if p in seen:
            return
        seen.add(p)
        if p.suffix.lower() == ".lua":
            info.files.append(p)
        elif p.suffix.lower() == ".xml":
            text = p.read_text(encoding="utf-8", errors="replace")
            info.xml_globals |= {
                n for n in re.findall(r'\bname="([A-Za-z_]\w*)"', text) if "$" not in n
            }
            for _, ref in _XML_REF.findall(text):
                if (q := _ci_path(p.parent, ref)) is not None:
                    add(q)

    for ln in lines:
        ln = re.sub(r"\[[^\]]*\]", "", ln).strip()
        if ln and not ln.startswith("#") and (p := _ci_path(toc.parent, ln)) is not None:
            add(p)
    return info


# ------------------------------------------------------------------ bytecode


@dataclass
class _Ins:
    pc: int
    line: int
    op: str
    args: list[int]
    comment: str


def _guarded_pcs(fn: list[_Ins]) -> tuple[set[int], set[str]]:
    """Instruction pcs that only run when a global feature test passed or
    failed, and the names tested. A feature test is a GETGLOBAL (optionally
    followed by one GETTABLE of a constant key) whose register feeds a
    TEST/EQ/LT/LE, then a JMP: everything up to the jump target is
    conditional, and if the instruction just before the target is itself a
    forward JMP (end of a then-block), the else-arm is conditional too. This
    covers `if C_X then`, `if not C_X then`, `C_X and C_X.F or F` and
    `if WOW_PROJECT_ID == WOW_PROJECT_CLASSIC then`."""
    by_pc = {i.pc: i for i in fn}
    guarded: set[int] = set()
    tested: set[str] = set()
    for k, ins in enumerate(fn):
        if ins.op != "GETGLOBAL":
            continue
        name, reg = ins.comment, ins.args[0]
        j = k + 1
        if (
            j < len(fn)
            and fn[j].op == "GETTABLE"
            and fn[j].args[1] == reg
            and fn[j].comment.startswith('"')
        ):
            name = f"{name}.{fn[j].comment.split(chr(34))[1]}"
            reg = fn[j].args[0]
            j += 1
        # comparisons may load the other operand first: look a few ahead
        for t in range(j, min(j + 3, len(fn))):
            if fn[t].op in _TEST_OPS and (reg in fn[t].args or fn[t].op != "TEST"):
                break
            if fn[t].op not in ("GETGLOBAL", "GETTABLE", "LOADK", "LOADNIL", "LOADBOOL"):
                t = None
                break
        else:
            t = None
        if t is None or t + 1 >= len(fn) or fn[t + 1].op != "JMP":
            continue
        jmp = fn[t + 1]
        target = jmp.pc + 1 + jmp.args[0]
        if target <= jmp.pc:
            continue
        tested.add(name)
        tested.add(name.split(".")[0])
        end = target
        before = by_pc.get(target - 1)
        if before and before.op == "JMP" and before.pc + 1 + before.args[0] > target:
            end = before.pc + 1 + before.args[0]
        guarded.update(range(jmp.pc + 1, end))
    return guarded, tested


def analyse(path: Path, rel: str, namespaces: set[str]) -> tuple[list[Ref], set[str], set[str]]:
    """-> (references, globals this file defines, table keys this file sets)."""
    p = subprocess.run(
        [LUAC, "-l", "-p", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if p.returncode:
        raise SyntaxError(p.stderr.strip())

    funcs: list[tuple[str, list[_Ins]]] = []
    for raw in p.stdout.splitlines():
        if m := _FUNC.match(raw):
            funcs.append((m.group(4), []))
        elif (m := _INSTR.match(raw)) and funcs:
            funcs[-1][1].append(
                _Ins(
                    int(m.group(1)),
                    int(m.group(2)),
                    m.group(3),
                    [int(x) for x in m.group(4).split()],
                    m.group(5) or "",
                )
            )

    refs: list[Ref] = []
    defines: set[str] = set()
    keys: set[str] = set()
    inherited: set[str] = set()  # closures created inside a guarded region
    inherited_tests: dict[str, set[str]] = {}
    # luac prints a function before its children, so guards flow downwards
    for addr, fn in funcs:
        guarded, tested = _guarded_pcs(fn)
        tested |= inherited_tests.get(addr, set())
        whole = addr in inherited
        g_regs: set[int] = set()
        for k, ins in enumerate(fn):
            g = whole or ins.pc in guarded
            nxt = fn[k + 1] if k + 1 < len(fn) else None
            if ins.op == "CLOSURE":
                child = ins.comment.strip()
                inherited_tests[child] = set(tested)
                if g:
                    inherited.add(child)
            elif ins.op == "GETGLOBAL":
                name = ins.comment
                if name == "_G":
                    g_regs.add(ins.args[0])
                else:
                    g_regs.discard(ins.args[0])
                if (
                    nxt
                    and nxt.op == "GETTABLE"
                    and nxt.args[1] == ins.args[0]
                    and nxt.comment.startswith('"')
                    and (name in namespaces or name in ("Enum", "Constants"))
                ):
                    composed = f"{name}.{nxt.comment.split(chr(34))[1]}"
                    refs.append(
                        Ref(composed, rel, ins.line, g or composed in tested or name in tested)
                    )
                refs.append(Ref(name, rel, ins.line, g or name in tested))
            elif ins.op == "SETGLOBAL":
                defines.add(ins.comment)
            elif ins.op == "SETTABLE" and ins.comment.startswith('"'):
                key = ins.comment.split('"')[1]
                keys.add(key)
                if ins.args[0] in g_regs:
                    defines.add(key)
            elif ins.op == "SELF" and ins.comment.startswith('"'):
                meth = ins.comment.split('"')[1]
                refs.append(Ref(meth, rel, ins.line, g or ("method:" + meth) in tested, True))
            elif (
                ins.op == "GETTABLE"
                and ins.comment.startswith('"')
                and nxt
                and nxt.op in ("TEST", "TESTSET")
            ):
                tested.add("method:" + ins.comment.split('"')[1])  # if frame.SetFoo then
    return refs, defines, keys


# ------------------------------------------------------------------ report


def scan(addon_dir: Path, flavors: list[str] | None = None, include_libs: bool = False) -> dict:
    idx = Index()
    addon_dir = addon_dir.expanduser().resolve()
    tocs = [read_toc(t, idx) for t in sorted(addon_dir.glob("*.toc"))]
    if not tocs:
        raise FileNotFoundError(f"no .toc in {addon_dir}")
    namespaces = set().union(*(idx.sets(f)["namespaces"] for f in idx.flavors))

    report: dict = {"addon": addon_dir.name, "tocs": [], "parse_errors": [], "flavors": {}}
    want = {resolve_flavor(f) for f in flavors} if flavors else None
    cache: dict[Path, tuple[list[Ref], set[str], set[str]]] = {}

    for toc in tocs:
        report["tocs"].append(
            {
                "toc": toc.path.name,
                "interfaces": toc.interfaces,
                "flavors": toc.flavors,
                "files": len(toc.files),
                "unindexed_interfaces": [
                    i for i in toc.interfaces if not _flavor_key_for_interface(i, idx)
                ],
            }
        )
        for p in toc.files:
            if p not in cache:
                rel = str(p.relative_to(addon_dir))
                try:
                    cache[p] = analyse(p, rel, namespaces)
                except SyntaxError as e:
                    report["parse_errors"].append(f"{rel}: {e}")
                    cache[p] = ([], set(), set())

    for toc in tocs:
        defined = set(_ALWAYS) | toc.saved_vars | toc.xml_globals
        keys: set[str] = set()
        for p in toc.files:
            defined |= cache[p][1]
            keys |= cache[p][2]
        for flavor in toc.flavors:
            if want and flavor not in want:
                continue
            fr = report["flavors"].setdefault(
                flavor,
                {
                    "toc": [],
                    "interface_current": idx.flavors[flavor]["interface"],
                    "missing": [],
                    "deprecated_shim": [],
                    "load_on_demand": [],
                    "methods_missing": [],
                    "guarded_absent": defaultdict(int),
                    "unknown": defaultdict(list),
                },
            )
            fr["toc"].append(
                {
                    "toc": toc.path.name,
                    "interface": [
                        i for i in toc.interfaces if _flavor_key_for_interface(i, idx) == flavor
                    ],
                }
            )
            s = idx.sets(flavor)
            other_methods = set().union(
                *(idx.sets(o)["methods"] for o in idx.flavors if o != flavor)
            )
            seen: set[tuple] = set()
            for p in toc.files:
                rel = str(p.relative_to(addon_dir))
                if _LIB_DIR.search(rel) and not include_libs:
                    continue
                for r in cache[p][0]:
                    key = (r.name, r.file, r.line, r.method)
                    if key in seen:
                        continue
                    seen.add(key)
                    loc = f"{r.file}:{r.line}"
                    if r.method:
                        if (
                            r.name not in s["methods"]
                            and r.name in other_methods
                            and r.name not in keys
                            and not r.guarded
                        ):
                            fr["methods_missing"].append({"method": r.name, "at": loc})
                        continue
                    if r.name in defined or r.name.split(".")[0] in defined:
                        continue
                    st = idx.flat_status(r.name, flavor)
                    if st in ("native", "blizzard_ui"):
                        continue
                    if r.guarded and st == "absent":
                        fr["guarded_absent"][r.name] += 1
                        continue
                    if st == "deprecated_shim":
                        fr["deprecated_shim"].append({"name": r.name, "at": loc})
                    elif st == "load_on_demand":
                        fr["load_on_demand"].append({"name": r.name, "at": loc})
                    else:
                        elsewhere = [
                            o for o in idx.flavors if idx.flat_status(r.name, o) != "absent"
                        ]
                        if elsewhere:
                            fr["missing"].append(
                                {
                                    "name": r.name,
                                    "at": loc,
                                    "exists_in": elsewhere,
                                    "guarded": r.guarded,
                                }
                            )
                        else:
                            fr["unknown"][r.name].append(loc)
    for fr in report["flavors"].values():
        fr["guarded_absent"] = dict(fr["guarded_absent"])
        fr["unknown"] = {k: v[:3] for k, v in sorted(fr["unknown"].items())}
        ifs = [i for t in fr["toc"] for i in t["interface"]]
        fr["interface_outdated"] = bool(ifs) and max(ifs) < (fr["interface_current"] or 0)
    return report


def problems(report: dict) -> int:
    return sum(
        len(fr["missing"]) + len(fr["deprecated_shim"]) + len(fr["methods_missing"])
        for fr in report["flavors"].values()
    ) + len(report["parse_errors"])


def format_report(report: dict) -> str:
    out = [f"# {report['addon']}"]
    for t in report["tocs"]:
        extra = f"  (not indexed: {t['unindexed_interfaces']})" if t["unindexed_interfaces"] else ""
        out.append(
            f"- {t['toc']}: Interface {t['interfaces']} -> {', '.join(t['flavors']) or '?'}"
            f", {t['files']} Lua files{extra}"
        )
    for e in report["parse_errors"]:
        out.append(f"! PARSE ERROR {e}")
    for flavor, fr in report["flavors"].items():
        label = FLAVORS[flavor].label
        ifs = sorted({i for t in fr["toc"] for i in t["interface"]})
        out.append(f"\n## {label} ({flavor}) — client interface {fr['interface_current']}")
        if fr["interface_outdated"]:
            out.append(
                f"! TOC Interface {ifs} is older than the client's {fr['interface_current']}: "
                "shown as out of date in the AddOns list"
            )
        for m in fr["missing"]:
            g = " (guarded on the same line — check the fallback)" if m["guarded"] else ""
            out.append(
                f"x MISSING {m['name']} at {m['at']} — exists in {', '.join(m['exists_in'])}{g}"
            )
        for m in fr["deprecated_shim"]:
            out.append(f"~ DEPRECATED {m['name']} at {m['at']} — only a Blizzard_Deprecated shim")
        for m in fr["methods_missing"]:
            out.append(
                f"? METHOD :{m['method']}() at {m['at']} — no widget/mixin in this client has it "
                "(receiver type unknown, so possibly a false positive)"
            )
        for m in fr["load_on_demand"]:
            out.append(f"i LOD {m['name']} at {m['at']} — needs its Blizzard addon loaded first")
        if fr["guarded_absent"]:
            out.append(
                "  ok guarded, absent here (expected compat idiom): "
                + ", ".join(sorted(fr["guarded_absent"]))
            )
        if fr["unknown"]:
            out.append(
                "  ? unknown in every client (other addons, libraries, or typos): "
                + ", ".join(f"{k} ({v[0]})" for k, v in fr["unknown"].items())
            )
        if not (fr["missing"] or fr["deprecated_shim"] or fr["methods_missing"]):
            out.append("  clean: no missing or deprecated API references")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="wow-api-scan", description=__doc__.splitlines()[0])
    ap.add_argument("addon", nargs="+", type=Path)
    ap.add_argument("--flavor", action="append", help="limit to these flavors")
    ap.add_argument("--include-libs", action="store_true", help="also report findings in Libs/")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    rc = 0
    for a in args.addon:
        r = scan(a, args.flavor, args.include_libs)
        print(json.dumps(r, indent=1) if args.json else format_report(r))
        rc = rc or (1 if problems(r) else 0)
    return rc


if __name__ == "__main__":
    sys.exit(main())

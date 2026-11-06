"""Cross-flavor compatibility scanner for an addon directory.

    wowdev scan FarmMode
    wowdev scan iChat --flavor classic_era --json

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

import re
import subprocess
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
# opcodes that overwrite register A: a register that held _G stops being _G
_WRITES_A = {
    "MOVE", "LOADK", "LOADBOOL", "LOADNIL", "GETUPVAL", "GETTABLE", "NEWTABLE",
    "SELF", "ADD", "SUB", "MUL", "DIV", "MOD", "POW", "UNM", "NOT", "LEN",
    "CONCAT", "CALL", "TESTSET", "CLOSURE", "VARARG", "FORPREP", "FORLOOP",
}  # fmt: skip
_EVENTISH = re.compile(r'^"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+"$')
# functions that take the name of a global function to hook as a string; a
# vanished name errors at load (xCT+'s CombatText_AddMessage on TBC 2.5.6)
_HOOKERS = {"hooksecurefunc"}
_ACE_HOOKERS = {"Hook", "SecureHook", "RawHook"}


def _hooked_name(fn: list, k: int, reg: int) -> tuple[str, int] | None:
    """The string constant loaded into `reg` before the call at or after fn[k],
    i.e. the first argument when it is a literal name."""
    for ins in fn[k + 1 :]:
        if ins.op in ("CALL", "TAILCALL", "RETURN", "JMP"):
            return None
        if ins.op in _WRITES_A and ins.args and ins.args[0] == reg:
            if ins.op == "LOADK" and ins.comment.startswith('"'):
                return ins.comment.strip('"'), ins.line
            return None
    return None


@dataclass
class Ref:
    name: str
    file: str
    line: int
    guarded: bool
    method: bool = False
    literal: bool = False  # a string constant: event names reach the API this way


@dataclass
class TocInfo:
    path: Path
    interfaces: list[int]
    flavors: list[str]
    files: list[Path] = field(default_factory=list)
    saved_vars: set[str] = field(default_factory=set)
    xml_globals: set[str] = field(default_factory=set)


# ------------------------------------------------------------------ TOC handling


def bucket(n: int) -> int:
    """Interface number -> client family: 11508/11509 -> 115, 20505 -> 205,
    16001 -> 160 (Forever), 120001/120105 -> 1200 (every 12.x retail patch)."""
    return n // 10000 * 100 if n >= 100000 else n // 100


def _flavor_key_for_interface(iface: int, idx: Index) -> str | None:
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


# a field named like a flavor flag: Postal.WOWRetail, ns.isClassic, addon.IsTBC
_FLAVOR_FLAG = re.compile(
    r"^(is|has)?_?(wow)?_?(retail|mainline|classic|era|vanilla|bc|bcc|tbc|anniversary|"
    r"wotlk|wrath|cata|mists|mop|sod|hardcore|forever|camelot)",
    re.I,
)
# globals whose comparison is a client/version test
_VERSION_PROBES = {
    "GetBuildInfo",
    "WOW_PROJECT_ID",
    "WOW_PROJECT_MAINLINE",
    "WOW_PROJECT_CLASSIC",
    "WOW_PROJECT_BURNING_CRUSADE_CLASSIC",
    "WOW_PROJECT_WRATH_CLASSIC",
    "WOW_PROJECT_CATACLYSM_CLASSIC",
    "WOW_PROJECT_MISTS_CLASSIC",
    "WOW_PROJECT_CAMELOT",
    "LE_EXPANSION_LEVEL_CURRENT",
    "GetExpansionLevel",
    "GetClassicExpansionLevel",
    "GetServerExpansionLevel",
    "IsClassic",
    "IsRetail",
}


def _tested_regs(ins: _Ins) -> set[int]:
    if ins.op == "TEST":
        return {ins.args[0]}
    if ins.op == "TESTSET":
        return {ins.args[1]}
    return {a for a in ins.args[1:] if a >= 0}  # EQ/LT/LE: B and C (RK; constants < 0)


def _guarded_pcs(fn: list[_Ins]) -> tuple[set[int], set[str]]:
    """Instruction pcs that only run when a feature test passed or failed,
    and the global names that were tested.

    A feature test is a TEST/EQ/LT/LE followed by JMP whose operand, on the
    same source line, came from: a global (`if C_X then`, `C_X and C_X.F or
    F`), a field named like a flavor flag (`if Postal.WOWRetail then`), or a
    comparison involving a version probe (`select(4, GetBuildInfo()) >=
    110000`, `WOW_PROJECT_ID == WOW_PROJECT_CLASSIC`). Everything up to the
    jump target is conditional; if the instruction just before the target is
    a forward JMP (end of a then-block), the else arm is conditional too.
    Plain locals (`if isClassic then`) are not recognised."""
    by_pc = {i.pc: i for i in fn}
    guarded: set[int] = set()
    tested: set[str] = set()
    for k, ins in enumerate(fn):
        if ins.op not in _TEST_OPS or k + 1 >= len(fn) or fn[k + 1].op != "JMP":
            continue
        regs = _tested_regs(ins)
        names: list[str] = []
        is_guard = False
        back = [fn[j] for j in range(max(0, k - 8), k) if fn[j].line == ins.line]
        for j, b in enumerate(back):
            key = b.comment.split('"')[1] if b.comment.startswith('"') else None
            if b.op == "GETGLOBAL":
                nxt = back[j + 1] if j + 1 < len(back) else None
                composed = (
                    f"{b.comment}.{nxt.comment.split(chr(34))[1]}"
                    if nxt
                    and nxt.op == "GETTABLE"
                    and nxt.args[1] == b.args[0]
                    and nxt.comment.startswith('"')
                    else None
                )
                if b.args[0] in regs or (composed and nxt.args[0] in regs):
                    is_guard = True
                    names.append(b.comment)
                    if composed:
                        names.append(composed)
                if b.comment in _VERSION_PROBES and ins.op != "TEST":
                    is_guard = True
            elif b.op in ("GETTABLE", "SELF") and key and _FLAVOR_FLAG.match(key):
                if b.args[0] in regs or b.op == "SELF":
                    is_guard = True
        if not is_guard:
            continue
        tested.update(names)
        tested.update(n.split(".")[0] for n in names)
        jmp = fn[k + 1]
        target = jmp.pc + 1 + jmp.args[0]
        if target <= jmp.pc:
            continue
        end = target
        before = by_pc.get(target - 1)
        if before and before.op == "JMP" and before.pc + 1 + before.args[0] > target:
            end = before.pc + 1 + before.args[0]
        guarded.update(range(jmp.pc + 1, end))
    return guarded, tested


def analyse(path: Path, rel: str, namespaces: set[str]) -> tuple[list[Ref], set[str], set[str]]:
    """-> (references, globals this file defines, table keys this file sets)."""
    data = path.read_bytes()
    tmp = None
    if data.startswith(b"\xef\xbb\xbf"):  # luac5.1 rejects a BOM the client accepts
        import tempfile

        with tempfile.NamedTemporaryFile("wb", suffix=".lua", delete=False) as t:
            t.write(data[3:])
        tmp = Path(t.name)
    try:
        p = subprocess.run(
            [LUAC, "-l", "-p", str(tmp or path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    finally:
        if tmp:
            tmp.unlink(missing_ok=True)
    if p.returncode:
        raise SyntaxError(p.stderr.strip().replace(str(tmp or path), rel))

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
                if name in _HOOKERS and (hk := _hooked_name(fn, k, ins.args[0] + 1)):
                    refs.append(Ref(hk[0], rel, hk[1], g or hk[0] in tested))
            elif ins.op == "SETGLOBAL":
                defines.add(ins.comment)
            elif ins.op == "LOADK" and _EVENTISH.match(ins.comment):
                refs.append(Ref(ins.comment.strip('"'), rel, ins.line, g, literal=True))
            elif ins.op == "SETTABLE" and ins.comment.startswith('"'):
                key = ins.comment.split('"')[1]
                keys.add(key)
                if ins.args[0] in g_regs:
                    defines.add(key)
            elif ins.op == "SELF" and ins.comment.startswith('"'):
                meth = ins.comment.split('"')[1]
                refs.append(Ref(meth, rel, ins.line, g or ("method:" + meth) in tested, True))
                if meth in _ACE_HOOKERS and (hk := _hooked_name(fn, k, ins.args[0] + 2)):
                    refs.append(Ref(hk[0], rel, hk[1], g or hk[0] in tested))
            elif (
                ins.op == "GETTABLE"
                and ins.comment.startswith('"')
                and nxt
                and nxt.op in ("TEST", "TESTSET")
            ):
                tested.add("method:" + ins.comment.split('"')[1])  # if frame.SetFoo then
            if ins.op in _WRITES_A and ins.args:
                # `bar.x = ...` after the register that once held _G was reused
                # is a field write, not a global definition
                g_regs.discard(ins.args[0])
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
                    # deprecated fallback behind a guard: runs only where the
                    # modern API is missing, so a note rather than a warning
                    "guarded_deprecated": defaultdict(int),
                    "unknown": defaultdict(list),
                    # secret values / restricted APIs (Retail 12.x, Forever)
                    "restricted": {},
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
            restricted = idx.restricted(flavor)
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
                    if r.name in restricted and r.name not in fr["restricted"]:
                        fr["restricted"][r.name] = {"markers": restricted[r.name], "at": loc}
                    if r.literal:
                        continue
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
                    if st == "deprecated_shim" and r.guarded:
                        fr["guarded_deprecated"][r.name] += 1
                    elif st == "deprecated_shim":
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
        fr["guarded_deprecated"] = dict(fr["guarded_deprecated"])
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
        if fr["guarded_deprecated"]:
            out.append(
                "  ok guarded fallback to a deprecated shim (used only if the new API is missing): "
                + ", ".join(sorted(fr["guarded_deprecated"]))
            )
        for n, m in sorted(fr["restricted"].items()):
            out.append(
                f"s RESTRICTED {n} at {m['at']} — {m['markers'].replace(',', ', ')} "
                "(secret values / restricted for addon code; see wow_lookup)"
            )
        if fr["unknown"]:
            out.append(
                "  ? unknown in every client (other addons, libraries, or typos): "
                + ", ".join(f"{k} ({v[0]})" for k, v in fr["unknown"].items())
            )
        if not (fr["missing"] or fr["deprecated_shim"] or fr["methods_missing"]):
            out.append("  clean: no missing or deprecated API references")
    return "\n".join(out)

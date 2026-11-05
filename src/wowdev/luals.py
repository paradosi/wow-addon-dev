"""Type-check an addon with lua-language-server, headless.

Types come from Ketho's API annotations (Annotations/Core, retail-oriented)
plus NumyAddon/FramexmlAnnotations for the *flavor being checked*, so mixins
and FrameXML helpers match that client. `undefined-global` is off: whether a
global exists in a given client is the scanner's job (scan.py), and it knows
the flavors; LuaLS would judge every client by retail.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .config import DATA_DIR, UPSTREAM_DIR

LUALS = DATA_DIR / "tools" / "luals" / "bin" / "lua-language-server"
# Diagnostics that are noise for WoW addons or duplicated by luacheck/scan
DISABLED = [
    "undefined-global",  # scan.py, flavor-aware
    "lowercase-global",  # luacheck covers globals
    "unused-local",  # luacheck
    "unused-vararg",  # luacheck; `local addonName, ns = ...` is idiomatic
    "redefined-local",  # luacheck
    "trailing-space",
    "inject-field",  # adding fields to frames is how addons work
    "duplicate-set-field",  # common in mixin-style code
    "cast-local-type",  # untyped Lua reuses locals across types constantly
]
_SEVERITY = {1: "error", 2: "warn", 3: "info", 4: "hint"}


@dataclass
class Diag:
    file: str
    line: int
    code: str
    severity: str
    message: str

    def __str__(self) -> str:
        return f"{self.severity[0]} {self.file}:{self.line} [{self.code}] {self.message}"


def available() -> bool:
    return LUALS.exists()


def check(addon: Path, flavor: str = "classic_anniversary", level: str = "Warning") -> list[Diag]:
    if not available():
        raise FileNotFoundError(
            f"lua-language-server not installed at {LUALS} — run `wowdev setup`"
        )
    addon = addon.resolve()
    libs = [str(UPSTREAM_DIR / "ketho-annotations" / "Annotations" / "Core")]
    fxa = UPSTREAM_DIR / f"fxa-{flavor}" / "Annotations"
    if fxa.exists():
        libs.append(str(fxa))
    cfg = {
        "runtime.version": "Lua 5.1",
        "runtime.builtin": {k: "disable" for k in ("io", "os", "package", "debug", "utf8")},
        "workspace.library": libs,
        "workspace.ignoreDir": [".git", "Libs", "libs", ".release", "Libraries"],
        "workspace.checkThirdParty": False,
        "diagnostics.disable": DISABLED,
    }
    with tempfile.TemporaryDirectory(prefix="wowdev-luals-") as tmp:
        cfg_path = Path(tmp) / "luarc.json"
        cfg_path.write_text(json.dumps(cfg))
        p = subprocess.run(
            [
                str(LUALS),
                f"--check={addon}",
                f"--configpath={cfg_path}",
                f"--logpath={tmp}",
                f"--checklevel={level}",
                "--check_format=json",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
        )
        out_file = Path(tmp) / "check.json"
        if not out_file.exists():
            if "no problems found" in p.stdout + p.stderr:
                return []
            raise RuntimeError(f"lua-language-server produced no report: {p.stderr[-500:]}")
        data = json.loads(out_file.read_text()) or {}  # a clean run writes []
    out = []
    for uri, diags in data.items():
        path = Path(uri.removeprefix("file://"))
        try:
            rel = str(path.relative_to(addon))
        except ValueError:
            rel = str(path)
        for d in diags:
            out.append(
                Diag(
                    rel,
                    d["range"]["start"]["line"] + 1,
                    d.get("code", "?"),
                    _SEVERITY.get(d.get("severity", 2), "warn"),
                    " ".join(d["message"].split()),
                )
            )
    return sorted(out, key=lambda d: (d.file, d.line))

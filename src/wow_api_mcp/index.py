"""Read side of the index: shared by the MCP server and the addon scanner."""

from __future__ import annotations

import json
import sqlite3
from functools import cached_property
from pathlib import Path

from .config import DB_PATH, FLAVORS

# kinds whose presence means "the client itself provides this name"
NATIVE_KINDS = ("function", "lua", "global_const", "enum", "constants", "event", "cvar")
# kinds that are globals created by Blizzard's Lua/XML rather than C
UI_KINDS = ("framexml", "frame", "mixin", "template")

STATUS_DOC = {
    "native": "provided by the client (C API / Lua builtin / constant)",
    "blizzard_ui": "defined by Blizzard's own Lua/XML UI code",
    "load_on_demand": "defined by a Blizzard load-on-demand addon; exists only once that addon loads",
    "deprecated_shim": (
        "exists only as a Blizzard_Deprecated fallback (gated on CVar "
        "loadDeprecationFallbacks, default 1) — scheduled for removal"
    ),
    "absent": "does not exist in this client",
}


class Index:
    def __init__(self, path: Path = DB_PATH):
        self.path = path
        self._db: sqlite3.Connection | None = None
        self._mtime = 0.0

    @property
    def db(self) -> sqlite3.Connection:
        if not self.path.exists():
            raise FileNotFoundError(
                f"no index at {self.path} — run `just refresh` in the wow-api-mcp project"
            )
        mtime = self.path.stat().st_mtime
        if self._db is None or mtime != self._mtime:  # rebuilt underneath us: reopen
            if self._db is not None:
                self._db.close()
            self._db = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
            self._db.row_factory = sqlite3.Row
            self._mtime = mtime
            self.__dict__.pop("flavors", None)
            self.__dict__.pop("_sets", None)
        return self._db

    def q(self, sql: str, *args) -> list[sqlite3.Row]:
        return self.db.execute(sql, args).fetchall()

    @cached_property
    def flavors(self) -> dict[str, dict]:
        return {r["key"]: dict(r) for r in self.q("SELECT * FROM flavor ORDER BY rowid")}

    def meta(self) -> dict[str, str]:
        return {r["key"]: r["value"] for r in self.q("SELECT key, value FROM meta")}

    # ------------------------------------------------------------ name resolution

    def rows(self, name: str) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM api WHERE name = ? ORDER BY kind, flavor", name)

    def canonical(self, name: str) -> list[str]:
        """Exact match first; else case-insensitive; else `.Name` / `:Name` suffix
        matches (so 'GetContainerNumSlots' finds 'C_Container.GetContainerNumSlots')."""
        name = name.strip()
        if self.q("SELECT 1 FROM api WHERE name = ? LIMIT 1", name) or self.q(
            "SELECT 1 FROM def WHERE name = ? LIMIT 1", name
        ):
            return [name]
        r = self.q("SELECT DISTINCT name FROM api WHERE name = ? COLLATE NOCASE LIMIT 5", name)
        if r:
            return [x["name"] for x in r]
        r = self.q(
            "SELECT DISTINCT name FROM api WHERE name LIKE ? OR name LIKE ? LIMIT 10",
            f"%.{name}",
            f"%:{name}",
        )
        return [x["name"] for x in r]

    def status(self, name: str, flavor: str) -> str:
        """One of STATUS_DOC's keys."""
        kinds = {
            r["kind"]
            for r in self.q("SELECT kind FROM api WHERE name=? AND flavor=?", name, flavor)
        }
        if kinds & {
            *NATIVE_KINDS,
            "widget",
            "widget_method",
            "script_handler",
            "struct",
            "callback",
            "cvar_command",
        }:
            return "native"
        defs = self.q("SELECT deprecated, lod FROM def WHERE name=? AND flavor=?", name, flavor)
        if kinds & set(UI_KINDS) or defs:
            if any(not d["deprecated"] and not d["lod"] for d in defs) or (
                kinds & set(UI_KINDS) and not defs
            ):
                return "blizzard_ui"
            if any(not d["deprecated"] for d in defs):
                return "load_on_demand"
            return "deprecated_shim"
        return "absent"

    def availability(self, name: str) -> dict[str, str]:
        return {f: self.status(name, f) for f in self.flavors}

    # ------------------------------------------------------------- bulk sets (scanner)

    @cached_property
    def _sets(self) -> dict[str, dict[str, set[str]]]:
        """Per flavor: every global name, split by status, plus method names."""
        out: dict[str, dict[str, set[str]]] = {}
        for f in self.flavors:
            native = {
                r[0]
                for r in self.db.execute(
                    f"SELECT name FROM api WHERE flavor=? AND kind IN "
                    f"({','.join('?' * len(NATIVE_KINDS + UI_KINDS))})",
                    (f, *NATIVE_KINDS, *UI_KINDS),
                )
            }
            ui = set()
            lod = set()
            shim = set()
            for name, dep, ld in self.db.execute(
                "SELECT name, deprecated, lod FROM def WHERE flavor=?", (f,)
            ):
                (shim if dep else lod if ld else ui).add(name)
            lod -= ui
            shim -= ui | lod
            strings = {
                r[0] for r in self.db.execute("SELECT key FROM gstring WHERE flavor=?", (f,))
            }
            namespaces = {n.split(".")[0] for n in native | ui if "." in n}
            methods = {
                r[0].split(":", 1)[1]
                for r in self.db.execute(
                    "SELECT name FROM api WHERE flavor=? AND kind='widget_method'", (f,)
                )
            }
            methods |= {n.rsplit(":", 1)[1] for n in ui | lod if ":" in n}
            methods |= {n.rsplit(".", 1)[1] for n in ui | lod if "." in n}
            out[f] = {
                "native": native | strings,
                "ui": ui,
                "lod": lod,
                "shim": shim,
                "namespaces": namespaces,
                "methods": methods,
            }
        return out

    def sets(self, flavor: str) -> dict[str, set[str]]:
        return self._sets[flavor]

    def flat_status(self, name: str, flavor: str) -> str:
        s = self.sets(flavor)
        if name in s["native"] or name in s["namespaces"]:
            return "native"
        if name in s["ui"]:
            return "blizzard_ui"
        if name in s["lod"]:
            return "load_on_demand"
        if name in s["shim"]:
            return "deprecated_shim"
        return "absent"

    # ------------------------------------------------------------- details

    def annotation(self, name: str) -> dict | None:
        r = self.q("SELECT * FROM annotation WHERE name = ?", name)
        if not r:
            return None
        d = dict(r[0])
        d["params"] = json.loads(d["params"] or "[]")
        d["returns"] = json.loads(d["returns"] or "[]")
        return d


def flavor_keys(flavors: list[str] | None) -> list[str]:
    from .config import resolve_flavor

    return [resolve_flavor(f) for f in flavors] if flavors else list(FLAVORS)

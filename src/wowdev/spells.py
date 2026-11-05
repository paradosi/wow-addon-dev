"""Spell records per client, from wago.tools' DB2 exports of the exact build
the index describes: id, name, rank, duration, school, description.

The API index says which functions exist; this says which spell ids exist and
what they are, which is what an addon's own data tables (FloTotemBar's
data.lua, xCT+'s spell filters) are made of. Rank-1 ids, per-rank durations
and "is this id even in TBC" were unanswerable before.

Four tables per build, cached under upstream/spells-<flavor>/<build>/ and only
re-downloaded when the build changes:

    SpellName      ID, Name_lang
    Spell          ID, NameSubtext_lang (the rank), Description_lang
    SpellMisc      SpellID, DurationIndex, SchoolMask, SpellIconFileDataID (DifficultyID 0)
    SpellDuration  ID, Duration, MaxDuration (ms; -1 = until cancelled)
"""

from __future__ import annotations

import csv
import json
import sys
import time
import urllib.request
from pathlib import Path

from .config import UPSTREAM_DIR, Flavor

WAGO = "https://wago.tools"
TABLES = ("SpellName", "Spell", "SpellMisc", "SpellDuration")
_UA = {"User-Agent": "wowdev (wow-addon-dev; weekly index refresh)"}
csv.field_size_limit(1 << 24)


def _get(url: str, timeout: int = 300) -> bytes:
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _builds() -> dict:
    return json.loads(_get(f"{WAGO}/api/builds", timeout=60))


def spell_dir(f: Flavor) -> Path:
    return UPSTREAM_DIR / f"spells-{f.key}"


def fetch(f: Flavor, version: str, build: str, builds: dict | None = None) -> Path | None:
    """Download the tables for this client's build (or the newest wago has for
    the product, if that exact build is missing). Returns the build directory."""
    if not f.wago_product:
        return None
    want = f"{version}.{build}"
    builds = builds if builds is not None else _builds()
    have = [b["version"] for b in builds.get(f.wago_product, [])]
    if not have:
        print(f"  {f.key}: wago.tools has no builds for {f.wago_product}", file=sys.stderr)
        return None
    use = want if want in have else have[0]
    d = spell_dir(f) / use
    if all((d / f"{t}.csv").exists() for t in TABLES):
        return d
    d.mkdir(parents=True, exist_ok=True)
    for t in TABLES:
        data = _get(f"{WAGO}/db2/{t}/csv?build={use}")
        if not data.startswith(b"ID") and b"," not in data[:200]:
            raise RuntimeError(f"wago.tools returned no CSV for {t} {use}")
        (d / f"{t}.csv").write_bytes(data)
        time.sleep(0.5)  # be polite to a free service
    # keep only the build just fetched
    for old in spell_dir(f).iterdir():
        if old.is_dir() and old.name != use:
            for p in old.iterdir():
                p.unlink()
            old.rmdir()
    note = "" if use == want else f" (wanted {want}, not on wago yet)"
    print(f"  {f.key}: spells for {use}{note}", file=sys.stderr)
    return d


def _rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def collect(f: Flavor) -> tuple[str | None, list[tuple]]:
    """(build, rows) for the spell table:
    (flavor, id, name, rank, duration_ms, max_duration_ms, school, icon, description)."""
    root = spell_dir(f)
    dirs = sorted(p for p in root.iterdir() if p.is_dir()) if root.exists() else []
    if not dirs:
        return None, []
    d = dirs[-1]
    names = {int(r["ID"]): r["Name_lang"] for r in _rows(d / "SpellName.csv")}
    text = {int(r["ID"]): r for r in _rows(d / "Spell.csv")}
    dur = {
        int(r["ID"]): (int(r["Duration"]), int(r["MaxDuration"]))
        for r in _rows(d / "SpellDuration.csv")
    }
    misc = {}
    for r in _rows(d / "SpellMisc.csv"):
        if r.get("DifficultyID", "0") not in ("0", ""):
            continue
        misc.setdefault(int(r["SpellID"]), r)
    out = []
    for sid, name in names.items():
        m = misc.get(sid, {})
        di = int(m.get("DurationIndex") or 0)
        d_ms, max_ms = dur.get(di, (None, None))
        t = text.get(sid, {})
        out.append(
            (
                f.key,
                sid,
                name,
                t.get("NameSubtext_lang") or None,
                d_ms,
                max_ms,
                int(m["SchoolMask"]) if m.get("SchoolMask") else None,
                int(m["SpellIconFileDataID"]) if m.get("SpellIconFileDataID") else None,
                t.get("Description_lang") or None,
            )
        )
    return d.name, out

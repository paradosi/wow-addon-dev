"""Paths and the flavor table. Everything else derives from these."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DATA_DIR = Path(os.environ.get("WOWDEV_DATA", Path.home() / "data" / "wowdev")).expanduser()
UPSTREAM_DIR = DATA_DIR / "upstream"
DB_PATH = DATA_DIR / "index.sqlite"
PKG_DIR = Path(__file__).resolve().parent
DUMP_LUA = PKG_DIR / "dump.lua"
LUA = os.environ.get("WOWDEV_LUA", "lua5.1")

UI_SOURCE_URL = "https://github.com/Gethe/wow-ui-source"
BIR_URL = "https://github.com/Ketho/BlizzardInterfaceResources"
KETHO_URL = "https://github.com/Ketho/vscode-wow-api"
FXA_URL = "https://github.com/NumyAddon/FramexmlAnnotations"
TOOLS_DIR = DATA_DIR / "tools"
LUALS_VERSION = "3.19.2"


@dataclass(frozen=True)
class Flavor:
    key: str  # upstream branch name, used everywhere as the flavor id
    label: str
    aliases: tuple[str, ...]  # what people (and TOC suffixes) call it
    # <Addon>_<Suffix>.toc, in the order the client prefers them ("Classic" =
    # any Classic client, and is what most of Blizzard's own Classic TOCs use)
    toc_suffixes: tuple[str, ...]
    # values Blizzard's TOCs test with [AllowLoadGameType ...] in this client
    game_types: frozenset[str]

    @property
    def ui_dir(self) -> Path:
        return UPSTREAM_DIR / f"ui-{self.key}"

    @property
    def bir_dir(self) -> Path:
        return UPSTREAM_DIR / f"bir-{self.key}"


# Branch names are the upstream ones (Gethe/wow-ui-source and
# Ketho/BlizzardInterfaceResources use the same set), so a new flavor is one
# entry here plus `just refresh`.
FLAVORS: dict[str, Flavor] = {
    f.key: f
    for f in (
        Flavor(
            "live",
            "Retail",
            ("retail", "mainline", "midnight"),
            ("Mainline",),
            frozenset({"mainline", "standard"}),
        ),
        Flavor(
            "classic_anniversary",
            "TBC Anniversary",
            ("tbc", "bcc", "anniversary", "classic_anniversary"),
            ("TBC", "BCC", "Classic"),
            frozenset({"tbc", "classic", "standard"}),
        ),
        Flavor(
            "classic_era",
            "Classic Era",
            ("vanilla", "era", "classic_era"),
            ("Vanilla", "Classic"),
            frozenset({"vanilla", "classic", "standard"}),
        ),
        # Internally "Camelot" (WOW_PROJECT_CAMELOT = 18). It ships Camelot/
        # override folders gated on [AllowLoadGameType camelot]. Treating it as
        # camelot+standard is inferred from the TOCs, not documented anywhere.
        Flavor(
            "forever",
            "WoW Forever",
            ("forever", "wowforever", "camelot"),
            ("Forever", "Camelot"),
            frozenset({"camelot", "standard"}),
        ),
    )
}


def resolve_flavor(name: str) -> str:
    """Map a user-supplied flavor name or alias to its key. Raises ValueError."""
    n = name.strip().lower().replace("-", "_").replace(" ", "_")
    for f in FLAVORS.values():
        if n == f.key or n in f.aliases or n == f.label.lower().replace(" ", "_"):
            return f.key
    raise ValueError(f"unknown flavor {name!r}; known: {', '.join(FLAVORS)}")


def interface_from_version(version: str) -> int | None:
    """'2.5.6' -> 20506, '12.1.5' -> 120105 (the TOC ## Interface convention)."""
    try:
        a, b, c = (int(x) for x in version.split(".")[:3])
    except ValueError:
        return None
    return a * 10000 + b * 100 + c

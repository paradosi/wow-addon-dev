"""Unit tests for the pieces that need no index, plus index-backed checks that
skip when the index has not been built (`just refresh`)."""

import json
import subprocess
from pathlib import Path

import pytest

from wow_api_mcp.build import words
from wow_api_mcp.config import DB_PATH, DUMP_LUA, interface_from_version, resolve_flavor
from wow_api_mcp.scan import analyse

needs_index = pytest.mark.skipif(not DB_PATH.exists(), reason="index not built")


def test_interface_from_version():
    assert interface_from_version("2.5.6") == 20506
    assert interface_from_version("1.15.9") == 11509
    assert interface_from_version("12.1.5") == 120105
    assert interface_from_version("junk") is None


def test_resolve_flavor_aliases():
    assert resolve_flavor("retail") == "live"
    assert resolve_flavor("TBC") == "classic_anniversary"
    assert resolve_flavor("vanilla") == "classic_era"
    assert resolve_flavor("camelot") == "forever"
    with pytest.raises(ValueError):
        resolve_flavor("wotlk")


def test_words_splits_camel_and_namespaces():
    assert words("C_Container.GetContainerNumSlots") == "c container get container num slots"
    assert words("UIParent") == "ui parent"


def test_dump_lua_roundtrip(tmp_path: Path):
    f = tmp_path / "Res.lua"
    f.write_text('local Things = {\n\t"a", "b",\n}\nlocal Map = { x = 1, y = "q\\"z" }\n')
    out = json.loads(
        subprocess.run(
            ["lua5.1", str(DUMP_LUA), "res", str(f)], capture_output=True, text=True, check=True
        ).stdout
    )
    assert out == {"Things": ["a", "b"], "Map": {"x": 1, "y": 'q"z'}}


def _refs(tmp_path: Path, src: str):
    f = tmp_path / "t.lua"
    f.write_text(src)
    refs, defines, keys = analyse(f, "t.lua", {"C_AddOns", "C_FriendList"})
    return {(r.name, r.guarded) for r in refs}, defines, keys


def test_guard_idiom_and_or(tmp_path: Path):
    refs, _, _ = _refs(
        tmp_path, "local M = (C_AddOns and C_AddOns.GetAddOnMetadata) or GetAddOnMetadata\n"
    )
    assert ("GetAddOnMetadata", True) in refs
    assert ("C_AddOns.GetAddOnMetadata", True) in refs


def test_guard_if_else_branch_and_closure(tmp_path: Path):
    refs, defines, _ = _refs(
        tmp_path,
        "if C_FriendList then\n  X = C_FriendList.GetNumFriends()\nelse\n"
        "  X = GetNumFriends()\nend\n"
        "if not C_AddOns then\n  C_AddOns = {}\n"
        "  function C_AddOns.GetAddOnMetadata(a, f) return GetAddOnMetadata(a, f) end\nend\n"
        "UnitAura('player', 1)\n",
    )
    assert ("GetNumFriends", True) in refs  # else arm
    assert ("GetAddOnMetadata", True) in refs  # closure created inside the guard
    assert ("UnitAura", False) in refs  # after both blocks: unguarded
    assert {"X", "C_AddOns"} <= defines


def test_method_refs_and_g_assignments(tmp_path: Path):
    refs, defines, keys = _refs(
        tmp_path, 'local s = CreateFrame("Slider")\ns:SetObeyStepOnDrag(true)\n_G["Foo"] = 1\n'
    )
    assert ("SetObeyStepOnDrag", False) in refs
    assert "Foo" in defines


@needs_index
def test_index_known_facts():
    from wow_api_mcp.index import Index

    idx = Index()
    assert set(idx.flavors) >= {"live", "classic_anniversary", "classic_era"}
    assert idx.status("C_Container.GetContainerNumSlots", "classic_era") == "native"
    assert idx.status("UnitAura", "classic_era") == "deprecated_shim"
    assert idx.status("UnitAura", "live") == "absent"
    assert idx.status("DEFAULT_CHAT_FRAME", "classic_era") == "blizzard_ui"
    assert idx.status("Slider:SetObeyStepOnDrag", "live") == "native"

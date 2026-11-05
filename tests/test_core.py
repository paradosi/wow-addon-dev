"""Unit tests for the pieces that need no index, plus index-backed checks that
skip when the index has not been built (`just refresh`)."""

import json
import subprocess
from pathlib import Path

import pytest

from wowdev.build import words
from wowdev.config import DB_PATH, DUMP_LUA, interface_from_version, resolve_flavor
from wowdev.scan import analyse

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
    from wowdev.index import Index

    idx = Index()
    assert set(idx.flavors) >= {"live", "classic_anniversary", "classic_era"}
    assert idx.status("C_Container.GetContainerNumSlots", "classic_era") == "native"
    assert idx.status("UnitAura", "classic_era") == "deprecated_shim"
    assert idx.status("UnitAura", "live") == "absent"
    assert idx.status("DEFAULT_CHAT_FRAME", "classic_era") == "blizzard_ui"
    assert idx.status("Slider:SetObeyStepOnDrag", "live") == "native"
    # defined only in Deprecated_Specialization_[Game].lua: needs TOC placeholder expansion
    assert idx.status("GetTalentInfo", "classic_anniversary") == "deprecated_shim"
    assert idx.status("GetTalentInfo", "classic_era") == "deprecated_shim"


def test_toc_placeholders_expand_per_client():
    from wowdev.build import expand_tokens
    from wowdev.config import FLAVORS

    assert expand_tokens(FLAVORS["classic_anniversary"], "X_[Game].lua") == ["X_TBC.lua"]
    assert expand_tokens(FLAVORS["classic_era"], "[Family]\\A.lua") == ["Classic\\A.lua"]
    assert expand_tokens(FLAVORS["live"], "[Family]\\[Game].lua") == ["Mainline\\Mainline.lua"]
    assert expand_tokens(FLAVORS["live"], "plain.lua") == ["plain.lua"]


# ------------------------------------------------------------------ toc tooling


def _addon(tmp_path: Path) -> Path:
    a = tmp_path / "MyAddon"
    (a / "media" / "textures").mkdir(parents=True)
    (a / "media" / "textures" / "icon.tga").write_bytes(b"")
    (a / ".pkgmeta").write_text("package-as: MyAddon\n")
    (a / "MyAddon.toc").write_text(
        "## Interface: 20505\n## Version: 1.0.0\n"
        "## IconTexture: Interface\\AddOns\\MyAddon\\media\\textures\\icon\nMyAddon.lua\n"
    )
    (a / "MyAddon_Vanilla.toc").write_text(
        "## Interface: 11508\n## Version: 1.0.0\nMyAddon.lua\nMissing.lua\n"
    )
    (a / "MyAddon.lua").write_text(
        'local VERSION = "0.9.0"\n'
        'local t = "|TInterface\\\\AddOns\\\\MyAddon\\\\media\\\\textures\\\\icon:8|t"\n'
        'local bad = "Interface\\\\AddOns\\\\My-Addon\\\\x"\n'
    )
    (a / "CHANGELOG.md").write_text("# Changelog\n\n## [1.0.0] - 2026-01-01\n\n- first\n")
    return a


def test_toc_lint(tmp_path: Path):
    from wowdev.toc import lint

    msgs = [f"{f.level} {f.where} {f.message}" for f in lint(_addon(tmp_path), {205: 20506})]
    assert any("Missing.lua" in m and m.startswith("error") for m in msgs)
    assert any('VERSION = "0.9.0"' in m for m in msgs)
    assert any("20505 < current client 20506" in m for m in msgs)
    assert not any("icon" in m and "does not exist" in m for m in msgs)  # |T…:8|t parsed


def test_bump_plan_keeps_changelog_style(tmp_path: Path):
    from wowdev.toc import apply, changelog_version, plan_bump

    a = _addon(tmp_path)
    edits = plan_bump(a, "1.1.0")
    assert {e.path.name for e in edits} == {
        "MyAddon.toc",
        "MyAddon_Vanilla.toc",
        "MyAddon.lua",
        "CHANGELOG.md",
    }
    apply(edits)
    assert changelog_version(a) == "1.1.0"
    assert "## [1.1.0] - " in (a / "CHANGELOG.md").read_text()
    assert 'local VERSION = "1.1.0"' in (a / "MyAddon.lua").read_text()
    with pytest.raises(ValueError):
        plan_bump(a, "v1.2")


def test_interface_plan_only_raises_known_flavors(tmp_path: Path):
    from wowdev.toc import plan_interface

    a = _addon(tmp_path)
    (a / "MyAddon_Wrath.toc").write_text("## Interface: 30403\n## Version: 1.0.0\n")
    edits = {e.path.name: e.new for e in plan_interface(a, {205: 20506, 115: 11509})}
    assert "## Interface: 20506" in edits["MyAddon.toc"]
    assert "## Interface: 11509" in edits["MyAddon_Vanilla.toc"]
    assert "MyAddon_Wrath.toc" not in edits


def test_flavor_flag_guard(tmp_path: Path):
    refs, _, _ = _refs(
        tmp_path,
        "local P = {}\nif P.WOWRetail then\n  X = NUM_REAGENTBAG_FRAMES\nend\n"
        "if select(4, GetBuildInfo()) >= 110000 then\n  Y = 1\nelse\n"
        "  Y = GetSpellInfo(1)\nend\n",
    )
    assert ("NUM_REAGENTBAG_FRAMES", True) in refs
    assert ("GetSpellInfo", True) in refs


def test_guarded_deprecated_fallback_is_a_note(tmp_path: Path):
    from wowdev.scan import scan

    a = tmp_path / "Shim"
    a.mkdir()
    (a / "Shim.toc").write_text("## Interface: 20506\nShim.lua\n")
    (a / "Shim.lua").write_text(
        "local F = (C_CombatLog and C_CombatLog.GetCurrentEventInfo) or CombatLogGetCurrentEventInfo\n"
        'local b = UnitBuff("player", 1)\n'
    )
    fr = scan(a)["flavors"]["classic_anniversary"]
    assert [m["name"] for m in fr["deprecated_shim"]] == ["UnitBuff"]
    assert "CombatLogGetCurrentEventInfo" in fr["guarded_deprecated"]


def test_index_usable_from_worker_threads():
    from concurrent.futures import ThreadPoolExecutor

    from wowdev.index import Index

    idx = Index()
    idx.status("GetSpellInfo", "classic_era")  # open on this thread first
    with ThreadPoolExecutor(8) as ex:
        got = list(ex.map(lambda _: idx.status("GetSpellInfo", "classic_era"), range(32)))
    assert got == ["native"] * 32

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
    # LoD Blizzard_CombatLogBase is a dependency of always-loaded Blizzard_DeprecatedCombatLog
    assert idx.status("COMBATLOG_FILTER_MINE", "classic_anniversary") == "blizzard_ui"


def test_toc_placeholders_expand_per_client():
    from wowdev.build import expand_tokens
    from wowdev.config import FLAVORS

    assert expand_tokens(FLAVORS["classic_anniversary"], "X_[Game].lua") == ["X_TBC.lua"]
    assert expand_tokens(FLAVORS["classic_era"], "[Family]\\A.lua") == ["Classic\\A.lua"]
    assert expand_tokens(FLAVORS["live"], "[Family]\\[Game].lua") == ["Mainline\\Mainline.lua"]
    assert expand_tokens(FLAVORS["live"], "plain.lua") == ["plain.lua"]


def test_toc_exclude_and_bootstrap_tags(tmp_path: Path, monkeypatch):
    from wowdev import build
    from wowdev.config import FLAVORS

    ui = tmp_path / "ui"
    a = ui / "Interface" / "AddOns" / "Blizzard_Probe"
    (a / "Mainline").mkdir(parents=True)
    for n in ("Boot.lua", "Mainline/Both.lua", "Mainline/NotCamelot.lua", "Lod.lua"):
        (a / n).write_text("X = 1\n")
    (a / "Blizzard_Probe.toc").write_text(
        "## LoadOnDemand: 1\nBoot.lua [Bootstrap]\n[Family]\\Both.lua\n"
        "[Family]\\NotCamelot.lua [AllowLoadGameType mainline] [ExcludeLoadGameType camelot]\n"
        "Lod.lua\n"
    )
    monkeypatch.setattr(type(FLAVORS["live"]), "ui_dir", property(lambda self: ui))
    got = {
        k: {p.name: lod for p, lod in build.loaded_files(FLAVORS[k])} for k in ("live", "forever")
    }
    assert got["live"] == {
        "Boot.lua": False,
        "Both.lua": True,
        "NotCamelot.lua": True,
        "Lod.lua": True,
    }
    assert "NotCamelot.lua" not in got["forever"] and got["forever"]["Boot.lua"] is False


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


def test_game_errors_filters_by_addon_and_session(monkeypatch):
    from wowdev import game

    db = {
        "session": 10,
        "errors": [
            {
                "message": "Interface/AddOns/FloTotemBar/FloLib.lua:1: boom",
                "session": 10,
                "counter": 2,
                "time": 1778463495,
            },
            {
                "message": "Interface/AddOns/Other/x.lua:1: no",
                "session": 10,
                "time": "2026/01/01 00:00:00",
            },
            {
                "message": "x",
                "stack": "[Interface/AddOns/FloTotemBar/FloTotemBar.lua]:5: in f",
                "session": 7,
            },
        ],
    }
    monkeypatch.setattr(game, "_bug_grabber", lambda f: ("p", "t", db))
    r = game.errors("FloTotemBar", "classic_anniversary", sessions=1)
    assert [e["count"] for e in r["errors"]] == [2]
    assert r["errors"][0]["time"].startswith("2026/")
    assert len(game.errors("FloTotemBar", "classic_anniversary", sessions=None)["errors"]) == 2


def test_spells_collect_joins_tables(tmp_path: Path, monkeypatch):
    from wowdev import spells
    from wowdev.config import FLAVORS

    d = tmp_path / "spells-classic_anniversary" / "2.5.6.1"
    d.mkdir(parents=True)
    (d / "SpellName.csv").write_text("ID,Name_lang\n3599,Searing Totem\n13165,Aspect of the Hawk\n")
    (d / "Spell.csv").write_text(
        'ID,NameSubtext_lang,Description_lang\n3599,Rank 1,"Lasts $d, ""hot"""\n'
    )
    (d / "SpellMisc.csv").write_text(
        "ID,SpellID,DifficultyID,DurationIndex,SchoolMask,SpellIconFileDataID\n"
        "1,3599,0,9,4,135825\n2,3599,1,21,1,0\n3,13165,0,21,8,136076\n"
    )
    (d / "SpellDuration.csv").write_text("ID,Duration,MaxDuration\n9,30000,30000\n21,-1,-1\n")
    monkeypatch.setattr(spells, "UPSTREAM_DIR", tmp_path)
    build, rows = spells.collect(FLAVORS["classic_anniversary"])
    assert build == "2.5.6.1"
    by_id = {r[1]: r for r in rows}
    # difficulty-0 SpellMisc row wins; quoted CSV survives
    assert by_id[3599] == (
        "classic_anniversary",
        3599,
        "Searing Totem",
        "Rank 1",
        30000,
        30000,
        4,
        135825,
        'Lasts $d, "hot"',
    )
    assert by_id[13165][2:6] == ("Aspect of the Hawk", None, -1, -1)


@needs_index
def test_spell_lookup_by_id_and_name():
    from wowdev.server import wow_spell

    r = wow_spell("3599", ["tbc", "era"])
    assert r["match"] == "id"
    for f in ("classic_anniversary", "classic_era"):
        (s,) = r["spells"][f]
        assert (s["name"], s["rank"], s["duration"], s["school"]) == (
            "Searing Totem",
            "Rank 1",
            "30 s",
            "fire",
        )
    r = wow_spell("aspect of the hawk", ["tbc"])
    assert r["match"] == "exact"
    assert all(s["duration"] == "until cancelled" for s in r["spells"]["classic_anniversary"])
    assert wow_spell("13165", ["live"])["spells"]["live"] == "absent"


@needs_index
def test_restricted_apis_reported_only_where_secrets_exist(tmp_path: Path):
    from wowdev.index import Index
    from wowdev.scan import scan

    idx = Index()
    assert "SecretInChatMessagingLockdown" in idx.restricted("live")["CHAT_MSG_WHISPER"]
    # Classic marks CLEU HasRestrictions, yet addons use it freely there
    assert idx.restricted("classic_anniversary") == {}
    a = tmp_path / "Probe"
    a.mkdir()
    (a / "Probe.toc").write_text("## Interface: 120105, 20506\n## Title: Probe\ncore.lua\n")
    (a / "core.lua").write_text(
        'local f = CreateFrame("Frame")\nf:RegisterEvent("CHAT_MSG_WHISPER")\n'
        'f:RegisterEvent("PLAYER_LOGIN")\nlocal n = UnitName("target")\n'
    )
    r = scan(a)
    live = r["flavors"]["live"]["restricted"]
    assert set(live) == {"CHAT_MSG_WHISPER", "UnitName"}
    assert live["CHAT_MSG_WHISPER"]["at"] == "core.lua:2"
    assert r["flavors"]["classic_anniversary"]["restricted"] == {}


def test_client_builds_compares_build_info(monkeypatch):
    from wowdev import game
    from wowdev.index import Index

    info = (
        "Branch!STRING:0|Active!DEC:1|Version!STRING:0|Product!STRING:0\n"
        "us|1|2.5.5.68101|wow_anniversary\nus|1|12.2.0.1|wow\nus|1|5.5.4.1|wow_classic\n"
    )
    monkeypatch.setattr(game, "ps", lambda script, timeout=120: info)
    assert game.installed_builds() == {
        "wow_anniversary": "2.5.5.68101",
        "wow": "12.2.0.1",
        "wow_classic": "5.5.4.1",
    }
    monkeypatch.setattr(
        Index,
        "flavors",
        {
            "classic_anniversary": {"version": "2.5.6", "build": "70577"},
            "live": {"version": "12.1.5", "build": "70783"},
        },
    )
    rows = {r["product"]: r for r in game.client_builds()}
    assert rows["wow_anniversary"]["behind"] == "client"
    assert rows["wow"]["behind"] == "index"
    assert rows["wow_classic"]["flavor"] is None


def test_taint_log_grouped_and_filtered(monkeypatch):
    import base64

    from wowdev import game

    log = (
        "11/5 20:31:02.123  An action was blocked in combat because of taint from "
        "FloTotemBar - FloBarEARTHButton1:Show()\n"
        "11/5 20:31:02.123      Interface/AddOns/FloTotemBar/FloLib.lua:500 FloLib_Setup()\n"
        "11/5 20:31:02.123      Interface/AddOns/FloTotemBar/FloTotemBar.lua:120\n"
        "11/5 20:31:05.500  An action was blocked in combat because of taint from "
        "FloTotemBar - FloBarEARTHButton1:Show()\n"
        "11/5 20:31:05.500      Interface/AddOns/FloTotemBar/FloLib.lua:500 FloLib_Setup()\n"
        "11/5 20:32:00.000  Global variable FOO tainted by OtherAddon - "
        "Interface/AddOns/OtherAddon/x.lua:3\n"
    )
    out = (
        'CVAR SET taintLog "1"\nTIME 2026-11-05T20:33:00\n'
        + base64.b64encode(log.encode()).decode()
    )
    monkeypatch.setattr(game, "ps", lambda script, timeout=120: out)
    r = game.taint("FloTotemBar", "classic_anniversary")
    assert r["total"] == 3 and r["blocked"] == 2
    (g,) = r["entries"]
    assert g["count"] == 2 and g["last"].startswith("11/5 20:31:05")
    assert g["stack"][0].startswith("Interface/AddOns/FloTotemBar/FloLib.lua:500")
    assert r["taintLog_cvar"] == 'SET taintLog "1"'
    assert len(game.taint(None, "classic_anniversary")["entries"]) == 2


def test_reused_g_register_is_not_a_global_definition(tmp_path: Path):
    f = tmp_path / "t.lua"
    f.write_text('local f = _G["Foo"]\nlocal bar = {}\nbar.settings = 1\n_G["Real"] = 2\n')
    _, defines, keys = analyse(f, "t.lua", set())
    assert "Real" in defines
    assert "settings" not in defines and "settings" in keys


def test_unused_finds_dead_weight(tmp_path: Path):
    from wowdev.unused import unused

    a = tmp_path / "Demo"
    (a / "media").mkdir(parents=True)
    (a / "Demo.toc").write_text("## Interface: 20506\n## SavedVariables: DemoDB\ncore.lua\n")
    block = "\n".join(f"    total = total + someValue{i} * factor  -- line {i}" for i in range(10))
    (a / "core.lua").write_text(
        "local L = {}\n"
        'L["Used"] = "Used"\nL["Stale"] = "Stale"\n'
        "local function Never() end\n"
        "function Demo_Dead() end\n"
        "function Demo_Live() end\n"
        "DemoDB = DemoDB or {}\n"
        f"function A(total)\n{block}\n    return total\nend\n"
        f"function B(total)\n{block}\n    return total\nend\n"
        'print(L["Used"], Demo_Live, A, B, "media/used")\n'
    )
    (a / "extra.lua").write_text("print(1)\n")
    (a / "media" / "used.tga").write_bytes(b"x")
    (a / "media" / "orphan.tga").write_bytes(b"x")
    (a / ".pkgmeta").write_text("package-as: Demo\nignore:\n  - README.md\n")
    r = unused(a)
    assert r["unloaded"] == ["extra.lua"]
    assert r["media"] == ["media/orphan.tga"]
    assert [g["name"] for g in r["globals"]] == ["Demo_Dead"]
    assert any("Never" in x["message"] for x in r["locals"])
    assert r["duplicates"] and r["duplicates"][0]["count"] == 2

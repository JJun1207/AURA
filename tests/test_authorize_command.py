import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from aura import cli
from aura.authorization import authorize_plan, load_authorization
from aura.profiles import ProfileStore


@pytest.fixture
def authorize_device(tmp_path, monkeypatch):
    shutil.copytree(Path(__file__).parents[1] / "profiles", tmp_path / "profiles")
    monkeypatch.chdir(tmp_path)
    replies = {
        "getprop ro.build.version.release": "14",
        "dumpsys package com.android.chrome": "versionName=156.0.8078.79\n",
        "dumpsys package com.streetwriters.notesnook": "versionName=3.4.15\n",
        "dumpsys package com.whatsapp": "versionName=2.26.43.74\n",
        "dumpsys package org.telegram.messenger": "versionName=13.1.1\n",
        "dumpsys package com.sec.android.app.sbrowser": "versionName=30.0.5.27\n",
    }

    def connect(*, automation, expected_serial=None):
        assert automation is False, "Authority creation must not start UI automation"
        assert expected_serial in {None, 'TEST-S21'}
        return "TEST-S21", SimpleNamespace(shell=lambda command: replies[command])

    monkeypatch.setattr(cli, "_connected_device", connect)
    return replies


def answer(monkeypatch, values, *, selection="1"):
    responses = iter([selection, *values])
    monkeypatch.setattr("builtins.input", lambda prompt: next(responses))


def test_authorize_chrome_creates_usable_exact_offline_grant(authorize_device, monkeypatch, tmp_path):
    answer(monkeypatch, ["research_device_owner_instruction", "Chrome research test", "Examiner", "y"])
    assert cli.main(["authorize", "chrome", "--serial", "TEST-S21"]) == 0
    authority = load_authorization(Path("case-authority.json"))
    doc = authority["document"]
    assert doc["reference"] == "Chrome research test"
    assert doc["confirmed_by"] == "Examiner"
    assert doc["remote_acquisition"] == "denied"
    assert "TEST-S21" in doc["scope"]
    assert len(doc["grants"]) == 1
    grant = doc["grants"][0]
    assert (grant["app_id"], grant["method"], grant["acquisition_environment"]) == ("chrome", "materialize", "device_only")
    assert grant["target_ids"] == ["chrome.account", "chrome.history", "chrome.downloads", "chrome.bookmarks", "chrome.recent_tabs"]
    store = ProfileStore(tmp_path / "profiles")
    resolution = cli._select_app_profile(store, tmp_path / "profiles", "chrome", "com.android.chrome", "156.0.8078.79", "14")
    plan = cli._build_execution_plan("test", ("chrome",), (resolution,))
    assert authorize_plan(plan, authority)[0]["status"] == "planned"
    assert not (tmp_path / "runs").exists()


@pytest.mark.parametrize("app_name", ["sbrowser", "samsung_browser"])
def test_samsung_cli_names_share_canonical_authority(authorize_device, monkeypatch, tmp_path, app_name):
    answer(monkeypatch, ["evaluation_consent", "Samsung test", "Examiner", "yes"])
    assert cli.main(["authorize", app_name, "samsung_browser"]) == 0
    authority = load_authorization(Path("case-authority.json"))
    grants = authority["document"]["grants"]
    assert len(grants) == 1
    assert grants[0]["app_id"] == "samsung_browser"

    def acquire(app_ids, loaded_authority, *, serial=None):
        assert app_ids == ("samsung_browser",)
        assert loaded_authority == authority
        assert serial == 'TEST-S21'
        store = ProfileStore(tmp_path / "profiles")
        resolution = cli._select_app_profile(store, tmp_path / "profiles", "samsung_browser", "com.sec.android.app.sbrowser", "30.0.5.27", "14")
        plan = cli._build_execution_plan("test", app_ids, (resolution,))
        assert authorize_plan(plan, loaded_authority)[0]["status"] == "planned"
        return {"status": "complete"}, 0

    monkeypatch.setattr(cli, "_execute_acquire", acquire)
    assert cli.main(["acquire", app_name, "--authorization", "case-authority.json", "--serial", "TEST-S21"]) == 0


def test_authorize_method_filter_preserves_export_only_scope(authorize_device, monkeypatch):
    answer(monkeypatch, ["evaluation_consent", "Export test", "Examiner", "yes"])
    assert cli.main(["authorize", "whatsapp", "--method", "export", "--output", "export.json"]) == 0
    doc = load_authorization(Path("export.json"))["document"]
    assert [(g["app_id"], g["method"], g["acquisition_environment"]) for g in doc["grants"]] == [("whatsapp", "export", "device_only")]
    assert doc["grants"][0]["target_ids"] == ["whatsapp.chat_export", "whatsapp.export_summary"]


def test_authorize_online_records_remote_permission_and_network_declaration(authorize_device, monkeypatch):
    answer(monkeypatch, ["evaluation_consent", "Remote test", "Examiner", "configured", "Lab AP policy", "yes"], selection="all")
    assert cli.main(["authorize", "telegram", "--environment", "all"]) == 0
    doc = load_authorization(Path("case-authority.json"))["document"]
    assert [g["acquisition_environment"] for g in doc["grants"]] == ["device_only", "controlled_online"]
    assert doc["remote_acquisition"] == "allowed"
    assert doc["network_control"] == {"external_traffic_restriction": "configured", "reference": "Lab AP policy"}


@pytest.mark.parametrize("inputs", [
    ["evaluation_consent", "Test", "Examiner", "n"],
    ["", "Test", "Examiner", "y"],
])
def test_authorize_decline_or_blank_basis_creates_no_file(authorize_device, monkeypatch, inputs):
    answer(monkeypatch, inputs)
    assert cli.main(["authorize", "chrome"]) != 0
    assert not Path("case-authority.json").exists()


def test_authorize_interrupt_leaves_no_file(authorize_device, monkeypatch):
    def interrupt(prompt):
        raise KeyboardInterrupt()
    monkeypatch.setattr("builtins.input", interrupt)
    assert cli.main(["authorize", "chrome"]) != 0
    assert not Path("case-authority.json").exists()


def test_authorize_preserves_existing_document_before_connecting(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("case-authority.json").write_text("existing document")
    monkeypatch.setattr(cli, "_connected_device", lambda **kwargs: pytest.fail("must not connect before checking output"))
    assert cli.main(["authorize", "chrome"]) != 0
    assert Path("case-authority.json").read_text() == "existing document"


@pytest.mark.parametrize("version", ["156.0.8078.80", "unknown"])
def test_authorize_rejects_unmatched_installed_version(authorize_device, version):
    authorize_device["dumpsys package com.android.chrome"] = f"versionName={version}\n"
    assert cli.main(["authorize", "chrome"]) != 0
    assert not Path("case-authority.json").exists()


@pytest.mark.parametrize("arguments", [
    ["chrome", "--method", "export"],
    ["chrome", "whatsapp", "--method", "export"],
])
def test_authorize_rejects_selection_that_omits_a_requested_app(authorize_device, arguments):
    assert cli.main(["authorize", *arguments]) != 0
    assert not Path("case-authority.json").exists()


def test_authorize_notesnook_selects_export_in_both_environments(authorize_device, monkeypatch):
    answer(monkeypatch, ["evaluation_consent", "Notesnook export test", "Examiner", "not_applied", "yes"], selection="2, 4")
    assert cli.main(["authorize", "notesnook"]) == 0
    doc = load_authorization(Path("case-authority.json"))["document"]
    assert [(g["method"], g["acquisition_environment"]) for g in doc["grants"]] == [
        ("export", "device_only"), ("export", "controlled_online")]
    assert doc["remote_acquisition"] == "allowed"
    assert "materialize" not in doc["scope"]
    for version in ("3.4.5", "3.4.15"):
        profile = ProfileStore(Path("profiles")).load_app("notesnook", version)
        target = next(t for t in profile.targets if t.target_id == "notesnook.export")
        assert target.acquisition_environments == ("device_only", "controlled_online")


def test_authorize_excluding_online_does_not_grant_remote_access(authorize_device, monkeypatch):
    answer(monkeypatch, ["evaluation_consent", "Offline only", "Examiner", "yes"], selection="1 2")
    assert cli.main(["authorize", "notesnook", "--environment", "all"]) == 0
    doc = load_authorization(Path("case-authority.json"))["document"]
    assert len(doc["grants"]) == 2
    assert all(g["acquisition_environment"] == "device_only" for g in doc["grants"])
    assert doc["remote_acquisition"] == "denied"


@pytest.mark.parametrize("selection", ["", "0", "5", "1,export"])
def test_authorize_bad_or_empty_scope_selection_writes_nothing(authorize_device, monkeypatch, selection):
    answer(monkeypatch, [], selection=selection)
    assert cli.main(["authorize", "notesnook", "--environment", "all"]) != 0
    assert not Path("case-authority.json").exists()


def test_authorize_browser_online_is_selectable(authorize_device, monkeypatch):
    answer(monkeypatch, ["evaluation_consent", "Browser online test", "Examiner", "not_applied", "yes"], selection="all")
    assert cli.main(["authorize", "chrome", "--environment", "controlled_online"]) == 0
    doc = load_authorization(Path("case-authority.json"))["document"]
    assert [(g["method"], g["acquisition_environment"]) for g in doc["grants"]] == [("materialize", "controlled_online")]
    assert doc["remote_acquisition"] == "allowed"

import json
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
import xml.etree.ElementTree as ET
from zipfile import ZipFile

from PIL import Image, ImageDraw
import pytest

from aura.artifacts import ArtifactStore
from aura.apps.notion import exports
from aura.apps.notion.exports import (
    ExportCollectionError,
    _actions_bounds,
    collect_workspace_exports,
    include_subpages_enabled,
    parse_actions_export,
    parse_export_dialog,
    parse_export_format_option,
    parse_export_preview,
)
from aura.apps.notion.pages import PageCollectionError
from aura.apps.notion.support import (
    NotionCollectorError,
    item_attempt,
    write_json,
)
from aura.journal import EventJournal
from aura.profiles import ProfileStore
from aura.runtime import RunContext
from aura.ui import UiRuntime, UiTimeout


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("name", [r"..\escaped.zip", r"C:\escaped.zip"])
def test_export_pull_keeps_android_name_out_of_host_temp_path(name):
    payload = _zip_bytes("safe.md", b"content")
    destinations = []
    def pull(remote, local):
        destinations.append(local.name)
        # Never write the unsafe candidate, even on the pre-fix implementation.
        if local.name == "export.zip":
            local.write_bytes(payload)
    context = SimpleNamespace(
        app_profile=SimpleNamespace(parameters={"export_root": "/sdcard/Download/Notion"}),
        device=SimpleNamespace(pull=pull),
    )
    try:
        result = exports._pull_export(context, "/sdcard/Download/Notion/" + name, len(payload))
    except ExportCollectionError:
        result = None
    assert destinations == ["export.zip"]
    assert result == payload
GENERAL_ID = "11111111-1111-4111-8111-111111111111"
DATABASE_ID = "22222222-2222-4222-8222-222222222222"


def _tree(nodes: str) -> ET.Element:
    root = ET.fromstring(
        '<hierarchy><node package="notion.id" '
        'class="android.view.View" bounds="[0,0][1080,2400]">'
        f"{nodes}</node></hierarchy>"
    )
    for node in root.iter():
        node.attrib.setdefault("package", "notion.id")
    return root


def _toggle_png(enabled: bool) -> bytes:
    image = Image.new("RGB", (1080, 2400), "white")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (880, 453, 1032, 545),
        radius=46,
        fill=(45, 137, 224) if enabled else (232, 232, 232),
    )
    output = BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


def test_export_controls_are_resolved_from_visible_ui():
    actions = _tree(
        '<node text="Export" class="android.view.MenuItem" '
        'clickable="true" enabled="true" '
        'bounds="[48,1260][1032,1395]" />'
    )
    dialog = _tree(
        '<node text="Export format Markdown &amp; CSV" '
        'class="android.widget.Button" clickable="true" enabled="true" '
        'bounds="[0,297][1080,432]" />'
        '<node text="Include subpages" class="android.view.MenuItem" '
        'clickable="true" enabled="true" bounds="[0,432][1080,567]" />'
        '<node text="Export" class="android.widget.Button" '
        'clickable="true" enabled="true" '
        'bounds="[48,687][1032,747]" />'
    )

    assert parse_actions_export(actions) == (48, 1260, 1032, 1395)
    assert parse_export_dialog(dialog) == {
        "format": "Markdown & CSV",
        "format_bounds": (0, 297, 1080, 432),
        "include_subpages_bounds": (0, 432, 1080, 567),
        "export_bounds": (48, 687, 1032, 747),
    }
    options = _tree(
        '<node text="PDF" class="android.view.MenuItem" '
        'clickable="true" enabled="true" bounds="[48,360][1032,498]" />'
        '<node text="HTML" class="android.view.MenuItem" '
        'clickable="true" enabled="true" bounds="[48,495][1032,633]" />'
        '<node text="Markdown &amp; CSV" class="android.view.MenuItem" '
        'clickable="true" enabled="true" bounds="[48,630][1032,768]" />'
    )
    assert parse_export_format_option(options) == (
        48,
        630,
        1032,
        768,
    )


def test_duplicate_accessibility_nodes_at_same_bounds_are_one_action():
    page = _tree(
        '<node class="android.widget.Button" content-desc="Actions" '
        'clickable="true" enabled="true" bounds="[924,99][1032,210]"/>'
        '<node class="android.widget.Button" content-desc="Actions" '
        'clickable="true" enabled="true" bounds="[924,99][1032,210]"/>'
    )

    assert _actions_bounds(page) == (924, 99, 1032, 210)


def test_include_subpages_state_uses_switch_region_inside_row():
    row = (0, 432, 1080, 567)

    assert include_subpages_enabled(_toggle_png(True), row) is True
    assert include_subpages_enabled(_toggle_png(False), row) is False


def test_preview_exposes_one_zip_and_rightmost_bottom_action():
    root = _tree(
        '<node text="ExportBlock-example.zip" '
        'class="android.widget.TextView" '
        'bounds="[96,1156][984,1228]" />'
        '<node class="android.view.View" clickable="true" enabled="true" '
        'bounds="[0,2088][540,2256]" />'
        '<node class="android.view.View" clickable="true" enabled="true" '
        'bounds="[540,2088][1080,2256]" />'
    )

    assert parse_export_preview(root) == {
        "filename": "ExportBlock-example.zip",
        "download_bounds": (540, 2088, 1080, 2256),
    }


def _page_row(page_id: str, title: str, top: int) -> str:
    return (
        '<node package="notion.id" class="android.view.View" '
        f'resource-id="home-tab.private.page-row.{page_id}" '
        f'bounds="[60,{top}][1020,{top + 125}]">'
        '<node package="notion.id" class="android.view.View" '
        f'bounds="[60,{top}][1020,{top + 125}]">'
        '<node package="notion.id" class="android.view.View" '
        'resource-id="home-tab.private.page-row.expand" '
        f'bounds="[60,{top + 36}][132,{top + 108}]"/>'
        '<node package="notion.id" class="android.widget.TextView" '
        f'text="{title}" bounds="[264,{top + 36}][756,{top + 108}]"/>'
        "</node></node>"
    )


def _zip_bytes(name: str, payload: bytes) -> bytes:
    output = BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr(name, payload)
    return output.getvalue()


class NotionExportDevice:
    def __init__(self, *, missing_preview_for=None):
        self.state = "home"
        self.page_id = None
        self.actions_scrolled = False
        self.include_subpages = False
        self.missing_preview_for = missing_preview_for
        self.files = {}
        self.payloads = {
            GENERAL_ID: _zip_bytes("General.md", b"# General\n"),
            DATABASE_ID: _zip_bytes("Tasks.csv", b"Name\nTask\n"),
        }

    def exists(self, selector):
        return self.count(selector) > 0

    def count(self, selector):
        if selector == {"packageName": "notion.id"}:
            return 1
        if selector.get("resourceId") == "vision_tab_Home":
            return int(self.state == "home")
        return 0

    def click_bounds(self, value, *, anchor="center"):
        assert anchor == "center"
        value = tuple(value)
        if self.state == "home":
            rows = {
                (60, 450, 1020, 575): GENERAL_ID,
                (60, 575, 1020, 700): DATABASE_ID,
            }
            self.page_id = rows[value]
            self.state = "page"
            return
        if self.state == "page":
            assert value == (924, 99, 1032, 210)
            self.actions_scrolled = False
            self.state = "actions"
            return
        if self.state == "actions":
            assert value == (48, 1260, 1032, 1395)
            self.state = "dialog"
            self.include_subpages = False
            return
        if self.state == "dialog":
            if value == (0, 432, 1080, 567):
                self.include_subpages = True
                return
            assert value == (48, 687, 1032, 747)
            assert self.include_subpages is True
            self.state = "preview"
            return
        if self.state == "preview":
            assert value == (540, 2088, 1080, 2256)
            path = f"/sdcard/Download/Notion/{self._filename()}"
            self.files[path] = self.payloads[self.page_id]
            return
        raise AssertionError((self.state, value))

    def swipe(self, direction, duration=0.2):
        assert direction in {"up", "down"}
        if self.state == "actions" and direction == "up":
            self.actions_scrolled = True

    def back(self):
        transitions = {
            "preview": "dialog",
            "dialog": "actions",
            "actions": "page",
            "page": "home",
        }
        self.state = transitions[self.state]

    def hierarchy(self):
        return {
            "home": self._home(),
            "page": self._page(),
            "actions": self._actions(),
            "dialog": self._dialog(),
            "preview": self._preview(),
        }[self.state]

    def screenshot(self):
        image = Image.new("RGB", (1080, 2400), "white")
        if self.state == "dialog":
            ImageDraw.Draw(image).rounded_rectangle(
                (880, 453, 1032, 545),
                radius=46,
                fill=(
                    (45, 137, 224)
                    if self.include_subpages
                    else (232, 232, 232)
                ),
            )
        output = BytesIO()
        image.save(output, "PNG")
        return output.getvalue()

    def shell(self, command):
        assert "/sdcard/Download/Notion" in command
        return "".join(
            path
            + "\0"
            + str(len(payload))
            + "\0"
            + "1746240000"
            + "\0"
            for path, payload in sorted(self.files.items())
        )

    def pull(self, remote_path, local_path):
        Path(local_path).write_bytes(self.files[remote_path])

    def window_size(self):
        return 1080, 2400

    def _filename(self):
        label = (
            "general"
            if self.page_id == GENERAL_ID
            else "tasks"
        )
        return f"ExportBlock-{label}.zip"

    @staticmethod
    def _home():
        return (
            "<hierarchy>"
            '<node package="notion.id" class="android.view.View" '
            'resource-id="vision_tab_Home" selected="true" '
            'bounds="[204,80][460,224]"/>'
            '<node package="notion.id" class="android.view.View" '
            'resource-id="home-tab.sections.private-header" '
            'clickable="true" bounds="[0,325][1080,450]"/>'
            f"{_page_row(GENERAL_ID, 'General', 450)}"
            f"{_page_row(DATABASE_ID, 'Tasks', 575)}"
            "</hierarchy>"
        )

    def _page(self):
        title = "General" if self.page_id == GENERAL_ID else "Tasks"
        if self.page_id == GENERAL_ID:
            body = (
                '<node package="notion.id" '
                'class="android.widget.EditText" '
                'text="General&#10;&#10;Body" clickable="false" '
                'bounds="[0,234][1080,2400]"/>'
            )
        else:
            body = (
                '<node package="notion.id" '
                'class="android.widget.Button" '
                'content-desc="Filter and Sort" clickable="true" '
                'bounds="[612,1029][708,1128]"/>'
                '<node package="notion.id" '
                'class="android.widget.Button" '
                'content-desc="Edit view layout, grouping and more..." '
                'clickable="true" bounds="[708,1029][804,1128]"/>'
            )
        return (
            "<hierarchy>"
            '<node package="notion.id" class="android.widget.Button" '
            'content-desc="Back" clickable="true" '
            'bounds="[48,99][156,210]"/>'
            '<node package="notion.id" class="android.widget.Button" '
            'content-desc="Actions" clickable="true" '
            'bounds="[924,99][1032,210]"/>'
            '<node package="notion.id" class="android.widget.EditText" '
            f'text="{title}" clickable="true" '
            'bounds="[54,492][1026,615]"/>'
            f"{body}</hierarchy>"
        )

    def _actions(self):
        export = (
            '<node package="notion.id" class="android.view.MenuItem" '
            'text="Export" clickable="true" enabled="true" '
            'bounds="[48,1260][1032,1395]"/>'
            if self.actions_scrolled
            else ""
        )
        return (
            "<hierarchy>"
            '<node package="notion.id" class="android.widget.TextView" '
            'text="Actions" bounds="[360,177][720,309]"/>'
            f"{export}</hierarchy>"
        )

    @staticmethod
    def _dialog():
        return (
            "<hierarchy>"
            '<node package="notion.id" class="android.widget.TextView" '
            'text="Export" bounds="[360,78][720,210]"/>'
            '<node package="notion.id" class="android.widget.Button" '
            'text="Export format Markdown &amp; CSV" clickable="true" '
            'bounds="[0,297][1080,432]"/>'
            '<node package="notion.id" class="android.view.MenuItem" '
            'text="Include subpages" clickable="true" '
            'bounds="[0,432][1080,567]"/>'
            '<node package="notion.id" class="android.widget.Button" '
            'text="Export" clickable="true" '
            'bounds="[48,687][1032,747]"/>'
            "</hierarchy>"
        )

    def _preview(self):
        filename = (
            ""
            if self.page_id == self.missing_preview_for
            else (
                '<node package="notion.id" '
                'class="android.widget.TextView" '
                f'text="{self._filename()}" '
                'bounds="[96,1156][984,1228]"/>'
            )
        )
        return (
            "<hierarchy>"
            f"{filename}"
            '<node package="notion.id" class="android.view.View" '
            'clickable="true" enabled="true" '
            'bounds="[0,2088][540,2256]"/>'
            '<node package="notion.id" class="android.view.View" '
            'clickable="true" enabled="true" '
            'bounds="[540,2088][1080,2256]"/>'
            "</hierarchy>"
        )


class TransientExportPageDevice(NotionExportDevice):
    def __init__(self):
        super().__init__()
        self.page_hierarchy_calls = 0

    def click_bounds(self, value, *, anchor="center"):
        previous_state = self.state
        super().click_bounds(value, anchor=anchor)
        if previous_state == "home" and self.state == "page":
            self.page_hierarchy_calls = 0

    def hierarchy(self):
        if self.state == "page" and self.page_id == GENERAL_ID:
            self.page_hierarchy_calls += 1
            if self.page_hierarchy_calls in {2, 3}:
                return (
                    '<hierarchy><node package="notion.id" '
                    'class="android.widget.EditText" text="General" '
                    'clickable="true" bounds="[54,492][1026,615]"/>'
                    '</hierarchy>'
                )
        return super().hierarchy()


def _export_context(tmp_path, *, device=None, **device_options):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    journal = EventJournal(
        run_dir / "events.jsonl",
        {"run_id": "notion-export"},
    )
    artifacts = ArtifactStore(run_dir, journal)
    device = device or NotionExportDevice(**device_options)
    profile = ProfileStore(ROOT / "profiles").load_app(
        "notion", "0.6.4030"
    )
    profile.timings.update({
        "default_timeout": 0.01,
        "poll_interval": 0.0,
        "transition_settle": 0.0,
        "inventory_probe_interval": 0.0,
    })
    context = RunContext(
        run_dir=run_dir,
        base_context={"run_id": "notion-export"},
        device=device,
        journal=journal,
        artifacts=artifacts,
        ui=UiRuntime(
            device,
            journal,
            default_timeout=0.01,
            poll_interval=0.0,
        ),
        app_profile=profile,
        system_ui_profile=ProfileStore(ROOT / "profiles").load_system_ui(
            "samsung"
        ),
    )
    return context


def _run_export_fixture(tmp_path):
    context = _export_context(tmp_path)
    report = collect_workspace_exports(
        context,
        {"workspace_ref": "workspace-000001"},
        "notion/workspaces/workspace-000001 (Workspace)",
        "account.notion.test.001",
    )
    return report, context


def test_notion_item_identity_includes_parent_scope(tmp_path):
    context = _export_context(tmp_path)

    first = item_attempt(
        context,
        {
            "target_ref": "account.notion.test.001",
            "workspace_ref": "workspace-000001",
            "page_ref": "page-000001",
        },
        record_kind="notion_page_export",
    )
    second = item_attempt(
        context,
        {
            "target_ref": "account.notion.test.001",
            "workspace_ref": "workspace-000002",
            "page_ref": "page-000001",
        },
        record_kind="notion_page_export",
    )

    assert first != second
    assert len(context.items) == 2

    attachment_one = item_attempt(
        context,
        {
            "target_ref": "account.notion.test.001",
            "workspace_ref": "workspace-000001",
            "page_ref": "page-000010",
            "database_item_ref": "database-item-000001",
            "attachment_ref": "attachment-000001",
        },
        record_kind="notion_attachment",
    )
    attachment_two = item_attempt(
        context,
        {
            "target_ref": "account.notion.test.001",
            "workspace_ref": "workspace-000001",
            "page_ref": "page-000010",
            "database_item_ref": "database-item-000002",
            "attachment_ref": "attachment-000001",
        },
        record_kind="notion_attachment",
    )

    assert attachment_one != attachment_two


def test_export_summary_registers_unretained_results(tmp_path):
    context = _export_context(tmp_path)
    action = context.journal.record_action("summarize-export", status="success")

    write_json(
        context,
        "notion/workspaces/workspace-000001/exports/exports.json",
        {
            "record_kind": "notion_exports",
            "target_ref": "account.notion.test.001",
            "workspace_ref": "workspace-000001",
            "status": "partial",
            "exports": [
                {
                    "page_ref": "page-000001",
                    "acquisition_status": "partial",
                    "reason_code": "export_failed",
                },
                {
                    "page_ref": "page-000002",
                    "acquisition_status": "not_attempted",
                    "reason_code": None,
                },
            ],
        },
        action,
        {
            "target_ref": "account.notion.test.001",
            "workspace_ref": "workspace-000001",
        },
    )

    assert {
        item.source.get("page_ref")
        for item in context.items
        if item.source.get("page_ref")
    } >= {
        "page-000001",
        "page-000002",
    }
    assert {outcome.reason for outcome in context.outcomes} == {
        "export_failed"
    }


def test_workspace_summary_registers_later_workspaces_as_not_attempted(
    tmp_path,
):
    context = _export_context(tmp_path)
    action = context.journal.record_action("summarize-workspaces", status="success")

    write_json(
        context,
        "notion/account/workspaces.json",
        {
            "record_kind": "notion_workspaces",
            "target_ref": "account.notion.test.001",
            "status": "partial",
            "reason_code": "notion_workspace_selection_failed",
            "failed_workspace_ref": "workspace-000002",
            "discovered_workspace_refs": [
                "workspace-000001",
                "workspace-000002",
                "workspace-000003",
            ],
            "workspaces": [
                {
                    "workspace_ref": "workspace-000001",
                    "acquisition_status": "complete",
                }
            ],
        },
        action,
        {"target_ref": "account.notion.test.001"},
    )

    items = {
        item.source.get("workspace_ref"): item
        for item in context.items
        if item.source.get("workspace_ref")
    }
    attempts = {
        item.acquisition_item_id: next(
            attempt
            for attempt in context.attempts
            if attempt.acquisition_item_id == item.acquisition_item_id
        )
        for item in items.values()
    }
    assert attempts[items["workspace-000002"].acquisition_item_id].reason == (
        "notion_workspace_selection_failed"
    )
    later = attempts[items["workspace-000003"].acquisition_item_id]
    assert later.procedure_status.value == "not_attempted"
    assert later.acquisition_status is None


@pytest.mark.parametrize("status", ["out_of_scope", "not_attempted"])
def test_summary_exclusion_preserves_existing_evidence_interval(tmp_path, status):
    context = _export_context(tmp_path)
    linked = {"target_ref": "account.notion.test.001", "workspace_ref": "workspace-000001", "page_ref": "page-000001"}
    attempt_id = item_attempt(context, linked, record_kind="notion_page_export")
    action = context.linked_action("observe_export_scope", attempt_id)
    context.retain_observation("notion/export-scope", b"png", b"<hierarchy/>", attempt_id=attempt_id, action=action)
    started_at = context.attempts[0].started_at
    summary_action = context.journal.record_action("summarize_exports", status="success")
    write_json(context, "notion/exports.json", {
        "record_kind": "notion_exports", "target_ref": linked["target_ref"], "workspace_ref": linked["workspace_ref"],
        "exports": [
            {"page_ref": "page-000001", "acquisition_status": status, "reason_code": "scope_excluded"},
            {"page_ref": "page-000002", "acquisition_status": "not_attempted"},
        ],
    }, summary_action, {"target_ref": linked["target_ref"], "workspace_ref": linked["workspace_ref"]})
    observed = next(a for a in context.attempts if a.attempt_id == attempt_id)
    assert observed.procedure_status.value == "completed"
    assert observed.acquisition_status.value == "not_acquired"
    assert observed.started_at == started_at and observed.ended_at is not None
    untouched = next(a for a in context.attempts if a.context.get("page_ref") == "page-000002")
    assert untouched.procedure_status.value == "not_attempted"
    assert untouched.started_at is untouched.ended_at is untouched.acquisition_status is None


def test_collect_workspace_exports_retains_general_and_database_zips(
    tmp_path,
):
    report, context = _run_export_fixture(tmp_path)

    assert report["status"] == "complete"
    assert report["discovered_top_level_count"] == 2
    assert report["export_count"] == 2
    assert report["general_page_export_count"] == 1
    assert report["database_export_count"] == 1
    assert report["include_subpages_count"] == 2

    paths = {record.relative_path for record in context.artifacts.records}
    prefix = (
        "artifacts/notion/workspaces/workspace-000001 (Workspace)/"
        "exports"
    )
    assert {
        f"{prefix}/page-000001 (General)/ExportBlock-general.zip",
        f"{prefix}/page-000002 (Tasks)/ExportBlock-tasks.zip",
        f"{prefix}/exports.json",
    } <= paths

    summary = json.loads(
        (
            context.run_dir
            / f"{prefix}/exports.json"
        ).read_text(encoding="utf-8")
    )
    assert [
        item["classification"] for item in summary["exports"]
    ] == ["general_page", "database"]
    assert all(
        item["include_subpages"] is True
        and item["acquisition_status"] == "complete"
        for item in summary["exports"]
    )
    assert {item.item_type for item in context.items} >= {
        "page_export",
        "database_export",
    }


def test_export_page_observation_waits_for_parseable_stable_tree(tmp_path):
    context = _export_context(tmp_path, device=TransientExportPageDevice())

    report = collect_workspace_exports(
        context,
        {"workspace_ref": "workspace-000001"},
        "notion/workspaces/workspace-000001 (Workspace)",
        "account.notion.test.001",
    )

    assert report["status"] == "complete"
    assert report["export_count"] == 2


def test_workspace_export_retains_completed_zip_before_partial_result(
    tmp_path,
):
    context = _export_context(
        tmp_path,
        missing_preview_for=DATABASE_ID,
    )

    with pytest.raises(ExportCollectionError) as captured:
        collect_workspace_exports(
            context,
            {"workspace_ref": "workspace-000001"},
            "notion/workspaces/workspace-000001 (Workspace)",
            "account.notion.test.001",
        )

    assert captured.value.reason_code == (
        "notion_export_preview_unavailable"
    )
    assert captured.value.report["status"] == "partial"
    assert captured.value.report["export_count"] == 1
    assert [
        item["acquisition_status"]
        for item in captured.value.report["exports"]
    ] == ["complete", "partial"]
    assert (
        context.run_dir
        / "artifacts/notion/workspaces/"
        "workspace-000001 (Workspace)/exports/"
        "page-000001 (General)/ExportBlock-general.zip"
    ).is_file()
    events = [
        json.loads(line)
        for line in context.journal.path.read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    assert any(
        event["event_type"] == "observation_recorded"
        and event["context"].get("observation_id", "").startswith(
            "observation_notion_export_recovery"
        )
        for event in events
    )
    failed_items = [
        item
        for item in context.items
        if item.source.get("page_ref") == "page-000002"
    ]
    assert [item.item_type for item in failed_items] == ["database_export"]
    failed_attempt = next(
        attempt
        for attempt in context.attempts
        if attempt.acquisition_item_id
        == failed_items[0].acquisition_item_id
    )
    assert {
        artifact.attempt_id
        for artifact in context.artifacts.records
        if artifact.context.get("page_ref") == "page-000002"
    } == {failed_attempt.attempt_id}
    assert {
        outcome.attempt_id
        for outcome in context.outcomes
        if outcome.attempt_id == failed_attempt.attempt_id
    } == {failed_attempt.attempt_id}


def test_post_export_navigation_failure_keeps_completed_record(
    tmp_path,
    monkeypatch,
):
    context = _export_context(tmp_path)

    def fail_return(*args):
        raise NotionCollectorError("hierarchy did not settle")

    monkeypatch.setattr(exports, "_return_to_page", fail_return)

    with pytest.raises(ExportCollectionError) as captured:
        collect_workspace_exports(
            context,
            {"workspace_ref": "workspace-000001"},
            "notion/workspaces/workspace-000001 (Workspace)",
            "account.notion.test.001",
        )

    assert captured.value.report["export_count"] == 1
    retained = captured.value.report["exports"][0]
    assert retained["acquisition_status"] == "complete"
    assert retained["post_acquisition_status"] == "partial"
    assert retained["post_acquisition_reason_code"] == (
        "notion_export_page_return_failed"
    )


def test_post_export_home_failure_keeps_completed_record(
    tmp_path,
    monkeypatch,
):
    context = _export_context(tmp_path)
    original_back = context.ui.back

    def fail_home(*args, **kwargs):
        if context.device.state == "page":
            raise UiTimeout("home unavailable")
        return original_back(*args, **kwargs)

    monkeypatch.setattr(context.ui, "back", fail_home)

    with pytest.raises(ExportCollectionError) as captured:
        collect_workspace_exports(
            context,
            {"workspace_ref": "workspace-000001"},
            "notion/workspaces/workspace-000001 (Workspace)",
            "account.notion.test.001",
        )

    assert captured.value.report["export_count"] == 1
    retained = captured.value.report["exports"][0]
    assert retained["acquisition_status"] == "complete"
    assert retained["post_acquisition_status"] == "partial"


def test_page_lookup_failure_writes_partial_export_summary(
    tmp_path,
    monkeypatch,
):
    context = _export_context(tmp_path)

    def fail_lookup(*args, **kwargs):
        raise PageCollectionError("notion_page_row_unavailable")

    monkeypatch.setattr(exports, "_find_home_item", fail_lookup)

    with pytest.raises(ExportCollectionError) as captured:
        collect_workspace_exports(
            context,
            {"workspace_ref": "workspace-000001"},
            "notion/workspaces/workspace-000001 (Workspace)",
            "account.notion.test.001",
        )

    assert captured.value.reason_code == "notion_page_row_unavailable"
    assert [
        item["acquisition_status"]
        for item in captured.value.report["exports"]
    ] == ["partial", "not_attempted"]
    assert (
        context.run_dir
        / "artifacts/notion/workspaces/"
        "workspace-000001 (Workspace)/exports/exports.json"
    ).is_file()

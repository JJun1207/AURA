import hashlib
import json
import xml.etree.ElementTree as ET
from dataclasses import replace
from itertools import count
from pathlib import Path
from types import SimpleNamespace

import pytest

from aura.apps.notion import attachments
from aura.apps.notion.support import NotionCollectorError
from aura.artifacts import ArtifactStore
from aura.journal import EventJournal
from aura.profiles import ProfileStore
from aura.runtime import RunContext
from aura.ui import UiRuntime


ROOT = Path(__file__).resolve().parents[1]


def _bounds(value):
    return f"[{value[0]},{value[1]}][{value[2]},{value[3]}]"


def _node(
    parent,
    class_name,
    box,
    *,
    text="",
    description="",
    resource_id="",
    clickable=False,
    focusable=False,
):
    return ET.SubElement(parent, "node", {
        "package": "notion.id",
        "class": class_name,
        "bounds": _bounds(box),
        "text": text,
        "content-desc": description,
        "resource-id": resource_id,
        "clickable": str(clickable).lower(),
        "focusable": str(focusable).lower(),
        "enabled": "true",
    })


def _page_tree(
    *,
    file_buttons=(),
    image_bounds=None,
    decorative_images=(),
    video_menu_bounds=None,
    audio_menu_bounds=None,
    external_url=None,
):
    root = ET.Element("hierarchy")
    document = _node(
        root,
        "android.widget.EditText",
        (0, 234, 1080, 2400),
        text="AURA Attachment Test",
        focusable=True,
    )
    if external_url:
        _node(
            document,
            "android.widget.EditText",
            (72, 300, 1008, 387),
            text=external_url,
            clickable=True,
            focusable=True,
        )
    for text, box in file_buttons:
        _node(
            document,
            "android.widget.Button",
            box,
            text=text,
            clickable=True,
            focusable=True,
        )
    if image_bounds:
        _node(document, "android.widget.Image", image_bounds)
    for box in decorative_images:
        _node(document, "android.widget.Image", box)
    if video_menu_bounds:
        video = _node(
            document,
            "android.view.View",
            (78, video_menu_bounds[1] - 18, 1002, 1900),
            focusable=True,
        )
        _node(
            video,
            "android.widget.Button",
            (372, video_menu_bounds[1] + 200, 708, 1900),
            description="play",
            clickable=True,
            focusable=True,
        )
        _node(
            video,
            "android.widget.Button",
            (714, video_menu_bounds[1] + 200, 858, 1900),
            description="enter full screen",
            clickable=True,
        )
        _node(
            video,
            "android.widget.Button",
            video_menu_bounds,
            description="Open block actions menu",
            clickable=True,
            focusable=True,
        )
    if audio_menu_bounds:
        audio = _node(
            document,
            "android.view.View",
            (78, audio_menu_bounds[1] - 33, 1002, audio_menu_bounds[3] + 33),
            focusable=True,
        )
        _node(
            audio,
            "android.widget.Button",
            (108, audio_menu_bounds[1], 204, audio_menu_bounds[3]),
            description="play",
            clickable=True,
            focusable=True,
        )
        _node(
            audio,
            "android.widget.SeekBar",
            (420, audio_menu_bounds[1], 780, audio_menu_bounds[3]),
            clickable=True,
        )
        _node(
            audio,
            "android.widget.Button",
            (780, audio_menu_bounds[1], 876, audio_menu_bounds[3]),
            description="mute",
            clickable=True,
        )
        _node(
            audio,
            "android.widget.Button",
            audio_menu_bounds,
            clickable=True,
            focusable=True,
        )
    return root


def _generic_preview(*, filename=None, video=False, extra_action=False):
    root = ET.Element("hierarchy")
    _node(
        root,
        "android.view.View",
        (0, 80, 1080, 2088),
        clickable=True,
        focusable=True,
    )
    if filename:
        _node(
            root,
            "android.widget.TextView",
            (378, 1156, 702, 1228),
            text=filename,
        )
    if video:
        _node(
            root,
            "android.widget.Button",
            (372, 878, 708, 1217),
            description="play",
            clickable=True,
        )
        _node(
            root,
            "android.widget.SeekBar",
            (0, 2015, 1080, 2089),
            clickable=True,
        )
    _node(
        root,
        "android.view.View",
        (0, 2088, 540, 2256),
        clickable=True,
        focusable=True,
    )
    _node(
        root,
        "android.view.View",
        (540, 2088, 1080, 2256),
        clickable=True,
        focusable=True,
    )
    if extra_action:
        _node(
            root,
            "android.view.View",
            (360, 2088, 720, 2256),
            clickable=True,
            focusable=True,
        )
    return root


def _image_gallery():
    root = ET.Element("hierarchy")
    _node(
        root,
        "v2i",
        (0, 0, 1080, 2400),
        resource_id="notion.id:id/imagesPager",
    )
    _node(
        root,
        "android.widget.ImageButton",
        (60, 119, 120, 185),
        description="Close button",
        resource_id="notion.id:id/close_button",
        clickable=True,
    )
    _node(
        root,
        "android.widget.ImageButton",
        (676, 2124, 808, 2256),
        resource_id="notion.id:id/download",
        clickable=True,
    )
    return root


def _audio_menu(*, duplicate=False):
    root = ET.Element("hierarchy")
    _node(
        root,
        "android.view.MenuItem",
        (360, 561, 960, 702),
        description="download media",
        clickable=True,
    )
    if duplicate:
        _node(
            root,
            "android.view.MenuItem",
            (360, 702, 960, 843),
            description="download media",
            clickable=True,
        )
    return root


def _video_actions_on_page():
    root = _page_tree(video_menu_bounds=(912, 1341, 984, 1416))
    _node(
        root,
        "android.widget.TextView",
        (48, 873, 1032, 963),
        text="Actions",
    )
    _node(
        root,
        "android.widget.TextView",
        (48, 987, 1032, 1077),
        text="Video",
    )
    _node(
        root,
        "android.view.View",
        (48, 1113, 1032, 1251),
        text="View original",
        clickable=True,
    )
    return root


def test_parse_page_candidates_keeps_each_supported_occurrence():
    rows = attachments.parse_page_candidates(_page_tree(
        file_buttons=(
            ("duplicate.txt 54 B", (72, 543, 1008, 636)),
            ("duplicate.txt 54 B", (72, 663, 1008, 756)),
        ),
        image_bounds=(114, 795, 966, 1278),
        video_menu_bounds=(912, 1341, 984, 1416),
        audio_menu_bounds=(876, 1653, 972, 1752),
        external_url="https://example.com",
    ))
    assert [
        (row["kind"], row["displayed_name"], row["location"])
        for row in rows
    ] == [
        ("external_url", "https://example.com", "page_body"),
        ("file", "duplicate.txt", "page_body"),
        ("file", "duplicate.txt", "page_body"),
        ("image", None, "page_body"),
        ("video", None, "page_body"),
        ("audio", None, "page_body"),
    ]
    assert rows[1]["displayed_size"] == "54 B"
    assert rows[1]["bounds"] != rows[2]["bounds"]


def test_parse_page_candidates_rejects_small_decorative_images():
    root = _page_tree(
        decorative_images=((78, 1053, 150, 1128),),
    )
    assert attachments.parse_page_candidates(root) == ()


def test_parse_page_candidates_ignores_floating_toolbar_overlap():
    root = _page_tree(
        audio_menu_bounds=(876, 2166, 972, 2265),
    )
    _node(
        root,
        "android.view.View",
        (888, 2100, 1032, 2244),
        resource_id="floating-toolbar.create-button",
        clickable=True,
    )

    assert attachments.parse_page_candidates(root) == ()


def test_parse_file_preview_uses_structural_right_action():
    assert attachments.parse_materialize_control(
        _generic_preview(filename="aura-attachment.txt"),
        kind="file",
        expected_filename="aura-attachment.txt",
    ) == (540, 2088, 1080, 2256)


def test_parse_image_gallery_uses_download_resource_id():
    assert attachments.parse_materialize_control(
        _image_gallery(),
        kind="image",
        expected_filename=None,
    ) == (676, 2124, 808, 2256)


def test_parse_video_preview_requires_player_and_right_action():
    assert attachments.parse_materialize_control(
        _generic_preview(video=True),
        kind="video",
        expected_filename=None,
    ) == (540, 2088, 1080, 2256)


def test_parse_audio_menu_uses_download_media():
    assert attachments.parse_materialize_control(
        _audio_menu(),
        kind="audio",
        expected_filename=None,
    ) == (360, 561, 960, 702)


def test_parse_materialize_control_rejects_wrong_file():
    with pytest.raises(NotionCollectorError):
        attachments.parse_materialize_control(
            _generic_preview(filename="other.txt"),
            kind="file",
            expected_filename="aura-attachment.txt",
        )


def test_parse_materialize_control_rejects_ambiguous_actions():
    with pytest.raises(NotionCollectorError):
        attachments.parse_materialize_control(
            _generic_preview(filename="a.txt", extra_action=True),
            kind="file",
            expected_filename="a.txt",
        )
    with pytest.raises(NotionCollectorError):
        attachments.parse_materialize_control(
            _audio_menu(duplicate=True),
            kind="audio",
            expected_filename=None,
        )


def test_select_changed_file_requires_one_candidate():
    before = (
        ("/sdcard/Download/Notion/old.txt", 3, 10),
    )
    after = (
        ("/sdcard/Download/Notion/old.txt", 3, 10),
        ("/sdcard/Download/Notion/new.txt", 7, 11),
    )
    assert attachments.select_changed_file(before, after) == (
        "/sdcard/Download/Notion/new.txt",
        7,
        11,
    )


def test_select_changed_file_rejects_ambiguity():
    with pytest.raises(attachments.AttachmentCollectionError):
        attachments.select_changed_file((), (
            ("/sdcard/Download/Notion/a.txt", 1, 1),
            ("/sdcard/Download/Notion/b.txt", 1, 1),
        ))


def test_scan_files_uses_nul_delimited_inventory():
    roots = attachments._ATTACHMENT_ROOTS
    device = SimpleNamespace(
        shell=lambda command: (
            "/sdcard/Download/Notion/name with spaces.txt"
            "\0" "7" "\0" "11" "\0"
        ),
    )
    context = SimpleNamespace(device=device)

    assert attachments._scan_files(context, roots) == ((
        "/sdcard/Download/Notion/name with spaces.txt",
        7,
        11,
    ),)


_FIXTURE = b"AURA Notion attachment fixture\ncase=general-page-file\n"


class _MaterializeDevice:
    def __init__(self):
        self.state = "page"
        self.downloaded = False
        self.clicks = []
        self.shell_count = 0
        self.pull_count = 0

    def hierarchy(self):
        root = (
            _generic_preview(filename="aura-attachment.txt")
            if self.state == "preview"
            else _page_tree(file_buttons=(
                ("aura-attachment.txt 54 B", (72, 543, 1008, 636)),
            ))
        )
        return ET.tostring(root, encoding="unicode")

    def screenshot(self):
        return b"\x89PNG\r\n\x1a\nnotion-attachment"

    def click_bounds(self, box, *, anchor="center"):
        self.clicks.append((tuple(box), anchor))
        if self.state == "page":
            self.state = "preview"
        elif self.state == "preview":
            self.downloaded = True

    def back(self):
        self.state = "page"

    def shell(self, command):
        self.shell_count += 1
        if not self.downloaded:
            return ""
        return (
            "/sdcard/Download/Notion/aura-attachment.txt"
            "\0" "54" "\0" "101" "\0"
        )

    def pull(self, remote_path, local_path):
        self.pull_count += 1
        assert remote_path == (
            "/sdcard/Download/Notion/aura-attachment.txt"
        )
        Path(local_path).write_bytes(_FIXTURE)

    @staticmethod
    def window_size():
        return 1080, 2400


class _ImageMaterializeDevice(_MaterializeDevice):
    def hierarchy(self):
        root = (
            _image_gallery()
            if self.state == "preview"
            else _page_tree(image_bounds=(114, 675, 966, 1158))
        )
        return ET.tostring(root, encoding="unicode")

    def shell(self, command):
        self.shell_count += 1
        if not self.downloaded:
            return ""
        return (
            "/sdcard/Pictures/Notion/1000007859.png"
            "\0" "54" "\0" "101" "\0"
        )

    def pull(self, remote_path, local_path):
        self.pull_count += 1
        assert remote_path == (
            "/sdcard/Pictures/Notion/1000007859.png"
        )
        Path(local_path).write_bytes(_FIXTURE)


class _VideoMaterializeDevice(_MaterializeDevice):
    def hierarchy(self):
        if self.state == "actions":
            root = _video_actions_on_page()
        elif self.state == "preview":
            root = _generic_preview(video=True)
        else:
            root = _page_tree(
                video_menu_bounds=(912, 1341, 984, 1416),
            )
        return ET.tostring(root, encoding="unicode")

    def click_bounds(self, box, *, anchor="center"):
        self.clicks.append((tuple(box), anchor))
        if self.state == "page":
            self.state = "actions"
        elif self.state == "actions":
            self.state = "preview"
        elif self.state == "preview":
            self.downloaded = True

    def shell(self, command):
        self.shell_count += 1
        if not self.downloaded:
            return ""
        return (
            "/sdcard/Movies/Notion/video.mp4"
            "\0" "54" "\0" "101" "\0"
        )

    def pull(self, remote_path, local_path):
        self.pull_count += 1
        assert remote_path == "/sdcard/Movies/Notion/video.mp4"
        Path(local_path).write_bytes(_FIXTURE)


class _NoDownloadDevice(_MaterializeDevice):
    def shell(self, command):
        self.shell_count += 1
        return ""


class _NoPreviewDevice(_MaterializeDevice):
    def __init__(self):
        super().__init__()
        self.back_count = 0

    def click_bounds(self, box, *, anchor="center"):
        self.clicks.append((tuple(box), anchor))

    def back(self):
        self.back_count += 1
        super().back()


def _materialize_context(tmp_path, device=None):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    journal = EventJournal(
        run_dir / "events.jsonl",
        {"run_id": "notion-attachment"},
    )
    device = device or _MaterializeDevice()
    profile = ProfileStore(ROOT / "profiles").load_app(
        "notion", "0.6.4030"
    )
    profile = replace(
        profile,
        parameters={
            **profile.parameters,
            "attachment_roots": [
                "/sdcard/Download/Notion",
                "/sdcard/Pictures/Notion",
                "/sdcard/Movies/Notion",
                "/sdcard/Music/Notion",
            ],
            "inventory_probes": 3,
        },
        timings={
            **profile.timings,
            "transition_settle": 0.0,
            "inventory_probe_interval": 0.0,
        },
    )
    return RunContext(
        run_dir=run_dir,
        base_context={"run_id": "notion-attachment"},
        device=device,
        journal=journal,
        artifacts=ArtifactStore(run_dir, journal),
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


def _entry_observation(context):
    tree = context.device.hierarchy()
    return {
        "observation_id": "page-entry",
        "root": ET.fromstring(tree),
        "screen": context.device.screenshot(),
        "tree": tree.encode(),
    }


def _page_verified(root):
    return any(
        node.get("text") == "AURA Attachment Test"
        for node in root.iter()
    )


def test_materialize_candidate_writes_payload_record_and_pair(tmp_path):
    context = _materialize_context(tmp_path)
    entry_action = context.journal.record_action(
        "page_entry",
        status="success",
    )
    action, returned, record = attachments.materialize_candidate(
        context,
        count(1),
        candidate={
            "kind": "file",
            "displayed_name": "aura-attachment.txt",
            "displayed_size": "54 B",
            "location": "page_body",
            "open_mode": "file_preview",
            "bounds": (72, 543, 1008, 636),
        },
        attachment_ref="attachment-000001",
        parent_prefix=(
            "notion/workspaces/workspace-000001 (Workspace)/pages/"
            "page-000001 (AURA Attachment Test)"
        ),
        linked={
            "target_ref": "account.notion.test.001",
            "workspace_ref": "workspace-000001",
            "page_ref": "page-000001",
        },
        observation=_entry_observation(context),
        entry_action=entry_action,
        verify_parent=_page_verified,
    )

    assert record["source"]["observation_id"] == (
        context.observations[0].observation_id
    )
    assert action.status == "success"
    assert "attachment_parent" in returned["observation_id"]
    assert record["acquisition_status"] == "materialized"
    assert record["retained_basename"] == "aura-attachment.txt"
    assert record["size"] == 54
    assert record["sha256"] == hashlib.sha256(_FIXTURE).hexdigest()

    prefix = (
        "artifacts/notion/workspaces/workspace-000001 (Workspace)/pages/"
        "page-000001 (AURA Attachment Test)/attachments/"
        "attachment-000001 (aura-attachment.txt)"
    )
    paths = {
        artifact.relative_path
        for artifact in context.artifacts.records
    }
    assert {
        f"{prefix}/aura-attachment.txt",
        f"{prefix}/attachment.json",
        f"{prefix}/materialize-menu-screen.png",
        f"{prefix}/materialize-menu-screen.xml",
    } <= paths


def test_external_url_records_without_device_action(tmp_path):
    context = _materialize_context(tmp_path)
    entry_action = context.journal.record_action(
        "page_entry",
        status="success",
    )
    _, returned, record = attachments.materialize_candidate(
        context,
        count(1),
        candidate={
            "kind": "external_url",
            "displayed_name": "https://example.com",
            "displayed_size": None,
            "location": "page_body",
            "open_mode": "none",
            "bounds": (72, 300, 1008, 387),
        },
        attachment_ref="attachment-000001",
        parent_prefix="notion/page",
        linked={"page_ref": "page-000001"},
        observation=_entry_observation(context),
        entry_action=entry_action,
        verify_parent=_page_verified,
    )
    assert returned["observation_id"] == "page-entry"
    assert record["acquisition_status"] == "out_of_scope"
    assert record["reason_code"] == (
        "notion_external_object_out_of_scope"
    )
    assert context.device.clicks == []
    assert context.device.shell_count == 0
    assert context.device.pull_count == 0
    document = next(
        path
        for path in context.run_dir.glob(
            "artifacts/notion/page/attachments/*/attachment.json"
        )
    )
    retained_record = json.loads(document.read_text())
    assert retained_record["kind"] == "external_url"
    assert retained_record["displayed_name"] == "https://example.com"
    assert all(":" not in item.relative_path for item in context.artifacts.records)
    attempt = context.attempts[0]
    assert attempt.procedure_status.value == "completed"
    assert attempt.acquisition_status.value == "not_acquired"
    assert attempt.started_at is not None and attempt.ended_at is not None
    outcome = context.outcomes[0]
    assert outcome.reason == "notion_external_object_out_of_scope"
    assert outcome.observation_id == record["source"]["observation_id"]
    assert outcome.action_id is not None
    for event in map(json.loads, context.journal.path.read_text().splitlines()):
        if (event["event_type"] in {"action", "artifact_retained"}
                and event.get("attempt_id", event.get("details", {}).get("attempt_id")) == attempt.attempt_id):
            assert attempt.started_at <= event["timestamp"] <= attempt.ended_at
    assert {
        str(Path(record.relative_path).parent)
        for record in context.artifacts.records
    } == {
        (
            "artifacts/notion/page/attachments/"
            "attachment-000001 (https___example.com)"
        )
    }


def test_external_url_exclusion_package_passes_independent_validation(tmp_path, monkeypatch):
    from aura.models import Outcome, OutcomeStatus, Route
    from test_result_validation import _generated_session, _run

    profile = ProfileStore(ROOT / "profiles").load_app("notion", "0.6.4030")
    profile = replace(profile, routes=(Route.MATERIALIZE,))

    def collect(context):
        context.begin_identification("notion.attachments")
        action = context.journal.record_action("observe_external_link", status="success")
        attachments.materialize_candidate(
            context, count(1), candidate={
                "kind": "external_url", "displayed_name": "External link", "displayed_size": None,
                "location": "page_body", "open_mode": "none", "bounds": (72, 300, 1008, 387),
            }, attachment_ref="attachment-000001", parent_prefix="notion/page", linked={"page_ref": "page-000001"},
            observation={"observation_id": "scope-snapshot", "screen": b"scope-png", "tree": b"<hierarchy/>"},
            entry_action=action, verify_parent=lambda root: True,
        )
        context.finish_identification("notion.attachments", completion_condition="all_workspaces_visited")
        return Outcome(OutcomeStatus.COMPLETE)

    session = _generated_session(tmp_path, monkeypatch, profile=profile, collector=collect)
    result = _run(session)
    assert result.returncode == 0, result.stdout


def test_materialized_image_uses_retained_numeric_basename_for_one_folder(
    tmp_path,
):
    context = _materialize_context(
        tmp_path,
        _ImageMaterializeDevice(),
    )
    entry_action = context.journal.record_action(
        "page_entry",
        status="success",
    )
    attachments.materialize_candidate(
        context,
        count(1),
        candidate={
            "kind": "image",
            "displayed_name": None,
            "displayed_size": None,
            "location": "page_body",
            "open_mode": "image_gallery",
            "bounds": (114, 675, 966, 1158),
        },
        attachment_ref="attachment-000001",
        parent_prefix="notion/page",
        linked={"page_ref": "page-000001"},
        observation=_entry_observation(context),
        entry_action=entry_action,
        verify_parent=_page_verified,
    )
    parents = {
        str(Path(record.relative_path).parent)
        for record in context.artifacts.records
    }
    assert parents == {
        (
            "artifacts/notion/page/attachments/"
            "attachment-000001 (1000007859.png)"
        )
    }


def test_video_actions_override_visible_background_parent(tmp_path):
    device = _VideoMaterializeDevice()
    context = _materialize_context(tmp_path, device)
    entry_action = context.journal.record_action(
        "page_entry",
        status="success",
    )

    _, returned, record = attachments.materialize_candidate(
        context,
        count(1),
        candidate={
            "kind": "video",
            "displayed_name": None,
            "displayed_size": None,
            "location": "page_body",
            "open_mode": "video_actions",
            "bounds": (912, 1341, 984, 1416),
        },
        attachment_ref="attachment-000001",
        parent_prefix="notion/page",
        linked={"page_ref": "page-000001"},
        observation=_entry_observation(context),
        entry_action=entry_action,
        verify_parent=_page_verified,
    )

    assert record["acquisition_status"] == "materialized"
    assert record["retained_basename"] == "video.mp4"
    assert device.state == "page"
    assert returned["observation_id"]


def test_missing_download_retains_partial_record_and_restores_parent(
    tmp_path,
):
    context = _materialize_context(
        tmp_path,
        _NoDownloadDevice(),
    )
    entry_action = context.journal.record_action(
        "page_entry",
        status="success",
    )
    _, returned, record = attachments.materialize_candidate(
        context,
        count(1),
        candidate={
            "kind": "file",
            "displayed_name": "aura-attachment.txt",
            "displayed_size": "54 B",
            "location": "page_body",
            "open_mode": "file_preview",
            "bounds": (72, 543, 1008, 636),
        },
        attachment_ref="attachment-000001",
        parent_prefix="notion/page",
        linked={"page_ref": "page-000001"},
        observation=_entry_observation(context),
        entry_action=entry_action,
        verify_parent=_page_verified,
    )
    assert returned["observation_id"]
    assert context.device.state == "page"
    assert record["acquisition_status"] == "not_materialized"
    assert record["reason_code"] == (
        "notion_attachment_inventory_did_not_settle"
    )
    paths = {
        Path(artifact.relative_path).name
        for artifact in context.artifacts.records
    }
    assert paths == {
        "attachment.json",
        "materialize-menu-screen.png",
        "materialize-menu-screen.xml",
    }


def test_noop_open_retains_unavailable_record_without_leaving_parent(
    tmp_path,
):
    device = _NoPreviewDevice()
    context = _materialize_context(tmp_path, device)
    entry_action = context.journal.record_action(
        "page_entry",
        status="success",
    )

    action, returned, record = attachments.materialize_candidate(
        context,
        count(1),
        candidate={
            "kind": "file",
            "displayed_name": "aura-attachment.txt",
            "displayed_size": "54 B",
            "location": "page_body",
            "open_mode": "file_preview",
            "bounds": (72, 543, 1008, 636),
        },
        attachment_ref="attachment-000001",
        parent_prefix="notion/page",
        linked={"page_ref": "page-000001"},
        observation=_entry_observation(context),
        entry_action=entry_action,
        verify_parent=_page_verified,
    )

    assert action.status == "success"
    assert returned["observation_id"]
    assert record["acquisition_status"] == "not_materialized"
    assert record["reason_code"] == (
        "notion_attachment_control_unavailable"
    )
    assert device.back_count == 0
    assert device.state == "page"
    assert {
        Path(artifact.relative_path).name
        for artifact in context.artifacts.records
    } == {
        "attachment.json",
        "materialize-menu-screen.png",
        "materialize-menu-screen.xml",
    }

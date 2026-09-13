import importlib
import json
from pathlib import Path, PurePosixPath
from dataclasses import replace

import pytest

from aura.models import Outcome, OutcomeStatus, Route
from aura.profiles import ProfileStore
from aura.runtime import AcquisitionRuntime


ROOT = Path(__file__).resolve().parents[1]


def _profile():
    return ProfileStore(ROOT / "profiles").load_app(
        "whatsapp", "2.26.27.85"
    )


def _active_chat_list_rows(indexed_names, offset=0):
    rows = []
    for index, name in indexed_names:
        top = 500 + index * 200 + offset
        rows.append(
            '<node package="com.whatsapp" '
            'resource-id="com.whatsapp:id/contact_row_container" '
            'class="android.widget.LinearLayout" clickable="true" '
            f'bounds="[0,{top}][1080,{top + 200}]">'
            '<node package="com.whatsapp" '
            'resource-id="com.whatsapp:id/'
            'conversations_row_contact_name" '
            'class="android.widget.TextView" '
            f'text="{name}" bounds="[216,{top + 40}]'
            f'[800,{top + 110}]"/>'
            "</node>"
        )
    return (
        "<hierarchy>"
        '<node package="com.whatsapp" bounds="[0,0][1080,2400]">'
        '<node package="com.whatsapp" resource-id="android:id/list" '
        'class="androidx.recyclerview.widget.RecyclerView" '
        'bounds="[0,428][1080,2014]">'
        f"{''.join(rows)}"
        "</node></node></hierarchy>"
    )


def _active_chat_list(*names, offset=0):
    return _active_chat_list_rows(
        enumerate(names), offset=offset
    )


def test_active_chat_rows_keep_equal_names_in_ui_order():
    traversal = importlib.import_module(
        "aura.apps.whatsapp.traversal"
    )

    rows = traversal.parse_active_chat_rows(
        _active_chat_list("Same", "Same", "Other"),
        _profile(),
    )

    assert [row["display_name"] for row in rows] == [
        "Same",
        "Same",
        "Other",
    ]
    assert [row["bounds"] for row in rows] == [
        (0, 500, 1080, 700),
        (0, 700, 1080, 900),
        (0, 900, 1080, 1100),
    ]


WINDOW = """\
<hierarchy>
  <node package="com.whatsapp" resource-id="android:id/list"
        class="android.widget.ListView" bounds="[0,240][1080,2200]">
    <node package="com.whatsapp" bounds="[0,260][1080,500]">
      <node package="com.whatsapp"
            resource-id="com.whatsapp:id/conversation_row_date_divider"
            text="28 May 2026" bounds="[400,270][680,320]"/>
      <node package="com.whatsapp" resource-id="com.whatsapp:id/message_text"
            text="WA-001" bounds="[80,330][600,420]"/>
      <node package="com.whatsapp" resource-id="com.whatsapp:id/date"
            text="8:15&#8239;pm" bounds="[500,420][650,480]"/>
      <node package="com.whatsapp" resource-id="com.whatsapp:id/status"
            content-desc="Delivered" bounds="[650,420][700,480]"/>
    </node>
    <node package="com.whatsapp" bounds="[0,500][1080,900]">
      <node package="com.whatsapp" resource-id="com.whatsapp:id/media_container"
            clickable="true" bounds="[100,520][800,860]">
        <node package="com.whatsapp" resource-id="com.whatsapp:id/image"
              content-desc="Enlarge photo" bounds="[100,520][800,860]"/>
      </node>
      <node package="com.whatsapp" resource-id="com.whatsapp:id/date"
            text="8:16&#8239;pm" bounds="[650,820][800,880]"/>
    </node>
    <node package="com.whatsapp" bounds="[0,900][1080,1300]">
      <node package="com.whatsapp" resource-id="com.whatsapp:id/media_container"
            clickable="false" bounds="[100,920][800,1260]">
        <node package="com.whatsapp" resource-id="com.whatsapp:id/thumb"
              content-desc="Video, HD, duration 118 seconds"
              clickable="true" bounds="[120,940][780,1240]"/>
      </node>
      <node package="com.whatsapp" resource-id="com.whatsapp:id/date"
            text="8:17&#8239;pm" bounds="[650,1220][800,1280]"/>
    </node>
    <node package="com.whatsapp" bounds="[0,1300][1080,1700]">
      <node package="com.whatsapp" resource-id="com.whatsapp:id/content"
            clickable="true" bounds="[100,1320][900,1650]">
        <node package="com.whatsapp" resource-id="com.whatsapp:id/title"
              text="evidence.docx" bounds="[180,1360][700,1430]"/>
        <node package="com.whatsapp" resource-id="com.whatsapp:id/file_size"
              text="275 kB" bounds="[180,1450][350,1510]"/>
        <node package="com.whatsapp" resource-id="com.whatsapp:id/file_type"
              text="DOCX" bounds="[360,1450][500,1510]"/>
      </node>
      <node package="com.whatsapp" resource-id="com.whatsapp:id/date"
            text="8:18&#8239;pm" bounds="[650,1620][800,1680]"/>
    </node>
  </node>
</hierarchy>
"""


def test_parse_message_window_extracts_supported_rows():
    materialize = importlib.import_module(
        "aura.apps.whatsapp.materialize"
    )

    rows = materialize.parse_message_window(WINDOW, _profile())

    assert [row["kind"] for row in rows] == [
        "text",
        "photo",
        "video",
        "document",
    ]
    assert rows[0] == {
        "kind": "text",
        "body": "WA-001",
        "displayed_time": "8:15 pm",
        "date_marker": "28 May 2026",
        "direction": "outgoing",
        "attachment": None,
        "action_bounds": None,
        "viewport_visibility": "full",
    }
    assert rows[1]["action_bounds"] == (100, 520, 800, 860)
    assert rows[2]["action_bounds"] == (120, 940, 780, 1240)
    assert rows[2]["attachment"]["description"] == (
        "Video, HD, duration 118 seconds"
    )
    assert rows[3]["attachment"] == {
        "displayed_name": "evidence.docx",
        "displayed_size": "275 kB",
        "displayed_type": "DOCX",
    }


def test_parse_keeps_full_attachment_in_top_clipped_message_row():
    materialize = importlib.import_module(
        "aura.apps.whatsapp.materialize"
    )
    hierarchy = """\
<hierarchy>
  <node package="com.whatsapp" resource-id="android:id/list"
        class="android.widget.ListView"
        bounds="[0,240][1080,2200]">
    <node package="com.whatsapp" bounds="[0,100][1080,600]">
      <node package="com.whatsapp"
            resource-id="com.whatsapp:id/media_container"
            clickable="true" bounds="[100,260][800,560]">
        <node package="com.whatsapp" resource-id="com.whatsapp:id/image"
              content-desc="Enlarge photo" bounds="[100,260][800,560]"/>
      </node>
      <node package="com.whatsapp" resource-id="com.whatsapp:id/date"
            text="8:15 pm" bounds="[650,520][800,580]"/>
    </node>
  </node>
</hierarchy>
"""

    rows = materialize.parse_message_window(hierarchy, _profile())

    assert len(rows) == 1
    assert rows[0]["kind"] == "photo"
    assert rows[0]["viewport_visibility"] == "clipped"
    assert rows[0]["action_bounds"] == (100, 260, 800, 560)


def test_parse_keeps_full_attachment_inside_clipped_message_row():
    materialize = importlib.import_module(
        "aura.apps.whatsapp.materialize"
    )
    hierarchy = """\
<hierarchy>
  <node package="com.whatsapp" resource-id="android:id/list"
        class="android.widget.ListView"
        bounds="[0,248][1080,2089]">
    <node package="com.whatsapp" bounds="[0,300][1080,2089]">
      <node package="com.whatsapp"
            resource-id="com.whatsapp:id/media_container"
            clickable="true" bounds="[100,320][900,1800]">
        <node package="com.whatsapp" resource-id="com.whatsapp:id/image"
              content-desc="Enlarge photo" bounds="[100,320][900,1800]"/>
      </node>
      <node package="com.whatsapp" resource-id="com.whatsapp:id/date"
            text="8:02 pm" bounds="[700,2000][820,2060]"/>
    </node>
  </node>
</hierarchy>
"""

    rows = materialize.parse_message_window(hierarchy, _profile())

    assert len(rows) == 1
    assert rows[0]["viewport_visibility"] == "clipped"
    assert rows[0]["action_bounds"] == (100, 320, 900, 1800)


def test_materialize_accepts_full_attachment_in_clipped_row(monkeypatch):
    materialize = importlib.import_module(
        "aura.apps.whatsapp.materialize"
    )
    row = {
        "kind": "photo",
        "body": None,
        "displayed_time": "8:02 pm",
        "date_marker": None,
        "direction": "outgoing",
        "attachment": {"description": "Enlarge photo"},
        "action_bounds": (100, 320, 900, 1800),
        "viewport_visibility": "clipped",
    }
    calls = []

    def materialize_attachment(
        context, target, prefix, current, attachment_ref, linked
    ):
        calls.append(attachment_ref)
        return {"attachment_ref": attachment_ref}

    monkeypatch.setattr(
        materialize,
        "_materialize_attachment",
        materialize_attachment,
    )

    materialize._materialize_visible_attachments(
        object(), {}, "", [row], {}, {}
    )

    assert calls == ["attachment-000001"]


def test_materialize_keeps_document_metadata_without_acquisition(monkeypatch):
    materialize = importlib.import_module(
        "aura.apps.whatsapp.materialize"
    )
    row = {
        "kind": "document",
        "body": None,
        "displayed_time": "8:18 pm",
        "date_marker": None,
        "direction": "incoming",
        "attachment": {
            "displayed_name": "evidence.docx",
            "displayed_size": "275 kB",
            "displayed_type": "DOCX",
        },
        "action_bounds": (100, 1320, 900, 1650),
        "viewport_visibility": "full",
    }
    calls = []

    monkeypatch.setattr(
        materialize,
        "_materialize_attachment",
        lambda *args: calls.append(args),
    )

    materialize._materialize_visible_attachments(
        object(), {}, "", [row], {}, {}
    )

    assert calls == []
    assert row["attachment"]["displayed_name"] == "evidence.docx"
    assert "attachment_ref" not in row
    assert "materialization" not in row


def test_parse_message_window_defers_semantically_clipped_attachment():
    materialize = importlib.import_module(
        "aura.apps.whatsapp.materialize"
    )
    hierarchy = """\
<hierarchy>
  <node package="com.whatsapp" resource-id="android:id/list"
        class="android.widget.ListView"
        bounds="[0,248][1080,2089]">
    <node package="com.whatsapp" bounds="[0,1238][1080,2089]">
      <node package="com.whatsapp"
            resource-id="com.whatsapp:id/media_container"
            clickable="true" bounds="[100,1250][900,2089]">
        <node package="com.whatsapp" resource-id="com.whatsapp:id/image"
              content-desc="Enlarge photo" bounds="[100,1250][900,2089]"/>
      </node>
      <node package="com.whatsapp" resource-id="com.whatsapp:id/date"
            text="8:02 pm" bounds="[700,2000][820,2060]"/>
    </node>
  </node>
</hierarchy>
"""

    rows = materialize.parse_message_window(hierarchy, _profile())

    assert len(rows) == 1
    assert rows[0]["viewport_visibility"] == "clipped"
    assert rows[0]["action_bounds"] is None


def test_parse_message_window_requires_full_attachment_bounds():
    materialize = importlib.import_module(
        "aura.apps.whatsapp.materialize"
    )
    hierarchy = """\
<hierarchy>
  <node package="com.whatsapp" resource-id="android:id/list"
        class="android.widget.ListView"
        bounds="[0,248][1080,2089]">
    <node package="com.whatsapp" bounds="[0,300][1080,2000]">
      <node package="com.whatsapp"
            resource-id="com.whatsapp:id/media_container"
            clickable="true" bounds="[100,320][900,2089]">
        <node package="com.whatsapp" resource-id="com.whatsapp:id/image"
              content-desc="Enlarge photo" bounds="[100,320][900,2089]"/>
      </node>
      <node package="com.whatsapp" resource-id="com.whatsapp:id/date"
            text="8:02 pm" bounds="[700,1900][820,1960]"/>
    </node>
  </node>
</hierarchy>
"""

    rows = materialize.parse_message_window(hierarchy, _profile())

    assert len(rows) == 1
    assert rows[0]["viewport_visibility"] == "full"
    assert rows[0]["action_bounds"] is None


def test_materialize_keeps_identical_adjacent_photos_distinct(monkeypatch):
    materialize = importlib.import_module(
        "aura.apps.whatsapp.materialize"
    )
    rows = [{
        "kind": "photo",
        "body": None,
        "displayed_time": "7:59 pm",
        "date_marker": None,
        "direction": "outgoing",
        "attachment": {"description": "Enlarge photo"},
        "action_bounds": (100, 300, 900, 700),
        "viewport_visibility": "full",
    } for _ in range(2)]
    calls = []

    def materialize_attachment(
        context, target, prefix, row, attachment_ref, linked
    ):
        calls.append(attachment_ref)
        return {"attachment_ref": attachment_ref}

    monkeypatch.setattr(
        materialize,
        "_materialize_attachment",
        materialize_attachment,
    )

    materialize._materialize_visible_attachments(
        object(), {}, "", rows, {}, {}
    )

    assert calls == ["attachment-000001", "attachment-000002"]
    assert rows[0]["attachment_ref"] != rows[1]["attachment_ref"]


def test_merge_older_window_removes_only_boundary_overlap():
    materialize = importlib.import_module(
        "aura.apps.whatsapp.materialize"
    )
    message = lambda body: {
        "kind": "text",
        "body": body,
        "displayed_time": "8:15 pm",
        "date_marker": None,
        "direction": "incoming",
        "attachment": None,
        "action_bounds": None,
    }

    assert materialize.merge_older_messages(
        [message("A"), message("B"), message("B")],
        [message("B"), message("B"), message("C")],
    ) == [
        message("A"),
        message("B"),
        message("B"),
        message("C"),
    ]


def test_merge_recognizes_clipped_attachment_at_window_boundary():
    materialize = importlib.import_module(
        "aura.apps.whatsapp.materialize"
    )
    full = {
        "kind": "photo",
        "body": None,
        "displayed_time": "8:15 pm",
        "date_marker": None,
        "direction": "outgoing",
        "attachment": {"description": "Enlarge photo"},
        "action_bounds": (10, 10, 20, 20),
        "viewport_visibility": "full",
        "materialization": {"acquisition_status": "materialized"},
    }
    clipped = {
        **full,
        "direction": "incoming",
        "action_bounds": None,
        "viewport_visibility": "clipped",
        "materialization": None,
    }
    older_text = {
        "kind": "text",
        "body": "older",
        "displayed_time": "8:14 pm",
        "date_marker": None,
        "direction": "incoming",
        "attachment": None,
        "action_bounds": None,
    }

    merged = materialize.merge_older_messages(
        [older_text, full],
        [clipped],
    )

    assert len(merged) == 2
    assert merged[1]["materialization"] == {
        "acquisition_status": "materialized"
    }
    assert merged[1]["viewport_visibility"] == "full"


def test_merge_recognizes_clipped_text_at_window_boundary():
    materialize = importlib.import_module(
        "aura.apps.whatsapp.materialize"
    )
    full = {
        "kind": "text",
        "body": "same message",
        "displayed_time": "8:15 pm",
        "date_marker": None,
        "direction": "outgoing",
        "attachment": None,
        "action_bounds": None,
        "viewport_visibility": "full",
    }
    clipped = {
        **full,
        "displayed_time": None,
        "direction": "incoming",
        "viewport_visibility": "clipped",
    }

    assert materialize.merge_older_messages(
        [clipped],
        [full],
    ) == [full]


def test_select_materialized_file_distinguishes_changed_media():
    materialize = importlib.import_module(
        "aura.apps.whatsapp.materialize"
    )

    assert materialize.select_materialized_file(
        (("/sdcard/Pictures/WhatsApp/a.jpg", 10, 1),),
        (("/sdcard/Pictures/WhatsApp/a.jpg", 10, 2),),
        kind="photo",
    ) == (
        "/sdcard/Pictures/WhatsApp/a.jpg",
        10,
        2,
        "changed_media_file",
    )
    with pytest.raises(
        materialize.WhatsAppMaterializeError,
        match="ambiguous",
    ):
        materialize.select_materialized_file(
            (),
            (
                ("/sdcard/Pictures/WhatsApp/a.jpg", 10, 1),
                ("/sdcard/Pictures/WhatsApp/b.jpg", 10, 1),
            ),
            kind="photo",
        )


def _chat_window(*messages):
    rows = []
    for index, body in enumerate(messages):
        top = 300 + index * 260
        marker = (
            '<node package="com.whatsapp" '
            'resource-id="com.whatsapp:id/'
            'conversation_row_date_divider" text="28 May 2026" '
            f'bounds="[400,{top}][680,{top + 50}]"/>'
            if body == "A"
            else ""
        )
        rows.append(
            f'<node package="com.whatsapp" '
            f'bounds="[0,{top}][1080,{top + 240}]">'
            f"{marker}"
            '<node package="com.whatsapp" '
            'resource-id="com.whatsapp:id/message_text" '
            f'text="{body}" bounds="[80,{top + 60}][600,{top + 150}]"/>'
            '<node package="com.whatsapp" '
            'resource-id="com.whatsapp:id/date" text="8:15 pm" '
            f'bounds="[500,{top + 160}][650,{top + 220}]"/>'
            "</node>"
        )
    return (
        "<hierarchy>"
        '<node package="com.whatsapp" bounds="[0,0][1080,2400]">'
        '<node package="com.whatsapp" text="TEST TEST" '
        'resource-id="com.whatsapp:id/conversation_contact_name" '
        'bounds="[200,100][700,220]"/>'
        '<node package="com.whatsapp" resource-id="android:id/list" '
        'class="android.widget.ListView" bounds="[0,240][1080,2200]">'
        f"{''.join(rows)}"
        "</node></node></hierarchy>"
    )


class TextHistoryDevice:
    def __init__(self):
        self.state = "stopped"
        self.swipes = []

    def app_start(self, package_name):
        assert package_name == "com.whatsapp"
        self.state = "chat_list"

    def app_stop(self, package_name):
        pass

    def exists(self, selector):
        return (
            selector == {"packageName": "com.whatsapp"}
            and self.state != "stopped"
        )

    def count(self, selector):
        return int(self.exists(selector))

    def click(self, selector):
        raise AssertionError(selector)

    def click_xpath(self, xpath):
        raise AssertionError(xpath)

    def click_bounds(self, bounds, *, anchor="center"):
        if self.state == "chat_list":
            assert tuple(bounds) == (0, 476, 1080, 704)
            self.state = "latest"
            return
        raise AssertionError((self.state, bounds))

    def long_click(self, selector):
        raise AssertionError(selector)

    def long_click_bounds(self, bounds):
        raise AssertionError(bounds)

    def back(self):
        raise AssertionError("no attachment viewer expected")

    def swipe(self, direction, duration=0.2, distance_ratio=0.2):
        self.swipes.append(direction)
        if direction == "down" and self.state == "latest":
            self.state = "older"

    def hierarchy(self):
        if self.state == "chat_list":
            return (
                "<hierarchy>"
                '<node package="com.whatsapp" bounds="[0,0][1080,2400]">'
                '<node package="com.whatsapp" resource-id="android:id/list" '
                'class="androidx.recyclerview.widget.RecyclerView" '
                'bounds="[0,428][1080,2014]">'
                '<node package="com.whatsapp" '
                'resource-id="com.whatsapp:id/contact_row_container" '
                'class="android.widget.LinearLayout" clickable="true" '
                'bounds="[0,476][1080,704]">'
                '<node package="com.whatsapp" text="TEST TEST" '
                'resource-id="com.whatsapp:id/'
                'conversations_row_contact_name" '
                'class="android.widget.TextView" '
                'bounds="[216,518][462,583]"/>'
                "</node></node></node></hierarchy>"
            )
        if self.state == "latest":
            return _chat_window("B", "C")
        if self.state == "older":
            return _chat_window("A", "B")
        return "<hierarchy/>"

    def screenshot(self):
        return b"\x89PNG\r\n\x1a\nwhatsapp-window"

    def shell(self, command):
        return ""

    def pull(self, remote_path, local_path):
        raise AssertionError(remote_path)

    def window_size(self):
        return 1080, 2400


def test_materialize_route_collects_text_history_once(tmp_path):
    whatsapp = importlib.import_module("aura.apps.whatsapp")
    profile = replace(
        _profile(),
        timings={
            **_profile().timings,
            "default_timeout": 0.01,
            "poll_interval": 0.0,
            "application_start_settle": 0.0,
            "transition_settle": 0.0,
        },
    )
    system = ProfileStore(ROOT / "profiles").load_system_ui(
        "samsung"
    )
    device = TextHistoryDevice()

    result = AcquisitionRuntime(tmp_path, device).run(
        profile,
        system,
        Route.MATERIALIZE,
        {
            "target": {
                "kind": "chat",
                "ref": "chat.whatsapp.test.001",
                "display_name": "TEST TEST",
            }
        },
        whatsapp.collect,
        run_id="whatsapp-text-materialize",
    )

    assert result.outcome.status is OutcomeStatus.COMPLETE
    assert result.outcome.details["message_count"] == 3
    assert {item.item_type for item in result.items} >= {
        "conversation",
        "message",
    }
    assert all(
        attempt.acquisition_status.value == "acquired"
        for attempt in result.attempts
    )
    assert device.swipes == ["up", "down", "down"]
    message_files = sorted(
        (
            result.run_dir
            / "artifacts"
            / "whatsapp"
            / "chatrooms"
        ).glob("*/messages/message-*.json")
    )
    assert [
        json.loads(path.read_text())["body"] for path in message_files
    ] == ["A", "B", "C"]


class OpenTextHistoryDevice(TextHistoryDevice):
    def app_start(self, package_name):
        assert package_name == "com.whatsapp"
        self.state = "latest"


def test_materialize_reuses_open_chat_and_records_identity_basis(
    tmp_path,
):
    materialize = importlib.import_module(
        "aura.apps.whatsapp.materialize"
    )
    profile = replace(
        _profile(),
        timings={
            **_profile().timings,
            "default_timeout": 0.01,
            "poll_interval": 0.0,
            "application_start_settle": 0.0,
            "transition_settle": 0.0,
        },
    )
    system = ProfileStore(ROOT / "profiles").load_system_ui(
        "samsung"
    )

    def collect_open(context):
        assert context.start_app()
        return materialize.collect_materialize(
            context,
            {
                "kind": "chat",
                "ref": "whatsapp.active.chat-000001",
                "display_name": "TEST TEST",
            },
            already_open=True,
            identity_basis="active_list_ordinal",
        )

    result = AcquisitionRuntime(
        tmp_path, OpenTextHistoryDevice()
    ).run(
        profile,
        system,
        Route.MATERIALIZE,
        {"target": {"kind": "chat_list", "ref": "whatsapp.active"}},
        collect_open,
        run_id="whatsapp-open-chat-materialize",
    )

    chatroom = next(
        (
            result.run_dir
            / "artifacts"
            / "whatsapp"
            / "chatrooms"
        ).glob("*/chatroom.json")
    )
    assert json.loads(chatroom.read_text())["identity_basis"] == (
        "active_list_ordinal"
    )


class ActiveChatListDevice:
    def __init__(self, names, *, disappear_after_visit=None):
        self.names = list(names)
        self.disappear_after_visit = disappear_after_visit
        self.hidden_indexes = set()
        self.state = "stopped"
        self.current_name = None
        self.current_index = None
        self.clicks = []
        self.backs = 0
        self.swipes = []
        self.transient_list_reads = 0

    def app_start(self, package_name):
        assert package_name == "com.whatsapp"
        self.state = "chat_list"

    def app_stop(self, package_name):
        pass

    def exists(self, selector):
        return (
            selector == {"packageName": "com.whatsapp"}
            and self.state != "stopped"
        )

    def count(self, selector):
        return int(self.exists(selector))

    def click(self, selector):
        raise AssertionError(selector)

    def click_xpath(self, xpath):
        raise AssertionError(xpath)

    def click_bounds(self, bounds, *, anchor="center"):
        assert self.state == "chat_list"
        index = (bounds[1] - 500) // 200
        assert tuple(bounds) == (
            0,
            500 + index * 200,
            1080,
            700 + index * 200,
        )
        self.clicks.append(tuple(bounds))
        self.current_index = index
        self.current_name = self.names[index]
        self.state = "chat"

    def long_click(self, selector):
        raise AssertionError(selector)

    def long_click_bounds(self, bounds):
        raise AssertionError(bounds)

    def back(self):
        assert self.state == "chat"
        self.backs += 1
        if self.current_name == self.disappear_after_visit:
            self.hidden_indexes.add(self.current_index)
        self.current_name = None
        self.current_index = None
        self.state = "chat_list"
        self.transient_list_reads = 2

    def swipe(self, direction, duration=0.2, distance_ratio=0.2):
        assert self.state == "chat_list"
        self.swipes.append(direction)

    def hierarchy(self):
        if self.state == "chat_list":
            offset = -30 if self.transient_list_reads else 0
            if self.transient_list_reads:
                self.transient_list_reads -= 1
            return _active_chat_list_rows(
                (
                    (index, name)
                    for index, name in enumerate(self.names)
                    if index not in self.hidden_indexes
                ),
                offset=offset,
            )
        if self.state == "chat":
            return (
                "<hierarchy>"
                '<node package="com.whatsapp" bounds="[0,0][1080,2400]">'
                '<node package="com.whatsapp" '
                'resource-id="com.whatsapp:id/'
                'conversation_contact_name" '
                f'text="{self.current_name}" '
                'bounds="[300,127][800,195]"/>'
                "</node></hierarchy>"
            )
        return "<hierarchy/>"

    def screenshot(self):
        return b"\x89PNG\r\n\x1a\nwhatsapp-list"

    def shell(self, command):
        return ""

    def pull(self, remote_path, local_path):
        raise AssertionError(remote_path)

    def window_size(self):
        return 1080, 2400


class ActiveExportChatListDevice(ActiveChatListDevice):
    def shell(self, command):
        if command == "settings get global bluetooth_on":
            return "1"
        if command == "dumpsys bluetooth_manager":
            return (
                "Bonded devices:\n"
                "  name=DESKTOP-81NCFKT\n"
                "Devices in DB:\n"
            )
        return super().shell(command)


def test_two_chat_histories_keep_list_scope_open_until_summary(tmp_path):
    whatsapp = importlib.import_module("aura.apps.whatsapp")

    class TwoHistoryDevice(ActiveChatListDevice):
        def click_bounds(self, bounds, *, anchor="center"):
            super().click_bounds(bounds, anchor=anchor)
            self.history_window = "latest"

        def hierarchy(self):
            if self.state == "chat":
                return _chat_window(*(("B", "C") if self.history_window == "latest" else ("A", "B"))).replace("TEST TEST", self.current_name)
            return super().hierarchy()

        def swipe(self, direction, duration=.2, distance_ratio=.2):
            if self.state == "chat":
                if direction == "down":
                    self.history_window = "older"
                return
            super().swipe(direction, duration, distance_ratio)

    profile = replace(_profile(), timings={**_profile().timings, "default_timeout": .01, "poll_interval": 0, "application_start_settle": 0, "transition_settle": 0})
    result = AcquisitionRuntime(tmp_path, TwoHistoryDevice(["First", "Second"])).run(
        profile, ProfileStore(ROOT / "profiles").load_system_ui("samsung"), Route.MATERIALIZE,
        {"target": {"kind": "chat_list", "ref": "active"}}, whatsapp.collect, run_id="two-histories",
    )
    assert result.outcome.status is OutcomeStatus.COMPLETE
    summary = next(item for item in result.artifacts if item.relative_path.endswith("active-chat-traversal.json"))
    record = json.loads((result.run_dir / summary.relative_path).read_text())
    assert record["completed_count"] == 2
    attempt = next(item for item in result.attempts if item.attempt_id == summary.attempt_id)
    assert attempt.procedure_status.value == "completed"
    assert attempt.acquisition_status.value == "acquired"
    events = [json.loads(line) for line in (result.run_dir / "events.jsonl").read_text().splitlines()]
    assert all(attempt.started_at <= event["timestamp"] <= attempt.ended_at for event in events if event["event_type"] in {"action", "artifact_retained"} and event.get("attempt_id", event.get("details", {}).get("attempt_id")) == attempt.attempt_id)


def test_materialize_chat_list_visits_equal_names_once(
    tmp_path, monkeypatch
):
    traversal = importlib.import_module(
        "aura.apps.whatsapp.traversal"
    )
    whatsapp = importlib.import_module("aura.apps.whatsapp")
    visited = []

    def collect_open(context, target, **options):
        assert options == {
            "already_open": True,
            "identity_basis": "active_list_ordinal",
        }
        visited.append(dict(target))
        return Outcome(OutcomeStatus.COMPLETE)

    monkeypatch.setattr(
        traversal, "collect_materialize", collect_open
    )
    profile = replace(
        _profile(),
        timings={
            **_profile().timings,
            "default_timeout": 0.01,
            "poll_interval": 0.0,
            "application_start_settle": 0.0,
            "transition_settle": 0.0,
        },
    )
    system = ProfileStore(ROOT / "profiles").load_system_ui(
        "samsung"
    )
    device = ActiveChatListDevice(
        ["Same", "Same", "Other"],
        disappear_after_visit="Same",
    )

    result = AcquisitionRuntime(tmp_path, device).run(
        profile,
        system,
        Route.MATERIALIZE,
        {"target": {"kind": "chat_list", "ref": "whatsapp.active"}},
        whatsapp.collect,
        run_id="whatsapp-active-chat-traversal",
    )

    assert result.outcome.status is OutcomeStatus.COMPLETE
    assert [item["ref"] for item in visited] == [
        "whatsapp.active.chat-000001",
        "whatsapp.active.chat-000002",
        "whatsapp.active.chat-000003",
    ]
    assert [item["display_name"] for item in visited] == [
        "Same",
        "Same",
        "Other",
    ]
    assert len(device.clicks) == 3
    assert device.backs == 3
    assert device.swipes
    assert set(device.swipes) == {"up"}
    summary = json.loads(
        (
            result.run_dir
            / "artifacts"
            / "whatsapp"
            / "active-chat-traversal.json"
        ).read_text()
    )
    assert summary["active_list_end_reached"] is True
    assert summary["processed_count"] == 3


def test_export_chat_list_visits_equal_names_once(
    tmp_path,
    monkeypatch,
):
    traversal = importlib.import_module(
        "aura.apps.whatsapp.traversal"
    )
    whatsapp = importlib.import_module("aura.apps.whatsapp")
    exported = []

    def collect_open(context, target, **options):
        assert options == {"already_open": True}
        exported.append(dict(target))
        action = context.journal.record_action(
            "receive_whatsapp_export",
            status="success",
            context={"target_ref": target["ref"]},
        )
        return {
            "action": action,
            "artifact_id": f"artifact-{len(exported):06d}",
            "receiver_label": "DESKTOP-81NCFKT",
            "received_filename": f"export-{len(exported):06d}.zip",
        }

    monkeypatch.setattr(
        traversal,
        "collect_export",
        collect_open,
        raising=False,
    )
    profile = replace(
        _profile(),
        timings={
            **_profile().timings,
            "default_timeout": 0.01,
            "poll_interval": 0.0,
            "application_start_settle": 0.0,
            "transition_settle": 0.0,
        },
    )
    system = ProfileStore(ROOT / "profiles").load_system_ui(
        "samsung"
    )
    device = ActiveExportChatListDevice(
        ["Same", "Same", "Other"],
        disappear_after_visit="Same",
    )
    inbox = tmp_path / "Documents"
    inbox.mkdir()

    result = AcquisitionRuntime(tmp_path / "runs", device).run(
        profile,
        system,
        Route.EXPORT,
        {
            "target": {
                "kind": "chat_list",
                "ref": "chat-list.whatsapp.active",
            },
            "bluetooth": {
                "target_name": "DESKTOP-81NCFKT",
                "receiver_label": "DESKTOP-81NCFKT",
                "receive_dir": str(inbox),
            },
        },
        whatsapp.collect,
        run_id="whatsapp-active-chat-export",
    )

    assert result.outcome.status is OutcomeStatus.COMPLETE
    assert [item["ref"] for item in exported] == [
        "chat-list.whatsapp.active.chat-000001",
        "chat-list.whatsapp.active.chat-000002",
        "chat-list.whatsapp.active.chat-000003",
    ]
    assert [item["display_name"] for item in exported] == [
        "Same",
        "Same",
        "Other",
    ]
    assert len(device.clicks) == 3
    summary = json.loads(
        (
            result.run_dir
            / "artifacts"
            / "whatsapp"
            / "active-chat-export-traversal.json"
        ).read_text()
    )
    assert summary["active_list_end_reached"] is True
    assert summary["processed_count"] == 3


def test_export_chat_list_records_one_failure_and_continues(
    tmp_path,
    monkeypatch,
):
    traversal = importlib.import_module(
        "aura.apps.whatsapp.traversal"
    )
    collector = importlib.import_module(
        "aura.apps.whatsapp.collector"
    )
    whatsapp = importlib.import_module("aura.apps.whatsapp")
    visited = []

    def collect_open(context, target, **options):
        assert options == {"already_open": True}
        visited.append(target["ref"])
        if len(visited) == 2:
            raise collector.WhatsAppCollectorError(
                "whatsapp_export_test_failure"
            )
        action = context.journal.record_action(
            "receive_whatsapp_export",
            status="success",
            context={"target_ref": target["ref"]},
        )
        return {
            "action": action,
            "artifact_id": f"artifact-{len(visited):06d}",
            "receiver_label": "DESKTOP-81NCFKT",
            "received_filename": f"export-{len(visited):06d}.zip",
        }

    monkeypatch.setattr(
        traversal,
        "collect_export",
        collect_open,
    )
    profile = replace(
        _profile(),
        timings={
            **_profile().timings,
            "default_timeout": 0.01,
            "poll_interval": 0.0,
            "application_start_settle": 0.0,
            "transition_settle": 0.0,
        },
    )
    system = ProfileStore(ROOT / "profiles").load_system_ui(
        "samsung"
    )
    device = ActiveExportChatListDevice(
        ["First", "Second", "Third"]
    )
    inbox = tmp_path / "Documents"
    inbox.mkdir()

    result = AcquisitionRuntime(tmp_path / "runs", device).run(
        profile,
        system,
        Route.EXPORT,
        {
            "target": {
                "kind": "chat_list",
                "ref": "chat-list.whatsapp.active",
            },
            "bluetooth": {
                "target_name": "DESKTOP-81NCFKT",
                "receiver_label": "DESKTOP-81NCFKT",
                "receive_dir": str(inbox),
            },
        },
        whatsapp.collect,
        run_id="whatsapp-active-chat-export-partial",
    )

    assert result.outcome.status is OutcomeStatus.PARTIAL
    assert result.outcome.reason == (
        "whatsapp_active_chat_export_traversal_incomplete"
    )
    assert len(visited) == 3
    assert len(result.outcomes) == 1
    assert result.outcomes[0].reason == (
        "whatsapp_export_test_failure"
    )
    summary = json.loads(
        (
            result.run_dir
            / "artifacts"
            / "whatsapp"
            / "active-chat-export-traversal.json"
        ).read_text()
    )
    assert summary["completed_count"] == 2


SCROLL_BOTTOM = (912, 1915, 1080, 2053)


def _with_scroll_bottom(hierarchy):
    return hierarchy.replace(
        "</hierarchy>",
        '<node package="com.whatsapp" '
        'resource-id="com.whatsapp:id/scroll_bottom" '
        'content-desc="Go to most recent message" clickable="true" '
        'bounds="[912,1915][1080,2053]"/>'
        "</hierarchy>",
    )


class LatestShortcutDevice(TextHistoryDevice):
    def __init__(self):
        super().__init__()
        self.shortcut_clicks = 0

    def click_bounds(self, bounds, *, anchor="center"):
        bounds = tuple(bounds)
        if self.state == "chat_list":
            assert bounds == (0, 476, 1080, 704)
            self.state = "older"
            return
        if self.state == "older" and bounds == SCROLL_BOTTOM:
            self.shortcut_clicks += 1
            self.state = "latest"
            return
        raise AssertionError((self.state, bounds))

    def hierarchy(self):
        if self.state == "older":
            return _with_scroll_bottom(_chat_window("A"))
        if self.state == "latest":
            return _chat_window("B")
        return super().hierarchy()


def test_materialize_uses_latest_shortcut_then_keeps_swipe_fallback(
    tmp_path,
):
    whatsapp = importlib.import_module("aura.apps.whatsapp")
    profile = replace(
        _profile(),
        timings={
            **_profile().timings,
            "default_timeout": 0.01,
            "poll_interval": 0.0,
            "application_start_settle": 0.0,
            "transition_settle": 0.0,
        },
    )
    system = ProfileStore(ROOT / "profiles").load_system_ui(
        "samsung"
    )
    device = LatestShortcutDevice()

    result = AcquisitionRuntime(tmp_path, device).run(
        profile,
        system,
        Route.MATERIALIZE,
        {
            "target": {
                "kind": "chat",
                "ref": "chat.whatsapp.test.shortcut",
                "display_name": "TEST TEST",
            }
        },
        whatsapp.collect,
        run_id="whatsapp-latest-shortcut",
    )

    assert result.outcome.status is OutcomeStatus.COMPLETE
    assert device.shortcut_clicks == 1
    assert device.swipes[0] == "up"


ATTACHMENT_WINDOW = """\
<hierarchy>
  <node package="com.whatsapp" bounds="[0,0][1080,2400]">
    <node package="com.whatsapp" text="TEST TEST"
          resource-id="com.whatsapp:id/conversation_contact_name"
          bounds="[200,100][700,220]"/>
    <node package="com.whatsapp" resource-id="android:id/list"
          class="android.widget.ListView" bounds="[0,240][1080,2200]">
      <node package="com.whatsapp" bounds="[0,300][1080,700]">
        <node package="com.whatsapp"
              resource-id="com.whatsapp:id/media_container"
              clickable="true" bounds="[100,320][800,660]">
          <node package="com.whatsapp"
                resource-id="com.whatsapp:id/image"
                content-desc="Enlarge photo" bounds="[100,320][800,660]"/>
        </node>
        <node package="com.whatsapp" resource-id="com.whatsapp:id/date"
              text="8:15 pm" bounds="[650,620][800,680]"/>
      </node>
      <node package="com.whatsapp" bounds="[0,700][1080,1100]">
        <node package="com.whatsapp"
              resource-id="com.whatsapp:id/media_container"
              clickable="true" bounds="[100,720][800,1060]">
          <node package="com.whatsapp"
                resource-id="com.whatsapp:id/thumb"
                content-desc="Video, duration 10 seconds"
                clickable="true"
                bounds="[100,720][800,1060]"/>
        </node>
        <node package="com.whatsapp" resource-id="com.whatsapp:id/date"
              text="8:16 pm" bounds="[650,1020][800,1080]"/>
      </node>
      <node package="com.whatsapp" bounds="[0,1100][1080,1500]">
        <node package="com.whatsapp" resource-id="com.whatsapp:id/content"
              clickable="true" bounds="[100,1120][900,1460]">
          <node package="com.whatsapp" resource-id="com.whatsapp:id/title"
                text="evidence.docx" bounds="[180,1160][700,1230]"/>
          <node package="com.whatsapp"
                resource-id="com.whatsapp:id/file_size"
                text="7 B" bounds="[180,1250][350,1310]"/>
          <node package="com.whatsapp"
                resource-id="com.whatsapp:id/file_type"
                text="DOCX" bounds="[360,1250][500,1310]"/>
        </node>
        <node package="com.whatsapp" resource-id="com.whatsapp:id/date"
              text="8:17 pm" bounds="[650,1420][800,1480]"/>
      </node>
    </node>
  </node>
</hierarchy>
"""

MEDIA_VIEWER = """\
<hierarchy>
  <node package="com.whatsapp" bounds="[0,0][1080,2400]">
    <node package="com.whatsapp"
          resource-id="com.whatsapp:id/media_view_fragment_container"
          bounds="[0,0][1080,2400]"/>
    <node package="com.whatsapp" content-desc="Save"
          class="android.widget.Button" clickable="true"
          bounds="[528,92][672,236]"/>
  </node>
</hierarchy>
"""


class AttachmentDevice(TextHistoryDevice):
    PHOTO = (100, 320, 800, 660)
    VIDEO = (100, 720, 800, 1060)
    DOCUMENT = (100, 1120, 900, 1460)
    SAVE = (528, 92, 672, 236)

    def __init__(self):
        super().__init__()
        self.photo_saved = False
        self.video_saved = False
        self.document_clicks = 0
        self.opened_attachments = []

    def hierarchy(self):
        if self.state in {"latest", "older"}:
            return ATTACHMENT_WINDOW
        if self.state in {"photo_viewer", "video_viewer"}:
            return MEDIA_VIEWER
        if self.state == "resolver":
            return (
                "<hierarchy><node package=\"android\" "
                "bounds=\"[0,0][1080,2400]\"/></hierarchy>"
            )
        return super().hierarchy()

    def click_bounds(self, bounds, *, anchor="center"):
        bounds = tuple(bounds)
        if self.state == "latest" and bounds == self.PHOTO:
            self.opened_attachments.append("photo")
            self.state = "photo_viewer"
            return
        if self.state == "latest" and bounds == self.VIDEO:
            self.opened_attachments.append("video")
            self.state = "video_viewer"
            return
        if self.state == "latest" and bounds == self.DOCUMENT:
            self.opened_attachments.append("document")
            self.document_clicks += 1
            self.state = "resolver"
            return
        if self.state == "photo_viewer" and bounds == self.SAVE:
            self.photo_saved = True
            return
        if self.state == "video_viewer" and bounds == self.SAVE:
            self.video_saved = True
            return
        super().click_bounds(bounds, anchor=anchor)

    def back(self):
        if self.state in {"photo_viewer", "video_viewer", "resolver"}:
            self.state = "latest"
            return
        raise AssertionError(self.state)

    def shell(self, command):
        if "/sdcard/Pictures/WhatsApp" in command:
            return (
                "/sdcard/Pictures/WhatsApp/photo.jpg\0"
                "5\0" + ("2\0" if self.photo_saved else "1\0")
                if self.photo_saved
                else ""
            )
        if "/sdcard/Movies/WhatsApp" in command:
            return (
                "/sdcard/Movies/WhatsApp/video.mp4\0"
                "6\0" + ("2\0" if self.video_saved else "1\0")
                if self.video_saved
                else ""
            )
        return ""

    def pull(self, remote_path, local_path):
        sizes = {
            "photo.jpg": 5,
            "video.mp4": 6,
        }
        Path(local_path).write_bytes(
            b"x" * sizes[PurePosixPath(remote_path).name]
        )


def test_materialize_route_acquires_media_and_records_document_metadata(
    tmp_path,
):
    whatsapp = importlib.import_module("aura.apps.whatsapp")
    profile = replace(
        _profile(),
        timings={
            **_profile().timings,
            "default_timeout": 0.01,
            "poll_interval": 0.0,
            "application_start_settle": 0.0,
            "transition_settle": 0.0,
            "inventory_probe_interval": 0.0,
        },
    )
    system = ProfileStore(ROOT / "profiles").load_system_ui(
        "samsung"
    )

    device = AttachmentDevice()
    result = AcquisitionRuntime(tmp_path, device).run(
        profile,
        system,
        Route.MATERIALIZE,
        {
            "target": {
                "kind": "chat",
                "ref": "chat.whatsapp.test.attachment",
                "display_name": "TEST TEST",
            }
        },
        whatsapp.collect,
        run_id="whatsapp-attachment-materialize",
    )

    assert result.outcome.status is OutcomeStatus.COMPLETE
    assert result.outcome.details[
        "materialized_attachment_count"
    ] == 2
    assert result.outcome.details["attachment_count"] == 2
    assert {item.item_type for item in result.items} >= {
        "conversation",
        "message",
        "photo",
        "video",
    }
    retained = {
        record.relative_path
        for record in result.artifacts
        if record.kind == "app_materialized_file"
    }
    assert {PurePosixPath(path).name for path in retained} == {
        "photo.jpg",
        "video.mp4",
    }
    for artifact in result.artifacts:
        if artifact.kind != "app_materialized_file":
            continue
        observation = next(
            (item for item in result.observations
             if item.observation_id == artifact.observation_id),
            None,
        )
        assert observation is not None
        assert observation.attempt_id == artifact.attempt_id
        tree = next(item for item in result.artifacts
                    if item.artifact_id == observation.hierarchy_artifact_id)
        assert (result.run_dir / tree.relative_path).read_text() == MEDIA_VIEWER
    assert device.opened_attachments == ["video", "photo"]
    assert device.document_clicks == 0
    document_message = next(
        record
        for path in tmp_path.rglob("messages/*.json")
        if (record := json.loads(path.read_text()))["kind"]
        == "document"
    )
    assert document_message["attachment"] == {
        "displayed_name": "evidence.docx",
        "displayed_size": "7 B",
        "displayed_type": "DOCX",
    }
    assert "materialization" not in document_message


@pytest.mark.parametrize("capture_error,return_error", [
    (False, False), (True, False), (False, True),
])
def test_materialize_failure_retains_original_screen_before_return(
    tmp_path, capture_error, return_error,
):
    whatsapp = importlib.import_module("aura.apps.whatsapp")
    failure_xml = '<hierarchy><node package="com.whatsapp" text="Media unavailable"/></hierarchy>'

    class UnavailableViewerDevice(AttachmentDevice):
        def hierarchy(self):
            if self.state == "video_viewer":
                return failure_xml
            return super().hierarchy()

        def screenshot(self):
            if self.state == "video_viewer":
                if capture_error:
                    raise OSError("failure screen capture unavailable")
                return b"\x89PNG\r\n\x1a\nactual-failure-screen"
            return super().screenshot()

        def back(self):
            if self.state == "video_viewer" and return_error:
                raise OSError("cannot return to chat")
            return super().back()

    profile = replace(_profile(), timings={
        **_profile().timings, "default_timeout": .01,
        "poll_interval": 0, "application_start_settle": 0,
        "transition_settle": 0, "inventory_probe_interval": 0,
    })
    result = AcquisitionRuntime(tmp_path, UnavailableViewerDevice()).run(
        profile, ProfileStore(ROOT / "profiles").load_system_ui("samsung"),
        Route.MATERIALIZE,
        {"target": {"kind": "chat", "ref": "failure-chat", "display_name": "TEST TEST"}},
        whatsapp.collect, run_id="viewer-failure",
    )
    assert result.outcome.status is OutcomeStatus.PARTIAL
    item = next(item for item in result.items if item.item_type == "video")
    attempt = next(attempt for attempt in result.attempts
                   if attempt.acquisition_item_id == item.acquisition_item_id)
    assert attempt.acquisition_status.value == "not_acquired"
    outcome = next(item for item in result.outcomes
                   if item.attempt_id == attempt.attempt_id)
    assert outcome.reason == "whatsapp_media_viewer_unavailable"
    assert outcome.action_id is not None
    events = [json.loads(line) for line in
              (result.run_dir / "events.jsonl").read_text().splitlines()]
    action = next(event for event in events
                  if event.get("action_id") == outcome.action_id)
    assert action["attempt_id"] == attempt.attempt_id
    failure_action = next(event for event in events if event.get("action_id")
                          == action["context"]["source_action_id"])
    opening_action = next((event for event in events if "action_id" in event and event["action_id"]
                           == failure_action["context"].get("source_action_id")), None)
    assert opening_action is not None
    assert opening_action["action"] == "open_whatsapp_attachment"
    assert opening_action["context"]["attachment_ref"] == "attachment-000001"
    records = [artifact for artifact in result.artifacts
               if artifact.attempt_id == attempt.attempt_id
               and artifact.relative_path.endswith("/record.json")]
    assert len(records) == 1
    record = json.loads((result.run_dir / records[0].relative_path).read_text())
    assert record["acquisition_status"] == "not_materialized"
    assert record["reason_code"] == "whatsapp_media_viewer_unavailable"
    if capture_error:
        assert outcome.observation_id is None
        assert any(event.get("status") == "failed"
                   and event.get("attempt_id") == attempt.attempt_id
                   and event["details"].get("error_type") == "OSError"
                   for event in events)
    else:
        observation = next((item for item in result.observations
                            if item.observation_id == outcome.observation_id), None)
        assert observation is not None
        assert observation.attempt_id == attempt.attempt_id
        assert records[0].observation_id == observation.observation_id
        artifacts = {item.artifact_id: item for item in result.artifacts}
        tree = artifacts[observation.hierarchy_artifact_id]
        screen = artifacts[observation.screen_artifact_id]
        assert (result.run_dir / tree.relative_path).read_text() == failure_xml
        assert (result.run_dir / screen.relative_path).read_bytes() == b"\x89PNG\r\n\x1a\nactual-failure-screen"
        assert tree.attempt_id == screen.attempt_id == attempt.attempt_id
        assert tree.action_id == screen.action_id == observation.action_id


@pytest.mark.parametrize("return_error,reason", [
    (False, "whatsapp_chat_screen_unavailable"),
    (True, "whatsapp_attachment_return_failed"),
])
def test_materialized_file_survives_chat_return_failure(tmp_path, return_error, reason):
    whatsapp = importlib.import_module("aura.apps.whatsapp")
    failure_xml = '<hierarchy><node package="com.whatsapp" text="Return blocked"/></hierarchy>'

    class FailedReturnDevice(AttachmentDevice):
        returning = False

        def back(self):
            self.returning = True
            if return_error:
                raise OSError("Back transport failed")

        def hierarchy(self):
            return failure_xml if self.returning else super().hierarchy()

        def screenshot(self):
            return b"\x89PNG\r\n\x1a\nreturn-blocked" if self.returning else super().screenshot()

    profile = replace(_profile(), timings={
        **_profile().timings, "default_timeout": .01,
        "poll_interval": 0, "application_start_settle": 0,
        "transition_settle": 0, "inventory_probe_interval": 0,
    })
    result = AcquisitionRuntime(tmp_path, FailedReturnDevice()).run(
        profile, ProfileStore(ROOT / "profiles").load_system_ui("samsung"),
        Route.MATERIALIZE,
        {"target": {"kind": "chat", "ref": "return-failure", "display_name": "TEST TEST"}},
        whatsapp.collect, run_id="return-failure",
    )
    assert result.outcome.status is OutcomeStatus.PARTIAL
    assert result.outcome.reason == reason
    acquired = [item for item in result.artifacts if item.kind == "app_materialized_file"]
    assert len(acquired) == 1
    video = acquired[0]
    assert (result.run_dir / video.relative_path).read_bytes() == b"xxxxxx"
    attempt = next(item for item in result.attempts if item.attempt_id == video.attempt_id)
    assert attempt.acquisition_status.value == "acquired"
    assert attempt.procedure_status.value == "interrupted"
    assert attempt.reason == reason
    observations = {item.observation_id: item for item in result.observations}
    artifacts = {item.artifact_id: item for item in result.artifacts}
    original_tree = artifacts[observations[video.observation_id].hierarchy_artifact_id]
    assert (result.run_dir / original_tree.relative_path).read_text() == MEDIA_VIEWER
    records = [item for item in result.artifacts if item.attempt_id == video.attempt_id
               and item.relative_path.endswith("/record.json")]
    assert len(records) == 1
    record = json.loads((result.run_dir / records[0].relative_path).read_text())
    assert record["acquisition_status"] == "materialized"
    assert record["source"]["observation_id"] == video.observation_id
    recovery = next(item for item in result.artifacts
                    if item.relative_path.endswith("/recovery-failure.json"))
    assert recovery.attempt_id == video.attempt_id
    recovery_record = json.loads((result.run_dir / recovery.relative_path).read_text())
    assert recovery_record["reason_code"] == reason
    observed = observations[recovery.observation_id]
    assert observed.observation_id != video.observation_id
    assert observed.attempt_id == video.attempt_id
    assert (result.run_dir / artifacts[observed.hierarchy_artifact_id].relative_path).read_text() == failure_xml
    assert (result.run_dir / artifacts[observed.screen_artifact_id].relative_path).read_bytes() == b"\x89PNG\r\n\x1a\nreturn-blocked"

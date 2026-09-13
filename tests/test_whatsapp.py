import importlib
import json
import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path

import pytest

from aura.models import Outcome, OutcomeStatus, Route
from aura.profiles import ProfileStore
from aura.runtime import AcquisitionRuntime


ROOT = Path(__file__).resolve().parents[1]
CHAT_ROW = (0, 476, 1080, 704)
MORE_OPTIONS = (920, 100, 1060, 240)
MORE = (700, 400, 1040, 520)
EXPORT_CHAT = (600, 520, 1040, 640)
INCLUDE_MEDIA = (580, 1360, 1020, 1510)
BLUETOOTH = (551, 2133, 708, 2232)
SHARE_MORE = (760, 2133, 920, 2232)
RECEIVER = (0, 1211, 1080, 1421)


def _profile():
    return ProfileStore(ROOT / "profiles").load_app(
        "whatsapp", "2.26.27.85"
    )


def _samsung():
    return ProfileStore(ROOT / "profiles").load_system_ui("samsung")


def chat_list_xml(*, duplicate=False, row_class="android.widget.LinearLayout"):
    second = (
        '<node package="com.whatsapp" '
        'resource-id="com.whatsapp:id/contact_row_container" '
        f'class="{row_class}" clickable="true" '
        'bounds="[0,704][1080,932]">'
        '<node package="com.whatsapp" text="TEST TEST" '
        'resource-id="com.whatsapp:id/conversations_row_contact_name" '
        'class="android.widget.TextView" bounds="[216,746][462,811]"/>'
        "</node>"
        if duplicate
        else ""
    )
    return (
        "<hierarchy>"
        '<node package="com.whatsapp" bounds="[0,0][1080,2400]">'
        '<node package="com.whatsapp" resource-id="android:id/list" '
        'class="androidx.recyclerview.widget.RecyclerView" '
        'bounds="[0,428][1080,2014]">'
        '<node package="com.whatsapp" '
        'resource-id="com.whatsapp:id/contact_row_container" '
        f'class="{row_class}" clickable="true" '
        'bounds="[0,476][1080,704]">'
        '<node package="com.whatsapp" text="TEST TEST" '
        'resource-id="com.whatsapp:id/conversations_row_contact_name" '
        'class="android.widget.TextView" bounds="[216,518][462,583]"/>'
        "</node>"
        f"{second}"
        "</node>"
        "</node>"
        "</hierarchy>"
    )


def chat_xml():
    return (
        "<hierarchy>"
        '<node package="com.whatsapp" bounds="[0,0][1080,2400]">'
        '<node package="com.whatsapp" text="TEST TEST" '
        'resource-id="com.whatsapp:id/conversation_contact_name" '
        'class="android.widget.TextView" bounds="[200,100][700,220]"/>'
        '<node package="com.whatsapp" content-desc="More options" '
        'class="android.widget.ImageView" clickable="true" '
        'bounds="[920,100][1060,240]"/>'
        "</node>"
        "</hierarchy>"
    )


def menu_xml():
    return (
        "<hierarchy>"
        '<node package="com.whatsapp" bounds="[0,0][1080,2400]">'
        '<node package="com.whatsapp" text="More" '
        'class="android.widget.TextView" clickable="true" '
        'bounds="[700,400][1040,520]"/>'
        "</node>"
        "</hierarchy>"
    )


def more_menu_xml():
    return (
        "<hierarchy>"
        '<node package="com.whatsapp" bounds="[0,0][1080,2400]">'
        '<node package="com.whatsapp" text="Export chat" '
        'class="android.widget.TextView" clickable="true" '
        'bounds="[600,520][1040,640]"/>'
        "</node>"
        "</hierarchy>"
    )


def export_dialog_xml():
    return (
        "<hierarchy>"
        '<node package="com.whatsapp" bounds="[0,0][1080,2400]">'
        '<node package="com.whatsapp" text="Include media" '
        'class="android.widget.TextView" clickable="true" '
        'bounds="[580,1360][1020,1510]"/>'
        "</node>"
        "</hierarchy>"
    )


def share_sheet_xml(*, duplicate=False):
    second = (
        '<node package="com.android.intentresolver" text="Bluetooth" '
        'resource-id="com.android.intentresolver:id/text1" '
        'class="android.widget.TextView" clickable="false" '
        'bounds="[317,2133][474,2232]"/>'
        if duplicate
        else ""
    )
    return (
        "<hierarchy>"
        '<node package="com.android.intentresolver" '
        'bounds="[0,0][1080,2400]">'
        '<node package="com.android.intentresolver" text="Bluetooth" '
        'resource-id="com.android.intentresolver:id/text1" '
        'class="android.widget.TextView" clickable="false" '
        'bounds="[551,2133][708,2232]"/>'
        f"{second}"
        "</node>"
        "</hierarchy>"
    )


def share_sheet_without_bluetooth_xml():
    return (
        "<hierarchy>"
        '<node package="com.android.intentresolver" '
        'bounds="[0,0][1080,2400]">'
        '<node package="com.android.intentresolver" text="Quick Share" '
        'resource-id="com.android.intentresolver:id/text1" '
        'class="android.widget.TextView" clickable="false" '
        'bounds="[120,2133][300,2232]"/>'
        "</node>"
        "</hierarchy>"
    )


def share_sheet_more_xml():
    return (
        "<hierarchy>"
        '<node package="com.android.intentresolver" '
        'bounds="[0,0][1080,2400]">'
        '<node package="com.android.intentresolver" text="More" '
        'resource-id="com.android.intentresolver:id/text1" '
        'class="android.widget.TextView" clickable="false" '
        'bounds="[760,2133][920,2232]"/>'
        "</node>"
        "</hierarchy>"
    )


def receiver_picker_xml(*, duplicate=False):
    second = (
        '<node package="com.android.settings" '
        'class="android.widget.LinearLayout" clickable="true" '
        'bounds="[0,1421][1080,1631]">'
        '<node package="com.android.settings" '
        'text="‎김준기의 Mac mini‎" resource-id="android:id/title" '
        'bounds="[198,1488][596,1563]"/>'
        "</node>"
        if duplicate
        else ""
    )
    return (
        "<hierarchy>"
        '<node package="com.android.settings" bounds="[0,0][1080,2400]">'
        '<node package="com.android.settings" text="Select device" '
        'resource-id="com.android.settings:id/'
        'collapsing_appbar_extended_title" bounds="[0,395][1080,535]"/>'
        '<node package="com.android.settings" '
        'class="android.widget.LinearLayout" clickable="true" '
        'bounds="[0,1211][1080,1421]">'
        '<node package="com.android.settings" '
        'text="‎김준기의 Mac mini‎" resource-id="android:id/title" '
        'bounds="[198,1278][596,1353]"/>'
        "</node>"
        f"{second}"
        "</node>"
        "</hierarchy>"
    )


class WhatsAppDevice:
    TREES = {
        "chat_list": chat_list_xml(),
        "chat": chat_xml(),
        "menu": menu_xml(),
        "more_menu": more_menu_xml(),
        "export_dialog": export_dialog_xml(),
        "share_sheet": share_sheet_xml(),
    }

    def __init__(self):
        self.state = "stopped"
        self.started = False
        self.boundary_clicks = []
        self.bluetooth_clicked = False

    def app_start(self, package_name):
        assert package_name == "com.whatsapp"
        self.started = True
        self.state = "chat_list"

    def app_stop(self, package_name):
        assert package_name == "com.whatsapp"

    def exists(self, selector):
        return self.count(selector) > 0

    def count(self, selector):
        if selector == {"packageName": "com.whatsapp"}:
            return int(self.started)
        return 0

    def click(self, selector):
        raise AssertionError(f"unexpected semantic click: {selector}")

    def click_xpath(self, xpath):
        raise AssertionError(f"unexpected XPath click: {xpath}")

    def click_bounds(self, bounds, *, anchor="center"):
        bounds = tuple(bounds)
        self.boundary_clicks.append(bounds)
        transitions = {
            ("chat_list", CHAT_ROW): "chat",
            ("chat", MORE_OPTIONS): "menu",
            ("menu", MORE): "more_menu",
            ("more_menu", EXPORT_CHAT): "export_dialog",
            ("export_dialog", INCLUDE_MEDIA): "share_sheet",
        }
        if self.state == "share_sheet" and bounds == BLUETOOTH:
            self.bluetooth_clicked = True
        try:
            self.state = transitions[(self.state, bounds)]
        except KeyError:
            raise AssertionError(
                f"unexpected boundary click in {self.state}: {bounds}"
            ) from None

    def long_click(self, selector):
        raise AssertionError(f"unexpected long click: {selector}")

    def long_click_bounds(self, bounds):
        raise AssertionError(f"unexpected long click: {bounds}")

    def back(self):
        raise AssertionError("collector must stop on the share sheet")

    def swipe(self, direction, duration=0.2):
        raise AssertionError(f"unexpected swipe: {direction}")

    def hierarchy(self):
        return self.TREES[self.state]

    def screenshot(self):
        return b"\x89PNG\r\n\x1a\nwhatsapp-share-sheet"

    def shell(self, command):
        return ""

    def pull(self, remote_path, local_path):
        raise AssertionError("collector must not pull an Export")

    def window_size(self):
        return 1080, 2400


class WhatsAppFallbackDevice(WhatsAppDevice):
    TREES = {
        **WhatsAppDevice.TREES,
        "share_sheet": share_sheet_without_bluetooth_xml(),
        "share_more": share_sheet_more_xml(),
        "share_expanded": share_sheet_xml(),
        "receiver_picker": receiver_picker_xml(),
    }

    def __init__(self, inbox=None, *, change_after_swipe=True):
        super().__init__()
        self.inbox = inbox
        self.change_after_swipe = change_after_swipe
        self.swipes = []

    def click_bounds(self, bounds, *, anchor="center"):
        bounds = tuple(bounds)
        if self.state == "share_more" and bounds == SHARE_MORE:
            self.boundary_clicks.append(bounds)
            self.state = "share_expanded"
            return
        if self.state == "share_expanded" and bounds == BLUETOOTH:
            self.boundary_clicks.append(bounds)
            self.bluetooth_clicked = True
            self.state = "receiver_picker"
            return
        if self.state == "receiver_picker" and bounds == RECEIVER:
            self.boundary_clicks.append(bounds)
            if self.inbox is not None:
                (self.inbox / "WhatsApp Chat with TEST TEST.zip").write_bytes(
                    b"whatsapp-export"
                )
            return
        super().click_bounds(bounds, anchor=anchor)

    def swipe(self, direction, duration=0.2):
        self.swipes.append(direction)
        if (
            self.state == "share_sheet"
            and direction == "right"
            and self.change_after_swipe
        ):
            self.state = "share_more"
            return
        if self.state != "share_sheet":
            raise AssertionError(f"unexpected swipe in {self.state}: {direction}")

    def shell(self, command):
        if command == "settings get global bluetooth_on":
            return "1"
        if command == "dumpsys bluetooth_manager":
            return (
                "Bonded devices:\n"
                "  name=김준기의 Mac mini\n"
                "Devices in DB:\n"
            )
        return super().shell(command)


class WhatsAppExportDevice(WhatsAppDevice):
    TREES = {
        **WhatsAppDevice.TREES,
        "receiver_picker": receiver_picker_xml(),
    }

    def __init__(self, inbox, *, write_export=True, write_hidden=False):
        super().__init__()
        self.inbox = inbox
        self.write_export = write_export
        self.write_hidden = write_hidden

    def click_bounds(self, bounds, *, anchor="center"):
        bounds = tuple(bounds)
        if self.state == "share_sheet" and bounds == BLUETOOTH:
            self.boundary_clicks.append(bounds)
            self.bluetooth_clicked = True
            self.state = "receiver_picker"
            return
        if self.state == "receiver_picker" and bounds == RECEIVER:
            self.boundary_clicks.append(bounds)
            if self.write_hidden:
                (self.inbox / ".DS_Store").write_bytes(b"metadata")
            if self.write_export:
                (self.inbox / "WhatsApp Chat with TEST TEST.zip").write_bytes(
                    b"whatsapp-export"
                )
            return
        super().click_bounds(bounds, anchor=anchor)

    def shell(self, command):
        if command == "settings get global bluetooth_on":
            return "1"
        if command == "dumpsys bluetooth_manager":
            return (
                "Bonded devices:\n"
                "  name=김준기의 Mac mini\n"
                "Devices in DB:\n"
            )
        return super().shell(command)


class WhatsAppDelayedBluetoothDevice(WhatsAppExportDevice):
    def __init__(self, inbox):
        super().__init__(inbox)
        self.share_sheet_reads = 0
        self.swipes = []

    def hierarchy(self):
        if self.state == "share_sheet":
            self.share_sheet_reads += 1
            if self.share_sheet_reads <= 2:
                return share_sheet_without_bluetooth_xml()
        return super().hierarchy()

    def swipe(self, direction, duration=0.2):
        self.swipes.append(direction)
        raise AssertionError("share sheet was swiped before Bluetooth settled")


class OpenWhatsAppExportDevice(WhatsAppExportDevice):
    def app_start(self, package_name):
        assert package_name == "com.whatsapp"
        self.started = True
        self.state = "chat"


def test_chat_row_bounds_requires_one_exact_visible_name():
    collector = importlib.import_module("aura.apps.whatsapp.collector")

    assert collector.chat_row_bounds(
        ET.fromstring(chat_list_xml()),
        _profile(),
        "TEST TEST",
    ) == CHAT_ROW

    with pytest.raises(
        collector.WhatsAppCollectorError,
        match="chat target is ambiguous",
    ):
        collector.chat_row_bounds(
            ET.fromstring(chat_list_xml(duplicate=True)),
            _profile(),
            "TEST TEST",
        )


def test_chat_row_bounds_accepts_button_row_after_chat_navigation():
    collector = importlib.import_module("aura.apps.whatsapp.collector")

    assert collector.chat_row_bounds(
        ET.fromstring(chat_list_xml(row_class="android.widget.Button")),
        _profile(),
        "TEST TEST",
    ) == CHAT_ROW


def test_bluetooth_target_bounds_requires_one_samsung_candidate():
    collector = importlib.import_module("aura.apps.whatsapp.collector")
    operation = _samsung().operations["share_sheet"]

    assert collector.bluetooth_target_bounds(
        ET.fromstring(share_sheet_xml()),
        operation,
    ) == BLUETOOTH

    with pytest.raises(
        collector.WhatsAppCollectorError,
        match="Bluetooth target is ambiguous",
    ):
        collector.bluetooth_target_bounds(
            ET.fromstring(share_sheet_xml(duplicate=True)),
            operation,
        )


def test_receiver_bounds_normalizes_formatting_and_requires_one_match():
    collector = importlib.import_module("aura.apps.whatsapp.collector")
    operation = _samsung().operations["share_sheet"]

    assert collector.receiver_bounds(
        ET.fromstring(receiver_picker_xml()),
        operation,
        "김준기의 Mac mini",
    ) == RECEIVER

    with pytest.raises(
        collector.WhatsAppCollectorError,
        match="Bluetooth receiver is ambiguous",
    ):
        collector.receiver_bounds(
            ET.fromstring(receiver_picker_xml(duplicate=True)),
            operation,
            "김준기의 Mac mini",
        )


def test_received_file_ignores_hidden_host_metadata(tmp_path):
    collector = importlib.import_module("aura.apps.whatsapp.collector")
    (tmp_path / ".DS_Store").write_bytes(b"metadata")
    export = tmp_path / "WhatsApp Chat with TEST TEST.zip"
    export.write_bytes(b"whatsapp-export")

    received = collector._wait_for_received_file(
        tmp_path,
        {},
        timeout=0.01,
        poll_interval=0.0,
    )

    assert received == export


def test_received_file_reports_missing_receipt(tmp_path):
    collector = importlib.import_module("aura.apps.whatsapp.collector")

    with pytest.raises(
        collector.WhatsAppCollectorError,
        match="whatsapp_export_receipt_missing",
    ):
        collector._wait_for_received_file(
            tmp_path,
            {},
            timeout=0.0,
            poll_interval=0.0,
        )


def test_export_reuses_already_open_chat(tmp_path, monkeypatch):
    collector = importlib.import_module(
        "aura.apps.whatsapp.collector"
    )
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    device = OpenWhatsAppExportDevice(inbox)
    profile = replace(
        _profile(),
        timings={
            **_profile().timings,
            "default_timeout": 0.01,
            "poll_interval": 0.0,
            "application_start_settle": 0.0,
            "transition_settle": 0.0,
            "export_receipt_timeout": 0.01,
            "export_receipt_poll_interval": 0.0,
        },
    )
    monkeypatch.setattr(
        collector,
        "prepare_windows_bluetooth_receiver",
        lambda timeout_sec: {
            "ok": True,
            "reason": "receive_waiting_screen_detected",
            "detail": "",
        },
    )
    monkeypatch.setattr(
        collector,
        "finish_windows_bluetooth_receive",
        lambda **kwargs: {
            "ok": True,
            "reason": "finish_button_clicked",
            "detail": "Finish",
        },
    )

    def collect_open(context):
        assert context.start_app()
        report = collector.collect_export(
            context,
            {
                "kind": "chat",
                "ref": "chat-list.whatsapp.active.chat-000001",
                "display_name": "TEST TEST",
            },
            already_open=True,
        )
        return Outcome(
            OutcomeStatus.COMPLETE,
            action=report["action"],
        )

    result = AcquisitionRuntime(tmp_path / "runs", device).run(
        profile,
        _samsung(),
        Route.EXPORT,
        {
            "target": {
                "kind": "chat_list",
                "ref": "chat-list.whatsapp.active",
            },
            "bluetooth": {
                "target_name": "김준기의 Mac",
                "receiver_label": "김준기의 Mac mini",
                "receive_dir": str(inbox),
            },
        },
        collect_open,
        run_id="whatsapp-open-chat-export",
    )

    assert result.outcome.status is OutcomeStatus.COMPLETE
    assert {item.item_type for item in result.items} == {
        "conversation_export"
    }
    assert all(
        attempt.acquisition_status.value == "acquired"
        for attempt in result.attempts
    )
    assert CHAT_ROW not in device.boundary_clicks
    assert device.boundary_clicks[0] == MORE_OPTIONS


def test_collect_rejects_non_chat_target_without_starting(tmp_path):
    whatsapp = importlib.import_module("aura.apps.whatsapp")
    device = WhatsAppDevice()

    result = AcquisitionRuntime(tmp_path, device).run(
        _profile(),
        _samsung(),
        Route.EXPORT,
        {"target": {"kind": "account", "ref": "account.whatsapp.test"}},
        whatsapp.collect,
        run_id="whatsapp-invalid-target",
    )

    assert result.outcome.status is OutcomeStatus.FAILED
    assert result.outcome.reason == "whatsapp_target_invalid"
    assert device.started is False


def test_collect_selects_receiver_and_retains_received_export(
    tmp_path,
    monkeypatch,
):
    whatsapp = importlib.import_module("aura.apps.whatsapp")
    collector = importlib.import_module("aura.apps.whatsapp.collector")
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    profile = replace(
        _profile(),
        timings={
            "default_timeout": 0.01,
            "poll_interval": 0.0,
            "application_start_settle": 0.0,
            "transition_settle": 0.0,
            "share_target_wait": 0.0,
            "export_receipt_timeout": 0.01,
            "export_receipt_poll_interval": 0.0,
        },
    )
    device = WhatsAppExportDevice(inbox, write_export=False)
    monkeypatch.setattr(
        collector,
        "prepare_windows_bluetooth_receiver",
        lambda timeout_sec: {
            "ok": True,
            "reason": "receive_waiting_screen_detected",
            "detail": "",
        },
    )

    def finish_receiver(*, timeout_sec, poll_interval):
        (inbox / "WhatsApp Chat with TEST TEST.zip").write_bytes(
            b"whatsapp-export"
        )
        return {
            "ok": True,
            "reason": "finish_button_clicked",
            "detail": "Finish",
        }

    monkeypatch.setattr(
        collector,
        "finish_windows_bluetooth_receive",
        finish_receiver,
    )

    result = AcquisitionRuntime(tmp_path, device).run(
        profile,
        _samsung(),
        Route.EXPORT,
        {
            "bluetooth": {
                "target_name": "김준기의 Mac",
                "receiver_label": "김준기의 Mac mini",
                "receive_dir": str(inbox),
            },
            "target": {
                "kind": "chat",
                "ref": "chat.whatsapp.test.001",
                "display_name": "TEST TEST",
            },
        },
        whatsapp.collect,
        run_id="whatsapp-export-complete",
    )

    assert result.outcome.status is OutcomeStatus.COMPLETE
    exported = result.artifacts[-1]
    document = json.loads((result.run_dir / "acquisition.json").read_text())
    identified = next(record for record in document["identifications"] if record["target_id"] == "whatsapp.chat_export")
    assert identified["traversal_complete"] is True
    assert document["attempts"][0]["started_at"] <= next(
        json.loads(line)["timestamp"] for line in (result.run_dir / "events.jsonl").read_text().splitlines()
        if json.loads(line).get("action") == "prepare_windows_bluetooth_receiver"
    )
    assert exported.kind == "app_export"
    assert exported.relative_path.endswith(
        "/WhatsApp Chat with TEST TEST.zip"
    )
    assert exported.sha256 == (
        "8973bf440da54baaa52c3ef8c2cc84e738429c22727e9dfa6c87975637f6fe6c"
    )
    assert device.boundary_clicks[-2:] == [BLUETOOTH, RECEIVER]


def test_collect_waits_for_bluetooth_before_share_sheet_fallback(
    tmp_path,
    monkeypatch,
):
    whatsapp = importlib.import_module("aura.apps.whatsapp")
    collector = importlib.import_module("aura.apps.whatsapp.collector")
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    profile = replace(
        _profile(),
        timings={
            "default_timeout": 0.01,
            "poll_interval": 0.0,
            "application_start_settle": 0.0,
            "transition_settle": 0.0,
            "share_target_wait": 0.01,
            "export_receipt_timeout": 0.01,
            "export_receipt_poll_interval": 0.0,
        },
    )
    device = WhatsAppDelayedBluetoothDevice(inbox)
    monkeypatch.setattr(
        collector,
        "prepare_windows_bluetooth_receiver",
        lambda timeout_sec: {
            "ok": True,
            "reason": "receive_waiting_screen_detected",
            "detail": "",
        },
    )
    monkeypatch.setattr(
        collector,
        "finish_windows_bluetooth_receive",
        lambda **kwargs: {
            "ok": True,
            "reason": "finish_button_clicked",
            "detail": "Finish",
        },
    )

    result = AcquisitionRuntime(tmp_path, device).run(
        profile,
        _samsung(),
        Route.EXPORT,
        {
            "bluetooth": {
                "target_name": "김준기의 Mac",
                "receiver_label": "김준기의 Mac mini",
                "receive_dir": str(inbox),
            },
            "target": {
                "kind": "chat",
                "ref": "chat.whatsapp.delayed.001",
                "display_name": "TEST TEST",
            },
        },
        whatsapp.collect,
        run_id="whatsapp-export-delayed-bluetooth",
    )

    assert result.outcome.status is OutcomeStatus.COMPLETE
    assert device.swipes == []


def test_collect_rejects_missing_bluetooth_condition(tmp_path):
    whatsapp = importlib.import_module("aura.apps.whatsapp")
    profile = replace(
        _profile(),
        timings={
            "default_timeout": 0.01,
            "poll_interval": 0.0,
            "application_start_settle": 0.0,
            "transition_settle": 0.0,
            "share_target_wait": 0.0,
        },
    )
    device = WhatsAppDevice()

    result = AcquisitionRuntime(tmp_path, device).run(
        profile,
        _samsung(),
        Route.EXPORT,
        {
            "target": {
                "kind": "chat",
                "ref": "chat.whatsapp.test.001",
                "display_name": "TEST TEST",
            }
        },
        whatsapp.collect,
        run_id="whatsapp-bluetooth-identification",
    )

    assert result.outcome.status is OutcomeStatus.FAILED
    assert result.outcome.reason == "whatsapp_bluetooth_condition_invalid"
    assert result.artifacts == ()
    assert result.outcomes == ()
    assert device.state == "stopped"
    assert device.bluetooth_clicked is False
    assert device.boundary_clicks == []


def test_collect_uses_samsung_share_sheet_fallback_before_bluetooth(
    tmp_path,
    monkeypatch,
):
    whatsapp = importlib.import_module("aura.apps.whatsapp")
    collector = importlib.import_module("aura.apps.whatsapp.collector")
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    profile = replace(
        _profile(),
        timings={
            "default_timeout": 0.01,
            "poll_interval": 0.0,
            "application_start_settle": 0.0,
            "transition_settle": 0.0,
            "share_target_wait": 0.0,
            "export_receipt_timeout": 0.01,
            "export_receipt_poll_interval": 0.0,
        },
    )
    device = WhatsAppFallbackDevice(inbox)
    monkeypatch.setattr(
        collector,
        "prepare_windows_bluetooth_receiver",
        lambda timeout_sec: {
            "ok": True,
            "reason": "receive_waiting_screen_detected",
            "detail": "",
        },
    )
    monkeypatch.setattr(
        collector,
        "finish_windows_bluetooth_receive",
        lambda **kwargs: {
            "ok": True,
            "reason": "finish_button_clicked",
            "detail": "Finish",
        },
    )

    result = AcquisitionRuntime(tmp_path, device).run(
        profile,
        _samsung(),
        Route.EXPORT,
        {
            "bluetooth": {
                "target_name": "김준기의 Mac",
                "receiver_label": "김준기의 Mac mini",
                "receive_dir": str(inbox),
            },
            "target": {
                "kind": "chat",
                "ref": "chat.whatsapp.fallback.001",
                "display_name": "TEST TEST",
            }
        },
        whatsapp.collect,
        run_id="whatsapp-bluetooth-fallback",
    )

    assert result.outcome.status is OutcomeStatus.COMPLETE
    assert device.swipes == ["right"]
    assert SHARE_MORE in device.boundary_clicks
    assert device.state == "receiver_picker"
    assert device.bluetooth_clicked is True


def test_collect_stops_when_share_sheet_swipe_does_not_change_ui(
    tmp_path,
    monkeypatch,
):
    whatsapp = importlib.import_module("aura.apps.whatsapp")
    collector = importlib.import_module("aura.apps.whatsapp.collector")
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    profile = replace(
        _profile(),
        timings={
            "default_timeout": 0.01,
            "poll_interval": 0.0,
            "application_start_settle": 0.0,
            "transition_settle": 0.0,
            "share_target_wait": 0.0,
        },
    )
    device = WhatsAppFallbackDevice(change_after_swipe=False)
    monkeypatch.setattr(
        collector,
        "prepare_windows_bluetooth_receiver",
        lambda timeout_sec: {
            "ok": True,
            "reason": "receive_waiting_screen_detected",
            "detail": "",
        },
    )

    result = AcquisitionRuntime(tmp_path, device).run(
        profile,
        _samsung(),
        Route.EXPORT,
        {
            "bluetooth": {
                "target_name": "김준기의 Mac",
                "receiver_label": "김준기의 Mac mini",
                "receive_dir": str(inbox),
            },
            "target": {
                "kind": "chat",
                "ref": "chat.whatsapp.unchanged.001",
                "display_name": "TEST TEST",
            }
        },
        whatsapp.collect,
        run_id="whatsapp-bluetooth-unchanged",
    )

    assert result.outcome.status is OutcomeStatus.PARTIAL
    assert result.outcome.reason == "whatsapp_share_fallback_unavailable"
    assert device.swipes == ["right"]
    assert SHARE_MORE not in device.boundary_clicks

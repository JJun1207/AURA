import json
import xml.etree.ElementTree as ET

import pytest

from aura.journal import EventJournal
from aura.profiles import ProfileStore
from aura.session import EnvironmentSnapshot
from aura.system_ui import DndSnapshot, SystemUIError, SystemUIRuntime
from fakes import FakeBluetoothDevice, FakeDndDevice, FakeEnvironmentDevice


def samsung_profile():
    return ProfileStore("profiles").load_system_ui("samsung")


def generic_profile():
    return ProfileStore("profiles").load_system_ui("generic")


def huawei_profile():
    return ProfileStore("profiles").load_system_ui("huawei")


def make_system_ui_runtime(tmp_path, device):
    journal = EventJournal(tmp_path / "events.jsonl", {"run_id": "run-1"})
    return SystemUIRuntime(
        device,
        samsung_profile(),
        journal,
        timeout=0,
        poll_interval=0,
        transition_settle=0,
    )


def test_generic_button_dnd_round_trip(tmp_path):
    class PixelEnvironmentDevice(FakeEnvironmentDevice):
        def hierarchy(self):
            if self.screen != "dnd":
                return "<hierarchy/>"
            state = "off" if self.dnd_enabled else "on"
            label = "Turn off now" if self.dnd_enabled else "Turn on now"
            status_icon = (
                '<node package="com.android.systemui" '
                'content-desc="Do Not Disturb"/>'
                if self.dnd_enabled
                else ""
            )
            return (
                "<hierarchy>"
                f'{status_icon}<node package="com.android.settings" '
                'content-desc="Do Not Disturb"/>'
                '<node class="android.widget.Button" clickable="true" '
                'enabled="true" package="com.android.settings" '
                'resource-id="com.android.settings:id/'
                f'zen_mode_settings_turn_{state}_button" text="{label}"/>'
                "</hierarchy>"
            )

        def click(self, selector):
            self.clicks.append(dict(selector))
            if not selector.get("resourceId", "").startswith(
                "com.android.settings:id/zen_mode_settings_turn_"
            ):
                raise AssertionError(f"unexpected selector: {selector}")
            self.dnd_enabled = not self.dnd_enabled

    device = PixelEnvironmentDevice(wifi_enabled=True)
    runtime = SystemUIRuntime(
        device,
        generic_profile(),
        EventJournal(tmp_path / "events.jsonl"),
        timeout=0,
        poll_interval=0,
        transition_settle=0,
    )

    snapshot = runtime.snapshot_environment()
    runtime.prepare_environment(snapshot)
    runtime.verify_prepared_environment()
    runtime.restore_environment(snapshot)

    assert snapshot.hide_all is None
    assert device.dnd_enabled is False
    assert device.airplane_enabled is False
    assert len(device.clicks) == 2


def test_generic_bluetooth_round_trip(tmp_path):
    device = FakeBluetoothDevice()
    runtime = SystemUIRuntime(
        device,
        generic_profile(),
        EventJournal(tmp_path / "events.jsonl"),
        timeout=0,
        poll_interval=0,
        transition_settle=0,
    )

    runtime._set_bluetooth(True, "test-enable")
    runtime._set_bluetooth(False, "test-disable")

    assert device.bluetooth_enabled is False


def test_bluetooth_pair_check_waits_for_service_dump(tmp_path):
    class DelayedBluetoothDevice(FakeBluetoothDevice):
        def __init__(self):
            super().__init__(paired_targets=("AURA Receiver",))
            self.dump_reads = 0

        def shell(self, command):
            if command == "dumpsys bluetooth_manager":
                self.dump_reads += 1
                if self.dump_reads == 1:
                    return "Bluetooth Status\n  enabled: true\n"
            return super().shell(command)

    device = DelayedBluetoothDevice()
    runtime = SystemUIRuntime(
        device,
        samsung_profile(),
        EventJournal(tmp_path / "events.jsonl"),
        timeout=1,
        poll_interval=0,
        transition_settle=0,
    )

    snapshot = runtime.prepare_bluetooth_delivery("AURA Receiver")
    runtime.restore_bluetooth_delivery(snapshot)

    assert device.dump_reads == 2
    assert device.bluetooth_enabled is False


def test_huawei_switch_dnd_round_trip(tmp_path):
    device = FakeEnvironmentDevice(airplane_enabled=True)
    runtime = SystemUIRuntime(
        device,
        huawei_profile(),
        EventJournal(tmp_path / "events.jsonl"),
        timeout=0,
        poll_interval=0,
        transition_settle=0,
    )

    snapshot = runtime.snapshot_environment()
    runtime.prepare_environment(snapshot)
    runtime.verify_prepared_environment()
    runtime.restore_environment(snapshot)

    assert snapshot.hide_all is None
    assert device.dnd_enabled is False
    assert device.xpath_clicks == [
        '//*[@resource-id="android:id/title" and '
        '@text="Do not disturb"]/following::*'
        '[@resource-id="android:id/switch_widget"][1]',
        '//*[@resource-id="android:id/title" and '
        '@text="Do not disturb"]/following::*'
        '[@resource-id="android:id/switch_widget"][1]',
    ]


def read_actions(tmp_path):
    return [
        event
        for event in (
            json.loads(line)
            for line in (tmp_path / "events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        )
        if event["event_type"] == "action"
    ]


@pytest.mark.parametrize(
    ("value", "expected"),
    [("0", False), ("1", True), ("2", True), ("3", True)],
)
def test_reads_only_documented_zen_mode_values(tmp_path, value, expected):
    runtime = make_system_ui_runtime(
        tmp_path,
        FakeDndDevice(zen_mode_output=value),
    )

    assert runtime._dnd_enabled() is expected


@pytest.mark.parametrize(
    "value",
    ["", "null", "Error: permission denied", "4", "unknown"],
)
def test_rejects_unreadable_zen_mode_values(tmp_path, value):
    runtime = make_system_ui_runtime(
        tmp_path,
        FakeDndDevice(zen_mode_output=value),
    )

    with pytest.raises(SystemUIError, match="unreadable"):
        runtime._dnd_enabled()


def test_environment_snapshot_preparation_and_restoration(tmp_path):
    device = FakeEnvironmentDevice(
        dnd_enabled=False,
        hide_all=False,
        airplane_enabled=False,
        wifi_enabled=True,
    )
    runtime = make_system_ui_runtime(tmp_path, device)

    snapshot = runtime.snapshot_environment()
    runtime.prepare_environment(snapshot)

    assert snapshot == EnvironmentSnapshot(
        dnd_enabled=False,
        hide_all=False,
        airplane_enabled=False,
        wifi_enabled=True,
        wifi_connected=True,
    )
    preparation = [
        event["action"]
        for event in read_actions(tmp_path)
        if event["action"]
        in {"system_ui_airplane_prepare", "system_ui_dnd_prepare"}
    ]
    assert preparation == [
        "system_ui_airplane_prepare",
        "system_ui_dnd_prepare",
    ]
    assert (
        device.dnd_enabled,
        device.hide_all,
        device.airplane_enabled,
        device.wifi_enabled,
    ) == (True, True, True, False)

    runtime.apply_acquisition_environment("device_only")
    assert device.wifi_enabled is False
    runtime.apply_acquisition_environment("controlled_online")
    assert device.wifi_enabled is True
    runtime.verify_prepared_environment("controlled_online")

    runtime.restore_environment(snapshot)

    assert (
        device.dnd_enabled,
        device.hide_all,
        device.airplane_enabled,
        device.wifi_enabled,
    ) == (False, False, False, True)
    actions = {event["action"] for event in read_actions(tmp_path)}
    assert {
        "system_ui_airplane_prepare",
        "system_ui_wifi_condition",
        "system_ui_environment_verify",
        "system_ui_airplane_restore",
        "system_ui_wifi_restore",
    } <= actions


def test_prepare_does_not_toggle_airplane_mode_when_already_enabled(tmp_path):
    class RecordingDevice(FakeEnvironmentDevice):
        def __init__(self):
            super().__init__(airplane_enabled=True, wifi_enabled=False)
            self.commands = []

        def shell(self, command):
            self.commands.append(command)
            return super().shell(command)

    device = RecordingDevice()
    runtime = make_system_ui_runtime(tmp_path, device)
    snapshot = runtime.snapshot_environment()
    device.commands.clear()

    runtime.prepare_environment(snapshot)

    assert "cmd connectivity airplane-mode enable" not in device.commands
    assert device.airplane_enabled is True


def test_controlled_online_requires_observed_wifi_connection(tmp_path):
    device = FakeEnvironmentDevice(
        airplane_enabled=True,
        wifi_enabled=False,
        wifi_connected=False,
    )
    runtime = make_system_ui_runtime(tmp_path, device)

    with pytest.raises(SystemUIError, match="not connected"):
        runtime.apply_acquisition_environment("controlled_online")
    verification = next(event for event in read_actions(tmp_path)
                        if event["action"] == "system_ui_wifi_connection_verify")
    assert verification["details"] == {"acquisition_environment": "controlled_online"}


@pytest.mark.parametrize(
    ("network_state", "expected"),
    [
        ("CONNECTED/CONNECTED", True),
        ("DISCONNECTED/DISCONNECTED", False),
    ],
)
def test_android_9_wifi_status_fallback_uses_connectivity_dump(
    tmp_path,
    network_state,
    expected,
):
    device = FakeEnvironmentDevice(
        wifi_enabled=True,
        wifi_status_output=(
            "Security exception: Uid 2000 does not have access to wifi commands"
        ),
        connectivity_output=(
            "NetworkAgentInfo{ ni{[type: WIFI[], "
            f"state: {network_state}, reason: (unspecified)]}}"
        ),
    )
    runtime = make_system_ui_runtime(tmp_path, device)

    assert runtime._wifi_connected() is expected


def test_android_9_wifi_status_fallback_allows_pending_connectivity_dump(
    tmp_path,
):
    device = FakeEnvironmentDevice(
        wifi_enabled=True,
        wifi_status_output=(
            "Security exception: Uid 2000 does not have access to wifi commands"
        ),
        connectivity_output="Current Networks:\n",
    )
    runtime = make_system_ui_runtime(tmp_path, device)

    assert runtime._wifi_connected() is False


def test_unknown_acquisition_environment_does_not_change_wifi(tmp_path):
    device = FakeEnvironmentDevice(
        airplane_enabled=True,
        wifi_enabled=True,
    )
    runtime = make_system_ui_runtime(tmp_path, device)

    with pytest.raises(SystemUIError, match="environment"):
        runtime.apply_acquisition_environment("internet")

    assert device.wifi_enabled is True


def test_android_9_bluetooth_ui_fallback_uses_related_switch(tmp_path):
    device = FakeBluetoothDevice(
        refuse_shell_toggle=True,
        bluetooth_ui=True,
    )
    runtime = make_system_ui_runtime(tmp_path, device)

    runtime._set_bluetooth(True, "test")

    assert device.bluetooth_enabled is True
    assert device.xpath_clicks == [
        '//*[@resource-id="android:id/title" and @text="Bluetooth"]'
        '/ancestor::*[@clickable="true"][1]'
        '//*[@resource-id="android:id/switch_widget"]'
    ]


@pytest.mark.parametrize(
    ("initial_dnd", "initial_hide_all"),
    [(False, False), (False, True), (True, False), (True, True)],
)
def test_prepares_and_restores_required_samsung_hide_all(
    tmp_path,
    initial_dnd,
    initial_hide_all,
):
    device = FakeDndDevice(
        dnd_enabled=initial_dnd,
        hide_all=initial_hide_all,
    )
    runtime = make_system_ui_runtime(tmp_path, device)

    snapshot = runtime.prepare_notification_suppression()

    assert snapshot == DndSnapshot(
        enabled=initial_dnd,
        hide_all=initial_hide_all,
    )
    assert device.dnd_enabled is True
    assert device.hide_all is True
    assert all("x" not in selector and "y" not in selector for selector in device.clicks)
    assert all(
        "bounds" not in xpath and "coordinate" not in xpath
        for xpath in device.xpath_clicks
    )

    runtime.restore_notification_suppression(snapshot)

    assert device.dnd_enabled is initial_dnd
    assert device.hide_all is initial_hide_all
    actions = read_actions(tmp_path)
    transitions = [
        event
        for event in actions
        if event["action"]
        in {
            "system_ui_dnd_prepare",
            "system_ui_hide_all",
            "system_ui_dnd_restore",
        }
    ]
    assert [event["action"] for event in transitions] == [
        "system_ui_dnd_prepare",
        "system_ui_hide_all",
        "system_ui_hide_all",
        "system_ui_dnd_restore",
    ]
    assert [event["details"]["phase"] for event in transitions] == [
        "prepare",
        "prepare",
        "restore",
        "restore",
    ]
    assert all(
        set(("before", "target", "after", "profile", "strategy"))
        <= event["details"].keys()
        for event in transitions
    )
    navigation = [
        event
        for event in actions
        if event["action"].endswith("_navigation")
    ]
    assert {event["details"]["phase"] for event in navigation} == {
        "prepare",
        "restore",
    }
    dnd_navigation = any(
        event["action"] == "system_ui_dnd_navigation"
        and event["details"]["intent"].startswith(
            "am start -a android.settings.ZEN_MODE_SETTINGS"
        )
        and event["details"]["selector"]
        == {
            "resourceId": "android:id/title",
            "text": "Do not disturb",
        }
        for event in navigation
    )
    assert dnd_navigation is (not initial_dnd)
    assert any(
        event["action"] == "system_ui_hide_notifications_navigation"
        and event["details"]["attempt"] == 1
        and "Hide notifications" in event["details"]["selector"]
        for event in navigation
    )
    assert all(
        "bounds" not in json.dumps(event["details"])
        and "coordinates" not in json.dumps(event["details"])
        for event in actions
    )


def test_galaxy_s8_dnd_uses_clickable_turn_on_now_row(tmp_path):
    device = FakeDndDevice(legacy_dnd_main_row=True)
    runtime = make_system_ui_runtime(tmp_path, device)

    snapshot = runtime.prepare_notification_suppression()

    assert device.dnd_enabled is True
    assert '@text="Turn on now"' in "".join(device.xpath_clicks)

    runtime.restore_notification_suppression(snapshot)

    assert device.dnd_enabled is False


def test_resource_identified_dnd_title_wins_over_text_only_duplicate(tmp_path):
    device = FakeDndDevice(text_only_dnd_title=True)
    runtime = make_system_ui_runtime(tmp_path, device)

    runtime.prepare_notification_suppression()

    assert (
        '//*[@resource-id="android:id/title" and @text="Do not disturb"]'
        '/ancestor::*[@clickable="true"][1]'
    ) in device.xpath_clicks


def test_expanded_dnd_headers_do_not_conflict_with_clickable_row(tmp_path):
    device = FakeDndDevice(expanded_dnd_headers=True)
    runtime = make_system_ui_runtime(tmp_path, device)

    runtime._open_hide_notifications()

    assert device.screen == "hide"
    assert device.xpath_clicks == [
        '//*[@resource-id="android:id/title" and @text="Hide notifications"]'
        '/ancestor::*[@clickable="true"][1]'
    ]


def test_duplicate_resource_identified_dnd_titles_fail_before_clicking(tmp_path):
    device = FakeDndDevice(duplicate_dnd_title=True)
    runtime = make_system_ui_runtime(tmp_path, device)

    with pytest.raises(SystemUIError, match="exactly one"):
        runtime.prepare_notification_suppression()

    assert device.clicks == []
    assert not any(
        '@text="Do not disturb"' in xpath
        for xpath in device.xpath_clicks
    )


def test_dnd_rows_click_nearest_semantic_ancestors(tmp_path):
    device = FakeDndDevice()
    runtime = make_system_ui_runtime(tmp_path, device)

    runtime.prepare_notification_suppression()

    hide_notifications = (
        '//*[@resource-id="android:id/title" and @text="Hide notifications"]'
        '/ancestor::*[@clickable="true"][1]'
    )
    dnd = (
        '//*[@resource-id="android:id/title" and @text="Do not disturb"]'
        '/ancestor::*[@clickable="true"][1]'
    )
    hide_all = (
        '//*[@resource-id="com.android.settings:id/switch_text" '
        'and @text="Hide all"]/ancestor::*[@clickable="true"][1]'
    )
    assert device.xpath_clicks == [
        hide_notifications,
        dnd,
        hide_notifications,
        hide_all,
    ]
    assert device.clicks == []


def test_retries_transient_hide_notifications_navigation_once(tmp_path):
    device = FakeDndDevice(defer_first_hide_navigation=True)
    runtime = make_system_ui_runtime(tmp_path, device)

    runtime._open_hide_notifications()

    xpath = (
        '//*[@resource-id="android:id/title" and @text="Hide notifications"]'
        '/ancestor::*[@clickable="true"][1]'
    )
    assert device.screen == "hide"
    assert device.xpath_clicks == [xpath, xpath]
    navigation = [
        event
        for event in read_actions(tmp_path)
        if event["action"] == "system_ui_hide_notifications_navigation"
    ]
    assert [event["status"] for event in navigation] == [
        "failed",
        "success",
    ]
    assert [event["details"]["attempt"] for event in navigation] == [1, 2]
    assert all(event["details"]["phase"] == "prepare" for event in navigation)
    assert all(event["details"]["selector"] == xpath for event in navigation)


def test_visible_hide_notifications_opens_without_top_normalization(tmp_path):
    device = FakeDndDevice(hide_notifications_without_title=True)
    runtime = make_system_ui_runtime(tmp_path, device)

    runtime._open_hide_notifications()

    assert device.screen == "hide"
    assert device.swipes == []


def test_open_hide_notifications_reenters_stale_target_from_dnd(tmp_path):
    device = FakeDndDevice(preserve_hide_screen_on_dnd_intent=True)
    device.screen = "hide"
    runtime = make_system_ui_runtime(tmp_path, device)

    runtime._open_hide_notifications("restore")

    assert device.back_count == 1
    assert device.screen == "hide"
    assert device.xpath_clicks == [
        '//*[@resource-id="android:id/title" and @text="Hide notifications"]'
        '/ancestor::*[@clickable="true"][1]'
    ]
    assert device.swipes == []


def test_open_dnd_moves_to_top_when_title_is_initially_hidden(tmp_path):
    device = FakeDndDevice(dnd_scroll_pages=2)
    runtime = make_system_ui_runtime(tmp_path, device)

    runtime._open_dnd()

    assert device.swipes == [("down", 0.2)] * 2


def test_open_dnd_top_recovery_is_bounded(tmp_path):
    device = FakeDndDevice(dnd_scroll_pages=99)
    runtime = make_system_ui_runtime(tmp_path, device)

    with pytest.raises(SystemUIError, match="cannot open DND settings"):
        runtime._open_dnd()

    assert device.swipes == [("down", 0.2)] * 10


def test_open_dnd_returns_once_from_hide_notifications_child(tmp_path):
    device = FakeDndDevice(preserve_hide_screen_on_dnd_intent=True)
    device.screen = "hide"
    runtime = make_system_ui_runtime(tmp_path, device)

    runtime._open_dnd()

    assert device.screen == "dnd"
    assert device.back_count == 1
    assert device.swipes == []


def test_open_dnd_settles_after_back_before_reading_hierarchy(tmp_path):
    class DelayedBackDevice(FakeDndDevice):
        def __init__(self):
            super().__init__(preserve_hide_screen_on_dnd_intent=True)
            self.screen = "hide"
            self.pending_back = False

        def back(self):
            self.back_count += 1
            self.pending_back = True

        def settle(self, delay):
            if self.pending_back:
                self.screen = "dnd"
                self.pending_back = False

    device = DelayedBackDevice()
    journal = EventJournal(tmp_path / "events.jsonl")
    runtime = SystemUIRuntime(
        device,
        samsung_profile(),
        journal,
        timeout=0,
        poll_interval=0,
        sleep=device.settle,
    )

    runtime._open_dnd()

    assert device.screen == "dnd"
    assert device.back_count == 1
    assert device.swipes == []


def test_clickable_ancestor_xpath_escapes_both_quote_types():
    node = ET.fromstring(
        '<node resource-id="android:id/title" '
        'text="Do &quot;not&quot; disturb&apos;s"/>'
    )

    xpath = SystemUIRuntime._clickable_ancestor_xpath(node)

    assert xpath == (
        """//*[@resource-id="android:id/title" and """
        """@text=concat("Do ", '"', "not", '"', " disturb's")]"""
        '/ancestor::*[@clickable="true"][1]'
    )


def test_ambiguous_hide_all_fails_before_clicking(tmp_path):
    device = FakeDndDevice(duplicate_hide_all=True)
    runtime = make_system_ui_runtime(tmp_path, device)

    with pytest.raises(SystemUIError, match="exactly one"):
        runtime.prepare_notification_suppression()

    assert device.dnd_enabled is False
    assert device.hide_all is False
    assert not any(click.get("text") == "Hide all" for click in device.clicks)
    assert len(device.xpath_clicks) == 1


def test_missing_hide_all_fails_before_changing_dnd(tmp_path):
    device = FakeDndDevice(missing_hide_all=True)
    runtime = make_system_ui_runtime(tmp_path, device)

    with pytest.raises(SystemUIError, match="exactly one"):
        runtime.prepare_notification_suppression()

    assert device.dnd_enabled is False
    assert not any(click.get("text") == "Hide all" for click in device.clicks)


def test_unchanged_hide_all_preserves_prepared_dnd(tmp_path):
    device = FakeDndDevice(refuse_hide_all=True)
    runtime = make_system_ui_runtime(tmp_path, device)

    with pytest.raises(SystemUIError, match="Hide all"):
        runtime.prepare_notification_suppression()

    assert device.dnd_enabled is True
    assert device.hide_all is False
    actions = read_actions(tmp_path)
    failed_hide_all = next(
        event
        for event in actions
        if event["action"] == "system_ui_hide_all"
        and event["status"] == "failed"
    )
    assert failed_hide_all["details"]["phase"] == "prepare"
    rollback = [
        event
        for event in actions
        if event["details"].get("phase") == "rollback"
    ]
    assert rollback == []


@pytest.mark.parametrize("airplane", [False, True])
@pytest.mark.parametrize("wifi", [False, True])
@pytest.mark.parametrize("dnd", [False, True])
def test_startup_verifies_all_settings_for_every_initial_combination(
    tmp_path, airplane, wifi, dnd
):
    device = FakeEnvironmentDevice(
        airplane_enabled=airplane, wifi_enabled=wifi,
        dnd_enabled=dnd, hide_all=True,
    )
    runtime = make_system_ui_runtime(tmp_path, device)
    runtime.prepare_environment(runtime.snapshot_environment())
    observed = runtime.verify_prepared_environment()
    assert observed == EnvironmentSnapshot(True, True, True, False, False)
    assert (device.airplane_enabled, device.wifi_enabled, device.dnd_enabled) == (
        True, False, True
    )


def test_startup_verification_rejects_wifi_on(tmp_path):
    device = FakeEnvironmentDevice(
        airplane_enabled=True, wifi_enabled=True, dnd_enabled=True, hide_all=True
    )
    runtime = make_system_ui_runtime(tmp_path, device)
    with pytest.raises(SystemUIError, match="drifted"):
        runtime.verify_prepared_environment()


def test_unreadable_initial_hide_all_does_not_change_dnd(tmp_path):
    device = FakeDndDevice(unreadable_hide_all=True)
    runtime = make_system_ui_runtime(tmp_path, device)

    with pytest.raises(SystemUIError, match="unreadable"):
        runtime.prepare_notification_suppression()

    assert device.dnd_enabled is False
    assert device.hide_all is False
    assert device.clicks == []
    assert device.xpath_clicks == [
        '//*[@resource-id="android:id/title" and @text="Hide notifications"]'
        '/ancestor::*[@clickable="true"][1]'
    ]

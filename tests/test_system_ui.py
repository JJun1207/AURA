import json
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

from aura.journal import EventJournal
from aura.profiles import ProfileStore
from aura.session import EnvironmentSnapshot
from aura.system_ui import DndSnapshot, SystemUIError, SystemUIRuntime
from fakes import FakeBluetoothDevice, FakeDndDevice, FakeEnvironmentDevice


class PairingDevice(FakeBluetoothDevice):
    def __init__(self, *, outcome="approved", targets=("Lab Receiver",), paired_targets=()):
        super().__init__(paired_targets=paired_targets)
        self.outcome = outcome
        self.targets = targets
        self.pair_requested = False
        self.pair_checks = 0
        self.commands = []

    def shell(self, command):
        self.commands.append(command)
        if command.startswith("am start -a android.settings.BLUETOOTH"):
            self.screen = "pairing"
            return "Starting: Intent"
        if command == "dumpsys bluetooth_manager" and self.pair_requested:
            self.pair_checks += 1
            if self.outcome == "interrupt":
                raise KeyboardInterrupt()
            if self.outcome == "approved" and self.pair_checks >= 2:
                self.paired_targets = ("Lab Receiver",)
                self.pair_requested = False
        return super().shell(command)

    def click(self, selector):
        self.clicks.append(selector)
        if selector.get("resourceId") == "com.android.settings:id/deviceDetails":
            self.screen = "details"
        elif selector.get("text") == "Unpair":
            self.paired_targets = tuple(name for name in self.paired_targets if name != "Lab Receiver")
            self.screen = "pairing"
            self.pair_requested = False

    def hierarchy(self):
        if self.screen == "details":
            return ('<hierarchy><node package="com.android.settings" '
                    'resource-id="com.android.settings:id/bluetooth_header_title" text="Lab Receiver"/>'
                    '<node package="com.android.settings" resource-id="com.android.settings:id/navigation_bar_item_small_label_view" '
                    'text="Unpair"/></hierarchy>')
        if self.screen == "pairing":
            rows = "".join(
                '<node clickable="true" package="com.android.settings">'
                f'<node package="com.android.settings" resource-id="android:id/title" text="{name}"/>'
                '</node>' for name in self.targets
            )
            if "Lab Receiver" in self.paired_targets:
                rows += ('<node package="com.android.settings" resource-id="com.android.settings:id/deviceDetails" '
                         'content-desc="&#8206;Lab Receiver&#8206;, Device settings" clickable="true"/>')
            return f'<hierarchy><node package="com.android.settings" text="Bluetooth">{rows}</node></hierarchy>'
        return super().hierarchy()

    def click_xpath(self, xpath):
        if any(f'@text="{name}"' in xpath for name in self.targets):
            self.xpath_clicks.append(xpath)
            self.pair_requested = True
            return
        raise AssertionError(f"unexpected pairing click: {xpath}")


@pytest.mark.parametrize("label", ["Lab Receiver", "\u200eLab Receiver\u200e", "\u2068Lab Receiver\u2069"])
@pytest.mark.parametrize("outcome", ["approved", "timeout", "interrupt", "already_paired"])
def test_bluetooth_preparation_pairs_only_the_named_receiver(tmp_path, monkeypatch, outcome, label):
    from aura import windows_bluetooth

    monkeypatch.setattr(windows_bluetooth, "_IS_WINDOWS", True)
    opened = []
    monkeypatch.setattr(windows_bluetooth.os, "startfile", opened.append, raising=False)
    device = PairingDevice(
        outcome=outcome,
        targets=(label,),
        paired_targets=("Lab Receiver",) if outcome == "already_paired" else (),
    )
    runtime = make_system_ui_runtime(tmp_path, device)
    runtime.profile.operations["bluetooth"]["pairing"] = {
        "direct_intents": ["am start -a android.settings.BLUETOOTH_SETTINGS"],
        "owner_packages": ["com.android.settings"],
        "page_labels": ["Bluetooth"],
        "device_resource_ids": ["android:id/title"],
        "timeout_sec": 0.5,
    }
    ticks = iter(i / 10 for i in range(100))
    runtime._monotonic = lambda: next(ticks)
    if outcome in {"approved", "already_paired"}:
        snapshot = runtime.prepare_bluetooth_delivery("Lab Receiver")
        assert device.bluetooth_enabled is True
        assert device.paired_targets == ("Lab Receiver",)
        runtime.restore_bluetooth_delivery(snapshot)
        assert device.paired_targets == (("Lab Receiver",) if outcome == "already_paired" else ())
    else:
        error = KeyboardInterrupt if outcome == "interrupt" else SystemUIError
        with pytest.raises(error):
            runtime.prepare_bluetooth_delivery("Lab Receiver")
    assert device.bluetooth_enabled is False
    assert opened == ([] if outcome == "already_paired" else ["ms-settings:bluetooth"])
    assert len(device.xpath_clicks) == (0 if outcome == "already_paired" else 1)
    actions = read_actions(tmp_path)
    if outcome != "already_paired":
        completed = [a for a in actions if a["action"] == "system_ui_bluetooth_pairing"]
        assert completed[-1]["status"] == ("success" if outcome == "approved" else "failed")
        assert completed[-1]["details"]["elapsed_seconds"] >= 0


def test_bluetooth_pair_check_does_not_accept_a_longer_receiver_name(tmp_path):
    device = FakeBluetoothDevice(enabled=True, paired_targets=("Lab Receiver Other",))
    runtime = make_system_ui_runtime(tmp_path, device)
    assert runtime._bluetooth_target_paired("Lab Receiver") is False


@pytest.mark.parametrize("drift", [None, "power", "bond"])
def test_bluetooth_delivery_settles_and_rechecks_before_collection(tmp_path, drift):
    device = FakeBluetoothDevice(paired_targets=("Lab Receiver",))
    runtime = make_system_ui_runtime(tmp_path, device)
    runtime.transition_settle = 0.5
    waits = []
    checks = []
    original_shell = device.shell

    def shell(command):
        if command == "dumpsys bluetooth_manager":
            checks.append(len(waits))
        return original_shell(command)

    def sleep(seconds):
        waits.append(seconds)
        if len(waits) == 2:
            if drift == "power":
                device.bluetooth_enabled = False
            elif drift == "bond":
                device.paired_targets = ()

    device.shell = shell
    runtime._sleep = sleep
    if drift:
        with pytest.raises(SystemUIError, match="not stable"):
            runtime.prepare_bluetooth_delivery("Lab Receiver")
        assert device.bluetooth_enabled is False
    else:
        snapshot = runtime.prepare_bluetooth_delivery("Lab Receiver")
        assert snapshot.paired_before is True
        assert checks == [1, 2]
    assert waits == [0.5, 0.5]


@pytest.mark.parametrize("name,expected", [("Lab Receiver", True), ("Lab Receiver Other", False)])
def test_connected_dual_bond_with_address_alias(tmp_path, name, expected):
    device = FakeBluetoothDevice(enabled=True)
    device.shell = lambda command: (
        "Bonded devices:\n"
        f" (Connected) 02:00:00:00:00:01 => 02:00:00:00:00:02 [ DUAL ] {name} (AudioSource,AudioSink)\n"
        "Devices in DB:\n"
    )
    assert make_system_ui_runtime(tmp_path, device)._bluetooth_target_paired("Lab Receiver") is expected


@pytest.mark.parametrize("ending", ["  Devices in DB:\n", "mSnoopLogSettingAtEnable = false\n\nAdapterState:\n",
                                   "  Scan Mode Changes:\n", "sSnoopLogSettingAtEnable = EMPTY\n"])
@pytest.mark.parametrize("transports,expected", [
    (("BR/EDR",), True),
    (("BR/EDR", "LE"), True),
    (("LE", "DUAL"), True),
    (("BR/EDR", "DUAL", "LE"), None),
    (("LE",), True),
    ((), False),
])
def test_bluetooth_bond_formats_and_classic_le_duplicates(tmp_path, ending, transports, expected):
    device = FakeBluetoothDevice(enabled=True)
    original_shell = device.shell
    rows = "".join(
        f"    (Connected) 00:00:00:00:00:{index:02x} [ {transport} ] Lab Receiver\n"
        for index, transport in enumerate(transports)
    )
    # A matching name outside the bonded section must not count as a bond.
    dump = ("AdapterProperties\n  Bonded devices:\n" + rows + ending
            + "  11:11:11:11:11:11 [BR/EDR] Lab Receiver\n")
    device.shell = lambda command: dump if command == "dumpsys bluetooth_manager" else original_shell(command)
    runtime = make_system_ui_runtime(tmp_path, device)
    if expected is None:
        with pytest.raises(SystemUIError, match="ambiguous"):
            runtime._bluetooth_target_paired("Lab Receiver")
    elif expected:
        snapshot = runtime.prepare_bluetooth_delivery("Lab Receiver")
        assert snapshot.paired_before is True
        runtime.restore_bluetooth_delivery(snapshot)
        assert device.bluetooth_enabled is True
    else:
        assert runtime._bluetooth_target_paired("Lab Receiver") is False


def test_bluetooth_pair_check_rejects_truncated_bonded_section(tmp_path):
    device = FakeBluetoothDevice(enabled=True)
    device.shell = lambda command: "Bonded devices:\n 00:00:00:00:00:00 [BR/EDR] Lab Receiver\n"
    with pytest.raises(SystemUIError, match="unreadable"):
        make_system_ui_runtime(tmp_path, device)._bluetooth_target_paired("Lab Receiver")


def test_pair_button_is_clicked_in_captured_samsung_confirmation(tmp_path):
    device = FakeBluetoothDevice()
    xml = (Path(__file__).parent / "fixtures/system_ui/samsung_bluetooth_pair_request.xml").read_text()
    device.hierarchy = lambda: xml
    runtime = make_system_ui_runtime(tmp_path, device)
    assert runtime._confirm_bluetooth_dialog("Lab Receiver", runtime.profile.operations["bluetooth"]["pairing"], runtime.profile.operations["bluetooth"]["pairing"]["confirmation"], "system_ui_bluetooth_pair_confirm")
    assert device.clicks == [{"packageName": "com.android.settings", "resourceId": "android:id/button1", "text": "Pair"}]


@pytest.mark.parametrize("case", ["approved", "waiting_on_windows", "wrong_target", "disabled", "foreign_owner"])
def test_android_pair_confirmation_is_scoped_to_requested_receiver(tmp_path, case):
    class Device(PairingDevice):
        confirmations = 0

        def hierarchy(self):
            if not self.pair_requested:
                return super().hierarchy()
            owner = "other.app" if case == "foreign_owner" else "com.android.settings"
            target = "Lab Receiver Other" if case == "wrong_target" else "Lab Receiver"
            enabled = "false" if case == "disabled" else "true"
            return (
                '<hierarchy><node package="com.android.settings" text="Lab Receiver"/>'
                f'<node package="{owner}" resource-id="android:id/parentPanel">'
                f'<node package="{owner}" text="Bluetooth pairing request"/>'
                f'<node package="{owner}" text="Pair with \u200e{target}\u200e?"/>'
                f'<node package="{owner}" resource-id="android:id/button1" '
                f'text="Pair" clickable="true" enabled="{enabled}"/>'
                '</node></hierarchy>'
            )

        def click(self, selector):
            if selector.get("text") != "Pair":
                return super().click(selector)
            assert selector == {"packageName": "com.android.settings", "resourceId": "android:id/button1", "text": "Pair"}
            self.confirmations += 1
            if case == "approved":
                self.paired_targets = ("Lab Receiver",)
                self.pair_requested = False

    device = Device(outcome="requires_confirmation")
    runtime = make_system_ui_runtime(tmp_path, device)
    runtime.profile.operations["bluetooth"]["pairing"]["timeout_sec"] = 0.5
    ticks = iter(i / 100 for i in range(200))
    runtime._monotonic = lambda: next(ticks)
    if case == "approved":
        snapshot = runtime.prepare_bluetooth_delivery("Lab Receiver")
        assert device.paired_targets == ("Lab Receiver",)
        runtime.restore_bluetooth_delivery(snapshot)
    else:
        with pytest.raises(SystemUIError, match="timeout"):
            runtime.prepare_bluetooth_delivery("Lab Receiver")
    assert device.confirmations == (1 if case in {"approved", "waiting_on_windows"} else 0)
    assert device.bluetooth_enabled is False


@pytest.mark.parametrize("names,expected", [
    (("\u200eLab Receiver\u200e",), True),
    (("\u2068Lab Receiver\u2069",), True),
    (("\u200eLab Receiver Other\u200e",), False),
    (("\u200eLab Receiver\u200e", "Lab Receiver"), None),
])
def test_bluetooth_pair_check_handles_display_marks_without_guessing(tmp_path, names, expected):
    device = FakeBluetoothDevice(enabled=True, paired_targets=names)
    runtime = make_system_ui_runtime(tmp_path, device)
    if expected is None:
        with pytest.raises(SystemUIError, match="ambiguous"):
            runtime._bluetooth_target_paired("Lab Receiver")
    else:
        assert runtime._bluetooth_target_paired("Lab Receiver") is expected


def test_bluetooth_pairing_falls_back_when_first_intent_stays_on_dnd(tmp_path, monkeypatch):
    monkeypatch.setattr("aura.system_ui.prepare_windows_bluetooth_pairing", lambda: {"ok": True})

    class Device(PairingDevice):
        def shell(self, command):
            if command == "am start -a android.settings.BLUETOOTH_SETTINGS":
                self.commands.append(command)
                self.screen = "dnd"
                return "Error: Activity not started"
            return super().shell(command)

        def hierarchy(self):
            if self.screen == "dnd":
                return '<hierarchy><node package="com.android.settings" text="Do not disturb"/></hierarchy>'
            return super().hierarchy()

    device = Device()
    runtime = make_system_ui_runtime(tmp_path, device)
    runtime.profile.operations["bluetooth"]["pairing"]["timeout_sec"] = 0.01
    ticks = iter(i / 10000 for i in range(1000))
    runtime._monotonic = lambda: next(ticks)
    runtime.prepare_bluetooth_delivery("Lab Receiver")
    assert device.paired_targets == ("Lab Receiver",)
    assert "am start -a android.settings.BLUETOOTH_PAIRING_SETTINGS" in device.commands


def test_bluetooth_pairing_host_failure_restores_power_without_selecting_receiver(tmp_path, monkeypatch):
    from aura import windows_bluetooth
    monkeypatch.setattr(windows_bluetooth, "_IS_WINDOWS", True)
    def unavailable(uri):
        raise OSError("Settings unavailable")
    monkeypatch.setattr(windows_bluetooth.os, "startfile", unavailable, raising=False)
    device = PairingDevice()
    runtime = make_system_ui_runtime(tmp_path, device)
    with pytest.raises(SystemUIError, match="host preparation failed"):
        runtime.prepare_bluetooth_delivery("Lab Receiver")
    assert device.bluetooth_enabled is False
    assert device.pair_requested is False


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
        wifi_connection_timeout=0,
        poll_interval=0,
        transition_settle=0,
    )


@pytest.mark.parametrize('kind', ['airplane', 'wifi'])
@pytest.mark.parametrize('outcome', ['first', 'second', 'unverified', 'shell_error'])
def test_radio_candidates_only_continue_after_verification_failure(tmp_path, kind, outcome):
    device = FakeEnvironmentDevice()
    runtime = make_system_ui_runtime(tmp_path, device)
    operation = (runtime.profile.operations['airplane'] if kind == 'airplane'
                 else runtime.profile.operations['network']['wifi'])
    operation['enable_commands'] = ['first', 'second']
    original = device.shell
    commands = []
    failure = SystemUIError('shell command failed')

    def shell(command):
        if command not in {'first', 'second'}:
            return original(command)
        commands.append(command)
        if outcome == 'shell_error':
            raise failure
        if command == outcome:
            setattr(device, f'{kind}_enabled', True)
        return ''

    device.shell = shell
    set_state = getattr(runtime, f'_set_{kind}')
    if outcome in {'unverified', 'shell_error'}:
        with pytest.raises(SystemUIError) as raised:
            set_state(True, 'prepare')
        if outcome == 'shell_error':
            assert raised.value is failure
    else:
        set_state(True, 'prepare')
    assert commands == (['first'] if outcome in {'first', 'shell_error'} else ['first', 'second'])
    event = read_actions(tmp_path)[-1]
    assert event['action'] == f'system_ui_{kind}_prepare'
    assert event['status'] == ('success' if outcome in {'first', 'second'} else 'failed')
    assert event['details']['before'] is False
    assert event['details']['after'] is (True if outcome in {'first', 'second'} else None)


@pytest.mark.parametrize('timeout,expected', [(0, [0]), (0.15, [0, 0.1, 0.2])])
def test_system_samples_keep_full_interval_and_final_observation(tmp_path, timeout, expected):
    runtime = make_system_ui_runtime(tmp_path, FakeEnvironmentDevice())
    now = [0]
    waits = []

    def sleep(seconds):
        waits.append(seconds)
        now[0] += seconds

    runtime.timeout, runtime.poll_interval = timeout, 0.1
    runtime._monotonic, runtime._sleep = lambda: now[0], sleep
    assert list(runtime._samples(lambda: now[0])) == expected
    assert waits == [0.1] * (len(expected) - 1)


@pytest.mark.parametrize('kind', ['state', 'label', 'clickable_label', 'paired_list'])
@pytest.mark.parametrize('ready_at', [0, 0.2, None])
def test_system_waits_preserve_immediate_and_deadline_boundary_reads(tmp_path, kind, ready_at):
    runtime = make_system_ui_runtime(tmp_path, FakeBluetoothDevice(enabled=True))
    now, observed, waits = [0], [], []

    def ready():
        observed.append(now[0])
        return ready_at is not None and now[0] >= ready_at

    def sleep(seconds):
        waits.append(seconds)
        now[0] += seconds

    def root():
        return ET.fromstring('<node clickable="true"><node text="Target"/></node>' if ready() else '<node/>')

    def dump(command):
        return ('Bonded devices:\n 00:00:00:00:00:00 [BR/EDR] Target\nDevices in DB:'
                if ready() else 'Bluetooth starting')

    runtime.timeout, runtime.poll_interval = 0.15, 0.1
    runtime._monotonic, runtime._sleep = lambda: now[0], sleep
    runtime._root, runtime.device.shell = root, dump
    if kind == 'state':
        run = lambda: runtime._wait_for(ready, True, 'test')
    elif kind == 'label':
        run = lambda: runtime._wait_for_label(['Target'])
    elif kind == 'clickable_label':
        run = lambda: runtime._wait_for_clickable_label(['Target'])
    else:
        run = lambda: runtime._bluetooth_target_paired('Target')
    if ready_at is None:
        with pytest.raises(SystemUIError):
            run()
    else:
        result = run()
        if kind in {'state', 'paired_list'}:
            assert result is True
        else:
            assert result.get('text') == 'Target'
    assert observed == ([0] if ready_at == 0 else [0, 0.1, 0.2])
    assert waits == ([] if ready_at == 0 else [0.1, 0.1])


@pytest.mark.parametrize("value,enabled", [("0", False), ("1", True), ("2", True), ("3", False), ("null", None), ("4", None)])
def test_environment_observes_airplane_wifi_states(tmp_path, value, enabled):
    class Device(FakeEnvironmentDevice):
        def shell(self, command):
            if command == "settings get global wifi_on":
                return value
            return super().shell(command)

    device = Device(airplane_enabled=True, wifi_enabled=bool(enabled), dnd_enabled=True, hide_all=True)
    runtime = make_system_ui_runtime(tmp_path, device)
    if enabled is None:
        with pytest.raises(SystemUIError, match="Wi-Fi state is unreadable"):
            runtime.snapshot_environment()
    else:
        observed = runtime.verify_prepared_environment("controlled_online" if enabled else "device_only")
        assert observed.wifi_enabled is enabled
        assert observed.wifi_connected is enabled


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

    assert device.dump_reads == 3  # Service readiness retry, then the final stability check.
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
        wifi_network=snapshot.wifi_network,
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


@pytest.mark.parametrize("ready_at", [0.0, 9.2, None])
def test_controlled_online_waits_for_ip_with_separate_deadline(tmp_path, ready_at):
    now = [0.0]
    device = FakeEnvironmentDevice(
        airplane_enabled=True,
        wifi_status_output="Security exception: Uid 2000 does not have access to wifi commands",
    )
    original_shell = device.shell

    def shell(command):
        if command == "dumpsys connectivity":
            state = ("CONNECTED/CONNECTED" if ready_at is not None and now[0] >= ready_at
                     else "CONNECTING/OBTAINING_IPADDR")
            return f"NetworkAgentInfo{{ ni{{[type: WIFI[], state: {state}]}}}}"
        return original_shell(command)

    def sleep(seconds):
        now[0] += seconds

    device.shell = shell
    runtime = SystemUIRuntime(
        device, huawei_profile(), EventJournal(tmp_path / "events.jsonl"),
        monotonic=lambda: now[0], sleep=sleep,
    )
    if ready_at is None:
        with pytest.raises(SystemUIError, match="Wi-Fi is not connected"):
            runtime.apply_acquisition_environment("controlled_online")
        assert 30 <= now[0] <= 30.3
    else:
        runtime.apply_acquisition_environment("controlled_online")
        assert ready_at <= now[0] <= ready_at + 0.3
    event = read_actions(tmp_path)[-1]
    assert event["status"] == ("failed" if ready_at is None else "success")
    assert event["details"]["observed"]["wifi_connected"] is (ready_at is not None)
    assert event["details"]["timeout_seconds"] == 30.0

    started = now[0]
    with pytest.raises(SystemUIError):
        runtime._wait_for(lambda: False, True, "ordinary UI state")
    assert 5 <= now[0] - started <= 5.3


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
    assert verification["details"]["acquisition_environment"] == "controlled_online"
    assert verification["details"]["observed"]["wifi_connected"] is False


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


@pytest.mark.parametrize("intent_lands_on_child", [False, True])
def test_open_hide_notifications_accepts_current_target(tmp_path, intent_lands_on_child):
    class Device(FakeDndDevice):
        def shell(self, command):
            if command.startswith("am start"):
                self.commands.append(command)
                self.screen = "hide"
                return ""
            return super().shell(command)

    device = Device()
    device.commands = []
    device.screen = "home" if intent_lands_on_child else "hide"
    runtime = make_system_ui_runtime(tmp_path, device)

    runtime._open_hide_notifications("restore")

    assert len(device.commands) == int(intent_lands_on_child)
    assert device.back_count == 0
    assert device.screen == "hide"
    assert device.xpath_clicks == []
    assert device.swipes == []


def test_repeated_environment_checks_read_current_state_without_reentry(tmp_path):
    device = FakeEnvironmentDevice(
        airplane_enabled=True, wifi_enabled=False, dnd_enabled=True, hide_all=True,
    )
    runtime = make_system_ui_runtime(tmp_path, device)
    runtime.prepare_environment(runtime.snapshot_environment())
    for _ in range(5):
        assert runtime.verify_prepared_environment().hide_all is True
    assert device.hide_navigation_attempts == 1

    device.hide_all = False
    with pytest.raises(SystemUIError, match="drifted"):
        runtime.verify_prepared_environment()
    assert device.hide_navigation_attempts == 1

    device.hide_all = True
    device.screen = "home"
    assert runtime.verify_prepared_environment().hide_all is True
    assert device.hide_navigation_attempts == 2


def test_hide_all_label_in_another_app_does_not_skip_settings_navigation(tmp_path):
    class Device(FakeDndDevice):
        def hierarchy(self):
            if self.screen == "home":
                return ('<hierarchy><node package="com.example.other" '
                        'resource-id="com.example.other:id/switch_text" '
                        'text="Hide all"/></hierarchy>')
            return super().hierarchy()

    device = Device()
    runtime = make_system_ui_runtime(tmp_path, device)
    runtime._open_hide_notifications()
    assert device.screen == "hide"
    assert device.hide_navigation_attempts == 1


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
    assert observed == EnvironmentSnapshot(True, True, True, False, False, observed.wifi_network)
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


def test_hide_all_reuse_requires_a_successful_prior_verification(tmp_path):
    device = FakeEnvironmentDevice(
        airplane_enabled=True, wifi_enabled=False, dnd_enabled=True, hide_all=True
    )
    runtime = make_system_ui_runtime(tmp_path, device)
    with pytest.raises(SystemUIError, match="prior environment verification"):
        runtime.verify_prepared_environment(reuse_hide_all=True)
    runtime.verify_prepared_environment()
    device.wifi_enabled = True
    with pytest.raises(SystemUIError, match="drifted"):
        runtime.verify_prepared_environment(reuse_hide_all=True)
    device.wifi_enabled = False
    with pytest.raises(SystemUIError, match="prior environment verification"):
        runtime.verify_prepared_environment(reuse_hide_all=True)


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


@pytest.mark.parametrize("failure", [None, "confirmation", "unpair_failed", "wrong_details", "interrupt_after_bond"])
def test_new_bond_cleanup_preserves_other_devices_and_restores_power(tmp_path, failure):
    class Device(PairingDevice):
        confirmation_clicked = False

        def click(self, selector):
            if selector.get("text") == "Unpair" and failure == "unpair_failed":
                raise OSError("unpair refused")
            if failure == "confirmation" and selector.get("text") == "Unpair":
                if selector.get("resourceId") != "android:id/button1":
                    self.screen = "unpair_confirm"
                    return
                self.confirmation_clicked = True
            super().click(selector)

        def hierarchy(self):
            if self.screen == "unpair_confirm":
                return ('<hierarchy><node package="com.android.settings" resource-id="android:id/parentPanel">'
                        '<node package="com.android.settings" text="Unpair &#8206;Lab Receiver&#8206;?"/>'
                        '<node package="com.android.settings" resource-id="android:id/button1" text="Unpair" '
                        'clickable="true" enabled="true"/></node></hierarchy>')
            xml = super().hierarchy()
            if self.screen == "details" and failure == "wrong_details":
                return xml.replace('text="Lab Receiver"', 'text="Other Receiver"')
            return xml

    device = Device(paired_targets=("Other Receiver",))
    runtime = make_system_ui_runtime(tmp_path, device)
    def pair(target):
        device.paired_targets += (target,)
        if failure == "interrupt_after_bond":
            raise KeyboardInterrupt()
    runtime._pair_bluetooth_target = pair
    if failure == "interrupt_after_bond":
        with pytest.raises(KeyboardInterrupt):
            runtime.prepare_bluetooth_delivery("Lab Receiver")
    else:
        snapshot = runtime.prepare_bluetooth_delivery("Lab Receiver")
        if failure in {"unpair_failed", "wrong_details"}:
            with pytest.raises((SystemUIError, OSError)):
                runtime.restore_bluetooth_delivery(snapshot)
        else:
            runtime.restore_bluetooth_delivery(snapshot)
    assert device.bluetooth_enabled is False
    assert "Other Receiver" in device.paired_targets
    assert ("Lab Receiver" in device.paired_targets) is (failure in {"unpair_failed", "wrong_details"})
    if failure == "wrong_details":
        assert not any(click.get("text") == "Unpair" for click in device.clicks)

    assert device.confirmation_clicked is (failure == "confirmation")


@pytest.mark.parametrize("approved", [True, False])
def test_duplicate_pairing_rows_select_first_and_still_require_bond(tmp_path, approved):
    from lxml import etree

    class Device(PairingDevice):
        def hierarchy(self):
            if self.screen != "pairing":
                return super().hierarchy()
            return ('<hierarchy><node package="com.android.settings" text="Bluetooth"/>'
                    '<node package="com.android.settings" clickable="true" bounds="[0,1230][1080,1440]">'
                    '<node package="com.android.settings" resource-id="android:id/title" text="&#8206;Lab Receiver&#8206;"/>'
                    '</node><node package="com.android.settings" clickable="true" bounds="[0,1650][1080,1860]">'
                    '<node package="com.android.settings" resource-id="android:id/title" text="&#8206;Lab Receiver&#8206;"/>'
                    '</node></hierarchy>')

        def click_xpath(self, xpath):
            selected = etree.fromstring(self.hierarchy().encode()).xpath(xpath)
            assert len(selected) == 1
            assert selected[0].get("bounds") == "[0,1230][1080,1440]"
            self.xpath_clicks.append(xpath)
            self.pair_requested = True

    device = Device(outcome="approved" if approved else "timeout")
    runtime = make_system_ui_runtime(tmp_path, device)
    runtime.profile.operations["bluetooth"]["pairing"]["timeout_sec"] = 0.1
    ticks = iter(i / 100 for i in range(1000))
    runtime._monotonic = lambda: next(ticks)
    if approved:
        runtime.prepare_bluetooth_delivery("Lab Receiver")
        assert device.paired_targets == ("Lab Receiver",)
    else:
        with pytest.raises(SystemUIError, match="pairing was not confirmed"):
            runtime.prepare_bluetooth_delivery("Lab Receiver")
        assert device.bluetooth_enabled is False
    assert len(device.xpath_clicks) == 1
    requested = next(a for a in read_actions(tmp_path) if a["action"] == "system_ui_bluetooth_pair_request")
    assert requested["details"]["candidate_count"] == 2
    assert requested["details"]["selected_index"] == 0


@pytest.mark.parametrize("ready_at, malformed", [(12, None), (0, None),
    (None, "missing"), (None, "ambiguous"), (None, "unreadable")])
def test_huawei_dnd_navigation_waits_for_verified_switch(tmp_path, ready_at, malformed):
    now = [0.0]
    valid = ('<hierarchy><node resource-id="android:id/title" text="Do not disturb"/>'
             '<node resource-id="android:id/switch_widget" checked="false"/></hierarchy>')
    class SlowSettings(FakeEnvironmentDevice):
        def hierarchy(self):
            if ready_at is not None and now[0] >= ready_at:
                return valid
            if malformed == "ambiguous":
                return valid.replace('</hierarchy>', '<node resource-id="android:id/title" text="Do not disturb"/></hierarchy>')
            if malformed == "unreadable":
                return valid.replace('checked="false"', 'checked="unknown"')
            return '<hierarchy/>'
    def sleep(seconds):
        now[0] += seconds
    device = SlowSettings()
    runtime = SystemUIRuntime(device, huawei_profile(), EventJournal(tmp_path / 'events.jsonl'),
        poll_interval=1, transition_settle=0, monotonic=lambda: now[0], sleep=sleep)
    if ready_at is None:
        with pytest.raises(SystemUIError, match="cannot open DND settings"):
            runtime._open_basic_dnd_switch("test")
        assert now[0] == 60  # Two bounded navigation intents.
    else:
        assert 'following::*' in runtime._open_basic_dnd_switch("test")
        assert now[0] == ready_at
    assert device.xpath_clicks == []
    assert runtime.timeout == 5

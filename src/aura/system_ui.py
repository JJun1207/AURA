from __future__ import annotations

import math
import re
import sys
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from functools import partial

from .device import Device
from .collection import poll_samples
from .journal import EventJournal
from .profiles import SystemUIProfile
from .session import EnvironmentSnapshot
from .ui import apply_state
from .windows_bluetooth import normalize_bluetooth_label, prepare_windows_bluetooth_pairing


class SystemUIError(RuntimeError):
    pass


class _WaitTimeout(SystemUIError):
    pass


@dataclass(frozen=True)
class DndSnapshot:
    enabled: bool
    hide_all: bool | None


@dataclass(frozen=True)
class BluetoothSnapshot:
    enabled: bool
    target_name: str | None = None
    paired_before: bool | None = None


class SystemUIRuntime:
    def __init__(
        self,
        device: Device,
        profile: SystemUIProfile,
        journal: EventJournal,
        *,
        timeout: float = 5.0,
        wifi_connection_timeout: float = 30.0,
        dnd_navigation_timeout: float = 30.0,
        poll_interval: float = 0.2,
        transition_settle: float = 0.5,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.device = device
        self.profile = profile
        self.journal = journal
        self.timeout = timeout
        self.wifi_connection_timeout = wifi_connection_timeout
        self.dnd_navigation_timeout = dnd_navigation_timeout
        self.poll_interval = poll_interval
        self.transition_settle = transition_settle
        self._monotonic = monotonic
        self._sleep = sleep
        self._verified_hide_all: tuple[bool | None, str] | None = None

    def _settle_transition(self) -> None:
        if self.transition_settle:
            self._sleep(self.transition_settle)

    def _samples(self, read, *, timeout=None):
        return poll_samples(read, timeout=self.timeout if timeout is None else timeout, interval=self.poll_interval,
                            clock=self._monotonic, sleep=self._sleep)

    def _operation(self) -> tuple[dict, dict]:
        operation = self.profile.operations.get("dnd")
        if not isinstance(operation, dict):
            raise SystemUIError("profile does not declare DND")
        strategy = operation.get("strategy")
        if strategy not in {
            "switch_with_hide_all",
            "switch_basic",
            "button_based",
        }:
            raise SystemUIError("profile does not support notification suppression")
        hide = operation.get("hide_notifications")
        if not isinstance(hide, dict):
            raise SystemUIError("dnd.hide_notifications must be an object")
        common_lists = ("direct_intents", "title_labels")
        if strategy == "switch_with_hide_all":
            strategy_lists = ("main_switch_resource_ids",)
        elif strategy == "switch_basic":
            strategy_lists = (
                "title_resource_ids",
                "main_switch_resource_ids",
            )
        else:
            strategy_lists = (
                "enable_button_resource_ids",
                "disable_button_resource_ids",
                "enable_button_labels",
                "disable_button_labels",
            )
        for name in common_lists + strategy_lists:
            values = operation.get(name)
            if not isinstance(values, list) or not values or not all(
                isinstance(value, str) and value for value in values
            ):
                raise SystemUIError(f"dnd.{name} must be a non-empty string list")
        if strategy in {"button_based", "switch_basic"}:
            if hide.get("required") is not False:
                raise SystemUIError(
                    f"{strategy} DND must not require Hide all"
                )
            return operation, hide
        if hide.get("required") is not True:
            raise SystemUIError("profile does not require a verified Hide all")
        for name in ("row_labels", "hide_all_labels"):
            values = hide.get(name)
            if not isinstance(values, list) or not values or not all(
                isinstance(value, str) and value for value in values
            ):
                raise SystemUIError(
                    f"dnd.hide_notifications.{name} "
                    "must be a non-empty string list"
                )
        for name in ("click_resource_suffix", "state_resource_suffix"):
            if not isinstance(hide.get(name), str) or not hide[name]:
                raise SystemUIError(
                    f"dnd.hide_notifications.{name} "
                    "must be a non-empty string"
                )
        return operation, hide

    def _open_button_dnd(
        self,
        enabled: bool,
        phase: str,
    ) -> ET.Element:
        operation, _ = self._operation()
        resource_ids = operation[
            "enable_button_resource_ids"
            if enabled
            else "disable_button_resource_ids"
        ]
        labels = operation[
            "enable_button_labels" if enabled else "disable_button_labels"
        ]
        errors = []
        for attempt, command in enumerate(operation["direct_intents"], start=1):
            self.device.shell(command)
            self._settle_transition()
            try:
                root = self._root()
                buttons = [
                    node
                    for node in root.iter()
                    if node.get("resource-id") in resource_ids
                    and node.get("text") in labels
                    and node.get("clickable") == "true"
                    and node.get("enabled") == "true"
                ]
                if len(buttons) != 1:
                    raise SystemUIError("expected exactly one DND action button")
                owner = buttons[0].get("package")
                titles = [
                    node
                    for node in root.iter()
                    if node.get("package") == owner
                    and (
                        node.get("text") in operation["title_labels"]
                        or node.get("content-desc")
                        in operation["title_labels"]
                    )
                ]
                if len(titles) != 1:
                    raise SystemUIError("expected exactly one DND title")
                selector = self._selector(buttons[0])
            except SystemUIError as error:
                self._record_navigation(
                    "system_ui_dnd_navigation",
                    "failed",
                    phase=phase,
                    attempt=attempt,
                    selector=None,
                    intent=command,
                    intent_attempt=attempt,
                    error=error,
                )
                errors.append(str(error))
                continue
            self._record_navigation(
                "system_ui_dnd_navigation",
                "success",
                phase=phase,
                attempt=attempt,
                selector=selector,
                intent=command,
                intent_attempt=attempt,
            )
            return buttons[0]
        raise SystemUIError("cannot open DND settings: " + "; ".join(errors))

    def _open_basic_dnd_switch(self, phase: str) -> str:
        operation, _ = self._operation()
        errors = []
        for attempt, command in enumerate(operation["direct_intents"], start=1):
            self.device.shell(command)
            self._settle_transition()
            last_error = None

            def ready_selector():
                nonlocal last_error
                try:
                    nodes = list(self._root().iter())
                    titles = [
                        node
                        for node in nodes
                        if node.get("resource-id")
                        in operation["title_resource_ids"]
                        and node.get("text") in operation["title_labels"]
                    ]
                    if len(titles) != 1:
                        raise SystemUIError("expected exactly one DND switch title")
                    title = titles[0]
                    following = nodes[nodes.index(title) + 1 :]
                    switches = [
                        node
                        for node in following
                        if node.get("resource-id")
                        in operation["main_switch_resource_ids"]
                    ]
                    if not switches:
                        raise SystemUIError("DND switch marker is absent")
                    if switches[0].get("checked") not in {"true", "false"}:
                        raise SystemUIError("DND switch state is unreadable")
                    title_selector = " and ".join(
                        (
                            f"@resource-id={self._xpath_literal(title.get('resource-id', ''))}",
                            f"@text={self._xpath_literal(title.get('text', ''))}",
                        )
                    )
                    switch_selector = " or ".join(
                        f"@resource-id={self._xpath_literal(resource_id)}"
                        for resource_id in operation["main_switch_resource_ids"]
                    )
                    return (
                        f"//*[{title_selector}]/following::*"
                        f"[{switch_selector}][1]"
                    )
                except SystemUIError as error:
                    last_error = error
                    return None

            for selector in self._samples(ready_selector, timeout=self.dnd_navigation_timeout):
                if selector is not None:
                    break
            else:
                self._record_navigation(
                    "system_ui_dnd_navigation", "failed", phase=phase,
                    attempt=attempt, selector=None, intent=command,
                    intent_attempt=attempt, error=last_error,
                )
                errors.append(str(last_error))
                continue
            self._record_navigation(
                "system_ui_dnd_navigation",
                "success",
                phase=phase,
                attempt=attempt,
                selector=selector,
                intent=command,
                intent_attempt=attempt,
            )
            return selector
        raise SystemUIError("cannot open DND settings: " + "; ".join(errors))

    def _dnd_enabled(self) -> bool:
        value = self.device.shell("settings get global zen_mode").strip()
        if value not in {"0", "1", "2", "3"}:
            raise SystemUIError("DND state is unreadable")
        return value != "0"

    def _bluetooth_operation(self) -> dict:
        operation = self.profile.operations.get("bluetooth")
        if not isinstance(operation, dict):
            raise SystemUIError("profile does not declare Bluetooth")
        if operation.get("strategy") != "adb_shell":
            raise SystemUIError("profile does not support Bluetooth preparation")
        for name in (
            "state_command",
            "enabled_value",
            "disabled_value",
            "enable_command",
            "disable_command",
            "paired_devices_command",
        ):
            if not isinstance(operation.get(name), str) or not operation[name]:
                raise SystemUIError(
                    f"bluetooth.{name} must be a non-empty string"
                )
        fallback = operation.get("ui_fallback")
        if fallback is not None:
            if not isinstance(fallback, dict):
                raise SystemUIError("bluetooth.ui_fallback must be an object")
            for name in (
                "direct_intents",
                "row_labels",
                "switch_resource_ids",
            ):
                values = fallback.get(name)
                if (
                    not isinstance(values, list)
                    or not values
                    or not all(
                        isinstance(value, str) and value
                        for value in values
                    )
                ):
                    raise SystemUIError(
                        f"bluetooth.ui_fallback.{name} "
                        "must be a non-empty string list"
                    )
        pairing = operation.get("pairing")
        if pairing is not None:
            if not isinstance(pairing, dict):
                raise SystemUIError("bluetooth.pairing must be an object")
            for name in ("direct_intents", "owner_packages", "page_labels", "device_resource_ids"):
                values = pairing.get(name)
                if not isinstance(values, list) or not values or not all(
                    isinstance(value, str) and value for value in values
                ):
                    raise SystemUIError(f"bluetooth.pairing.{name} must be a non-empty string list")
            timeout = pairing.get("timeout_sec")
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout < 0:
                raise SystemUIError("bluetooth.pairing.timeout_sec must be finite and nonnegative")
        unpair = operation.get("unpair")
        if unpair is not None:
            if not isinstance(unpair, dict):
                raise SystemUIError("bluetooth.unpair must be an object")
            for name in ("details_resource_ids", "details_descriptions", "header_resource_ids", "button_labels"):
                values = unpair.get(name)
                if not isinstance(values, list) or not values or not all(isinstance(value, str) and value for value in values):
                    raise SystemUIError(f"bluetooth.unpair.{name} must be a non-empty string list")
            if any("{target}" not in value for value in unpair["details_descriptions"]):
                raise SystemUIError("Bluetooth device settings descriptions must identify the target")
        for spec_name, spec, fields in (
            ("ui_fallback", fallback, ("page_labels", "owner_packages")),
            ("pairing", pairing, ("pairing_direct_intents", "pairing_page_labels", "pair_new_device_labels", "scan_labels")),
            ("unpair", unpair, ("saved_devices_all_labels",)),
        ):
            for name in fields:
                if spec is not None and name in spec:
                    values = spec[name]
                    if not isinstance(values, list) or not values or not all(isinstance(v, str) and v for v in values):
                        raise SystemUIError(f"bluetooth.{spec_name}.{name} must be a non-empty string list")
        if fallback and ("page_labels" in fallback) != ("owner_packages" in fallback):
            raise SystemUIError("Bluetooth page switch requires page labels and owner packages")
        for spec in (pairing, fallback):
            if spec and "page_titles_only" in spec and type(spec["page_titles_only"]) is not bool:
                raise SystemUIError("Bluetooth page_titles_only must be boolean")
        navigation = operation.get("settings_navigation")
        if navigation is not None:
            if not isinstance(navigation, dict) or pairing is None:
                raise SystemUIError("Bluetooth settings navigation requires a pairing profile")
            commands, steps = navigation.get("direct_intents"), navigation.get("steps")
            if (not isinstance(commands, list) or not commands
                    or not all(isinstance(v, str) and v for v in commands)
                    or not isinstance(steps, list) or not steps
                    or not all(isinstance(step, list) and step
                               and all(isinstance(v, str) and v for v in step) for step in steps)):
                raise SystemUIError("Bluetooth settings navigation requires intents and label-list steps")
        if unpair:
            for name in ("details_by_target_row", "header_text_only"):
                if name in unpair and type(unpair[name]) is not bool:
                    raise SystemUIError(f"bluetooth.unpair.{name} must be boolean")
            for name in ("scroll_to_start_swipes", "max_swipes"):
                if name in unpair and (type(unpair[name]) is not int or not 0 <= unpair[name] <= 20):
                    raise SystemUIError(f"bluetooth.unpair.{name} must be an integer from 0 to 20")
            if unpair.get("max_swipes", 0) or unpair.get("scroll_to_start_swipes", 0):
                duration = unpair.get("scroll_duration_sec")
                if type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0:
                    raise SystemUIError("Bluetooth saved-device scrolling requires a positive duration")
        for operation_name, spec in (("pairing", pairing), ("unpair", unpair)):
            confirmation = spec.get("confirmation") if spec is not None else None
            if confirmation is None:
                continue
            if not isinstance(confirmation, dict):
                raise SystemUIError(f"bluetooth.{operation_name}.confirmation must be an object")
            for name in ("dialog_resource_ids", "button_resource_ids", "button_labels", "target_labels"):
                values = confirmation.get(name)
                if not isinstance(values, list) or not values or not all(isinstance(value, str) and value for value in values):
                    raise SystemUIError(f"bluetooth.{operation_name}.confirmation.{name} must be a non-empty string list")
            if any("{target}" not in value for value in confirmation["target_labels"]):
                raise SystemUIError("Bluetooth confirmation labels must identify the target")
        return operation

    def _airplane_operation(self) -> dict:
        operation = self.profile.operations.get("airplane")
        if not isinstance(operation, dict):
            raise SystemUIError("profile does not declare airplane mode")
        if operation.get("strategy") != "adb_shell_candidates":
            raise SystemUIError("profile does not support airplane preparation")
        for name in (
            "state_command",
            "enabled_value",
            "disabled_value",
            "enable_commands",
            "disable_commands",
        ):
            value = operation.get(name)
            if name.endswith("_commands"):
                if (
                    not isinstance(value, list)
                    or not value
                    or not all(
                        isinstance(command, str) and command
                        for command in value
                    )
                ):
                    raise SystemUIError(
                        f"airplane.{name} must be a non-empty string list"
                    )
            elif not isinstance(value, str) or not value:
                raise SystemUIError(
                    f"airplane.{name} must be a non-empty string"
                )
        return operation

    def _wifi_operation(self) -> dict:
        network = self.profile.operations.get("network")
        operation = network.get("wifi") if isinstance(network, dict) else None
        if not isinstance(operation, dict):
            raise SystemUIError("profile does not declare Wi-Fi")
        if operation.get("strategy") != "adb_shell_candidates":
            raise SystemUIError("profile does not support Wi-Fi preparation")
        for name in ("enable_commands", "disable_commands"):
            commands = operation.get(name)
            if (
                not isinstance(commands, list)
                or not commands
                or not all(
                    isinstance(command, str) and command
                    for command in commands
                )
            ):
                raise SystemUIError(
                    f"network.wifi.{name} must be a non-empty string list"
                )
        return operation

    def _airplane_enabled(self) -> bool:
        operation = self._airplane_operation()
        value = self.device.shell(operation["state_command"]).strip()
        if value == operation["enabled_value"]:
            return True
        if value == operation["disabled_value"]:
            return False
        raise SystemUIError("airplane-mode state is unreadable")

    def _wifi_enabled(self) -> bool:
        value = self.device.shell(
            "settings get global wifi_on"
        ).strip()
        # Android WifiSettingsStore: 2 = airplane override, 3 = airplane-disabled.
        if value in {"1", "2"}:
            return True
        if value in {"0", "3"}:
            return False
        raise SystemUIError("Wi-Fi state is unreadable")

    def _wifi_connected(self) -> bool:
        return self._wifi_connection()[0]

    def _wifi_connection(self, *, enabled: bool = True) -> tuple[bool, dict]:
        # Reuse the connectivity read; SSID availability is never a readiness gate.
        output = self.device.shell("cmd wifi status") if enabled else ""
        network = {
            "ssid": None,
            "status": "unavailable" if enabled else "disabled",
            "source": "cmd wifi status" if enabled else "settings get global wifi_on",
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "reason": "wifi_status_unrecognized" if enabled else "wifi_disabled",
        }
        if not enabled:
            return False, network
        value = output.casefold()
        if "not connected" in value or "disconnected" in value:
            network.update(status="not_connected", reason="wifi_not_connected")
            return False, network
        if "connected to" in value:
            names = list(re.finditer(r"^Wifi is connected to ([^\r\n]*)", output, re.MULTILINE | re.IGNORECASE))
            network["reason"] = "multiple_connected_networks" if len(names) > 1 else "ssid_not_reported"
            if len(names) == 1:
                ssid = names[0].group(1)
                following = output[names[0].end():].removeprefix("\r\n").removeprefix("\n")
                # Embedded line breaks must not turn a truncated name into evidence.
                if (re.fullmatch(r'"[^\r\n]*"|[^"\r\n]*', ssid) is None
                        or (following and not following.startswith("WifiInfo:"))):
                    network["reason"] = "ssid_output_ambiguous"
                    return True, network
                if len(ssid) >= 2 and ssid[0] == ssid[-1] == '"':
                    ssid = ssid[1:-1]
                if ssid and ssid.casefold() != "<unknown ssid>":
                    network.update(ssid=ssid, status="observed", reason=None)
            return True, network
        fallback = self.device.shell("dumpsys connectivity")
        match = re.search(
            r"type:\s*WIFI\[\][^\n]*state:\s*([A-Z_]+)/([A-Z_]+)",
            fallback,
            re.IGNORECASE,
        )
        if match is None:
            return False, network
        return all(state.casefold() == "connected" for state in match.groups()), network

    def _bluetooth_enabled(self) -> bool:
        operation = self._bluetooth_operation()
        value = self.device.shell(operation["state_command"]).strip()
        if value == operation["enabled_value"]:
            return True
        if value == operation["disabled_value"]:
            return False
        raise SystemUIError("Bluetooth state is unreadable")

    def _bluetooth_target_paired(self, target_name: str) -> bool:
        operation = self._bluetooth_operation()
        for dump in self._samples(lambda: self.device.shell(operation["paired_devices_command"])):
            _, bonded_marker, remainder = dump.partition("Bonded devices:")
            # Android versions use different section boundaries after bonded devices.
            sections = re.split(r"(?m)^[ \t]*(?:Devices in DB:|Scan Mode Changes:|[ms]SnoopLogSettingAtEnable[ \t]*=)", remainder, maxsplit=1)
            if bonded_marker and len(sections) == 2:
                bonded = sections[0]
                break
        else:
            raise SystemUIError("Bluetooth paired-device list is unreadable")
        # Samsung may prefix connected rows and map a private address to the radio.
        mac = r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}"
        row = re.compile(
            rf"\s*(?:\(Connected\)\s+)?{mac}(?:\s+=>\s+{mac})?\s+\[(?P<transport>[^\]\r\n]+)\]\s+"
            + re.escape(normalize_bluetooth_label(target_name)) + r"(?:\s+\([^\r\n()]*\))?\s*",
            re.IGNORECASE,
        )
        matches = [match for line in bonded.splitlines() if (match := row.fullmatch(normalize_bluetooth_label(line)))]
        # A companion LE bond does not make the named Classic file-transfer receiver ambiguous.
        if any(match["transport"].strip().upper() in {"BR/EDR", "DUAL"} for match in matches):
            matches = [match for match in matches if match["transport"].strip().upper() != "LE"]
        if len(matches) > 1:
            raise SystemUIError(f"Bluetooth paired target name is ambiguous: {target_name}")
        return bool(matches)

    def _root(self) -> ET.Element:
        try:
            return ET.fromstring(self.device.hierarchy())
        except ET.ParseError as error:
            raise SystemUIError("System UI hierarchy is malformed") from error

    @staticmethod
    def _selector(node: ET.Element) -> dict[str, str]:
        selector = {}
        if node.get("resource-id"):
            selector["resourceId"] = node.get("resource-id", "")
        if node.get("text"):
            selector["text"] = node.get("text", "")
        if not selector:
            raise SystemUIError("semantic selector is empty")
        return selector

    @staticmethod
    def _xpath_literal(value: str) -> str:
        if '"' not in value:
            return f'"{value}"'
        if "'" not in value:
            return f"'{value}'"
        return "concat(" + ", '\"', ".join(
            f'"{part}"' for part in value.split('"')
        ) + ")"

    @classmethod
    def _clickable_ancestor_xpath(cls, node: ET.Element) -> str:
        attributes = [
            f"@{name}={cls._xpath_literal(value)}"
            for name in ("resource-id", "text")
            if (value := node.get(name))
        ]
        if not attributes:
            raise SystemUIError("semantic XPath selector is empty")
        return (
            f"//*[{ ' and '.join(attributes) }]"
            '/ancestor::*[@clickable="true"][1]'
        )

    @staticmethod
    def _matching_nodes(
        root: ET.Element,
        labels: list[str],
        *,
        resource_suffix: str | None = None,
    ) -> list[ET.Element]:
        for label in labels:
            matches = [
                node
                for node in root.iter()
                if node.get("text") == label
                and (
                    resource_suffix is None
                    or node.get("resource-id", "").endswith(resource_suffix)
                )
            ]
            if len(matches) > 1:
                matches = [
                    node for node in matches if node.get("resource-id")
                ]
                if len(matches) != 1:
                    raise SystemUIError(
                        f"expected exactly one {label!r} target"
                    )
            if matches:
                return matches
        return []

    def _unique_label_node(
        self,
        root: ET.Element,
        labels: list[str],
        *,
        resource_suffix: str | None = None,
    ) -> ET.Element:
        matches = self._matching_nodes(
            root,
            labels,
            resource_suffix=resource_suffix,
        )
        if not matches:
            raise SystemUIError(f"expected exactly one of {labels!r}")
        return matches[0]

    @staticmethod
    def _clickable_label_nodes(
        root: ET.Element,
        labels: list[str],
    ) -> list[ET.Element]:
        parents = {child: parent for parent in root.iter() for child in parent}
        for label in labels:
            matches = []
            for node in root.iter():
                if node.get("text") != label:
                    continue
                parent = parents.get(node)
                while parent is not None:
                    if parent.get("clickable") == "true":
                        matches.append(node)
                        break
                    parent = parents.get(parent)
            if len(matches) > 1:
                raise SystemUIError(
                    f"expected exactly one clickable {label!r} target"
                )
            if matches:
                return matches
        return []

    def _wait_for_label(
        self,
        labels: list[str],
        *,
        resource_suffix: str | None = None,
    ) -> ET.Element:
        for matches in self._samples(lambda: self._matching_nodes(
                self._root(), labels, resource_suffix=resource_suffix)):
            if matches:
                return matches[0]
        raise _WaitTimeout(f"expected exactly one of {labels!r}")

    def _wait_for_clickable_label(
        self,
        labels: list[str],
    ) -> ET.Element:
        for matches in self._samples(lambda: self._clickable_label_nodes(self._root(), labels)):
            if matches:
                return matches[0]
        raise _WaitTimeout(f"expected exactly one clickable row of {labels!r}")

    def _record_navigation(
        self,
        action: str,
        status: str,
        *,
        phase: str,
        attempt: int,
        selector: dict[str, str] | str | None,
        intent: str | None = None,
        intent_attempt: int | None = None,
        error: Exception | None = None,
    ) -> None:
        operation, _ = self._operation()
        details = {
            "phase": phase,
            "attempt": attempt,
            "selector": selector,
            "profile": self.profile.profile_id,
            "strategy": operation["strategy"],
        }
        if intent is not None:
            details["intent"] = intent
        if intent_attempt is not None:
            details["intent_attempt"] = intent_attempt
        if error is not None:
            details["error_type"] = type(error).__name__
            details["error"] = str(error)
        self.journal.record_action(
            action,
            status=status,
            details=details,
        )

    def _open_dnd(
        self,
        phase: str = "prepare",
        *,
        navigation_attempt: int = 1,
    ) -> ET.Element:
        operation, hide = self._operation()
        errors = []
        for intent_attempt, command in enumerate(
            operation["direct_intents"],
            start=1,
        ):
            self.device.shell(command)
            self._settle_transition()
            selector = None
            try:
                root = self._root()
                if self._matching_nodes(
                    root,
                    hide["hide_all_labels"],
                    resource_suffix=hide["click_resource_suffix"],
                ):
                    self.device.back()
                    self._settle_transition()
                    root = self._root()
                for top_attempt in range(6):
                    matches = self._clickable_label_nodes(
                        root,
                        operation["title_labels"],
                    )
                    if matches:
                        break
                    if top_attempt == 5:
                        raise SystemUIError("DND title is absent")
                    self.device.swipe("down")
                    self._settle_transition()
                    root = self._root()
                title = matches[0]
                selector = self._selector(title)
                switch_present = any(
                    node.get("resource-id")
                    in operation["main_switch_resource_ids"]
                    for node in root.iter()
                )
                if not switch_present:
                    raise SystemUIError("main DND switch marker is absent")
            except SystemUIError as error:
                self._record_navigation(
                    "system_ui_dnd_navigation",
                    "failed",
                    phase=phase,
                    attempt=navigation_attempt,
                    selector=selector,
                    intent=command,
                    intent_attempt=intent_attempt,
                    error=error,
                )
                errors.append(str(error))
                continue
            self._record_navigation(
                "system_ui_dnd_navigation",
                "success",
                phase=phase,
                attempt=navigation_attempt,
                selector=selector,
                intent=command,
                intent_attempt=intent_attempt,
            )
            return title
        raise SystemUIError("cannot open DND settings: " + "; ".join(errors))

    def _open_hide_notifications(self, phase: str = "prepare") -> None:
        operation, hide = self._operation()
        errors = []
        for intent_attempt, command in enumerate(
            [None, *operation["direct_intents"]],
        ):
            if command is not None:
                self.device.shell(command)
                self._settle_transition()
            for attempt in range(6):
                try:
                    root = self._root()
                    current_target = self._matching_nodes(
                        root,
                        hide["hide_all_labels"],
                        resource_suffix=hide["click_resource_suffix"],
                    )
                    if current_target and (
                        current_target[0].get("resource-id", "").startswith(
                            "com.android.settings:"
                        )
                        and current_target[0].get("package", "com.android.settings")
                        == "com.android.settings"
                    ):
                        self._record_navigation(
                            "system_ui_hide_notifications_navigation",
                            "already_visible",
                            phase=phase,
                            attempt=intent_attempt,
                            selector=self._selector(current_target[0]),
                            intent=command,
                            intent_attempt=intent_attempt if command else None,
                        )
                        return
                    if command is None:
                        break
                    matches = self._matching_nodes(
                        root,
                        hide["row_labels"],
                    )
                except Exception as error:
                    self._record_navigation(
                        "system_ui_hide_notifications_navigation",
                        "failed",
                        phase=phase,
                        attempt=intent_attempt,
                        selector=None,
                        intent=command,
                        intent_attempt=intent_attempt,
                        error=error,
                    )
                    raise
                if matches:
                    selector = self._clickable_ancestor_xpath(
                        matches[0]
                    )
                    try:
                        self.device.click_xpath(selector)
                        self._settle_transition()
                        self._wait_for_label(
                            hide["hide_all_labels"],
                            resource_suffix=hide["click_resource_suffix"],
                        )
                    except Exception as error:
                        self._record_navigation(
                            "system_ui_hide_notifications_navigation",
                            "failed",
                            phase=phase,
                            attempt=intent_attempt,
                            selector=selector,
                            intent=command,
                            intent_attempt=intent_attempt,
                            error=error,
                        )
                        if not isinstance(error, _WaitTimeout):
                            raise
                        errors.append(str(error))
                        break
                    self._record_navigation(
                        "system_ui_hide_notifications_navigation",
                        "success",
                        phase=phase,
                        attempt=intent_attempt,
                        selector=selector,
                        intent=command,
                        intent_attempt=intent_attempt,
                    )
                    return
                if attempt < 5:
                    self.device.swipe("up")
                    self._settle_transition()
            else:
                error = SystemUIError(
                    "Hide notifications is not reachable"
                )
                self._record_navigation(
                    "system_ui_hide_notifications_navigation",
                    "failed",
                    phase=phase,
                    attempt=intent_attempt,
                    selector=None,
                    intent=command,
                    intent_attempt=intent_attempt,
                    error=error,
                )
                errors.append(str(error))
        raise SystemUIError(
            "cannot open Hide notifications: " + "; ".join(errors)
        )

    def _hide_all_enabled(self) -> bool:
        _, hide = self._operation()
        root = self._root()
        label = self._unique_label_node(
            root,
            hide["hide_all_labels"],
            resource_suffix=hide["click_resource_suffix"],
        )
        parents = {child: parent for parent in root.iter() for child in parent}
        container = parents.get(label)
        while container is not None:
            switches = [
                node
                for node in container.iter()
                if node.get("resource-id", "").endswith(
                    hide["state_resource_suffix"]
                )
            ]
            if len(switches) == 1:
                checked = switches[0].get("checked")
                if checked not in {"true", "false"}:
                    raise SystemUIError("Hide all state is unreadable")
                return checked == "true"
            container = parents.get(container)
        raise SystemUIError("expected exactly one switch related to Hide all")

    def _wait_for(
        self,
        read: Callable[[], bool],
        target: bool,
        name: str,
        *,
        timeout: float | None = None,
    ) -> bool:
        for observed in self._samples(read, timeout=timeout):
            if observed is target:
                return observed
        raise SystemUIError(f"{name} did not become {target}")

    def _record(
        self, action: str, status: str, *, before: bool, target: bool,
        after: bool | None, phase: str, error: Exception | None = None,
    ) -> None:
        operation, _ = self._operation()
        self._record_environment(action, status, before=before, target=target,
                                 after=after, phase=phase, strategy=operation["strategy"], error=error)

    def _set_dnd(self, enabled: bool, action: str, phase: str) -> None:
        operation, _ = self._operation()

        def change():
            if operation["strategy"] == "button_based":
                button = self._open_button_dnd(enabled, phase)
                self.device.click(self._selector(button))
            elif operation["strategy"] == "switch_basic":
                self.device.click_xpath(self._open_basic_dnd_switch(phase))
            else:
                title = self._open_dnd(phase)
                self.device.click_xpath(self._clickable_ancestor_xpath(title))
            self._settle_transition()

        apply_state(self._dnd_enabled, enabled, change,
                    partial(self._wait_for, self._dnd_enabled, enabled, "DND"),
                    partial(self._record, action, phase=phase))

    def _set_hide_all(self, enabled: bool, phase: str) -> None:
        def change():
            _, hide = self._operation()
            label = self._unique_label_node(
                self._root(), hide["hide_all_labels"],
                resource_suffix=hide["click_resource_suffix"],
            )
            self.device.click_xpath(self._clickable_ancestor_xpath(label))
            self._settle_transition()

        apply_state(self._hide_all_enabled, enabled, change,
                    partial(self._wait_for, self._hide_all_enabled, enabled, "Hide all"),
                    partial(self._record, "system_ui_hide_all", phase=phase))

    def snapshot_notification_suppression(self) -> DndSnapshot:
        initial_dnd = self._dnd_enabled()
        operation, _ = self._operation()
        if operation["strategy"] in {"button_based", "switch_basic"}:
            return DndSnapshot(initial_dnd, None)
        self._open_hide_notifications("prepare")
        initial_hide_all = self._hide_all_enabled()
        return DndSnapshot(initial_dnd, initial_hide_all)

    def prepare_notification_suppression(
        self,
        snapshot: DndSnapshot | None = None,
    ) -> DndSnapshot:
        snapshot = snapshot or self.snapshot_notification_suppression()
        operation, _ = self._operation()
        try:
            self._set_dnd(
                True,
                "system_ui_dnd_prepare",
                "prepare",
            )
            if operation["strategy"] == "switch_with_hide_all":
                self._open_hide_notifications("prepare")
                self._set_hide_all(True, "prepare")
        except Exception as error:
            raise SystemUIError(
                f"notification suppression failed: {error}"
            ) from error
        return snapshot

    def _record_environment(
        self,
        action: str,
        status: str,
        *,
        before: bool,
        target: bool,
        after: bool | None,
        phase: str,
        strategy: str,
        error: Exception | None = None,
    ) -> None:
        details = {
            "before": before,
            "target": target,
            "after": after,
            "profile": self.profile.profile_id,
            "strategy": strategy,
            "phase": phase,
        }
        if error is not None:
            details["error_type"] = type(error).__name__
            details["error"] = str(error)
        self.journal.record_action(action, status=status, details=details)

    def _set_shell_state(self, enabled, phase, *, operation, read, name, action):
        def change():
            errors = []
            for command in operation["enable_commands" if enabled else "disable_commands"]:
                self.device.shell(command)
                try:
                    self._wait_for(read, enabled, name)
                    break
                except SystemUIError as error:
                    errors.append(str(error))
            else:
                raise SystemUIError("; ".join(errors))

        return apply_state(read, enabled, change, read,
                           partial(self._record_environment, action, phase=phase,
                                   strategy=operation["strategy"]))

    def _set_airplane(self, enabled: bool, phase: str) -> None:
        self._set_shell_state(enabled, phase, operation=self._airplane_operation(),
                              read=self._airplane_enabled, name="airplane mode",
                              action=f"system_ui_airplane_{phase}")

    def _set_wifi(self, enabled: bool, phase: str) -> bool:
        return self._set_shell_state(enabled, phase, operation=self._wifi_operation(),
                                     read=self._wifi_enabled, name="Wi-Fi",
                                     action=f"system_ui_wifi_{phase}")

    def restore_wifi(self, enabled: bool) -> bool:
        return self._set_wifi(enabled, "restore")

    def snapshot_environment(self) -> EnvironmentSnapshot:
        notification = self.snapshot_notification_suppression()
        wifi_enabled = self._wifi_enabled()
        airplane_enabled = self._airplane_enabled()
        wifi_connected, wifi_network = self._wifi_connection(enabled=wifi_enabled)
        return EnvironmentSnapshot(
            dnd_enabled=notification.enabled,
            hide_all=notification.hide_all,
            airplane_enabled=airplane_enabled,
            wifi_enabled=wifi_enabled,
            wifi_connected=wifi_connected,
            wifi_network=wifi_network,
        )

    def prepare_environment(
        self,
        snapshot: EnvironmentSnapshot,
    ) -> None:
        operation, _ = self._operation()
        if (
            operation["strategy"] == "switch_with_hide_all"
            and snapshot.hide_all is None
        ):
            raise SystemUIError(
                "notification-suppression snapshot is incomplete"
            )
        self._set_airplane(True, "prepare")
        self._set_wifi(False, "prepare")
        self.prepare_notification_suppression(
            DndSnapshot(
                snapshot.dnd_enabled,
                snapshot.hide_all,
            )
        )

    def verify_prepared_environment(
        self, acquisition_environment: str | None = "device_only", *, reuse_hide_all: bool = False
    ) -> EnvironmentSnapshot:
        if acquisition_environment not in {None, "device_only", "controlled_online"}:
            raise SystemUIError("unknown acquisition environment")
        previous = self._verified_hide_all
        self._verified_hide_all = None
        if reuse_hide_all:
            if previous is None:
                raise SystemUIError("Hide all reuse requires a successful prior environment verification")
            wifi_enabled = self._wifi_enabled()
            dnd_enabled = self._dnd_enabled()
            airplane_enabled = self._airplane_enabled()
            wifi_connected, wifi_network = self._wifi_connection(enabled=wifi_enabled)
            observed = EnvironmentSnapshot(
                dnd_enabled=dnd_enabled, hide_all=previous[0],
                airplane_enabled=airplane_enabled, wifi_enabled=wifi_enabled,
                wifi_connected=wifi_connected, wifi_network=wifi_network,
            )
        else:
            observed = self.snapshot_environment()
        ready = (
            observed.dnd_enabled and observed.hide_all is not False
            and observed.airplane_enabled
            and (acquisition_environment is None or observed.wifi_enabled == (acquisition_environment == "controlled_online"))
            and (acquisition_environment != "controlled_online" or observed.wifi_connected)
        )
        action = self.journal.record_action(
            "system_ui_environment_verify",
            status="success" if ready else "failed",
            details={
                **asdict(observed),
                "acquisition_environment": acquisition_environment,
                "profile": self.profile.profile_id,
                "hide_all_reobserved": not reuse_hide_all and observed.hide_all is not None,
                **({"hide_all_source_action_id": previous[1]} if reuse_hide_all else {}),
            },
        )
        if not ready:
            raise SystemUIError("prepared device environment has drifted")
        self._verified_hide_all = previous if reuse_hide_all else (observed.hide_all, action.action_id)
        return observed

    def apply_acquisition_environment(self, name: str) -> None:
        if name not in {"device_only", "controlled_online"}:
            raise SystemUIError(
                f"unknown acquisition environment: {name!r}"
            )
        target = name == "controlled_online"
        self._set_wifi(target, "condition")
        if target:
            details = {"acquisition_environment": name,
                       "timeout_seconds": self.wifi_connection_timeout, "observed": {}}

            def connected():
                value, network = self._wifi_connection()
                details["observed"] = {"wifi_connected": value, "wifi_network": network}
                return value

            try:
                self._wait_for(
                    connected,
                    True,
                    "Wi-Fi connection",
                    timeout=self.wifi_connection_timeout,
                )
            except SystemUIError as error:
                self.journal.record_action(
                    "system_ui_wifi_connection_verify",
                    status="failed",
                    details=details,
                )
                raise SystemUIError("Wi-Fi is not connected") from error
            self.journal.record_action(
                "system_ui_wifi_connection_verify",
                status="success",
                details=details,
            )

    def restore_environment(
        self,
        snapshot: EnvironmentSnapshot,
    ) -> None:
        operation, _ = self._operation()
        failures = []
        try:
            self.restore_wifi(snapshot.wifi_enabled)
        except Exception as error:
            failures.append(f"Wi-Fi: {error}")
        try:
            self._set_airplane(
                snapshot.airplane_enabled,
                "restore",
            )
        except Exception as error:
            failures.append(f"airplane mode: {error}")
        if (
            operation["strategy"] == "switch_with_hide_all"
            and snapshot.hide_all is None
        ):
            failures.append(
                "notification suppression: snapshot is incomplete"
            )
        else:
            try:
                self.restore_notification_suppression(
                    DndSnapshot(
                        snapshot.dnd_enabled,
                        snapshot.hide_all,
                    )
                )
            except Exception as error:
                failures.append(f"notification suppression: {error}")
        if failures:
            raise SystemUIError("; ".join(failures))

    def restore_notification_suppression(
        self,
        snapshot: DndSnapshot,
        *,
        phase: str = "restore",
    ) -> None:
        operation, _ = self._operation()
        failures = []
        if operation["strategy"] == "switch_with_hide_all":
            if snapshot.hide_all is None:
                failures.append("Hide all: snapshot is incomplete")
            else:
                try:
                    self._open_hide_notifications(phase)
                    self._set_hide_all(snapshot.hide_all, phase)
                except Exception as error:
                    failures.append(f"Hide all: {error}")
        try:
            self._set_dnd(
                snapshot.enabled,
                "system_ui_dnd_restore",
                phase,
            )
        except Exception as error:
            failures.append(f"DND: {error}")
        if failures:
            raise SystemUIError("; ".join(failures))

    def _record_bluetooth(
        self, action: str, status: str, *, before: bool, target: bool,
        after: bool | None, phase: str, error: Exception | None = None,
    ) -> None:
        operation = self._bluetooth_operation()
        self._record_environment(action, status, before=before, target=target,
                                 after=after, phase=phase, strategy=operation["strategy"], error=error)

    @staticmethod
    def _bluetooth_page_matches(root: ET.Element, spec: dict) -> bool:
        parents = {child: parent for parent in root.iter() for child in parent}
        for node in root.iter():
            if (node.get("package") not in spec["owner_packages"]
                    or node.get("text") not in spec["page_labels"]
                    or node.get("visible-to-user") == "false"):
                continue
            if spec.get("page_titles_only"):
                ancestor = node
                while ancestor is not None and ancestor.get("clickable") != "true":
                    ancestor = parents.get(ancestor)
                if ancestor is not None:
                    continue
            return True
        return False

    def _bluetooth_settings_routes(self, commands: list[str]):
        navigation = self._bluetooth_operation().get("settings_navigation", {})
        return [(command, navigation.get("steps", []))
                for command in commands + navigation.get("direct_intents", [])]

    def _follow_bluetooth_settings_route(self, command: str, steps: list, target_name: str) -> None:
        self.device.shell(command)
        self._settle_transition()
        if steps:
            pairing = self._bluetooth_operation()["pairing"]
            for labels in steps:
                self._click_bluetooth_label(labels, pairing, "system_ui_bluetooth_settings_step",
                                            target_name, clickable_only=True)

    def _set_bluetooth_with_ui(self, enabled: bool, phase: str) -> None:
        operation = self._bluetooth_operation()
        fallback = operation.get("ui_fallback")
        if not isinstance(fallback, dict):
            raise SystemUIError("Bluetooth UI fallback is unavailable")
        errors = []
        for command, steps in self._bluetooth_settings_routes(fallback["direct_intents"]):
            try:
                self._follow_bluetooth_settings_route(command, steps, "")
                root = self._root()
                if fallback.get("page_labels"):
                    if not self._bluetooth_page_matches(root, fallback):
                        raise SystemUIError("Bluetooth power page does not match")
                    container = root
                else:
                    label = self._unique_label_node(root, fallback["row_labels"])
                    parents = {child: parent for parent in root.iter() for child in parent}
                    container = parents.get(label)
                    while container is not None and container.get("clickable") != "true":
                        container = parents.get(container)
                    if container is None:
                        raise SystemUIError("Bluetooth row is not clickable")
                switches = [
                    node
                    for node in container.iter()
                    if node.get("resource-id")
                    in fallback["switch_resource_ids"]
                    and (not fallback.get("page_labels") or node.get("package") in fallback["owner_packages"])
                    and node.get("enabled") != "false" and node.get("visible-to-user") != "false"
                ]
                if len(switches) != 1:
                    raise SystemUIError(
                        "expected exactly one switch related to Bluetooth"
                    )
                checked = switches[0].get("checked")
                if checked not in {"true", "false"}:
                    raise SystemUIError("Bluetooth UI state is unreadable")
                if (checked == "true") is not enabled:
                    resource_predicate = " or ".join(
                        f"@resource-id={self._xpath_literal(resource_id)}"
                        for resource_id in fallback["switch_resource_ids"]
                    )
                    if fallback.get("page_labels"):
                        self.device.click({"packageName": switches[0].get("package"), **self._selector(switches[0])})
                    else:
                        self.device.click_xpath(self._clickable_ancestor_xpath(label) + f"//*[{resource_predicate}]")
                    self._settle_transition()
                self._wait_for(self._bluetooth_enabled, enabled, "Bluetooth")
            except Exception as error:
                errors.append(str(error))
                continue
            self.journal.record_action(
                "system_ui_bluetooth_ui_fallback",
                status="success",
                details={
                    "target": enabled,
                    "phase": phase,
                    "intent": command,
                    "profile": self.profile.profile_id,
                },
            )
            return
        error = SystemUIError(
            "Bluetooth UI fallback failed: " + "; ".join(errors)
        )
        self.journal.record_action(
            "system_ui_bluetooth_ui_fallback",
            status="failed",
            details={
                "target": enabled,
                "phase": phase,
                "profile": self.profile.profile_id,
                "error": str(error),
            },
        )
        raise error

    def _set_bluetooth(self, enabled: bool, phase: str) -> None:
        operation = self._bluetooth_operation()

        def change():
            command = operation["enable_command"] if enabled else operation["disable_command"]
            try:
                self.device.shell(command)
                self._wait_for(self._bluetooth_enabled, enabled, "Bluetooth")
            except Exception as command_error:
                try:
                    self._set_bluetooth_with_ui(enabled, phase)
                except SystemUIError as fallback_error:
                    raise SystemUIError(f"{command_error}; {fallback_error}") from command_error

        apply_state(self._bluetooth_enabled, enabled, change, self._bluetooth_enabled,
                    partial(self._record_bluetooth, f"system_ui_bluetooth_{phase}", phase=phase))

    def _confirm_bluetooth_dialog(self, target_name: str, pairing: dict, confirmation: dict | None, action: str) -> bool:
        if confirmation is None:
            return False
        target_labels = {
            normalize_bluetooth_label(label.replace("{target}", target_name)).casefold()
            for label in confirmation["target_labels"]
        }
        root = self._root()
        buttons = set()
        for panel in root.iter():
            if (panel.get("package") not in pairing["owner_packages"]
                    or panel.get("resource-id") not in confirmation["dialog_resource_ids"]
                    or panel.get("visible-to-user") == "false"):
                continue
            texts = {
                normalize_bluetooth_label(node.get("text", "")).casefold()
                for node in panel.iter()
                if node.get("package") in pairing["owner_packages"]
                and node.get("visible-to-user") != "false"
            }
            if not any(
                text == label or (label.endswith("?") and text.startswith(label + " "))
                for text in texts for label in target_labels
            ):
                continue
            buttons.update(
                node for node in panel.iter()
                if node.get("package") in pairing["owner_packages"]
                and node.get("resource-id") in confirmation["button_resource_ids"]
                and node.get("text", "").strip() in confirmation["button_labels"]
                and node.get("clickable") == "true"
                and node.get("enabled") != "false"
                and node.get("visible-to-user") != "false"
            )
        if len(buttons) > 1:
            raise SystemUIError("Bluetooth confirmation button is ambiguous")
        if not buttons:
            return False
        button = next(iter(buttons))
        selector = {"packageName": button.get("package"), **self._selector(button)}
        if sum(
            node.get("package") == button.get("package")
            and node.get("resource-id") == button.get("resource-id")
            and node.get("text") == button.get("text")
            for node in root.iter()
        ) != 1:
            raise SystemUIError("Bluetooth confirmation selector is ambiguous")
        self.device.click(selector)
        self.journal.record_action(action, status="success", details={
            "target_name": target_name, "selector": selector, "side": "android",
        })
        return True

    def _click_bluetooth_label(self, labels: list[str], pairing: dict, action: str, target_name: str,
                               *, clickable_only: bool = False) -> bool:
        if not labels:
            return False
        root = self._root()
        matches = self._matching_nodes(root, labels)
        if not matches:
            return False
        node = matches[0]
        if (node.get("package") not in pairing["owner_packages"] or node.get("enabled") == "false"
                or node.get("visible-to-user") == "false"):
            return False
        if clickable_only and node.get("clickable") != "true":
            if node not in self._clickable_label_nodes(root, [node.get("text", "")]):
                return False
        selector = {"packageName": node.get("package"), **self._selector(node)}
        self.device.click(selector)
        self.journal.record_action(action, status="success", details={"target_name": target_name, "selector": selector})
        self._settle_transition()
        return True

    def _open_bluetooth_settings(self, target_name: str, pairing: dict, *, for_pairing: bool = False) -> None:
        commands = (pairing.get("pairing_direct_intents", []) if for_pairing else []) + pairing["direct_intents"]
        for command, steps in self._bluetooth_settings_routes(commands):
            try:
                self._follow_bluetooth_settings_route(command, steps, target_name)
                self._wait_for(lambda: self._bluetooth_page_matches(self._root(), pairing),
                               True, "Bluetooth Settings screen")
                if for_pairing and pairing.get("pairing_page_labels"):
                    self._click_bluetooth_label(pairing.get("pair_new_device_labels", []), pairing,
                                                "system_ui_bluetooth_pairing_page", target_name)
                    self._wait_for(lambda: any(
                        node.get("package") in pairing["owner_packages"]
                        and node.get("text") in pairing["pairing_page_labels"]
                        for node in self._root().iter()), True, "Bluetooth pairing page")
            except Exception as error:
                self.journal.record_action("system_ui_bluetooth_pairing_navigation", status="failed", details={"intent": command, "error": str(error)})
                continue
            self.journal.record_action("system_ui_bluetooth_pairing_navigation", status="success", details={"intent": command, "target_name": target_name})
            break
        else:
            raise SystemUIError("Bluetooth pairing settings could not be opened")

    def _pair_bluetooth_target(self, target_name: str) -> None:
        pairing = self._bluetooth_operation().get("pairing")
        if pairing is None:
            raise SystemUIError("Bluetooth pairing is not declared by the System UI Profile")
        started = self._monotonic()
        details = {
            "target_name": target_name, "profile": self.profile.profile_id,
            "timeout_sec": pairing["timeout_sec"],
            "operator_confirmation_required": True,
            "android_confirmation": "automatic_when_dialog_matches" if pairing.get("confirmation") else "operator_required",
            "windows_confirmation": "operator_required",
            "timing_clock": "monotonic", "timing_scope": "bluetooth_pairing_preparation",
        }
        self.journal.record_action("system_ui_bluetooth_pairing", status="started", details=details)
        phone_instruction = (
            "Pairing on the phone is approved automatically when the confirmation dialog matches the profile. "
            if pairing.get("confirmation") else "Please also approve pairing on the phone. "
        )
        print(
            f"AURA: Preparing Bluetooth pairing with {target_name}. "
            "Check the displayed code and approve pairing on the PC. "
            + phone_instruction +
            f"Waiting up to {pairing['timeout_sec']:g} seconds. Press Ctrl+C to cancel.",
            file=sys.stderr, flush=True,
        )
        try:
            host = prepare_windows_bluetooth_pairing()
            self.journal.record_action("bluetooth_host_pairing_prepare", status="success" if host["ok"] else "failed", details=host)
            if not host["ok"]:
                raise SystemUIError(f"Bluetooth host preparation failed: {host['reason']}")
            self._open_bluetooth_settings(target_name, pairing, for_pairing=True)
            requested = False
            phone_confirmed = False
            scan_requested = False
            expected_name = normalize_bluetooth_label(target_name).casefold()
            while True:
                if self._bluetooth_target_paired(target_name):
                    if not self._bluetooth_enabled():
                        raise SystemUIError("Bluetooth turned off during pairing")
                    break
                if not requested:
                    matches = [
                        node for node in self._root().iter()
                        if node.get("package") in pairing["owner_packages"]
                        and node.get("resource-id") in pairing["device_resource_ids"]
                        and normalize_bluetooth_label(node.get("text", "")).casefold() == expected_name
                    ]
                    if matches:
                        # ponytail: PoC uses UI order; use receiver identity if names must distinguish hosts.
                        selector = f"({self._clickable_ancestor_xpath(matches[0])})[1]"
                        self.device.click_xpath(selector)
                        requested = True
                        self.journal.record_action("system_ui_bluetooth_pair_request", status="success", details={
                            "target_name": target_name, "selector": selector,
                            "candidate_count": len(matches), "selected_index": 0,
                            "selected_node": dict(matches[0].attrib),
                        })
                    elif not scan_requested:
                        scan_requested = self._click_bluetooth_label(pairing.get("scan_labels", []), pairing,
                                                                    "system_ui_bluetooth_scan", target_name)
                if requested and not phone_confirmed:
                    phone_confirmed = self._confirm_bluetooth_dialog(target_name, pairing, pairing.get("confirmation"), "system_ui_bluetooth_pair_confirm")
                if self._monotonic() - started >= pairing["timeout_sec"]:
                    raise SystemUIError(f"Bluetooth pairing was not confirmed before timeout: {target_name}")
                self._sleep(self.poll_interval)
        except (Exception, KeyboardInterrupt) as error:
            self.journal.record_action("system_ui_bluetooth_pairing", status="failed", details={
                **details, "error": str(error) or type(error).__name__,
                "elapsed_seconds": self._monotonic() - started,
            })
            raise
        self.journal.record_action("system_ui_bluetooth_pairing", status="success", details={
            **details, "paired": True, "confirmation_source": "bonded_device_list",
            "elapsed_seconds": self._monotonic() - started,
        })

    def prepare_bluetooth_delivery(
        self,
        target_name: str,
    ) -> BluetoothSnapshot:
        snapshot = BluetoothSnapshot(self._bluetooth_enabled())
        try:
            self._set_bluetooth(True, "prepare")
            self._settle_transition()
            paired = self._bluetooth_target_paired(target_name)
            snapshot = BluetoothSnapshot(snapshot.enabled, target_name, paired)
            self.journal.record_action(
                "system_ui_bluetooth_target_check",
                status="success" if paired else "not_paired",
                details={
                    "target_name": target_name,
                    "paired": paired,
                    "profile": self.profile.profile_id,
                    "strategy": self._bluetooth_operation()["strategy"],
                },
            )
            if not paired:
                self._pair_bluetooth_target(target_name)
            self._settle_transition()
            if not self._bluetooth_enabled() or not self._bluetooth_target_paired(target_name):
                raise SystemUIError("Bluetooth delivery state is not stable")
            self.journal.record_action(
                "system_ui_bluetooth_delivery_ready", status="success",
                details={"target_name": target_name, "paired": True,
                         "settle_seconds_per_check": self.transition_settle},
            )
        except (Exception, KeyboardInterrupt) as error:
            try:
                self.restore_bluetooth_delivery(snapshot, phase="rollback")
            except Exception as rollback_error:
                raise SystemUIError(
                    f"Bluetooth preparation failed: {error}; "
                    f"rollback failed: {rollback_error}"
                ) from error
            if isinstance(error, KeyboardInterrupt):
                raise
            raise SystemUIError(
                f"Bluetooth preparation failed: {error}"
            ) from error
        return snapshot

    def restore_bluetooth_delivery(
        self,
        snapshot: BluetoothSnapshot,
        *,
        phase: str = "restore",
    ) -> None:
        try:
            if snapshot.paired_before is False and snapshot.target_name:
                self._set_bluetooth(True, "cleanup_check")
                if self._bluetooth_target_paired(snapshot.target_name):
                    self._unpair_bluetooth_target(snapshot.target_name)
        finally:
            self._set_bluetooth(snapshot.enabled, phase)

    def _bluetooth_device_settings_selectors(self, target_name: str, pairing: dict, unpair: dict) -> list:
        root = self._root()
        descriptions = {normalize_bluetooth_label(label.replace("{target}", target_name)).casefold()
                        for label in unpair["details_descriptions"]}
        def is_button(node):
            return (node.get("package") in pairing["owner_packages"]
                    and node.get("resource-id") in unpair["details_resource_ids"]
                    and node.get("clickable") == "true" and node.get("enabled") != "false"
                    and node.get("visible-to-user") != "false")
        selectors = [{"packageName": node.get("package"), "resourceId": node.get("resource-id"),
                      "description": node.get("content-desc")}
                     for node in root.iter() if is_button(node)
                     and normalize_bluetooth_label(node.get("content-desc", "")).casefold() in descriptions]
        if selectors or not unpair.get("details_by_target_row"):
            return selectors
        parents = {child: parent for parent in root.iter() for child in parent}
        titles = [node for node in root.iter() if node.get("package") in pairing["owner_packages"]
                  and node.get("resource-id") in pairing["device_resource_ids"]
                  and node.get("visible-to-user") != "false"]
        for title in titles:
            if normalize_bluetooth_label(title.get("text", "")).casefold() != normalize_bluetooth_label(target_name).casefold():
                continue
            row = parents.get(title)
            while row is not None:
                buttons = [node for node in row.iter() if is_button(node)]
                if buttons:
                    # Never borrow a settings icon from a sibling receiver row.
                    if sum(node in titles for node in row.iter()) == 1:
                        anchor = "//*[" + " and ".join(f"@{key}={self._xpath_literal(title.get(key, ''))}"
                                                        for key in ("package", "resource-id", "text")) + "]"
                        for button in buttons:
                            predicate = " and ".join(f"@{key}={self._xpath_literal(button.get(key, ''))}"
                                                     for key in ("package", "resource-id", "clickable"))
                            selectors.append(anchor + f"/ancestor::*[.//*[{predicate}]][1]//*[{predicate}]")
                    break
                row = parents.get(row)
        return selectors

    def _unpair_bluetooth_target(self, target_name: str) -> None:
        operation = self._bluetooth_operation()
        pairing = operation.get("pairing")
        unpair = operation.get("unpair")
        if pairing is None or unpair is None:
            raise SystemUIError("Bluetooth unpair is not declared by the System UI Profile")
        details = {"target_name": target_name, "profile": self.profile.profile_id}
        self.journal.record_action("system_ui_bluetooth_unpair", status="started", details=details)
        try:
            self._open_bluetooth_settings(target_name, pairing)
            def details_buttons():
                return self._bluetooth_device_settings_selectors(target_name, pairing, unpair)
            buttons = []
            for buttons in self._samples(details_buttons):
                if buttons:
                    break
            if not buttons and self._click_bluetooth_label(unpair.get("saved_devices_all_labels", []), pairing,
                                                          "system_ui_bluetooth_saved_devices", target_name):
                for buttons in self._samples(details_buttons):
                    if buttons:
                        break
            directions = ["down"] * unpair.get("scroll_to_start_swipes", 0) + ["up"] * unpair.get("max_swipes", 0)
            for direction in directions:
                if buttons:
                    break
                self.device.swipe(direction, unpair["scroll_duration_sec"])
                self.journal.record_action("system_ui_bluetooth_saved_devices_scroll", status="success",
                                           details={**details, "direction": direction})
                self._settle_transition()
                for buttons in self._samples(details_buttons):
                    if buttons:
                        break
            if len(buttons) != 1:
                raise SystemUIError("Bluetooth device settings target is ambiguous or unavailable")
            selector = buttons[0]
            if isinstance(selector, str):
                self.device.click_xpath(selector)
            else:
                self.device.click(selector)
            self.journal.record_action("system_ui_bluetooth_device_details", status="success", details={**details, "selector": selector})
            self._settle_transition()
            def unpair_buttons():
                root = self._root()
                if not any(node.get("package") in pairing["owner_packages"]
                           and (unpair.get("header_text_only") or node.get("resource-id") in unpair["header_resource_ids"])
                           and normalize_bluetooth_label(node.get("text", "")).casefold() == normalize_bluetooth_label(target_name).casefold()
                           for node in root.iter()):
                    return []
                return [node for node in root.iter()
                        if node.get("package") in pairing["owner_packages"]
                        and node.get("text") in unpair["button_labels"]
                        and node.get("enabled") != "false" and node.get("visible-to-user") != "false"]
            self._wait_for(lambda: bool(unpair_buttons()), True, "Bluetooth Unpair button for target")
            buttons = unpair_buttons()
            if len(buttons) != 1:
                raise SystemUIError("Bluetooth Unpair button is ambiguous")
            selector = {"packageName": buttons[0].get("package"), **self._selector(buttons[0])}
            self.device.click(selector)
            self.journal.record_action("system_ui_bluetooth_unpair_request", status="success", details={**details, "selector": selector})
            self._settle_transition()
            deadline = self._monotonic() + self.timeout
            confirmed = False
            while self._bluetooth_target_paired(target_name):
                if not confirmed:
                    confirmed = self._confirm_bluetooth_dialog(target_name, pairing, unpair.get("confirmation"), "system_ui_bluetooth_unpair_confirm")
                    if confirmed:
                        continue  # Re-read the bond after confirming, including at the deadline.
                if self._monotonic() >= deadline:
                    raise SystemUIError("Bluetooth target is still paired after Unpair")
                self._sleep(self.poll_interval)
        except (Exception, KeyboardInterrupt) as error:
            self.journal.record_action("system_ui_bluetooth_unpair", status="failed", details={**details, "error": str(error) or type(error).__name__})
            raise
        self.journal.record_action("system_ui_bluetooth_unpair", status="success", details={**details, "paired": False, "confirmation_source": "bonded_device_list"})

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import asdict, dataclass

from .device import Device
from .journal import EventJournal
from .profiles import SystemUIProfile
from .session import EnvironmentSnapshot


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


class SystemUIRuntime:
    def __init__(
        self,
        device: Device,
        profile: SystemUIProfile,
        journal: EventJournal,
        *,
        timeout: float = 5.0,
        poll_interval: float = 0.2,
        transition_settle: float = 0.5,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.device = device
        self.profile = profile
        self.journal = journal
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.transition_settle = transition_settle
        self._monotonic = monotonic
        self._sleep = sleep

    def _settle_transition(self) -> None:
        if self.transition_settle:
            self._sleep(self.transition_settle)

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
                selector = (
                    f"//*[{title_selector}]/following::*"
                    f"[{switch_selector}][1]"
                )
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
        if value == "1":
            return True
        if value == "0":
            return False
        raise SystemUIError("Wi-Fi state is unreadable")

    def _wifi_connected(self) -> bool:
        value = self.device.shell("cmd wifi status").casefold()
        if "not connected" in value or "disconnected" in value:
            return False
        if "connected to" in value:
            return True
        fallback = self.device.shell("dumpsys connectivity")
        match = re.search(
            r"type:\s*WIFI\[\][^\n]*state:\s*([A-Z_]+)/([A-Z_]+)",
            fallback,
            re.IGNORECASE,
        )
        if match is None:
            return False
        return all(state.casefold() == "connected" for state in match.groups())

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
        deadline = self._monotonic() + self.timeout
        while True:
            dump = self.device.shell(operation["paired_devices_command"])
            _, bonded_marker, remainder = dump.partition("Bonded devices:")
            bonded, database_marker, _ = remainder.partition("Devices in DB:")
            if bonded_marker and database_marker:
                break
            if self._monotonic() >= deadline:
                raise SystemUIError(
                    "Bluetooth paired-device list is unreadable"
                )
            self._sleep(self.poll_interval)
        target = target_name.casefold()
        return any(target in line.casefold() for line in bonded.splitlines())

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
        deadline = self._monotonic() + self.timeout
        while True:
            matches = self._matching_nodes(
                self._root(),
                labels,
                resource_suffix=resource_suffix,
            )
            if matches:
                return matches[0]
            if self._monotonic() >= deadline:
                raise _WaitTimeout(f"expected exactly one of {labels!r}")
            self._sleep(self.poll_interval)

    def _wait_for_clickable_label(
        self,
        labels: list[str],
    ) -> ET.Element:
        deadline = self._monotonic() + self.timeout
        while True:
            matches = self._clickable_label_nodes(self._root(), labels)
            if matches:
                return matches[0]
            if self._monotonic() >= deadline:
                raise _WaitTimeout(
                    f"expected exactly one clickable row of {labels!r}"
                )
            self._sleep(self.poll_interval)

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
            operation["direct_intents"],
            start=1,
        ):
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
                    if current_target:
                        self._open_dnd(
                            phase=phase,
                            navigation_attempt=intent_attempt,
                        )
                        root = self._root()
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
    ) -> bool:
        deadline = self._monotonic() + self.timeout
        while True:
            observed = read()
            if observed is target:
                return observed
            if self._monotonic() >= deadline:
                raise SystemUIError(f"{name} did not become {target}")
            self._sleep(self.poll_interval)

    def _record(
        self,
        action: str,
        status: str,
        *,
        before: bool,
        target: bool,
        after: bool | None,
        phase: str,
        error: Exception | None = None,
    ) -> None:
        operation, _ = self._operation()
        details = {
            "before": before,
            "target": target,
            "after": after,
            "profile": self.profile.profile_id,
            "strategy": operation["strategy"],
            "phase": phase,
        }
        if error is not None:
            details["error_type"] = type(error).__name__
            details["error"] = str(error)
        self.journal.record_action(
            action,
            status=status,
            details=details,
        )

    def _set_dnd(
        self,
        enabled: bool,
        action: str,
        phase: str,
    ) -> None:
        operation, _ = self._operation()
        before = self._dnd_enabled()
        try:
            if before is not enabled:
                if operation["strategy"] == "button_based":
                    button = self._open_button_dnd(enabled, phase)
                    self.device.click(self._selector(button))
                elif operation["strategy"] == "switch_basic":
                    self.device.click_xpath(
                        self._open_basic_dnd_switch(phase)
                    )
                else:
                    title = self._open_dnd(phase)
                    self.device.click_xpath(
                        self._clickable_ancestor_xpath(title)
                    )
                self._settle_transition()
            after = self._wait_for(self._dnd_enabled, enabled, "DND")
        except Exception as error:
            self._record(
                action,
                "failed",
                before=before,
                target=enabled,
                after=None,
                phase=phase,
                error=error,
            )
            raise
        self._record(
            action,
            "success",
            before=before,
            target=enabled,
            after=after,
            phase=phase,
        )

    def _set_hide_all(self, enabled: bool, phase: str) -> None:
        before = self._hide_all_enabled()
        try:
            if before is not enabled:
                _, hide = self._operation()
                label = self._unique_label_node(
                    self._root(),
                    hide["hide_all_labels"],
                    resource_suffix=hide["click_resource_suffix"],
                )
                self.device.click_xpath(
                    self._clickable_ancestor_xpath(label)
                )
                self._settle_transition()
            after = self._wait_for(
                self._hide_all_enabled,
                enabled,
                "Hide all",
            )
        except Exception as error:
            self._record(
                "system_ui_hide_all",
                "failed",
                before=before,
                target=enabled,
                after=None,
                phase=phase,
                error=error,
            )
            raise
        self._record(
            "system_ui_hide_all",
            "success",
            before=before,
            target=enabled,
            after=after,
            phase=phase,
        )

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

    def _set_airplane(self, enabled: bool, phase: str) -> None:
        operation = self._airplane_operation()
        before = self._airplane_enabled()
        try:
            if before is not enabled:
                commands = operation[
                    "enable_commands" if enabled else "disable_commands"
                ]
                errors = []
                for command in commands:
                    self.device.shell(command)
                    try:
                        self._wait_for(
                            self._airplane_enabled,
                            enabled,
                            "airplane mode",
                        )
                        break
                    except SystemUIError as error:
                        errors.append(str(error))
                else:
                    raise SystemUIError("; ".join(errors))
            after = self._airplane_enabled()
        except Exception as error:
            self._record_environment(
                f"system_ui_airplane_{phase}",
                "failed",
                before=before,
                target=enabled,
                after=None,
                phase=phase,
                strategy=operation["strategy"],
                error=error,
            )
            raise
        self._record_environment(
            f"system_ui_airplane_{phase}",
            "success",
            before=before,
            target=enabled,
            after=after,
            phase=phase,
            strategy=operation["strategy"],
        )

    def _set_wifi(self, enabled: bool, phase: str) -> None:
        operation = self._wifi_operation()
        before = self._wifi_enabled()
        try:
            if before is not enabled:
                commands = operation[
                    "enable_commands" if enabled else "disable_commands"
                ]
                errors = []
                for command in commands:
                    self.device.shell(command)
                    try:
                        self._wait_for(
                            self._wifi_enabled,
                            enabled,
                            "Wi-Fi",
                        )
                        break
                    except SystemUIError as error:
                        errors.append(str(error))
                else:
                    raise SystemUIError("; ".join(errors))
            after = self._wifi_enabled()
        except Exception as error:
            self._record_environment(
                f"system_ui_wifi_{phase}",
                "failed",
                before=before,
                target=enabled,
                after=None,
                phase=phase,
                strategy=operation["strategy"],
                error=error,
            )
            raise
        self._record_environment(
            f"system_ui_wifi_{phase}",
            "success",
            before=before,
            target=enabled,
            after=after,
            phase=phase,
            strategy=operation["strategy"],
        )

    def snapshot_environment(self) -> EnvironmentSnapshot:
        notification = self.snapshot_notification_suppression()
        wifi_enabled = self._wifi_enabled()
        return EnvironmentSnapshot(
            dnd_enabled=notification.enabled,
            hide_all=notification.hide_all,
            airplane_enabled=self._airplane_enabled(),
            wifi_enabled=wifi_enabled,
            wifi_connected=wifi_enabled and self._wifi_connected(),
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
        self, acquisition_environment: str | None = "device_only"
    ) -> EnvironmentSnapshot:
        if acquisition_environment not in {None, "device_only", "controlled_online"}:
            raise SystemUIError("unknown acquisition environment")
        observed = self.snapshot_environment()
        ready = (
            observed.dnd_enabled and observed.hide_all is not False
            and observed.airplane_enabled
            and (acquisition_environment is None or observed.wifi_enabled == (acquisition_environment == "controlled_online"))
            and (acquisition_environment != "controlled_online" or observed.wifi_connected)
        )
        self.journal.record_action(
            "system_ui_environment_verify",
            status="success" if ready else "failed",
            details={
                **asdict(observed),
                "acquisition_environment": acquisition_environment,
                "profile": self.profile.profile_id,
            },
        )
        if not ready:
            raise SystemUIError("prepared device environment has drifted")
        return observed

    def apply_acquisition_environment(self, name: str) -> None:
        if name not in {"device_only", "controlled_online"}:
            raise SystemUIError(
                f"unknown acquisition environment: {name!r}"
            )
        target = name == "controlled_online"
        self._set_wifi(target, "condition")
        if target:
            try:
                self._wait_for(
                    self._wifi_connected,
                    True,
                    "Wi-Fi connection",
                )
            except SystemUIError as error:
                self.journal.record_action(
                    "system_ui_wifi_connection_verify",
                    status="failed",
                    details={"acquisition_environment": name},
                )
                raise SystemUIError("Wi-Fi is not connected") from error
            self.journal.record_action(
                "system_ui_wifi_connection_verify",
                status="success",
                details={"acquisition_environment": name},
            )

    def restore_environment(
        self,
        snapshot: EnvironmentSnapshot,
    ) -> None:
        operation, _ = self._operation()
        failures = []
        try:
            self._set_wifi(snapshot.wifi_enabled, "restore")
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
        self,
        action: str,
        status: str,
        *,
        before: bool,
        target: bool,
        after: bool | None,
        phase: str,
        error: Exception | None = None,
    ) -> None:
        operation = self._bluetooth_operation()
        details = {
            "before": before,
            "target": target,
            "after": after,
            "profile": self.profile.profile_id,
            "strategy": operation["strategy"],
            "phase": phase,
        }
        if error is not None:
            details["error_type"] = type(error).__name__
            details["error"] = str(error)
        self.journal.record_action(action, status=status, details=details)

    def _set_bluetooth_with_ui(self, enabled: bool, phase: str) -> None:
        operation = self._bluetooth_operation()
        fallback = operation.get("ui_fallback")
        if not isinstance(fallback, dict):
            raise SystemUIError("Bluetooth UI fallback is unavailable")
        errors = []
        for command in fallback["direct_intents"]:
            try:
                self.device.shell(command)
                self._settle_transition()
                root = self._root()
                label = self._unique_label_node(root, fallback["row_labels"])
                parents = {
                    child: parent for parent in root.iter() for child in parent
                }
                container = parents.get(label)
                while (
                    container is not None
                    and container.get("clickable") != "true"
                ):
                    container = parents.get(container)
                if container is None:
                    raise SystemUIError("Bluetooth row is not clickable")
                switches = [
                    node
                    for node in container.iter()
                    if node.get("resource-id")
                    in fallback["switch_resource_ids"]
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
                    self.device.click_xpath(
                        self._clickable_ancestor_xpath(label)
                        + f"//*[{resource_predicate}]"
                    )
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
        before = self._bluetooth_enabled()
        try:
            if before is not enabled:
                command = (
                    operation["enable_command"]
                    if enabled
                    else operation["disable_command"]
                )
                self.device.shell(command)
                try:
                    self._wait_for(
                        self._bluetooth_enabled,
                        enabled,
                        "Bluetooth",
                    )
                except SystemUIError as command_error:
                    try:
                        self._set_bluetooth_with_ui(enabled, phase)
                    except SystemUIError as fallback_error:
                        raise SystemUIError(
                            f"{command_error}; {fallback_error}"
                        ) from command_error
            after = self._bluetooth_enabled()
        except Exception as error:
            self._record_bluetooth(
                f"system_ui_bluetooth_{phase}",
                "failed",
                before=before,
                target=enabled,
                after=None,
                phase=phase,
                error=error,
            )
            raise
        self._record_bluetooth(
            f"system_ui_bluetooth_{phase}",
            "success",
            before=before,
            target=enabled,
            after=after,
            phase=phase,
        )

    def prepare_bluetooth_delivery(
        self,
        target_name: str,
    ) -> BluetoothSnapshot:
        snapshot = BluetoothSnapshot(self._bluetooth_enabled())
        try:
            self._set_bluetooth(True, "prepare")
            paired = self._bluetooth_target_paired(target_name)
            self.journal.record_action(
                "system_ui_bluetooth_target_check",
                status="success" if paired else "failed",
                details={
                    "target_name": target_name,
                    "paired": paired,
                    "profile": self.profile.profile_id,
                    "strategy": self._bluetooth_operation()["strategy"],
                },
            )
            if not paired:
                raise SystemUIError(
                    f"Bluetooth target is not paired: {target_name}"
                )
        except Exception as error:
            try:
                self.restore_bluetooth_delivery(snapshot, phase="rollback")
            except Exception as rollback_error:
                raise SystemUIError(
                    f"Bluetooth preparation failed: {error}; "
                    f"rollback failed: {rollback_error}"
                ) from error
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
        self._set_bluetooth(snapshot.enabled, phase)

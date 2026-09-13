"""Compatibility bridge from the Telegram collector to the AURA runtime."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import time
from datetime import datetime
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import Any

from ...device import Bounds, validate_selector
from ...models import (
    AcquisitionStatus,
    ActionRef,
    ProcedureStatus,
)
from ...runtime import RunContext, source_snapshot_references


PACKAGE = "org.telegram.messenger"
SELECTOR_ATTRIBUTES = {
    "class_name": "class",
    "content_description": "content-desc",
    "text": "text",
}
UIAUTOMATOR_KEYS = {
    "class_name": "className",
    "content_description": "description",
    "text": "text",
}
CONSTRAINT_ATTRIBUTES = {
    **SELECTOR_ATTRIBUTES,
    "checked": "checked",
    "clickable": "clickable",
    "focusable": "focusable",
    "scrollable": "scrollable",
}
CONSTRAINT_UIAUTOMATOR_KEYS = {
    **UIAUTOMATOR_KEYS,
    "checked": "checked",
    "clickable": "clickable",
    "focusable": "focusable",
    "scrollable": "scrollable",
}
_DOWNLOAD_ROOT = PurePosixPath("/sdcard/Download")
_SUFFIX = re.compile(r"^\.[a-zA-Z0-9]+$")
_LOGICAL_CHATROOM_ID = re.compile(r"^telegram-chat-[0-9a-f]{64}$")
_CONTAINER_REF = re.compile(r"^container-\d{6}$")
_UNSAFE_PATH_LABEL = re.compile(r"[/\\\x00-\x1f\x7f]")
# ponytail: S21/12.9.0 and S8/12.9.2 calibration; profile it only after a
# tested device or version demonstrates a different transition interval.
_TRANSITION_SETTLE_SECONDS = 0.5


class TelegramAdapterError(RuntimeError):
    pass


def normalize_retained_basename(value: str) -> str:
    if not isinstance(value, str):
        raise TelegramAdapterError("retained basename is invalid")
    normalized = _UNSAFE_PATH_LABEL.sub("_", value).strip()
    if not normalized or normalized in {".", ".."}:
        raise TelegramAdapterError("retained basename is invalid")
    return normalized


def _xml_value(value: object) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, str):
        return value
    raise TelegramAdapterError("selector constraint is invalid")


def _validated_external_roots(
    roots: tuple[str, ...],
    allowed_roots: tuple[PurePosixPath, ...],
) -> tuple[PurePosixPath, ...]:
    if (
        not isinstance(roots, tuple)
        or not roots
        or any(not isinstance(root, str) for root in roots)
    ):
        raise TelegramAdapterError("external file roots are invalid")
    parsed = tuple(PurePosixPath(root) for root in roots)
    if (
        len(set(parsed)) != len(parsed)
        or any(
            not root.is_absolute()
            or ".." in root.parts
            or root not in allowed_roots
            for root in parsed
        )
    ):
        raise TelegramAdapterError("external file roots are invalid")
    return parsed


def _external_snapshot_command(roots: tuple[PurePosixPath, ...]) -> str:
    values = " ".join(shlex.quote(root.as_posix()) for root in roots)
    return (
        f"for root in {values}; do "
        '[ ! -d "$root" ] || find "$root" -mindepth 1 -type f '
        "-exec stat -c '%n\t%s\t%Y' {} +; done"
    )


def _parse_file_inventory(
    output: str,
    roots: tuple[PurePosixPath, ...],
    label: str,
) -> tuple[tuple[str, int, int], ...]:
    records = []
    for line in output.splitlines():
        fields = line.rsplit("\t", 2)
        if len(fields) != 3:
            raise TelegramAdapterError(f"{label} inventory is invalid")
        remote, size_raw, mtime_raw = fields
        path = PurePosixPath(remote)
        try:
            size, mtime = int(size_raw), int(mtime_raw)
        except ValueError:
            raise TelegramAdapterError(f"{label} inventory is invalid") from None
        if (
            not path.is_absolute()
            or ".." in path.parts
            or path in roots
            or not any(path.is_relative_to(root) for root in roots)
            or size < 0
            or mtime < 0
        ):
            raise TelegramAdapterError(f"{label} inventory is invalid")
        records.append((path.as_posix(), size, mtime))
    if len({record[0] for record in records}) != len(records):
        raise TelegramAdapterError(f"{label} inventory is invalid")
    return tuple(sorted(records))


def _await_inventory_change(
    scan: Callable[[], tuple[tuple[str, int, int], ...]],
    baseline: tuple[tuple[str, int, int], ...],
    probes: int,
    interval: float,
    label: str,
) -> tuple[tuple[str, int, int], ...]:
    if not isinstance(baseline, tuple):
        raise TelegramAdapterError(f"{label} baseline is invalid")
    before = {path: (size, mtime) for path, size, mtime in baseline}
    current = baseline
    for probe in range(probes):
        if probe:
            time.sleep(interval)
        current = scan()
        if any(
            before.get(record[0]) != record[1:] for record in current
        ):
            return current
    raise TelegramAdapterError(f"{label} inventory did not change")


class TelegramDeviceAdapter:
    def __init__(self, context: RunContext):
        self.context = context
        self.actions: dict[str, ActionRef] = {}
        self.observations: dict[str, dict[str, bytes]] = {}
        self.observation_actions: dict[str, ActionRef] = {}
        self.last_action: ActionRef | None = None
        parameters = context.app_profile.parameters
        try:
            self.stagnation_rounds = int(parameters["stagnation_rounds"])
            self.inventory_probes = int(parameters["inventory_probes"])
            roots = parameters["attachment_roots"]
            all_roots = {
                path
                for values in roots.values()
                for path in values
                if path != _DOWNLOAD_ROOT.as_posix()
            }
        except (KeyError, TypeError, ValueError):
            raise TelegramAdapterError("Telegram Profile parameters are invalid") from None
        if min(self.stagnation_rounds, self.inventory_probes) < 1:
            raise TelegramAdapterError("Telegram Profile limits must be positive")
        self._external_roots = tuple(
            sorted(PurePosixPath(path) for path in all_roots)
        )
        self._inventory_interval = context.app_profile.timings.get(
            "inventory_probe_interval", 0.25
        )

    def matching_elements(
        self,
        root: ET.Element,
        element_id: str,
    ) -> tuple[ET.Element, ...]:
        try:
            entry = self.context.app_profile.selectors[element_id]
            selector = entry["selector"]
            constraints = entry.get("constraints", {})
            kind = selector["kind"]
            attribute = SELECTOR_ATTRIBUTES[kind]
            value = selector["value"]
        except (KeyError, TypeError):
            raise TelegramAdapterError(f"unknown Telegram element: {element_id}") from None
        if (
            selector.get("owner_package") != PACKAGE
            or not isinstance(value, str)
            or not value
            or not isinstance(constraints, Mapping)
        ):
            raise TelegramAdapterError("Telegram selector is invalid")
        expected = {attribute: value}
        for key, constraint in constraints.items():
            try:
                expected[CONSTRAINT_ATTRIBUTES[key]] = _xml_value(constraint)
            except KeyError:
                raise TelegramAdapterError("Telegram selector constraint is invalid") from None
        return tuple(
            node
            for node in root.iter()
            if node.get("package") == PACKAGE
            and all(node.get(key) == expected_value for key, expected_value in expected.items())
        )

    def element_selector(self, element_id: str) -> dict[str, object]:
        try:
            entry = self.context.app_profile.selectors[element_id]
            selector = dict(entry["selector"])
            selector["constraints"] = dict(entry.get("constraints", {}))
        except (KeyError, TypeError):
            raise TelegramAdapterError(f"unknown Telegram element: {element_id}") from None
        return selector

    def click(
        self,
        action_id: str,
        selector: Mapping[str, object],
        before_observation_id: str,
    ) -> None:
        if selector.get("kind") == "xpath":
            try:
                validate_selector(selector)
                xpath = selector.get("value")
                if (
                    selector.get("owner_package") != PACKAGE
                    or not isinstance(xpath, str)
                    or not xpath
                ):
                    raise TelegramAdapterError("Telegram XPath selector is invalid")
                self.context.device.click_xpath(xpath)
                action = self.context.journal.record_action(
                    action_id,
                    status="success",
                    context={"before_observation_id": before_observation_id},
                    details={"xpath": xpath},
                )
            except Exception as error:
                self.context.journal.record_action(
                    action_id,
                    status="failed",
                    context={"before_observation_id": before_observation_id},
                    details={"error": type(error).__name__},
                )
                raise
            self._settle_transition()
            self._remember(action_id, action)
            return

        resolved = self._uiautomator_selector(selector)
        action = self.context.ui.click(
            action_id,
            resolved,
            context={"before_observation_id": before_observation_id},
        )
        self._settle_transition()
        self._remember(action_id, action)

    def back(self, action_id: str, before_observation_id: str) -> None:
        action = self.context.ui.back(
            action_id,
            context={"before_observation_id": before_observation_id},
        )
        self._settle_transition()
        self._remember(action_id, action)

    def click_bounds(
        self,
        action_id: str,
        bounds: Bounds,
        before_observation_id: str,
        *,
        anchor: str = "lower_third",
    ) -> None:
        action = self.context.ui.click_bounds(
            action_id,
            bounds,
            anchor=anchor,
            context={"before_observation_id": before_observation_id},
        )
        self._settle_transition()
        self._remember(action_id, action)

    def scroll(
        self,
        action_id: str,
        before_observation_id: str,
        direction: str = "forward",
    ) -> bool:
        try:
            ui_direction = {"forward": "up", "backward": "down"}[direction]
        except KeyError:
            raise TelegramAdapterError("scroll direction is invalid") from None
        before = self._stable_ui_structure()
        try:
            self.context.device.swipe(ui_direction)
            self._settle_transition()
            moved = (
                self._stable_ui_structure(self._settled_hierarchy()) != before
            )
        except Exception as error:
            self.context.journal.record_action(
                action_id,
                status="failed",
                context={"before_observation_id": before_observation_id},
                details={"direction": ui_direction, "error": type(error).__name__},
            )
            raise
        action = self.context.journal.record_action(
            action_id,
            status="success",
            context={"before_observation_id": before_observation_id},
            details={"direction": ui_direction, "moved": moved},
        )
        self._remember(action_id, action)
        return moved

    def observe(self, observation_id: str) -> dict[str, str]:
        if observation_id in self.observations:
            raise TelegramAdapterError("observation is already recorded")
        action = self.last_action or self.context.journal.record_action(
            "telegram_observe_initial",
            status="success",
            context={"observation_id": observation_id},
        )
        tree = self._settled_hierarchy().encode("utf-8")
        screen = self.context.device.screenshot()
        self.context.journal.append(
            "observation_recorded",
            status="success",
            context={"observation_id": observation_id},
            details={"action_event_id": action.event_id},
        )
        self.observations[observation_id] = {
            "screen_image": screen,
            "ui_tree": tree,
        }
        self.observation_actions[observation_id] = action
        return {"observation_id": observation_id}

    def read_observation(self, observation_id: str, kind: str) -> bytes:
        if kind not in {"screen_image", "ui_tree"}:
            raise TelegramAdapterError("observation kind is invalid")
        try:
            return self.observations[observation_id][kind]
        except KeyError:
            raise TelegramAdapterError("observation is unavailable") from None

    def device_temporal_anchor(
        self,
        action_id: str,
        before_observation_id: str,
    ) -> str:
        try:
            raw_anchor = self.context.device.shell(
                "date +%Y-%m-%dT%H:%M:%S%z"
            ).strip()
            anchor = datetime.strptime(
                raw_anchor, "%Y-%m-%dT%H:%M:%S%z"
            ).isoformat(timespec="seconds")
        except Exception as error:
            self.context.journal.record_action(
                action_id,
                status="failed",
                context={
                    "before_observation_id": before_observation_id,
                },
                details={"error": type(error).__name__},
            )
            raise TelegramAdapterError(
                "device temporal anchor is unavailable"
            ) from None
        action = self.context.journal.record_action(
            action_id,
            status="success",
            context={"before_observation_id": before_observation_id},
            details={"device_temporal_anchor": anchor},
        )
        self._remember(action_id, action)
        return anchor

    def snapshot_downloads(
        self,
        action_id: str,
        before_observation_id: str,
        baseline=None,
    ) -> tuple[tuple[str, int, int], ...]:
        scan = lambda: self._scan_files((_DOWNLOAD_ROOT,), "download")
        return self._inventory_action(
            action_id, before_observation_id, scan, baseline, "download"
        )

    def snapshot_external_files(
        self,
        action_id: str,
        before_observation_id: str,
        roots: tuple[str, ...],
        baseline=None,
    ) -> tuple[tuple[str, int, int], ...]:
        validated = _validated_external_roots(roots, self._external_roots)
        scan = lambda: self._scan_files(validated, "external file")
        return self._inventory_action(
            action_id, before_observation_id, scan, baseline, "external file"
        )

    def pull_download(
        self,
        action_id: str,
        remote_path: str,
        before_observation_id: str,
    ) -> bytes:
        return self._pull_action(
            action_id,
            remote_path,
            before_observation_id,
            (_DOWNLOAD_ROOT, PurePosixPath("/storage/emulated/0/Download")),
        )

    def pull_external_file(
        self,
        action_id: str,
        remote_path: str,
        before_observation_id: str,
        roots: tuple[str, ...],
    ) -> bytes:
        validated = _validated_external_roots(roots, self._external_roots)
        return self._pull_action(
            action_id, remote_path, before_observation_id, validated
        )

    @staticmethod
    def canonical_sha256(value: object) -> str:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _uiautomator_selector(
        self, selector: Mapping[str, object]
    ) -> dict[str, object]:
        validate_selector(selector)
        try:
            kind = selector["kind"]
            resolved = {
                UIAUTOMATOR_KEYS[kind]: selector["value"],
                "packageName": selector["owner_package"],
            }
        except (KeyError, TypeError):
            raise TelegramAdapterError("Telegram selector is invalid") from None
        if resolved["packageName"] != PACKAGE or not isinstance(
            selector["value"], str
        ):
            raise TelegramAdapterError("Telegram selector is invalid")
        constraints = selector.get("constraints", {})
        if not isinstance(constraints, Mapping):
            raise TelegramAdapterError("Telegram selector constraints are invalid")
        try:
            resolved.update(
                {
                    CONSTRAINT_UIAUTOMATOR_KEYS[key]: value
                    for key, value in constraints.items()
                }
            )
        except KeyError:
            raise TelegramAdapterError("Telegram selector constraint is invalid") from None
        return resolved

    def _stable_ui_structure(
        self, hierarchy: str | None = None
    ) -> tuple[Any, ...]:
        try:
            root = ET.fromstring(
                hierarchy
                if hierarchy is not None
                else self.context.device.hierarchy()
            )
        except ET.ParseError:
            raise TelegramAdapterError("Telegram hierarchy is invalid") from None

        scrollables = [
            node
            for node in root.iter()
            if node.get("package") == PACKAGE
            and node.get("scrollable") == "true"
        ]
        if len(scrollables) == 1:
            return tuple(
                (
                    row.get("class", ""),
                    row.get("bounds", ""),
                    tuple(
                        value.strip().casefold()
                        for node in row.iter()
                        for value in (
                            node.get("text", ""),
                            node.get("content-desc", ""),
                        )
                        if value.strip()
                    ),
                )
                for row in list(scrollables[0])
            )

        def structure(node: ET.Element) -> tuple[Any, ...]:
            attributes = tuple(
                sorted(
                    (key, value)
                    for key, value in node.attrib.items()
                    if key not in {"content-desc", "text"}
                )
            )
            return node.tag, attributes, tuple(structure(child) for child in node)

        return structure(root)

    def _settled_hierarchy(self) -> str:
        previous = None
        current = None
        stable_polls = 0

        def settled() -> bool:
            nonlocal current, previous, stable_polls
            current = self.context.device.hierarchy()
            try:
                ET.fromstring(current)
            except (ET.ParseError, TypeError):
                raise TelegramAdapterError(
                    "Telegram hierarchy is invalid"
                ) from None
            stable_polls = stable_polls + 1 if current == previous else 1
            previous = current
            return stable_polls >= 2

        if not self.context.ui.wait_until(settled):
            raise TelegramAdapterError("Telegram hierarchy did not settle")
        assert isinstance(current, str)
        return current

    def _scan_files(
        self,
        roots: tuple[PurePosixPath, ...],
        label: str,
    ) -> tuple[tuple[str, int, int], ...]:
        output = self.context.device.shell(_external_snapshot_command(roots))
        if not isinstance(output, str):
            raise TelegramAdapterError(f"{label} inventory failed")
        return _parse_file_inventory(output, roots, label)

    def _inventory_action(
        self,
        action_id: str,
        before_observation_id: str,
        scan: Callable[[], tuple[tuple[str, int, int], ...]],
        baseline: object,
        label: str,
    ) -> tuple[tuple[str, int, int], ...]:
        try:
            result = (
                scan()
                if baseline is None
                else _await_inventory_change(
                    scan,
                    baseline,
                    self.inventory_probes,
                    self._inventory_interval,
                    label,
                )
            )
        except Exception as error:
            self.context.journal.record_action(
                action_id,
                status="failed",
                context={"before_observation_id": before_observation_id},
                details={"error": type(error).__name__},
            )
            raise
        action = self.context.journal.record_action(
            action_id,
            status="success",
            context={"before_observation_id": before_observation_id},
            details={"record_count": len(result)},
        )
        self._remember(action_id, action)
        return result

    def _pull_action(
        self,
        action_id: str,
        remote_path: str,
        before_observation_id: str,
        roots: tuple[PurePosixPath, ...],
    ) -> bytes:
        if not isinstance(remote_path, str) or "\x00" in remote_path:
            raise TelegramAdapterError("external file path is outside requested roots")
        path = PurePosixPath(remote_path)
        if (
            not path.is_absolute()
            or ".." in path.parts
            or path in roots
            or not any(path.is_relative_to(root) for root in roots)
        ):
            raise TelegramAdapterError("external file path is outside requested roots")
        try:
            with TemporaryDirectory() as directory:
                local = Path(directory) / "artifact"
                self.context.device.pull(path.as_posix(), local)
                value = local.read_bytes()
        except Exception as error:
            self.context.journal.record_action(
                action_id,
                status="failed",
                context={"before_observation_id": before_observation_id},
                details={"remote_path": remote_path, "error": type(error).__name__},
            )
            raise
        action = self.context.journal.record_action(
            action_id,
            status="success",
            context={"before_observation_id": before_observation_id},
            details={"remote_path": remote_path, "size": len(value)},
        )
        self._remember(action_id, action)
        return value

    def _remember(self, legacy_id: str, action: ActionRef) -> None:
        self.actions[legacy_id] = action
        self.last_action = action

    @staticmethod
    def _settle_transition() -> None:
        time.sleep(_TRANSITION_SETTLE_SECONDS)


class TelegramOutputs:
    def __init__(
        self,
        context: RunContext,
        device: TelegramDeviceAdapter,
        target_ref: str,
        *,
        relative_root: str = "telegram/account",
        logical_chatroom_id: str | None = None,
    ):
        self.context = context
        self.device = device
        self.target_ref = target_ref
        self.relative_root = relative_root
        self.logical_chatroom_id = logical_chatroom_id
        self.history_attempt_id: str | None = None

    def begin_history(self) -> None:
        if self.logical_chatroom_id is None:
            raise TelegramAdapterError("history requires a chatroom scope")
        self.history_attempt_id = self.context.item_attempt(
            f"conversation:{self.logical_chatroom_id}", "telegram.conversations", "conversation",
            source={"target_ref": self.target_ref, "comparison_ref": self.logical_chatroom_id},
            context={"target_ref": self.target_ref, "logical_chatroom_id": self.logical_chatroom_id},
        )

    def begin_target(self, target_id: str) -> None:
        self.context.begin_identification(target_id)
        if target_id == "telegram.account":
            self.context.item_attempt(f"account:{self.target_ref}", target_id, "account", source={"target_ref": self.target_ref})

    def begin_attachment(self, attachment_ref: str, row: Mapping[str, object], **linked: object) -> None:
        self.context.item_attempt(f"attachment:{attachment_ref}", "telegram.attachments", str(row["rendered"]["kind"]),
                                  source={"target_ref": self.target_ref, "comparison_ref": attachment_ref},
                                  context={"target_ref": self.target_ref, "logical_chatroom_id": self.logical_chatroom_id, "attachment_kind": row["rendered"]["kind"], **linked})

    def finish_attachment(self, attachment_ref: str, limitation: str | None, *, evidence=None) -> None:
        attempt_id = self.context._item_attempts[f"attachment:{attachment_ref}"][0]
        observation_id = evidence.get("observation_id") if evidence else None
        observation = next((o for o in self.context.observations if o.observation_id == observation_id), None)
        action = next((a for a in self.context.journal.actions if observation is not None and a.action_id == observation.action_id), None)
        if limitation == "attachment_materialize_save_action_unavailable":
            self.context.finish_fallback(attempt_id, "display_fallback", "save_action_unavailable", reason=limitation,
                                         action=action, observation_id=observation_id)
        else:
            status = self.context.result_status(attempt_id)
            self.context.finish_attempt(attempt_id, ProcedureStatus.COMPLETED, status,
                                        action=action, observation_id=observation_id,
                                        reason=limitation or ("required_artifacts_missing" if status is not AcquisitionStatus.ACQUIRED else None))

    def for_chatroom(
        self,
        logical_chatroom_id: str,
        display_name: str,
        *,
        container_ref: str | None = None,
        identity_status: str = "identified",
    ) -> TelegramOutputs:
        if _LOGICAL_CHATROOM_ID.fullmatch(logical_chatroom_id) is None:
            raise TelegramAdapterError(
                "logical chatroom identifier is invalid"
            )
        if not isinstance(display_name, str) or not display_name.strip():
            raise TelegramAdapterError("chatroom display name is invalid")
        label = " ".join(
            _UNSAFE_PATH_LABEL.sub("_", display_name).split()
        )[:40]
        suffix = ""
        if identity_status != "identified":
            if (
                identity_status not in {"ambiguous", "insufficient"}
                or not isinstance(container_ref, str)
                or _CONTAINER_REF.fullmatch(container_ref) is None
            ):
                raise TelegramAdapterError(
                    "ambiguous chatroom scope is invalid"
                )
            suffix = f" [{container_ref}]"
        return TelegramOutputs(
            self.context,
            self.device,
            self.target_ref,
            relative_root=(
                f"telegram/chatrooms/{logical_chatroom_id} ({label}){suffix}"
            ),
            logical_chatroom_id=logical_chatroom_id,
        )

    def write_json(self, **kwargs: object):
        document = kwargs.get("value")
        is_message = isinstance(document, Mapping) and document.get("record_kind") == "message"
        if is_message:
            message_ref = document.get("message_ref")
            if (self.history_attempt_id is None or not isinstance(message_ref, str)
                or kwargs.get("artifact_class") != "message"
                or kwargs.get("output_kind") != "ui_record"
                or re.fullmatch(r"message-\d{6}", message_ref) is None
                or document.get("logical_chatroom_id") != self.logical_chatroom_id
                or kwargs.get("artifact_id") != "message-ui-" + message_ref.removeprefix("message-")):
                raise TelegramAdapterError("message requires its real history scope")
        try:
            value = (
                json.dumps(
                    source_snapshot_references(kwargs.pop("value"), "telegram"),
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            ).encode("utf-8")
        except (KeyError, TypeError, ValueError):
            raise TelegramAdapterError("output JSON is invalid") from None
        record = self._write(kwargs, value, ".json", document=document)
        if is_message:
            self.context.register_item("telegram.conversations", "message", source={
                "collection_attempt_id": self.history_attempt_id,
                "artifact_id": record.artifact_id,
                "message_ref": message_ref,
                "logical_chatroom_id": self.logical_chatroom_id,
            })
        return record

    def write_bytes(self, **kwargs: object):
        try:
            value = kwargs.pop("value")
            suffix = kwargs.pop("suffix")
            retained_basename = kwargs.pop(
                "retained_basename", None
            )
        except KeyError:
            raise TelegramAdapterError("output bytes are invalid") from None
        if not isinstance(value, bytes) or not isinstance(suffix, str):
            raise TelegramAdapterError("output bytes are invalid")
        return self._write(
            kwargs,
            value,
            suffix,
            retained_basename=retained_basename,
        )

    def write_observation_pair(
        self,
        *,
        artifact_class: str,
        screen_artifact_id: str,
        tree_artifact_id: str,
        action_id: str,
        observation_id: str,
        comparison_ref: str,
    ) -> dict[str, object]:
        try:
            source_action = self.device.actions[action_id]
            screen = self.device.read_observation(
                observation_id, "screen_image"
            )
            tree = self.device.read_observation(observation_id, "ui_tree")
        except KeyError:
            raise TelegramAdapterError(
                "observation action is unavailable"
            ) from None
        fields = {
            "action_id": action_id,
            "artifact_class": artifact_class,
            "artifact_id": screen_artifact_id,
            "comparison_ref": comparison_ref,
            "observation_id": observation_id,
            "output_kind": "screen_image",
        }
        screen_path = self._relative_path(fields, ".png", None)
        tree_path = self._relative_path(
            {**fields, "artifact_id": tree_artifact_id}, ".xml", None
        )
        if not screen_path.endswith(".png") or (
            screen_path.removesuffix(".png")
            != tree_path.removesuffix(".xml")
        ):
            raise TelegramAdapterError("observation output pair is invalid")
        target_id, item_type, key, source, item_context = self._item(
            fields, ".png", None
        )
        attempt_id = self.context.item_attempt(
            key,
            target_id,
            item_type,
            source=source,
            context=item_context,
        )
        linked = {
            "target_ref": self.target_ref,
            "source_snapshot_id": f"telegram-snapshot:{observation_id}",
            "comparison_ref": comparison_ref,
            "artifact_class": artifact_class,
        }
        if self.logical_chatroom_id is not None:
            linked["logical_chatroom_id"] = self.logical_chatroom_id
        retained = self.context.linked_action(
            "retain_telegram_observation",
            attempt_id,
            source_action=source_action,
            context=linked,
        )
        screen_record, tree_record = self.context.retain_observation(
            f"{self.relative_root}/{screen_path.removesuffix('.png')}",
            screen,
            tree,
            attempt_id=attempt_id,
            action=retained,
            context=linked,
        )
        return {
            "observation_id": self.context.observations[-1].observation_id,
            "source_snapshot_id": f"telegram-snapshot:{observation_id}",
            "screen_artifact_id": screen_record.artifact_id,
            "screen_path": screen_record.relative_path,
            "ui_tree_artifact_id": tree_record.artifact_id,
            "ui_tree_path": tree_record.relative_path,
        }

    def _write(
        self,
        fields: dict[str, object],
        value: bytes,
        suffix: str,
        *,
        retained_basename: str | None = None,
        document: object = None,
    ):
        required = {
            "action_id",
            "artifact_class",
            "artifact_id",
            "comparison_ref",
            "observation_id",
            "output_kind",
        }
        if set(fields) != required or _SUFFIX.fullmatch(suffix) is None:
            raise TelegramAdapterError("output arguments are invalid")
        action_id = fields["action_id"]
        if not isinstance(action_id, str):
            raise TelegramAdapterError("output action is invalid")
        try:
            action = self.device.actions[action_id]
        except KeyError:
            raise TelegramAdapterError("output action is unavailable") from None
        artifact_id = fields["artifact_id"]
        if not isinstance(artifact_id, str) or not artifact_id:
            raise TelegramAdapterError("output artifact identifier is invalid")
        context = {
            "target_ref": self.target_ref,
            "source_snapshot_id": f"telegram-snapshot:{fields['observation_id']}",
            "comparison_ref": fields["comparison_ref"],
            "artifact_class": fields["artifact_class"],
            "output_kind": fields["output_kind"],
        }
        if self.logical_chatroom_id is not None:
            context["logical_chatroom_id"] = self.logical_chatroom_id
        target_id, item_type, key, source, item_context = self._item(
            fields,
            suffix,
            document,
        )
        attempt_id = self.context.item_attempt(
            key,
            target_id,
            item_type,
            source=source,
            context=item_context,
        )
        observation = self._preserve_source_snapshot(context["source_snapshot_id"], attempt_id, context)
        if document is not None:
            self._preserve_source_snapshots(document, attempt_id, context)
        action = self.context.linked_action(
            "retain_telegram_output",
            attempt_id,
            source_action=action,
            context=context,
        )
        return self.context.artifacts.write_bytes(
            (
                f"{self.relative_root}/"
                f"{self._relative_path(fields, suffix, retained_basename)}"
            ),
            value,
            kind=str(fields["output_kind"]),
            attempt_id=attempt_id,
            action=action,
            observation_id=observation.observation_id,
            context=context,
        )

    def _preserve_source_snapshot(self, source_id, attempt_id, context):
        if not isinstance(source_id, str) or not source_id.startswith("telegram-snapshot:"):
            raise TelegramAdapterError("source snapshot reference is invalid")
        existing = next((o for o in self.context.observations
                         if o.source_snapshot_id == source_id and o.attempt_id == attempt_id), None)
        if existing is not None:
            return existing
        local_id = source_id.removeprefix("telegram-snapshot:")
        try:
            source_action = self.device.observation_actions[local_id]
            screen = self.device.read_observation(local_id, "screen_image")
            tree = self.device.read_observation(local_id, "ui_tree")
        except KeyError:
            raise TelegramAdapterError("source snapshot is unavailable") from None
        linked = {**context, "source_snapshot_id": source_id}
        linked.pop("output_kind", None)
        action = self.context.linked_action(
            "retain_telegram_observation", attempt_id, source_action=source_action, context=linked,
        )
        self.context.retain_observation(
            f"{self.relative_root}/observations/source-{len(self.context.observations) + 1:06d}",
            screen, tree, attempt_id=attempt_id, action=action, context=linked,
        )
        return self.context.observations[-1]

    def _preserve_source_snapshots(self, document, attempt_id, context):
        preserved = {record.source_snapshot_id for record in self.context.observations}
        pending = [source_snapshot_references(document, "telegram")]
        while pending:
            value = pending.pop()
            if isinstance(value, list):
                pending.extend(value)
            elif isinstance(value, Mapping):
                for key, child in value.items():
                    if key.endswith("source_snapshot_id"):
                        sources = [child]
                    elif key.endswith("source_snapshot_ids") and isinstance(child, list):
                        sources = child
                    else:
                        pending.append(child)
                        continue
                    for source_id in sources:
                        if source_id in preserved:
                            continue
                        self._preserve_source_snapshot(source_id, attempt_id, context)
                        preserved.add(source_id)

    def _item(
        self,
        fields: Mapping[str, object],
        suffix: str,
        document: object,
    ) -> tuple[str, str, str, dict[str, object], dict[str, object]]:
        artifact_class = str(fields["artifact_class"])
        artifact_id = str(fields["artifact_id"])
        if artifact_class == "message" and self.logical_chatroom_id is not None:
            history = next((attempt for attempt in self.context.attempts if attempt.attempt_id == self.history_attempt_id), None)
            if history is None or history.procedure_status is not None:
                raise TelegramAdapterError("message output requires an open history acquisition scope")
        comparison_ref = str(fields["comparison_ref"])
        source = {
            "target_ref": self.target_ref,
            "comparison_ref": comparison_ref,
        }
        linked = dict(source)
        if self.logical_chatroom_id is not None:
            linked["logical_chatroom_id"] = self.logical_chatroom_id
        if isinstance(document, Mapping):
            linked.update({
                key: document[key]
                for key in (
                    "attachment_ref",
                    "container_ref",
                    "occurrence_ref",
                    "message_ref",
                )
                if key in document
            })
            rendered = document.get("rendered")
            if isinstance(rendered, Mapping):
                linked["attachment_kind"] = rendered.get("kind")
            observed = document.get("source")
            if isinstance(observed, Mapping):
                local_id = observed.get("observation_id")
                if local_id:
                    linked["source_snapshot_id"] = f"telegram-snapshot:{local_id}"

        if artifact_class == "account" or self.logical_chatroom_id is None:
            if artifact_id not in {"account", "account-screen", "account-tree"} and comparison_ref != "account-profile":
                return ("telegram.account", "account_summary", f"account-summary:{self.target_ref}", source, linked)
            return (
                "telegram.account",
                "account",
                f"account:{self.target_ref}",
                source,
                linked,
            )
        if artifact_class in {"chatroom", "chatroom_list"}:
            key = self.logical_chatroom_id or comparison_ref
            return (
                "telegram.conversations",
                "conversation",
                f"conversation:{key}",
                source,
                linked,
            )
        if artifact_class == "attachment":
            existing_item = next(
                (
                    item
                    for item in self.context.items
                    if item.target_id == "telegram.attachments"
                    and item.source.get("comparison_ref") == comparison_ref
                    and item.context.get("logical_chatroom_id")
                    == self.logical_chatroom_id
                ),
                None,
            )
            if existing_item is not None:
                return (
                    existing_item.target_id,
                    existing_item.item_type,
                    f"attachment:{comparison_ref}",
                    source,
                    linked,
                )
            kind = linked.get("attachment_kind")
            if kind not in {"photo", "video", "file"}:
                if (
                    fields["output_kind"] == "original_artifact"
                    and suffix in {".jpg", ".jpeg", ".png", ".webp"}
                ):
                    kind = "photo"
                elif (
                    fields["output_kind"] == "original_artifact"
                    and suffix in {".mp4", ".mkv", ".webm"}
                ):
                    kind = "video"
                else:
                    kind = "file"
            return (
                "telegram.attachments",
                str(kind),
                f"attachment:{comparison_ref}",
                source,
                linked,
            )
        if artifact_id.startswith(("message-ui-", "message-screen-", "message-tree-")):
            if self.history_attempt_id is None:
                raise TelegramAdapterError("message requires its real history scope")
            return (
                "telegram.conversations",
                "conversation",
                f"conversation:{self.logical_chatroom_id}",
                source,
                linked,
            )
        return (
            "telegram.conversations",
            "conversation",
            f"conversation:{self.logical_chatroom_id}",
            source,
            linked,
        )

    def _relative_path(
        self,
        fields: Mapping[str, object],
        suffix: str,
        retained_basename: str | None,
    ) -> str:
        artifact_id = str(fields["artifact_id"])
        if self.logical_chatroom_id is None:
            if retained_basename is not None:
                raise TelegramAdapterError("retained basename is invalid")
            names = {
                "account": "account.json",
                "account-screen": "account.png",
                "account-tree": "account.xml",
                "chatroom-list": "chatroom-list.json",
                "account-closure-000001": "account-closure.json",
            }
            try:
                return names[artifact_id]
            except KeyError:
                raise TelegramAdapterError(
                    "account output is invalid"
                ) from None

        artifact_class = fields["artifact_class"]
        original = (
            artifact_class == "attachment"
            and artifact_id.startswith("attachment-original-")
        )
        if retained_basename is not None and not original:
            raise TelegramAdapterError("retained basename is invalid")
        if artifact_class == "chatroom":
            names = {
                "chatroom": "chatroom.json",
                "chatroom-screen": "chatroom.png",
                "chatroom-tree": "chatroom.xml",
            }
            if artifact_id in names:
                return names[artifact_id]
        if artifact_class == "message":
            if artifact_id.startswith("container-history-context-"):
                return "history.json"
            boundary_names = {
                "message-history-latest-screen-":
                    "observations/latest-boundary.png",
                "message-history-latest-tree-":
                    "observations/latest-boundary.xml",
                "message-history-earliest-screen-":
                    "observations/earliest-boundary.png",
                "message-history-earliest-tree-":
                    "observations/earliest-boundary.xml",
            }
            for prefix, name in boundary_names.items():
                if artifact_id.startswith(prefix):
                    return name
            if artifact_id.startswith("message-ui-"):
                ordinal = artifact_id.removeprefix("message-ui-")
                return f"messages/message-{ordinal}.json"
            if artifact_id.startswith("message-window-screen-"):
                ordinal = artifact_id.removeprefix(
                    "message-window-screen-"
                )
                return f"message-windows/message-window-{ordinal}.png"
            if artifact_id.startswith("message-window-tree-"):
                ordinal = artifact_id.removeprefix(
                    "message-window-tree-"
                )
                return f"message-windows/message-window-{ordinal}.xml"
            if artifact_id.startswith("conversation-"):
                return "conversation.txt"
            if artifact_id.startswith("message-screen-"):
                ordinal = artifact_id.removeprefix("message-screen-")
                return f"messages/message-{ordinal}.png"
            if artifact_id.startswith("message-tree-"):
                ordinal = artifact_id.removeprefix("message-tree-")
                return f"messages/message-{ordinal}.xml"
        if artifact_class == "attachment":
            comparison_ref = fields["comparison_ref"]
            if (
                not isinstance(comparison_ref, str)
                or re.fullmatch(r"attachment-\d+", comparison_ref) is None
            ):
                raise TelegramAdapterError(
                    "attachment comparison reference is invalid"
                )
            names = {
                "attachment-ui-": "record.json",
                "attachment-menu-screen-": "materialize-menu-screen.png",
                "attachment-menu-tree-": "materialize-menu-screen.xml",
                "attachment-unavailable-screen-":
                    "materialize-unavailable-screen.png",
                "attachment-unavailable-tree-":
                    "materialize-unavailable-screen.xml",
                "attachment-audit-": "audit.json",
            }
            if original:
                if (
                    not isinstance(retained_basename, str)
                    or normalize_retained_basename(retained_basename)
                    != retained_basename
                ):
                    raise TelegramAdapterError(
                        "retained basename is invalid"
                    )
                return (
                    f"attachments/{comparison_ref}/"
                    f"{retained_basename}"
                )
            for prefix, name in names.items():
                if artifact_id.startswith(prefix):
                    return f"attachments/{comparison_ref}/{name}"
        raise TelegramAdapterError("chatroom output is invalid")

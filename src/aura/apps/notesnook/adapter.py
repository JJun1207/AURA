"""Notesnook export bridge to the shared AURA runtime."""

from __future__ import annotations

import json
import shlex
import time
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

from ...device import Bounds, validate_selector
from ...models import AcquisitionStatus, ActionRef, ProcedureStatus
from ...runtime import RunContext, source_snapshot_references


PACKAGE = "com.streetwriters.notesnook"
DOCUMENTS_PACKAGE = "com.google.android.documentsui"
PACKAGES = {PACKAGE, DOCUMENTS_PACKAGE}
SELECTOR_ATTRIBUTES = {
    "class_name": "class",
    "content_description": "content-desc",
    "resource_id": "resource-id",
    "text": "text",
}
UIAUTOMATOR_KEYS = {
    "class_name": "className",
    "content_description": "description",
    "resource_id": "resourceId",
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


class NotesnookAdapterError(RuntimeError):
    pass


def _xml_value(value: object) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, str):
        return value
    raise NotesnookAdapterError("selector constraint is invalid")


def _parse_inventory(
    output: str,
    root: PurePosixPath,
) -> tuple[tuple[str, int, int], ...]:
    records = []
    for line in output.splitlines():
        fields = line.rsplit("\t", 2)
        if len(fields) != 3:
            raise NotesnookAdapterError("export inventory is invalid")
        remote, size_raw, mtime_raw = fields
        path = PurePosixPath(remote)
        try:
            size, mtime = int(size_raw), int(mtime_raw)
        except ValueError:
            raise NotesnookAdapterError("export inventory is invalid") from None
        if (
            not root.is_absolute()
            or ".." in root.parts
            or not path.is_absolute()
            or ".." in path.parts
            or path == root
            or not path.is_relative_to(root)
            or size < 0
            or mtime < 0
        ):
            raise NotesnookAdapterError("export inventory is invalid")
        records.append((path.as_posix(), size, mtime))
    if len({path for path, _, _ in records}) != len(records):
        raise NotesnookAdapterError("export inventory is invalid")
    return tuple(sorted(records))


class NotesnookDeviceAdapter:
    def __init__(self, context: RunContext):
        self.context = context
        self.actions: dict[str, ActionRef] = {}
        self.observations: dict[str, dict[str, bytes]] = {}
        self.last_action: ActionRef | None = None
        parameters = context.app_profile.parameters
        try:
            self.export_root = PurePosixPath(parameters["export_root"])
            self.inventory_probes = int(parameters["inventory_probes"])
        except (KeyError, TypeError, ValueError):
            raise NotesnookAdapterError("Notesnook Profile parameters are invalid") from None
        if (
            not self.export_root.is_absolute()
            or ".." in self.export_root.parts
            or self.inventory_probes < 1
        ):
            raise NotesnookAdapterError("Notesnook Profile parameters are invalid")
        self._transition_settle = context.app_profile.timings.get(
            "transition_settle", 0.5
        )
        self._application_return_settle = context.app_profile.timings.get(
            "application_return_settle", 1.0
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
            expected = {
                SELECTOR_ATTRIBUTES[selector["kind"]]: selector["value"]
            }
            owner = selector["owner_package"]
        except (KeyError, TypeError):
            raise NotesnookAdapterError(
                f"unknown Notesnook element: {element_id}"
            ) from None
        if (
            owner not in PACKAGES
            or not isinstance(expected[next(iter(expected))], str)
            or not isinstance(constraints, Mapping)
        ):
            raise NotesnookAdapterError("Notesnook selector is invalid")
        try:
            expected.update(
                {
                    CONSTRAINT_ATTRIBUTES[key]: _xml_value(value)
                    for key, value in constraints.items()
                }
            )
        except KeyError:
            raise NotesnookAdapterError(
                "Notesnook selector constraint is invalid"
            ) from None
        return tuple(
            node
            for node in root.iter()
            if node.get("package") == owner
            and all(node.get(key) == value for key, value in expected.items())
        )

    def element_selector(self, element_id: str) -> dict[str, object]:
        try:
            entry = self.context.app_profile.selectors[element_id]
            selector = dict(entry["selector"])
            selector["constraints"] = dict(entry.get("constraints", {}))
            return selector
        except (KeyError, TypeError):
            raise NotesnookAdapterError(
                f"unknown Notesnook element: {element_id}"
            ) from None

    def click(
        self,
        action_id: str,
        selector: Mapping[str, object],
        before_observation_id: str,
    ) -> None:
        action = self.context.ui.click(
            action_id,
            self._uiautomator_selector(selector),
            context={"before_observation_id": before_observation_id},
        )
        self._settle(
            self._application_return_settle
            if action_id == "action-confirm-aura-directory"
            else self._transition_settle
        )
        self._remember(action_id, action)

    def long_click_bounds(
        self,
        action_id: str,
        bounds: Bounds,
        before_observation_id: str,
    ) -> None:
        action = self.context.ui.long_click_bounds(
            action_id,
            bounds,
            context={"before_observation_id": before_observation_id},
        )
        self._settle()
        self._remember(action_id, action)

    def click_bounds(
        self,
        action_id: str,
        bounds: Bounds,
        before_observation_id: str,
    ) -> None:
        action = self.context.ui.click_bounds(
            action_id,
            bounds,
            context={"before_observation_id": before_observation_id},
        )
        self._settle()
        self._remember(action_id, action)

    def back(
        self,
        action_id: str,
        before_observation_id: str,
    ) -> None:
        action = self.context.ui.back(
            action_id,
            context={"before_observation_id": before_observation_id},
        )
        self._settle()
        self._remember(action_id, action)

    def swipe(
        self,
        action_id: str,
        direction: str,
        before_observation_id: str,
    ) -> None:
        action = self.context.ui.swipe(
            action_id,
            direction,
            context={"before_observation_id": before_observation_id},
        )
        self._settle()
        self._remember(action_id, action)

    def observe(self, observation_id: str) -> dict[str, str]:
        if observation_id in self.observations:
            raise NotesnookAdapterError("observation is already recorded")
        action = self.last_action or self.context.journal.record_action(
            "notesnook_observe_initial",
            status="success",
            context={"observation_id": observation_id},
        )
        self.last_action = action
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
        return {"observation_id": observation_id}

    def read_observation(self, observation_id: str, kind: str) -> bytes:
        if kind not in {"screen_image", "ui_tree"}:
            raise NotesnookAdapterError("observation kind is invalid")
        try:
            return self.observations[observation_id][kind]
        except KeyError:
            raise NotesnookAdapterError("observation is unavailable") from None

    def snapshot_files(
        self,
        action_id: str,
        before_observation_id: str,
        baseline: tuple[tuple[str, int, int], ...] | None = None,
    ) -> tuple[tuple[str, int, int], ...]:
        try:
            result = self._scan_files()
            if baseline is not None:
                for _ in range(self.inventory_probes - 1):
                    if result != baseline:
                        break
                    time.sleep(self._inventory_interval)
                    result = self._scan_files()
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

    def pull_file(
        self,
        action_id: str,
        remote_path: str,
        before_observation_id: str,
    ) -> bytes:
        path = PurePosixPath(remote_path)
        if (
            not isinstance(remote_path, str)
            or "\x00" in remote_path
            or not path.is_absolute()
            or ".." in path.parts
            or path == self.export_root
            or not path.is_relative_to(self.export_root)
        ):
            raise NotesnookAdapterError("export path is outside requested root")
        try:
            with TemporaryDirectory() as directory:
                local = Path(directory) / "export.zip"
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

    def prepare_output_directory(self) -> None:
        quoted = shlex.quote(self.export_root.as_posix())
        result = self.context.device.shell(
            f"mkdir -p {quoted} && if [ -d {quoted} ]; then printf 'ready'; fi"
        )
        if result.strip() != "ready":
            raise NotesnookAdapterError("configured output directory could not be prepared")
        self.context.journal.record_action(
            "prepare_notesnook_output_directory", status="success",
            details={"path": self.export_root.as_posix(), "existing_files": "preserved"},
        )

    def _scan_files(self) -> tuple[tuple[str, int, int], ...]:
        self.prepare_output_directory()
        quoted = shlex.quote(self.export_root.as_posix())
        output = self.context.device.shell(
            f"if [ -d {quoted} ]; then find {quoted} -mindepth 1 -type f "
            "-exec stat -c '%n\t%s\t%Y' {} +; fi"
        )
        if not isinstance(output, str):
            raise NotesnookAdapterError("export inventory failed")
        return _parse_inventory(output, self.export_root)

    def _settled_hierarchy(self) -> str:
        previous = None
        current = None
        stable_polls = 0

        def settled() -> bool:
            nonlocal previous, current, stable_polls
            current = self.context.device.hierarchy()
            try:
                ET.fromstring(current)
            except (ET.ParseError, TypeError):
                raise NotesnookAdapterError(
                    "Notesnook hierarchy is invalid"
                ) from None
            stable_polls = stable_polls + 1 if current == previous else 1
            previous = current
            return stable_polls >= 2

        if not self.context.ui.wait_until(settled):
            raise NotesnookAdapterError("Notesnook hierarchy did not settle")
        assert isinstance(current, str)
        return current

    def _uiautomator_selector(
        self,
        selector: Mapping[str, object],
    ) -> dict[str, object]:
        validate_selector(selector)
        try:
            resolved = {
                UIAUTOMATOR_KEYS[selector["kind"]]: selector["value"],
                "packageName": selector["owner_package"],
            }
            constraints = selector.get("constraints", {})
        except (KeyError, TypeError):
            raise NotesnookAdapterError("Notesnook selector is invalid") from None
        if (
            resolved["packageName"] not in PACKAGES
            or not isinstance(selector["value"], str)
            or not isinstance(constraints, Mapping)
        ):
            raise NotesnookAdapterError("Notesnook selector is invalid")
        try:
            resolved.update(
                {
                    CONSTRAINT_UIAUTOMATOR_KEYS[key]: value
                    for key, value in constraints.items()
                }
            )
        except KeyError:
            raise NotesnookAdapterError(
                "Notesnook selector constraint is invalid"
            ) from None
        return resolved

    def _remember(self, action_id: str, action: ActionRef) -> None:
        self.actions[action_id] = action
        self.last_action = action

    def _settle(self, seconds: float | None = None) -> None:
        time.sleep(self._transition_settle if seconds is None else seconds)


class NotesnookOutputs:
    def __init__(
        self,
        context: RunContext,
        device: NotesnookDeviceAdapter,
        target_ref: str,
    ):
        self.context = context
        self.device = device
        self.target_ref = target_ref
        self._partial_attempts: dict[str, str] = {}

    def begin_item(self, key: str, target_id: str, item_type: str, **source: object) -> None:
        self.context.item_attempt(key, target_id, item_type, source={"target_ref": self.target_ref, **source}, context={"target_ref": self.target_ref, **source})

    def _attempt(
        self,
        key: str,
        target_id: str,
        item_type: str,
        source_action: ActionRef,
        linked: Mapping[str, object],
    ) -> tuple[str, ActionRef]:
        attempt_id = self.context.item_attempt(
            key,
            target_id,
            item_type,
            source=dict(linked),
            context=dict(linked),
        )
        action = self.context.linked_action(
            "retain_notesnook_output",
            attempt_id,
            source_action=source_action,
            context=dict(linked),
        )
        return attempt_id, action

    def finalize(self) -> None:
        for attempt_id, reason in self._partial_attempts.items():
            attempt = next(
                item
                for item in self.context.attempts
                if item.attempt_id == attempt_id
            )
            if attempt.procedure_status is None:
                self.context.finish_attempt(
                    attempt_id,
                    ProcedureStatus.COMPLETED,
                    AcquisitionStatus.PARTIAL,
                    reason=reason,
                )
        self.context.complete_open_attempts(AcquisitionStatus.ACQUIRED)

    def preserve_failure(self, reason: str) -> None:
        attempt = next((row for row in reversed(self.context.attempts)
                        if row.procedure_status is None), None)
        if attempt is None:
            return
        retained = self.context.linked_action(
            "retain_notesnook_failure", attempt.attempt_id,
            source_action=self.context.journal.actions[-1] if self.context.journal.actions else self.device.last_action,
            context=dict(attempt.context),
            details={"failure_reason": reason},
        )
        observation_id = None
        details = {}
        try:
            snapshot = self.device.observe(f"observation-failure-{attempt.attempt_id}")["observation_id"]
            screen, _ = self.context.retain_observation(
                f"notesnook/failures/{attempt.attempt_id}",
                self.device.read_observation(snapshot, "screen_image"),
                self.device.read_observation(snapshot, "ui_tree"),
                attempt_id=attempt.attempt_id, action=retained,
                context=dict(attempt.context), source_snapshot_id=f"notesnook-snapshot:{snapshot}",
            )
            observation_id = screen.observation_id
        except Exception as error:
            details["failure_evidence_error"] = f"{type(error).__name__}: {error}"
            self.context.journal.append(
                "failure_evidence_unavailable", status="failed", attempt_id=attempt.attempt_id,
                details={"original_reason": reason, **details},
            )
        status = self.context.result_status(attempt.attempt_id)
        self.context.finish_attempt(
            attempt.attempt_id, ProcedureStatus.INTERRUPTED,
            AcquisitionStatus.PARTIAL if status is not AcquisitionStatus.NOT_ACQUIRED else status,
            reason=reason, action=retained, observation_id=observation_id, details=details,
        )

    def write_record(
        self,
        relative_path: str,
        record: Mapping[str, object],
        *,
        action_id: str,
        observation_id: str | None = None,
        include_observation: bool = False,
    ) -> None:
        path = PurePosixPath(relative_path)
        if (
            not isinstance(relative_path, str)
            or "\x00" in relative_path
            or path.is_absolute()
            or ".." in path.parts
            or not path.parts
            or path.parts[0] != "materialize"
            or path.suffix != ".json"
            or include_observation and not observation_id
        ):
            raise NotesnookAdapterError("output path is invalid")
        try:
            action = self.device.actions[action_id]
        except KeyError:
            raise NotesnookAdapterError("output action is unavailable") from None
        values = [(path.as_posix(), self._json(record), "ui_record")]
        if include_observation:
            assert observation_id is not None
            prefix = path.with_suffix("").as_posix()
            values.extend((
                (
                    f"{prefix}-screen.png",
                    self.device.read_observation(
                        observation_id, "screen_image"
                    ),
                    "screen_image",
                ),
                (
                    f"{prefix}-screen.xml",
                    self.device.read_observation(observation_id, "ui_tree"),
                    "ui_hierarchy",
                ),
            ))
        context = {
            key: record[key]
            for key in (
                "target_ref",
                "note_ref",
                "version_ref",
                "attachment_ref",
                "trash_ref",
            )
            if key in record
        }
        if observation_id is not None:
            context["source_snapshot_id"] = f"notesnook-snapshot:{observation_id}"
        if "version_ref" in record:
            item_type, ref = "revision", record["version_ref"]
        elif "trash" in path.parts:
            item_type, ref = "trash_item", record.get("trash_ref", path.stem)
        else:
            item_type, ref = "note", record.get("note_ref", path.stem)
        aggregate = path.as_posix() in {
            "materialize/summary.json",
            "materialize/trash/trash.json",
        }
        if aggregate and self.context.attempts:
            attempt_id = self.context.attempts[-1].attempt_id
            retained = self.context.linked_action(
                "retain_notesnook_output",
                attempt_id,
                source_action=action,
                context=context,
            )
        else:
            note_ref = str(record.get("note_ref", ""))
            attempt_id, retained = self._attempt(
                f"{item_type}:{note_ref}:{ref}",
                "notesnook.notes",
                item_type,
                action,
                context,
            )
        self.context.artifacts.write_bytes(
            f"notesnook/{values[0][0]}",
            values[0][1],
            kind=values[0][2],
            attempt_id=attempt_id,
            action=retained,
            context=context,
        )
        if include_observation:
            self.context.retain_observation(
                f"notesnook/{path.with_suffix('').as_posix()}-screen",
                values[1][1],
                values[2][1],
                attempt_id=attempt_id,
                action=retained,
                context=context,
            )
        if record.get("content_status") == "partial":
            self._partial_attempts[attempt_id] = (
                "notesnook_note_content_partial"
            )

    def write_account(
        self,
        *,
        record: Mapping[str, object],
        action_id: str,
        observation_id: str,
    ) -> None:
        try:
            action = self.device.actions[action_id]
            values = (
                ("account.json", self._json(record), "ui_record"),
                (
                    "account-screen.png",
                    self.device.read_observation(observation_id, "screen_image"),
                    "screen_image",
                ),
                (
                    "account-screen.xml",
                    self.device.read_observation(observation_id, "ui_tree"),
                    "ui_hierarchy",
                ),
            )
        except KeyError:
            raise NotesnookAdapterError("output action is unavailable") from None
        context = {
            "target_ref": self.target_ref,
            "source_snapshot_id": f"notesnook-snapshot:{observation_id}",
        }
        attempt_id, retained = self._attempt(
            f"account:{self.target_ref}",
            "notesnook.notes",
            "account",
            action,
            context,
        )
        self.context.artifacts.write_bytes(
            "notesnook/account/account.json",
            values[0][1],
            kind=values[0][2],
            attempt_id=attempt_id,
            action=retained,
            context=context,
        )
        self.context.retain_observation(
            "notesnook/account/account-screen",
            values[1][1],
            values[2][1],
            attempt_id=attempt_id,
            action=retained,
            context=context,
        )

    def write_attachment(
        self,
        *,
        note_ref: str,
        attachment_ref: str,
        filename: str,
        payload: bytes,
        record: Mapping[str, object],
        action_id: str,
        observation_id: str,
    ) -> None:
        if (
            PurePosixPath(filename).name != filename
            or "/" in note_ref
            or "/" in attachment_ref
            or not note_ref
            or not attachment_ref
        ):
            raise NotesnookAdapterError("attachment output path is invalid")
        try:
            action = self.device.actions[action_id]
        except KeyError:
            raise NotesnookAdapterError("output action is unavailable") from None
        prefix = (
            f"notesnook/materialize/notes/{note_ref}/attachments/"
            f"{attachment_ref}"
        )
        context = {
            "target_ref": self.target_ref,
            "note_ref": note_ref,
            "attachment_ref": attachment_ref,
            "source_snapshot_id": f"notesnook-snapshot:{observation_id}",
        }
        values = (
            (filename, payload, "app_materialized_file"),
            ("attachment.json", self._json(record), "ui_record"),
            (
                "attachment-screen.png",
                self.device.read_observation(
                    observation_id, "screen_image"
                ),
                "screen_image",
            ),
            (
                "attachment-screen.xml",
                self.device.read_observation(observation_id, "ui_tree"),
                "ui_hierarchy",
            ),
        )
        attempt_id, retained = self._attempt(
            f"attachment:{note_ref}:{attachment_ref}",
            "notesnook.attachments",
            "attachment",
            action,
            context,
        )
        screen, _ = self.context.retain_observation(
            f"{prefix}/attachment-screen",
            values[2][1],
            values[3][1],
            attempt_id=attempt_id,
            action=retained,
            context=context,
        )
        for name, value, kind in values[:2]:
            self.context.artifacts.write_bytes(
                f"{prefix}/{name}", value, kind=kind, attempt_id=attempt_id,
                action=retained, observation_id=screen.observation_id, context=context,
            )

    def write_export(
        self,
        *,
        payload: bytes,
        record: Mapping[str, object],
        audit: Mapping[str, object],
        action_id: str,
        observation_id: str,
    ) -> None:
        try:
            action = self.device.actions[action_id]
            values = (
                ("export.zip", payload, "app_export"),
                ("export.json", self._json(record), "ui_record"),
                (
                    "export-screen.png",
                    self.device.read_observation(observation_id, "screen_image"),
                    "screen_image",
                ),
                (
                    "export-screen.xml",
                    self.device.read_observation(observation_id, "ui_tree"),
                    "ui_hierarchy",
                ),
                ("audit.json", self._json(audit), "audit_record"),
            )
        except KeyError:
            raise NotesnookAdapterError("output action is unavailable") from None
        context = {
            "target_ref": self.target_ref,
            "source_snapshot_id": f"notesnook-snapshot:{observation_id}",
            "comparison_ref": self.target_ref,
        }
        attempt_id, retained = self._attempt(
            f"export:{self.target_ref}",
            "notesnook.export",
            "note_export",
            action,
            context,
        )
        screen, _ = self.context.retain_observation(
            "notesnook/export/export-screen",
            values[2][1],
            values[3][1],
            attempt_id=attempt_id,
            action=retained,
            context=context,
        )
        for name, value, kind in values[:2]:
            self.context.artifacts.write_bytes(
                f"notesnook/export/{name}", value, kind=kind, attempt_id=attempt_id,
                action=retained, observation_id=screen.observation_id, context=context,
            )
        self.context.artifacts.write_bytes(
            "notesnook/export/audit.json",
            values[4][1],
            kind=values[4][2],
            attempt_id=attempt_id,
            action=retained,
            context=context,
        )

    @staticmethod
    def _json(value: Mapping[str, object]) -> bytes:
        value = source_snapshot_references(value, "notesnook")
        try:
            return (
                json.dumps(
                    value,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            ).encode("utf-8")
        except (TypeError, ValueError):
            raise NotesnookAdapterError("output JSON is invalid") from None

from __future__ import annotations

import json
import os
import time
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .artifacts import ArtifactStore
from .device import Device
from .journal import EventJournal
from .models import (
    AcquisitionItem,
    AcquisitionOutcome,
    AcquisitionStatus,
    ActionRef,
    AttemptRecord,
    IdentificationRecord,
    ObservationRecord,
    Outcome,
    OutcomeStatus,
    ProcedureStatus,
    Route,
    RunResult,
)
from .profiles import AppProfile, SystemUIProfile
from .session import capture_environment
from .system_ui import SystemUIRuntime
from .ui import UiRuntime


Collector = Callable[["RunContext"], Outcome]


def source_snapshot_references(value: Any, namespace: str) -> Any:
    if isinstance(value, Mapping):
        result = {}
        for key, child in value.items():
            if key.endswith("observation_id") and isinstance(child, str) and not (
                key == "observation_id" and "screen_artifact_id" in value
            ):
                result[key.removeprefix("source_").replace("observation_id", "source_snapshot_id")] = f"{namespace}-snapshot:{child}"
            elif key.endswith("observation_ids") and isinstance(child, (list, tuple)):
                result[key.replace("observation_ids", "source_snapshot_ids")] = [f"{namespace}-snapshot:{item}" for item in child]
            else:
                result[key] = source_snapshot_references(child, namespace)
        return result
    if isinstance(value, (list, tuple)):
        return [source_snapshot_references(child, namespace) for child in value]
    return value


class AcquisitionRunError(RuntimeError):
    def __init__(self, run_dir: Path, message: str):
        super().__init__(f"acquisition failed in {run_dir}: {message}")
        self.run_dir = run_dir


class AcquisitionInterrupted(KeyboardInterrupt):
    def __init__(self, run_dir: Path, message: str):
        super().__init__(message)
        self.run_dir = run_dir


class RunContext:
    def __init__(
        self,
        *,
        run_dir: Path,
        base_context: dict[str, Any],
        device: Device,
        journal: EventJournal,
        artifacts: ArtifactStore,
        ui: UiRuntime,
        app_profile: AppProfile,
        system_ui_profile: SystemUIProfile,
    ):
        self.run_dir = run_dir
        self.base_context = dict(base_context)
        self.device = device
        self.journal = journal
        self.artifacts = artifacts
        self.ui = ui
        self.app_profile = app_profile
        self.system_ui_profile = system_ui_profile
        self._items: list[AcquisitionItem] = []
        self._attempts: list[AttemptRecord] = []
        self._outcomes: list[AcquisitionOutcome] = []
        self._observations: list[ObservationRecord] = []
        self._item_attempts: dict[str, tuple[str, str, str]] = {}
        self._identifications: dict[str, IdentificationRecord] = {}
        for target in app_profile.targets:
            target.validate_rules()
            environment = self.base_context.get("condition", {}).get("acquisition_environment", target.acquisition_environments[0])
            if environment in target.acquisition_environments:
                self._identifications[target.target_id] = IdentificationRecord(
                    target.target_id, environment, "not_attempted", None, None,
                    False, 0, "identification_not_started", {},
                )

    def target(self, target_id: str):
        return next(target for target in self.app_profile.targets if target.target_id == target_id)

    @property
    def identifications(self) -> tuple[IdentificationRecord, ...]:
        return tuple(self._identifications.values())

    def begin_identification(self, target_id: str) -> None:
        target = self.target(target_id)
        current = self._identifications[target_id]
        if current.started_at is not None:
            return
        rules = {name: target.rules[name]["id"] for name in ("identification", "traversal_completion", "error_handling")}
        event = self.journal.append("identification_started", status="started", details={"target_id": target_id, "applied_rules": rules})
        self._identifications[target_id] = replace(current, identification_status="in_progress", started_at=event["timestamp"], reason=None, applied_rules=rules)

    def finish_identification(self, target_id: str, *, completion_condition: str | None = None, reason: str | None = None) -> None:
        current = self._identifications[target_id]
        if current.started_at is None or current.ended_at is not None:
            raise ValueError("identification must be started and open")
        target = self.target(target_id)
        if completion_condition is not None and completion_condition not in target.rules["traversal_completion"]["parameters"]["completion_conditions"]:
            raise ValueError("completion condition is not declared in rules")
        if completion_condition is None and not reason:
            raise ValueError("interrupted identification requires a reason")
        status = "completed" if completion_condition is not None else "interrupted"
        event = self.journal.append("identification_" + status, status=status, details={"target_id": target_id, "completion_condition": completion_condition, "reason": reason})
        self._identifications[target_id] = replace(current, identification_status=status, ended_at=event["timestamp"], traversal_complete=completion_condition is not None, reason=reason, completion_condition=completion_condition)

    def interrupt_identifications(self, reason: str) -> None:
        for record in self.identifications:
            if record.started_at is not None and record.ended_at is None:
                self.finish_identification(record.target_id, reason=reason)

    @property
    def items(self) -> tuple[AcquisitionItem, ...]:
        return tuple(self._items)

    @property
    def attempts(self) -> tuple[AttemptRecord, ...]:
        return tuple(self._attempts)

    @property
    def outcomes(self) -> tuple[AcquisitionOutcome, ...]:
        return tuple(self._outcomes)

    @property
    def observations(self) -> tuple[ObservationRecord, ...]:
        return tuple(self._observations)

    def register_item(
        self,
        target_id: str,
        item_type: str,
        *,
        source: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> str:
        target = next(
            (
                candidate
                for candidate in self.app_profile.targets
                if candidate.target_id == target_id
            ),
            None,
        )
        if target is None:
            raise ValueError(f"unknown acquisition target: {target_id!r}")
        if item_type not in target.item_types:
            raise ValueError(
                f"item type {item_type!r} is not declared by {target_id!r}"
            )
        item_id = f"item-{len(self._items) + 1:06d}"
        self._items.append(
            AcquisitionItem(
                acquisition_item_id=item_id,
                target_id=target_id,
                item_type=item_type,
                source=dict(source or {}),
                context=dict(context or {}),
            )
        )
        counted = target.rules["identification"]["parameters"]["counted_item_types"]
        if item_type in counted and target_id in self._identifications:
            current = self._identifications[target_id]
            if current.started_at is None:
                self.begin_identification(target_id)
                current = self._identifications[target_id]
            self._identifications[target_id] = replace(current, identified_item_count=current.identified_item_count + 1)
        return item_id

    def begin_attempt(
        self,
        acquisition_item_id: str,
        method: Route,
        acquisition_environment: str,
        *,
        context: dict[str, Any] | None = None,
    ) -> str:
        item = next(
            (
                candidate
                for candidate in self._items
                if candidate.acquisition_item_id == acquisition_item_id
            ),
            None,
        )
        if item is None:
            raise ValueError(
                f"unknown acquisition item: {acquisition_item_id!r}"
            )
        target = next(
            candidate
            for candidate in self.app_profile.targets
            if candidate.target_id == item.target_id
        )
        try:
            resolved_method = Route(method)
        except (TypeError, ValueError) as error:
            raise ValueError(f"unknown acquisition method: {method!r}") from error
        if resolved_method is not target.method:
            raise ValueError("acquisition method is not declared by the target")
        if acquisition_environment not in target.acquisition_environments:
            raise ValueError("acquisition environment is not declared by the target")
        attempt_id = f"attempt-{len(self._attempts) + 1:06d}"
        self._attempts.append(
            AttemptRecord(
                attempt_id=attempt_id,
                acquisition_item_id=acquisition_item_id,
                method=resolved_method,
                acquisition_environment=acquisition_environment,
                procedure_status=None,
                acquisition_status=None,
                reason=None,
                context=dict(context or {}),
                details={},
                started_at=AcquisitionRuntime._now(),
                applied_rules={name: rule["id"] for name, rule in target.rules.items()},
            )
        )
        return attempt_id

    def item_attempt(
        self,
        key: str,
        target_id: str,
        item_type: str,
        *,
        source: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
        retention: bool = False,
    ) -> str:
        if not isinstance(key, str) or not key:
            raise ValueError("acquisition item key is required")
        existing = self._item_attempts.get(key)
        if existing is not None:
            attempt_id, existing_target, existing_type = existing
            if (existing_target, existing_type) != (target_id, item_type):
                raise ValueError("acquisition item key is already in use")
            previous = next(attempt for attempt in self._attempts if attempt.attempt_id == attempt_id)
            if retention and previous.procedure_status is not None:
                return attempt_id
            if previous.procedure_status is None:
                if context:
                    index = self._attempts.index(previous)
                    self._attempts[index] = replace(previous, context={**previous.context, **context})
                return attempt_id
            attempt_id = self.begin_attempt(previous.acquisition_item_id, previous.method, previous.acquisition_environment, context=context)
            self._item_attempts[key] = (attempt_id, target_id, item_type)
            return attempt_id
        target = next(
            (
                candidate
                for candidate in self.app_profile.targets
                if candidate.target_id == target_id
            ),
            None,
        )
        if target is None:
            raise ValueError(f"unknown acquisition target: {target_id!r}")
        condition = self.base_context.get("condition", {})
        acquisition_environment = (
            condition.get("acquisition_environment")
            if isinstance(condition, Mapping)
            else None
        )
        acquisition_environment = (
            acquisition_environment
            if isinstance(acquisition_environment, str)
            else target.acquisition_environments[0]
        )
        item_id = self.register_item(
            target_id,
            item_type,
            source=source,
            context=context,
        )
        attempt_id = self.begin_attempt(
            item_id,
            target.method,
            acquisition_environment,
            context=context,
        )
        self._item_attempts[key] = (attempt_id, target_id, item_type)
        return attempt_id

    def linked_action(
        self,
        name: str,
        attempt_id: str,
        *,
        source_action: ActionRef | None = None,
        context: dict[str, Any] | None = None,
        details: dict[str, Any] | None = None,
    ) -> ActionRef:
        attempt = next(record for record in self.attempts if record.attempt_id == attempt_id)
        if attempt.procedure_status is not None:
            raise ValueError("cannot retain output for a closed attempt")
        linked = dict(context or {})
        if source_action is not None:
            linked["source_action_id"] = source_action.action_id
        return self.journal.record_action(
            name,
            status="success",
            attempt_id=attempt_id,
            context=linked,
            details=details,
        )

    def complete_open_attempts(
        self,
        acquisition_status: AcquisitionStatus,
        *,
        reason: str | None = None,
        attempt_ids: set[str] | None = None,
    ) -> None:
        status = AcquisitionStatus(acquisition_status)
        for attempt in tuple(self._attempts):
            if attempt.procedure_status is not None or (attempt_ids is not None and attempt.attempt_id not in attempt_ids):
                continue
            actual = self.result_status(attempt.attempt_id) if status is AcquisitionStatus.ACQUIRED else status
            self.finish_attempt(
                attempt.attempt_id,
                ProcedureStatus.COMPLETED if actual is AcquisitionStatus.ACQUIRED else ProcedureStatus.INTERRUPTED,
                actual,
                reason=reason or ("required_artifacts_missing" if actual is not AcquisitionStatus.ACQUIRED else None),
            )

    def result_status(self, attempt_id: str) -> AcquisitionStatus:
        attempt = next(record for record in self.attempts if record.attempt_id == attempt_id)
        item = next(record for record in self.items if record.acquisition_item_id == attempt.acquisition_item_id)
        required = self.target(item.target_id).rules["result"]["parameters"]["required_artifacts"]
        kinds = {record.kind for record in self.artifacts.records if record.attempt_id == attempt_id}
        if all((set(alternatives) <= kinds) if name == "ui_observation" else kinds.intersection(alternatives)
               for name, alternatives in required.items()):
            return AcquisitionStatus.ACQUIRED
        data_kinds = {kind for name, alternatives in required.items() if name != "ui_observation" for kind in alternatives}
        return AcquisitionStatus.PARTIAL if kinds.intersection(data_kinds) else AcquisitionStatus.NOT_ACQUIRED

    def register_collection_records(self, target_id: str, artifact, records: list[dict[str, Any]], *, item_type: str | None = None, record_key: str = "records") -> None:
        counted = self.target(target_id).rules["identification"]["parameters"]["counted_item_types"]
        if item_type is None:
            item_type, = counted
        if item_type not in counted or record_key not in {"records", "accounts"}:
            raise ValueError("unsupported collection row binding")
        for index, record in enumerate(records):
            self.register_item(target_id, item_type, source={
                "collection_attempt_id": artifact.attempt_id,
                "artifact_id": artifact.artifact_id,
                "record_index": index,
                **({"record_key": record_key} if record_key != "records" else {}),
            })

    def finish_fallback(self, attempt_id: str, fallback: str, condition: str, *, reason: str | None = None,
                        action: ActionRef | None = None, observation_id: str | None = None) -> None:
        attempt = next(record for record in self.attempts if record.attempt_id == attempt_id)
        item = next(record for record in self.items if record.acquisition_item_id == attempt.acquisition_item_id)
        rule = self.target(item.target_id).rules["error_handling"]["parameters"]["fallbacks"].get(fallback)
        kinds = {record.kind for record in self.artifacts.records if record.attempt_id == attempt_id}
        if rule is None or condition not in rule["conditions"] or not set(rule["required_artifacts"]) <= kinds:
            raise ValueError("fallback condition or required evidence is not satisfied")
        self.finish_attempt(attempt_id, ProcedureStatus.COMPLETED, AcquisitionStatus(rule["acquisition_status"]),
                            reason=reason or condition, action=action, observation_id=observation_id,
                            details={"fallback": fallback, "fallback_condition": condition})

    def finish_attempt(
        self,
        attempt_id: str,
        procedure_status: ProcedureStatus,
        acquisition_status: AcquisitionStatus | None = None,
        *,
        reason: str | None = None,
        action: ActionRef | None = None,
        observation_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> AttemptRecord:
        index = next(
            (
                index
                for index, candidate in enumerate(self._attempts)
                if candidate.attempt_id == attempt_id
            ),
            None,
        )
        if index is None:
            raise ValueError(f"unknown attempt: {attempt_id!r}")
        current = self._attempts[index]
        if current.procedure_status is not None:
            raise ValueError(f"attempt is already finished: {attempt_id!r}")
        try:
            procedure = ProcedureStatus(procedure_status)
            acquisition = (
                None
                if acquisition_status is None
                else AcquisitionStatus(acquisition_status)
            )
        except (TypeError, ValueError) as error:
            raise ValueError("attempt status is invalid") from error
        if procedure is ProcedureStatus.NOT_ATTEMPTED:
            if acquisition is not None:
                raise ValueError(
                    "not_attempted must not have acquisition_status"
                )
            if not reason:
                raise ValueError("not_attempted reason is required")
        elif acquisition is None:
            raise ValueError(
                "completed and interrupted attempts require acquisition_status"
            )
        if action is not None and action.attempt_id != attempt_id:
            raise ValueError("outcome action must reference the same attempt")
        if action is not None and not any(
            recorded.action_id == action.action_id
            for recorded in self.journal.actions
        ):
            raise ValueError("outcome references an unknown action")
        if observation_id is not None and not any(
            record.observation_id == observation_id
            and record.attempt_id == attempt_id
            for record in self._observations
        ):
            raise ValueError("unknown observation for attempt")
        if acquisition in {
            AcquisitionStatus.PARTIAL,
            AcquisitionStatus.NOT_ACQUIRED,
        } and not reason:
            raise ValueError("incomplete acquisition reason is required")

        if acquisition is AcquisitionStatus.ACQUIRED:
            acquisition = self.result_status(attempt_id)
            if acquisition is not AcquisitionStatus.ACQUIRED:
                reason = reason or "required_artifacts_missing"

        finished = replace(
            current,
            procedure_status=procedure,
            acquisition_status=acquisition,
            reason=reason,
            details=dict(details or {}),
            started_at=None if procedure is ProcedureStatus.NOT_ATTEMPTED else current.started_at,
            ended_at=None if procedure is ProcedureStatus.NOT_ATTEMPTED else AcquisitionRuntime._now(),
        )
        self._attempts[index] = finished
        self.journal.append("attempt_finished", status=procedure.value, attempt_id=attempt_id,
                            details={"started_at": finished.started_at, "ended_at": finished.ended_at, "acquisition_status": acquisition, "reason": reason})
        if acquisition in {
            AcquisitionStatus.PARTIAL,
            AcquisitionStatus.NOT_ACQUIRED,
        }:
            outcome = AcquisitionOutcome(
                outcome_id=f"outcome-{len(self._outcomes) + 1:06d}",
                attempt_id=attempt_id,
                acquisition_status=acquisition,
                reason=reason or "",
                action_id=action.action_id if action is not None else None,
                observation_id=observation_id,
                context=dict(current.context),
                details=dict(details or {}),
            )
            self._outcomes.append(outcome)
            self.journal.append(
                "acquisition_outcome",
                status=acquisition.value,
                attempt_id=attempt_id,
                details={
                    "outcome_id": outcome.outcome_id,
                    "reason": outcome.reason,
                    "action_id": outcome.action_id,
                    "observation_id": outcome.observation_id,
                    **outcome.details,
                },
            )
        return finished

    def start_app(self) -> bool:
        package_name = self.app_profile.package_name
        package_selector = {"packageName": package_name}
        for _ in range(2):
            self.device.app_start(package_name)
            if not self.ui.wait_for(package_selector):
                continue
            time.sleep(
                self.app_profile.timings.get("application_start_settle", 0.0)
            )
            previous = None
            stable_polls = 0

            def settled() -> bool:
                nonlocal previous, stable_polls
                current = self.device.hierarchy()
                try:
                    root = ET.fromstring(current)
                except (ET.ParseError, TypeError):
                    previous = None
                    stable_polls = 0
                    return False
                if not any(
                    node.get("package") == package_name for node in root.iter()
                ):
                    previous = None
                    stable_polls = 0
                    return False
                stable_polls = stable_polls + 1 if current == previous else 1
                previous = current
                return stable_polls >= 2

            if self.ui.wait_until(settled):
                return True
        return False

    def capture_observation(
        self,
        name: str,
        *,
        attempt_id: str,
        action: ActionRef,
        context: dict[str, Any] | None = None,
    ):
        return self.retain_observation(
            f"observations/{name}",
            self.device.screenshot(),
            self.device.hierarchy().encode("utf-8"),
            attempt_id=attempt_id,
            action=action,
            context=context,
        )

    def retain_observation(
        self,
        prefix: str,
        screen: bytes,
        hierarchy: bytes,
        *,
        attempt_id: str,
        action: ActionRef,
        context: dict[str, Any] | None = None,
        source_snapshot_id: str | None = None,
    ):
        source_snapshot_id = source_snapshot_id or (context or {}).get("source_snapshot_id")
        if not any(record.attempt_id == attempt_id for record in self._attempts):
            raise ValueError(f"unknown attempt: {attempt_id!r}")
        if action.attempt_id != attempt_id:
            raise ValueError("observation action must reference the same attempt")
        observation_id = f"observation-{len(self._observations) + 1:06d}"
        screenshot = self.artifacts.write_bytes(
            f"{prefix}.png",
            screen,
            kind="screen_image",
            attempt_id=attempt_id,
            action=action,
            observation_id=observation_id,
            context=context,
        )
        hierarchy = self.artifacts.write_bytes(
            f"{prefix}.xml",
            hierarchy,
            kind="ui_hierarchy",
            attempt_id=attempt_id,
            action=action,
            observation_id=observation_id,
            context=context,
        )
        self._observations.append(
            ObservationRecord(
                observation_id=observation_id,
                attempt_id=attempt_id,
                action_id=action.action_id,
                screen_artifact_id=screenshot.artifact_id,
                hierarchy_artifact_id=hierarchy.artifact_id,
                source_snapshot_id=source_snapshot_id,
            )
        )
        return screenshot, hierarchy

    def interrupt_open_attempts(
        self,
        reason: str,
        acquisition_status: AcquisitionStatus = AcquisitionStatus.NOT_ACQUIRED,
    ) -> None:
        status = AcquisitionStatus(acquisition_status)
        for attempt in tuple(self._attempts):
            if attempt.procedure_status is None:
                self.finish_attempt(
                    attempt.attempt_id,
                    ProcedureStatus.INTERRUPTED,
                    AcquisitionStatus.PARTIAL if self.result_status(attempt.attempt_id) is not AcquisitionStatus.NOT_ACQUIRED else AcquisitionStatus.NOT_ACQUIRED,
                    reason=reason,
                )

    def validate_records(self) -> None:
        if any(attempt.procedure_status is None for attempt in self._attempts):
            raise ValueError("all acquisition attempts must be finished")
        item_ids = {item.acquisition_item_id for item in self._items}
        attempt_ids = {attempt.attempt_id for attempt in self._attempts}
        attempts_by_id = {attempt.attempt_id: attempt for attempt in self._attempts}
        with self.journal.path.open(encoding="utf-8") as stream:
            for line in stream:
                event = json.loads(line)
                if event["event_type"] not in {"action", "artifact_retained"}:
                    continue
                attempt_id = event.get("attempt_id", event.get("details", {}).get("attempt_id"))
                attempt = attempts_by_id.get(attempt_id)
                if attempt is not None and attempt.started_at is not None and attempt.ended_at is not None and not attempt.started_at <= event["timestamp"] <= attempt.ended_at:
                    raise ValueError("retained action or artifact falls outside its attempt interval")
        action_ids = {action.action_id for action in self.journal.actions}
        observation_ids = {
            observation.observation_id for observation in self._observations
        }
        artifact_ids = {
            artifact.artifact_id for artifact in self.artifacts.records
        }
        if any(
            attempt.acquisition_item_id not in item_ids
            for attempt in self._attempts
        ):
            raise ValueError("attempt contains an unknown item reference")
        if any(
            action.attempt_id is not None
            and action.attempt_id not in attempt_ids
            for action in self.journal.actions
        ):
            raise ValueError("action contains an unknown attempt reference")
        if any(
            artifact.attempt_id not in attempt_ids
            or artifact.action_id not in action_ids
            or artifact.observation_id is not None
            and artifact.observation_id not in observation_ids
            for artifact in self.artifacts.records
        ):
            raise ValueError("artifact contains an unknown record reference")
        if any(
            observation.attempt_id not in attempt_ids
            or observation.action_id not in action_ids
            or observation.screen_artifact_id not in artifact_ids
            or observation.hierarchy_artifact_id not in artifact_ids
            for observation in self._observations
        ):
            raise ValueError("observation contains an unknown record reference")
        artifact_by_id = {record.artifact_id: record for record in self.artifacts.records}
        action_by_id = {record.action_id: record for record in self.journal.actions}
        for observation in self.observations:
            for artifact_id, kind in ((observation.screen_artifact_id, "screen_image"), (observation.hierarchy_artifact_id, "ui_hierarchy")):
                artifact = artifact_by_id[artifact_id]
                if (artifact.kind != kind or artifact.attempt_id != observation.attempt_id
                    or artifact.action_id != observation.action_id or artifact.observation_id != observation.observation_id
                    or action_by_id[observation.action_id].attempt_id != observation.attempt_id):
                    raise ValueError("observation PNG/XML/action must reference the same attempt")
        if any(
            outcome.attempt_id not in attempt_ids
            or outcome.action_id is not None
            and outcome.action_id not in action_ids
            or outcome.observation_id is not None
            and outcome.observation_id not in observation_ids
            for outcome in self._outcomes
        ):
            raise ValueError("outcome contains an unknown record reference")
        identifiers = [
            *item_ids,
            *attempt_ids,
            *action_ids,
            *observation_ids,
            *artifact_ids,
            *(outcome.outcome_id for outcome in self._outcomes),
        ]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("provenance identifiers must be unique")


class AcquisitionRuntime:
    def __init__(self, runs_root: str | Path, device: Device):
        self.runs_root = Path(runs_root)
        self.device = device

    def run(
        self,
        app_profile: AppProfile,
        system_ui_profile: SystemUIProfile,
        route: Route,
        condition: Mapping[str, Any],
        collector: Collector,
        *,
        run_id: str | None = None,
        prepared_session_id: str | None = None,
        installed_app_version: str | None = None,
    ) -> RunResult:
        route = Route(route)
        if route not in app_profile.routes:
            raise ValueError(
                f"Route {route.value!r} is not declared by {app_profile.app_id}"
            )
        android_version = self.device.shell("getprop ro.build.version.release").strip() or None
        if app_profile.android_versions is not None:
            if not app_profile.supports_android(android_version):
                raise ValueError(f"Profile is not applicable to Android {android_version!r}")
        if not isinstance(condition, Mapping):
            raise ValueError("condition must be an object")
        condition_value = dict(condition)
        try:
            json.dumps(condition_value)
        except (TypeError, ValueError) as error:
            raise ValueError("condition must contain JSON values") from error
        notifications = condition_value.get("notifications")
        if "notifications" in condition_value and notifications != "suppress_all":
            raise ValueError(
                "condition.notifications must be 'suppress_all' when present"
            )
        acquisition_environment = condition_value.get("acquisition_environment")
        if "acquisition_environment" in condition_value and (
            not isinstance(acquisition_environment, str)
            or acquisition_environment not in {"device_only", "controlled_online"}
        ):
            raise ValueError("invalid acquisition environment")
        if prepared_session_id is not None:
            self._validate_run_id(prepared_session_id)
            if (
                notifications != "suppress_all"
                or condition_value.get("airplane_mode") != "enabled"
                or acquisition_environment
                not in {"device_only", "controlled_online"}
            ):
                raise ValueError(
                    "prepared sessions require suppressed notifications, "
                    "enabled airplane mode, and a valid acquisition environment"
                )
        bluetooth = condition_value.get("bluetooth")
        bluetooth_target_name = None
        if "bluetooth" in condition_value:
            if (
                not isinstance(bluetooth, Mapping)
                or not isinstance(bluetooth.get("target_name"), str)
                or not bluetooth["target_name"].strip()
            ):
                raise ValueError(
                    "condition.bluetooth.target_name must be a non-empty string"
                )
            bluetooth_target_name = bluetooth["target_name"].strip()

        resolved_run_id = run_id or self._new_run_id()
        self._validate_run_id(resolved_run_id)
        run_dir = self.runs_root / resolved_run_id
        run_dir.mkdir(parents=True, exist_ok=False)

        started_at = self._now()
        installed_version = installed_app_version
        if installed_version is None:
            try:
                installed_version = next((package.version_name for package in self.device.installed_packages() if package.package_name == app_profile.package_name), None)
            except Exception:
                installed_version = None
        exact_profile_match = installed_version == app_profile.app_version
        base_context = {
            "run_id": resolved_run_id,
            "app_id": app_profile.app_id,
            "package_name": app_profile.package_name,
            "app_version": app_profile.app_version,
            "installed_app_version": installed_version,
            "profile_app_version": app_profile.app_version,
            "app_profile_exact_match": exact_profile_match,
            "system_ui_profile": system_ui_profile.profile_id,
            "route": route.value,
            "condition": condition_value,
            "device_model": self.device.shell("getprop ro.product.model").strip() or None,
            "android_version": android_version,
        }
        if prepared_session_id is not None:
            base_context["device_session_id"] = prepared_session_id
        journal = EventJournal(run_dir / "events.jsonl", base_context)
        artifacts = ArtifactStore(run_dir, journal)
        ui = UiRuntime(
            self.device,
            journal,
            default_timeout=app_profile.timings.get("default_timeout", 5.0),
            poll_interval=app_profile.timings.get("poll_interval", 0.2),
        )
        context = RunContext(
            run_dir=run_dir,
            base_context=base_context,
            device=self.device,
            journal=journal,
            artifacts=artifacts,
            ui=ui,
            app_profile=app_profile,
            system_ui_profile=system_ui_profile,
        )
        if installed_version is None:
            journal.append("app_profile_version_unknown", status="warning", details={"profile_app_version": app_profile.app_version})
        elif not exact_profile_match:
            journal.record_action(
                "app_profile_version_mismatch",
                status="warning",
                details={
                    "installed_app_version": installed_version,
                    "profile_app_version": app_profile.app_version,
                },
            )
        journal.append("run_started", status="success")

        system_ui = (
            SystemUIRuntime(
                self.device,
                system_ui_profile,
                journal,
                timeout=app_profile.timings.get("default_timeout", 5.0),
                poll_interval=app_profile.timings.get("poll_interval", 0.2),
                transition_settle=app_profile.timings.get(
                    "transition_settle", 0.5
                ),
            )
            if prepared_session_id is not None
            or notifications == "suppress_all"
            or bluetooth_target_name is not None
            else None
        )
        bluetooth_snapshot = None
        run_error = None
        failure_action = None
        failure_stage = "collector"
        outcome = None

        try:
            if system_ui is not None:
                failure_stage = "system_ui_prepare"
                if prepared_session_id is not None:
                    system_ui.verify_prepared_environment(None)
                    system_ui.apply_acquisition_environment(acquisition_environment)
                    observed = system_ui.verify_prepared_environment(acquisition_environment)
                    base_context["verified_state"] = capture_environment(lambda: observed)
                    context.base_context["verified_state"] = base_context["verified_state"]
                elif notifications == "suppress_all":
                    system_ui.prepare_notification_suppression()
                if bluetooth_target_name is not None:
                    bluetooth_snapshot = system_ui.prepare_bluetooth_delivery(
                        bluetooth_target_name
                    )
            failure_stage = "collector"
            outcome = collector(context)
            self._validate_outcome(outcome)
            context.interrupt_identifications("collector_returned_before_traversal_completion")
            context.validate_records()
        except (Exception, KeyboardInterrupt) as error:
            run_error = error
            failure_action = journal.record_action(
                f"{failure_stage}_exception",
                status="failed",
                details={
                    "error_type": type(error).__name__,
                    "error": str(error),
                },
            )
        finally:
            if bluetooth_snapshot is not None and system_ui is not None:
                try:
                    system_ui.restore_bluetooth_delivery(bluetooth_snapshot)
                except (Exception, KeyboardInterrupt) as restore_error:
                    restore_action = journal.record_action(
                        "system_ui_restore_exception",
                        status="failed",
                        details={
                            "error_type": type(restore_error).__name__,
                            "error": str(restore_error),
                        },
                    )
                    if run_error is None or isinstance(restore_error, KeyboardInterrupt):
                        run_error = restore_error
                        failure_stage = "system_ui_restore"
                        failure_action = restore_action
            if system_ui is not None:
                base_context["final_state"] = capture_environment(
                    system_ui.snapshot_environment, base_context.get("verified_state")
                )
                if base_context["final_state"].get("error_type") == "KeyboardInterrupt":
                    run_error = KeyboardInterrupt()
                    failure_action = journal.record_action(
                        "system_ui_final_query_exception", status="interrupted",
                        details=base_context["final_state"],
                    )

        if run_error is not None:
            assert failure_action is not None
            context.interrupt_identifications(str(run_error) or type(run_error).__name__)
            for attempt in context.attempts:
                if attempt.procedure_status is None:
                    context.finish_attempt(
                        attempt.attempt_id,
                        ProcedureStatus.INTERRUPTED,
                        AcquisitionStatus.PARTIAL if context.result_status(attempt.attempt_id) is not AcquisitionStatus.NOT_ACQUIRED else AcquisitionStatus.NOT_ACQUIRED,
                        reason=str(run_error) or type(run_error).__name__,
                    )
            outcome = Outcome(
                OutcomeStatus.INTERRUPTED if isinstance(run_error, KeyboardInterrupt) else OutcomeStatus.FAILED,
                str(run_error) or type(run_error).__name__,
                action=failure_action,
            )
            journal.append(
                "run_finished",
                status=outcome.status.value,
                details={"reason": outcome.reason},
            )
            self._write_acquisition(
                run_dir,
                base_context,
                started_at,
                self._now(),
                outcome,
                artifacts,
                context,
            )
            if isinstance(run_error, KeyboardInterrupt):
                raise AcquisitionInterrupted(run_dir, outcome.reason) from run_error
            raise AcquisitionRunError(run_dir, str(run_error)) from run_error

        assert outcome is not None

        journal.append(
            "run_finished",
            status=outcome.status.value,
            details={"reason": outcome.reason},
        )
        self._write_acquisition(
            run_dir,
            base_context,
            started_at,
            self._now(),
            outcome,
            artifacts,
            context,
        )
        return RunResult(
            run_dir=run_dir,
            outcome=outcome,
            items=context.items,
            attempts=context.attempts,
            outcomes=context.outcomes,
            observations=context.observations,
            artifacts=artifacts.records,
        )

    @staticmethod
    def _validate_outcome(outcome: Outcome) -> None:
        if not isinstance(outcome, Outcome):
            raise TypeError("collector must return Outcome")
        if (
            not isinstance(outcome.status, OutcomeStatus)
            or outcome.reason is not None
            and not isinstance(outcome.reason, str)
            or outcome.action is not None
            and not isinstance(outcome.action, ActionRef)
            or not isinstance(outcome.context, dict)
            or not isinstance(outcome.details, dict)
        ):
            raise TypeError("collector returned an invalid Outcome")

    @staticmethod
    def _write_acquisition(
        run_dir: Path,
        run: dict[str, Any],
        started_at: str,
        ended_at: str,
        outcome: Outcome,
        artifacts: ArtifactStore,
        context: RunContext,
    ) -> None:
        context.validate_records()
        document = {
            "schema_version": 2,
            "profile": asdict(context.app_profile),
            "run": run,
            "started_at": started_at,
            "ended_at": ended_at,
            "outcome": {
                "status": outcome.status.value,
                "reason": outcome.reason,
                "action_id": (
                    outcome.action.action_id if outcome.action is not None else None
                ),
                "action": (
                    outcome.action.action if outcome.action is not None else None
                ),
                "context": dict(outcome.context),
                "details": dict(outcome.details),
            },
            "items": [asdict(record) for record in context.items],
            "identifications": [asdict(record) for record in context.identifications],
            "attempts": [
                {
                    **asdict(record),
                    "method": record.method.value,
                    "procedure_status": (
                        record.procedure_status.value
                        if record.procedure_status is not None
                        else None
                    ),
                    "acquisition_status": (
                        record.acquisition_status.value
                        if record.acquisition_status is not None
                        else None
                    ),
                }
                for record in context.attempts
            ],
            "outcomes": [
                {
                    **asdict(record),
                    "acquisition_status": record.acquisition_status.value,
                }
                for record in context.outcomes
            ],
            "observations": [
                asdict(record) for record in context.observations
            ],
            "artifacts": [asdict(record) for record in artifacts.records],
        }
        temporary = run_dir / "acquisition.json.tmp"
        destination = run_dir / "acquisition.json"
        encoded = json.dumps(
            document,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(encoded + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)

    @staticmethod
    def _validate_run_id(run_id: str) -> None:
        if (
            not isinstance(run_id, str)
            or not run_id
            or run_id in {".", ".."}
            or "/" in run_id
            or "\\" in run_id
            or Path(run_id).is_absolute()
        ):
            raise ValueError(f"unsafe run_id: {run_id!r}")

    @staticmethod
    def _new_run_id() -> str:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return f"{timestamp}-{uuid.uuid4().hex[:8]}"

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

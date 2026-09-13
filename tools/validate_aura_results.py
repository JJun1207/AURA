#!/usr/bin/env python3
"""Validate an AURA device session without importing AURA acquisition code."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path, PurePosixPath
from zipfile import BadZipFile, ZipFile


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _lookup(index: dict, reference: object) -> dict:
    return index.get(reference, {}) if isinstance(reference, str) else {}


def _reference(index: dict, reference: object, label: str, errors: list[str]):
    record = _lookup(index, reference)
    if not record:
        errors.append(f"{label} {reference}")
    return record


def _object(value: object, label: str, errors: list[str]) -> dict:
    if isinstance(value, dict):
        return value
    errors.append(f"{label} must be an object")
    return {}


def _strings(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) and v for v in value)


def _safe_path(value: object) -> bool:
    return (isinstance(value, str) and bool(value) and "\\" not in value
            and ":" not in value and "\x00" not in value
            and not PurePosixPath(value).is_absolute()
            and all(part not in ("", ".", "..") for part in value.split("/")))


def _timestamp(value: object, label: str, errors: list[str]):
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else None
        if parsed is not None and parsed.utcoffset() is not None:
            return parsed
    except ValueError:
        pass
    errors.append(f"{label} must be a timezone-aware timestamp")
    return None


def _interval(record: dict, label: str, errors: list[str], outer=None):
    start = _timestamp(record.get("started_at"), f"{label} started_at", errors)
    end = _timestamp(record.get("ended_at"), f"{label} ended_at", errors)
    if start is not None and end is not None:
        if start > end or (outer and not outer[0] <= start <= end <= outer[1]):
            errors.append(f"{label} interval is reversed or outside its containing interval")
        return start, end
    return None


def _load_json(value: bytes, label: str, errors: list[str]):
    try:
        return json.loads(value)
    except (UnicodeDecodeError, json.JSONDecodeError):
        errors.append(f"{label} is not valid JSON")
        return None


def _records(document: dict, name: str, errors: list[str]) -> list[dict]:
    value = document.get(name)
    if not isinstance(value, list) or not all(
        isinstance(record, dict) for record in value
    ):
        errors.append(f"acquisition.json field {name} must be a list of objects")
        return []
    return value


def _index(
    records: list[dict], field: str, errors: list[str]
) -> dict[str, dict]:
    result = {}
    for record in records:
        identifier = record.get(field)
        if not isinstance(identifier, str) or not identifier:
            errors.append(f"record is missing {field}")
        elif identifier in result:
            errors.append(f"duplicate identifier {identifier}")
        else:
            result[identifier] = record
    return result


def _archive_path(session_path: Path, value: object) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    return path if path.is_absolute() else session_path.parent / path


def _read_events(value: bytes, label: str, errors: list[str]) -> list[dict]:
    records = []
    try:
        lines = value.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        errors.append(f"{label} is not UTF-8")
        return records
    for number, line in enumerate(lines, 1):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            errors.append(f"{label} line {number} is not valid JSON")
            continue
        if not isinstance(record, dict):
            errors.append(f"{label} line {number} is not an object")
        else:
            records.append(record)
    return records


def _validate_references(
    acquisition: dict,
    actions: dict[str, dict],
    members: dict[str, bytes],
    errors: list[str],
) -> dict[str, dict[str, dict]]:
    items = _index(_records(acquisition, "items", errors), "acquisition_item_id", errors)
    attempts = _index(_records(acquisition, "attempts", errors), "attempt_id", errors)
    observations = _index(
        _records(acquisition, "observations", errors), "observation_id", errors
    )
    artifacts = _index(
        _records(acquisition, "artifacts", errors), "artifact_id", errors
    )
    outcomes = _index(
        _records(acquisition, "outcomes", errors), "outcome_id", errors
    )
    all_identifiers: dict[str, str] = {}
    for kind, records in (
        ("item", items),
        ("attempt", attempts),
        ("action", actions),
        ("observation", observations),
        ("artifact", artifacts),
        ("outcome", outcomes),
    ):
        for identifier in records:
            if identifier in all_identifiers:
                errors.append(f"duplicate identifier {identifier}")
            else:
                all_identifiers[identifier] = kind

    for attempt_id, attempt in attempts.items():
        item_id = attempt.get("acquisition_item_id")
        if not _lookup(items, item_id):
            errors.append(f"{attempt_id} references unknown item {item_id}")
    for action_id, action in actions.items():
        attempt_id = action.get("attempt_id")
        if attempt_id is not None and not _lookup(attempts, attempt_id):
            errors.append(f"{action_id} references unknown attempt {attempt_id}")
        context = action.get("context", {})
        if not isinstance(context, dict):
            errors.append(f"{action_id} context must be an object")
            context = {}
        source_id = context.get("source_action_id")
        if source_id is not None and not _lookup(actions, source_id):
            errors.append(f"{action_id} references unknown action {source_id}")
        seen = set()
        current = action
        while current:
            identifier = current.get("action_id")
            if identifier in seen:
                errors.append(f"{action_id} source action cycle")
                break
            seen.add(identifier)
            source = current.get("context")
            current = _lookup(actions, source.get("source_action_id")) if isinstance(source, dict) else {}
    for observation_id, observation in observations.items():
        for field, known, label in (
            ("attempt_id", attempts, "attempt"),
            ("action_id", actions, "action"),
            ("screen_artifact_id", artifacts, "artifact"),
            ("hierarchy_artifact_id", artifacts, "artifact"),
        ):
            reference = observation.get(field)
            if not _lookup(known, reference):
                errors.append(
                    f"{observation_id} references unknown {label} {reference}"
                )
    for artifact_id, artifact in artifacts.items():
        for field, known, label in (
            ("attempt_id", attempts, "attempt"),
            ("action_id", actions, "action"),
        ):
            reference = artifact.get(field)
            if not _lookup(known, reference):
                errors.append(f"{artifact_id} references unknown {label} {reference}")
        observation_id = artifact.get("observation_id")
        if observation_id is not None and not _lookup(observations, observation_id):
            errors.append(
                f"{artifact_id} references unknown observation {observation_id}"
            )
        path = artifact.get("relative_path")
        if not _safe_path(path):
            errors.append(f"{artifact_id} has an invalid relative_path")
        elif path not in members:
            errors.append(f"{artifact_id} references missing member {path}")
        elif type(artifact.get("size")) is not int or artifact.get("size") != len(members[path]):
            errors.append(f"{artifact_id} size mismatch")
        elif artifact.get("sha256") != _sha256(members[path]):
            errors.append(f"{artifact_id} SHA-256 mismatch")
    for outcome_id, outcome in outcomes.items():
        for field, known, label in (
            ("attempt_id", attempts, "attempt"),
            ("action_id", actions, "action"),
            ("observation_id", observations, "observation"),
        ):
            reference = outcome.get(field)
            if reference is not None and not _lookup(known, reference):
                errors.append(f"{outcome_id} references unknown {label} {reference}")
    return {
        "items": items,
        "attempts": attempts,
        "actions": actions,
        "observations": observations,
        "artifacts": artifacts,
        "outcomes": outcomes,
    }


def _target_rules(target: dict, errors: list[str]) -> dict:
    label = str(target.get("target_id"))
    rules = _object(target.get("rules"), f"{label} rules", errors)
    names = ("identification", "traversal_completion", "result", "error_handling")
    if set(rules) != set(names):
        errors.append(f"{label} rules must declare identification, traversal_completion, result and error_handling")
        return {}
    for name in names:
        rule = _object(rules[name], f"{label} {name} rule", errors)
        if not isinstance(rule.get("id"), str) or not rule["id"] or not isinstance(rule.get("parameters"), dict):
            errors.append(f"{label} invalid {name} rule")
            return {}
    counted = rules["identification"]["parameters"].get("counted_item_types")
    boundaries = rules["traversal_completion"]["parameters"].get("completion_conditions")
    required = rules["result"]["parameters"].get("required_artifacts")
    handling = rules["error_handling"]["parameters"]
    if (not _strings(counted) or not counted or not _strings(target.get("item_types"))
            or not set(counted) <= set(target["item_types"])
            or not _strings(boundaries) or not boundaries or not isinstance(required, dict)
            or not _strings(target.get("expected_artifacts"))
            or set(required) != set(target["expected_artifacts"])
            or not required or any(not _strings(kinds) or not kinds for kinds in required.values())
            or handling.get("on_interruption") != "preserve_partial"
            or not isinstance(handling.get("fallbacks"), dict)):
        errors.append(f"{label} invalid rule parameters")
        return {}
    allowed = {
        "structured_data": {"structured_data", "ui_record"} | ({"structured_json"} if label.startswith("whatsapp.") else set()),
        "ui_observation": {"screen_image", "ui_hierarchy"},
        "downloaded_file": {"downloaded_file", "original_artifact", "app_materialized_file"},
        "saved_file": {"saved_file", "original_artifact", "app_materialized_file"},
        "export_file": {"export_file", "original_artifact", "app_export"},
        "file": {"file", "acquired_file"},
    }
    if any(group not in allowed or not set(kinds) <= allowed[group]
           or (group == "ui_observation" and set(kinds) != allowed[group]) for group, kinds in required.items()):
        errors.append(f"{label} unsupported required artifacts rule")
        return {}
    fallback = {"display_fallback": {"conditions": ["save_action_unavailable"],
                "required_artifacts": ["ui_record", "screen_image", "ui_hierarchy", "audit_record"],
                "acquisition_status": "partial"}} if label == "telegram.attachments" else {}
    if handling["fallbacks"] != fallback:
        errors.append(f"{label} unsupported fallback rule")
        return {}
    return rules


def _validate_state(value, label, errors, *, expected=None):
    state = _object(value, label, errors)
    if state.get("status") in ("observed", "verified"):
        observed = _object(state.get("observed"), f"{label} observed", errors)
        for field in ("airplane_enabled", "wifi_enabled", "wifi_connected", "dnd_enabled"):
            if type(observed.get(field)) is not bool:
                errors.append(f"{label} {field} must be boolean")
        observed_at = _timestamp(state.get("observed_at"), f"{label} observed_at", errors)
        if expected:
            for field, wanted in expected.items():
                if observed.get(field) is not wanted:
                    errors.append(f"{label} {field} does not match verified environment")
        return observed_at
    elif state.get("status") == "unavailable":
        if expected:
            errors.append(f"{label} unavailable observation cannot establish verified environment")
        if state.get("observed") is not None or state.get("observed_at") is not None or not state.get("error") or not state.get("error_type"):
            errors.append(f"{label} unavailable state must have null current observation and a query error")
        failed = _timestamp(state.get("query_failed_at"), f"{label} query_failed_at", errors)
        previous = state.get("last_confirmed")
        if previous is not None:
            previous = _object(previous, f"{label} last_confirmed", errors)
            if previous.get("status") != "observed":
                errors.append(f"{label} last_confirmed must be a separate observed state")
            else:
                _validate_state(previous, f"{label} last_confirmed", errors)
                at = _timestamp(previous.get("observed_at"), f"{label} last_confirmed observed_at", errors)
                if at and failed and at > failed:
                    errors.append(f"{label} last_confirmed is later than failed query")
    else:
        errors.append(f"{label} invalid observation status")


def _validate_session_records(session, errors):
    preparation = _object(session.get("preparation"), "session preparation", errors)
    preparation_at = None
    if preparation.get("status") == "verified":
        preparation_at = _validate_state(preparation, "session preparation", errors,
                                        expected={"airplane_enabled": True, "wifi_enabled": False, "dnd_enabled": True})
    elif preparation.get("status") == "failed":
        if session.get("executions") or not preparation.get("error") or session.get("status") not in ("prepare_failed", "interrupted"):
            errors.append("failed preparation must prevent executions and record failed/interrupted session status")
        _timestamp(preparation.get("at"), "preparation failure at", errors)
    else:
        errors.append("session preparation must be verified or failed")
    for field in ("prepared_at", "finalized_at"):
        _timestamp(session.get(field), f"session {field}", errors)
    for field in ("device_model", "android_version"):
        if field not in session or (session[field] is not None and (not isinstance(session[field], str) or not session[field])):
            errors.append(f"session {field} must be an observed string or null")
    _validate_state(session.get("final_state"), "session final_state", errors)
    if "restoration" in session or "restored_at" in session:
        errors.append("schema2 session must not contain automatic restoration fields")
    plans = _index(_records(session, "execution_plan", errors), "run_id", errors)
    executions = _index(_records(session, "executions", errors), "run_id", errors)
    installed = _records(session, "installed_apps", errors)
    online = False
    for run_id, plan in plans.items():
        environment = plan.get("acquisition_environment")
        if environment not in ("device_only", "controlled_online") or (online and environment == "device_only"):
            errors.append(f"{run_id} execution_plan acquisition_environment/order invalid")
        online = online or environment == "controlled_online"
        execution = executions.get(run_id)
        if execution:
            started_at = _timestamp(execution.get("started_at"), f"{run_id} execution started_at", errors)
            if preparation_at and started_at and preparation_at > started_at:
                errors.append(f"{run_id} preparation observed_at must precede execution started_at")
            for field in ("app_id", "method", "acquisition_environment", "order", "status"):
                if plan.get(field) != execution.get(field):
                    errors.append(f"{run_id} execution {field} does not match execution_plan")
        elif plan.get("status") != "planned":
            errors.append(f"{run_id} missing execution: incomplete validation scope")
        matches = [row for row in installed if row.get("app_id") == plan.get("app_id") and row.get("package_name") == plan.get("package_name")]
        if len(matches) != 1:
            errors.append(f"{run_id} installed app/profile correspondence missing or ambiguous")
        else:
            for field, other in (("installed_app_version", "version_name"), ("profile_app_version", "profile_app_version"), ("profile_status", "profile_status")):
                if plan.get(field) != matches[0].get(other):
                    errors.append(f"{run_id} {field} differs from installed app metadata")
    for transition in _records(session, "environment_transitions", errors):
        environment = transition.get("acquisition_environment")
        if environment not in ("device_only", "controlled_online"):
            errors.append("transition acquisition_environment invalid")
        _timestamp(transition.get("at"), "transition at", errors)
        status = transition.get("status")
        if status not in ("started", "completed", "interrupted", "failed"):
            errors.append("transition status invalid")
        expected = None
        if status == "started":
            expected = {"airplane_enabled": True, "dnd_enabled": True, "wifi_enabled": environment == "controlled_online"}
            if environment == "controlled_online":
                expected["wifi_connected"] = True
        _validate_state(transition.get("state"), "transition state", errors, expected=expected)


def _validate_collection_bindings(indexes, documents, run, errors):
    items, attempts, artifacts = indexes["items"], indexes["attempts"], indexes["artifacts"]
    owned_items = {attempt.get("acquisition_item_id") for attempt in attempts.values() if isinstance(attempt.get("acquisition_item_id"), str)}
    bindings = {}
    occupied = set()
    condition = run.get("condition")
    environment = condition.get("acquisition_environment") if isinstance(condition, dict) else None
    for item_id, item in items.items():
        source = item.get("source")
        if not isinstance(source, dict):
            continue
        if "collection_attempt_id" not in source:
            if item_id not in owned_items:
                errors.append(f"{item_id} has neither an attempt nor a supported collection binding")
            continue
        label = f"{item_id} collection binding"
        attempt = _reference(attempts, source.get("collection_attempt_id"), f"{label} unknown attempt", errors)
        artifact = _reference(artifacts, source.get("artifact_id"), f"{label} unknown artifact", errors)
        owner = _lookup(items, attempt.get("acquisition_item_id"))
        payload = _lookup(documents, source.get("artifact_id"))
        if (not attempt or not artifact or not payload or artifact.get("attempt_id") != source.get("collection_attempt_id")
                or item.get("target_id") != owner.get("target_id") or item_id in owned_items
                or (environment is not None and attempt.get("acquisition_environment") != environment)
                or attempt.get("procedure_status") == "not_attempted"):
            errors.append(f"{label} must match its real collection attempt/artifact/target/environment")
            continue
        app_id, target = run.get("app_id"), item.get("target_id")
        if app_id == "telegram":
            message = source.get("message_ref")
            chat = source.get("logical_chatroom_id")
            owner_source = owner.get("source", {})
            if (target != "telegram.conversations" or item.get("item_type") != "message" or owner.get("item_type") != "conversation"
                    or artifact.get("kind") != "ui_record" or payload.get("record_kind") != "message"
                    or "record_index" in source or "record_key" in source
                    or not isinstance(message, str) or re.fullmatch(r"message-\d{6}", message) is None
                    or payload.get("message_ref") != message or payload.get("logical_chatroom_id") != chat
                    or not isinstance(owner_source, dict) or owner_source.get("comparison_ref") != chat):
                errors.append(f"{label} invalid Telegram singleton message/history identity")
                continue
            binding_key = (source["artifact_id"], None, None)
        else:
            key = source.get("record_key", "records")
            browser = app_id in ("chrome", "samsung_browser") and key == "records"
            chrome_account = (app_id == "chrome" and target == "chrome.account"
                              and item.get("item_type") == "account_entry"
                              and owner.get("item_type") == "account_context"
                              and artifact.get("kind") == "structured_data"
                              and payload.get("record_kind") == "chrome_account")
            notion = app_id == "notion" and target == "notion.content" and key == "accounts" and item.get("item_type") == "account" and owner.get("item_type") == "account_collection"
            rows = payload.get(key) if isinstance(key, str) else None
            index = source.get("record_index")
            if (not (browser or notion) or (browser and not (chrome_account or str(owner.get("item_type")).endswith("_collection")))
                    or not isinstance(rows, list) or type(index) is not int or not 0 <= index < len(rows)
                    or not isinstance(rows[index], dict) or not rows[index]):
                errors.append(f"{label} invalid array key/index/row/scope")
                continue
            if notion and (not isinstance(rows[index].get("email"), str) or "@" not in rows[index]["email"]):
                errors.append(f"{label} account must bind an email identity, not a workspace")
                continue
            binding_key = (source["artifact_id"], key, index)
        if binding_key in occupied:
            errors.append(f"{label} duplicates an existing original row/message binding")
        occupied.add(binding_key)
        bindings.setdefault(source["artifact_id"], []).append(item)
    # Start from preserved originals, including those whose common items are all missing.
    for artifact_id, payload in documents.items():
        artifact = artifacts[artifact_id]
        attempt = _lookup(attempts, artifact.get("attempt_id"))
        owner = _lookup(items, attempt.get("acquisition_item_id"))
        app_id, target = run.get("app_id"), owner.get("target_id")
        kind = payload.get("record_kind")
        expected_count = None
        if (app_id in ("chrome", "samsung_browser") and artifact.get("kind") == "structured_data"
                and isinstance(target, str) and target.startswith(f"{app_id}.")
                and ((str(owner.get("item_type")).endswith("_collection")
                      and isinstance(kind, str) and kind.startswith(f"{app_id}_"))
                     or (app_id == "chrome" and target == "chrome.account"
                         and owner.get("item_type") == "account_context"
                         and kind == "chrome_account"))
                and isinstance(payload.get("records"), list)):
            expected_count = len(payload["records"])
        elif (app_id == "notion" and target == "notion.content" and owner.get("item_type") == "account_collection"
                and artifact.get("kind") in ("ui_record", "structured_data")
                and kind == "notion_accounts" and isinstance(payload.get("accounts"), list)):
            expected_count = len(payload["accounts"])
        elif (app_id == "telegram" and target == "telegram.conversations" and owner.get("item_type") == "conversation"
                and artifact.get("kind") == "ui_record" and kind == "message"):
            expected_count = 1
        if expected_count is not None and len(bindings.get(artifact_id, [])) != expected_count:
            errors.append(f"{artifact_id} collection binding does not cover each original row/message exactly once")
    return bindings


def _validate_app_json(indexes, members, run, errors):
    artifacts, observations = indexes["artifacts"], indexes["observations"]
    documents = {}
    # Original/downloaded/exported JSON is user data, not an AURA reference document.
    for artifact_id, artifact in artifacts.items():
        path = artifact.get("relative_path")
        if (artifact.get("kind") not in ("ui_record", "structured_data", "structured_json", "audit_record")
                or not isinstance(path, str) or not path.endswith(".json") or path not in members):
            continue
        payload = _load_json(members[path], f"{artifact_id} app JSON", errors)
        if isinstance(payload, dict):
            documents[artifact_id] = payload
        else:
            errors.append(f"{artifact_id} app JSON must be an object")
            continue
        label = f"{artifact_id} app JSON"
        if run.get("app_id") in ("chrome", "samsung_browser") and "records" in payload:
            rows = payload["records"]
            if not isinstance(rows, list) or type(payload.get("record_count")) is not int or payload.get("record_count") != len(rows):
                errors.append(f"{label} record_count does not match records")
        if run.get("app_id") == "telegram" and payload.get("record_kind") == "attachment_materialize_audit":
            unavailable = payload.get("limitation") == "attachment_materialize_save_action_unavailable"
            if unavailable or payload.get("after_inventory_status") == "not_performed":
                if payload.get("after_inventory_status") != "not_performed" or payload.get("after_count") is not None or payload.get("after_manifest_sha256") is not None:
                    errors.append(f"{label} unperformed after inventory must be explicitly not_performed with null count/hash")
        pending = [(payload, {})]
        while pending:
            value, identity = pending.pop()
            if isinstance(value, list):
                pending.extend((child, identity) for child in value)
                continue
            if not isinstance(value, dict):
                continue
            identity = {**identity, **{key: value[key] for key in ("workspace_ref", "page_ref", "logical_chatroom_id", "message_ref", "attachment_ref", "note_ref") if key in value}}
            pending.extend((child, identity) for child in value.values() if isinstance(child, (dict, list)))
            if "artifact_id" in value:
                linked = _reference(artifacts, value["artifact_id"], f"{label} unknown artifact reference", errors)
                if "path" in value and value["path"] != linked.get("relative_path"):
                    errors.append(f"{label} artifact path does not match reference")
                linked_context = linked.get("context", {})
                if isinstance(linked_context, dict):
                    for key, expected in identity.items():
                        if key in linked_context and linked_context[key] != expected:
                            errors.append(f"{label} artifact {key} does not match source item identity")
            # Only preserved-source dictionaries carry common observation/artifact IDs.
            if any(key in value for key in ("observation_id", "screen_artifact_id", "ui_tree_artifact_id", "hierarchy_artifact_id")):
                observation = _reference(observations, value.get("observation_id"), f"{label} unknown observation", errors)
                for field, expected_field in (("screen_artifact_id", "screen_artifact_id"), ("ui_tree_artifact_id", "hierarchy_artifact_id"), ("hierarchy_artifact_id", "hierarchy_artifact_id")):
                    if field in value:
                        linked = _reference(artifacts, value[field], f"{label} unknown {field}", errors)
                        if linked and (linked.get("attempt_id") != observation.get("attempt_id") or value[field] != observation.get(expected_field)):
                            errors.append(f"{label} {field} must match the same observation/attempt")
                observed_artifact = _lookup(artifacts, observation.get("screen_artifact_id"))
                observed_context = observed_artifact.get("context", {})
                if isinstance(observed_context, dict):
                    for key, expected in identity.items():
                        if key in observed_context and observed_context[key] != expected:
                            errors.append(f"{label} observation {key} does not match source item identity")
                for path_field, id_field in (("screen_path", "screen_artifact_id"), ("ui_tree_path", "ui_tree_artifact_id")):
                    if path_field in value and value[path_field] != _lookup(artifacts, value.get(id_field)).get("relative_path"):
                        errors.append(f"{label} {path_field} does not match referenced artifact")
            for key, reference in value.items():
                if key.endswith("source_snapshot_id") or key.endswith("source_snapshot_ids"):
                    references = reference if key.endswith("source_snapshot_ids") and isinstance(reference, list) else [reference]
                    for source_id in references:
                        matches = [o for o in observations.values() if isinstance(source_id, str) and o.get("source_snapshot_id") == source_id]
                        if not matches:
                            errors.append(f"{label} unknown source_snapshot reference {source_id}")
                        elif value.get("observation_id") is not None and all(o.get("observation_id") != value["observation_id"] for o in matches):
                            errors.append(f"{label} source_snapshot does not map to preserved observation")
    indexes["associated_items"] = _validate_collection_bindings(indexes, documents, run, errors)


def _validate_schema2(acquisition, indexes, events, members, execution, session, errors):
    run = acquisition["run"]
    label = str(run.get("run_id"))
    plans = [row for row in _records(session, "execution_plan", errors) if row.get("run_id") == run.get("run_id")]
    plan = plans[0] if len(plans) == 1 else {}
    if len(plans) != 1:
        errors.append(f"{label} must have exactly one execution_plan entry")
    for field in ("app_id", "method", "acquisition_environment", "status", "order"):
        if execution.get(field) != plan.get(field):
            errors.append(f"{label} execution {field} does not match plan")
    outcome = _object(acquisition.get("outcome"), f"{label} outcome", errors)
    if outcome.get("status") not in ("complete", "partial", "failed", "unresolved", "interrupted") or outcome.get("status") != execution.get("status"):
        errors.append(f"{label} run outcome status does not match execution")
    profile = _object(acquisition.get("profile"), f"{label} profile", errors)
    condition = _object(run.get("condition"), f"{label} condition", errors)
    environment = condition.get("acquisition_environment")
    if environment not in ("device_only", "controlled_online"):
        errors.append(f"{label} invalid acquisition_environment")
    verification_at = None
    if "verified_state" in run:
        expected = {"airplane_enabled": True, "dnd_enabled": True, "wifi_enabled": environment == "controlled_online"}
        if environment == "controlled_online":
            expected["wifi_connected"] = True
        verification_at = _validate_state(run["verified_state"], f"{label} verified_state", errors, expected=expected)
    elif acquisition.get("attempts"):
        errors.append(f"{label} attempts require a verified_state before acquisition")
    _validate_state(run.get("final_state"), f"{label} final_state", errors)
    for field in ("app_id", "package_name", "installed_app_version", "profile_app_version"):
        if run.get(field) != plan.get(field):
            errors.append(f"{label} {field} does not match session execution_plan")
    for field in ("device_model", "android_version", "system_ui_profile"):
        if field not in run or run.get(field) != session.get(field):
            errors.append(f"{label} {field} does not match session")
    for field in ("app_id", "package_name"):
        if profile.get(field) != run.get(field):
            errors.append(f"{label} profile {field} does not match run")
    if profile.get("app_version") != run.get("profile_app_version") or run.get("app_version") != profile.get("app_version"):
        errors.append(f"{label} profile_app_version does not match preserved profile")
    if "profile_version" in profile or "profile_version" in run:
        errors.append(f"{label} independent profile_version is not part of schema2")
    exact = run.get("installed_app_version") is not None and run.get("installed_app_version") == profile.get("app_version")
    if run.get("app_profile_exact_match") is not exact:
        errors.append(f"{label} installed_app_version/exact profile correspondence mismatch")
    if plan.get("profile_status") != ("exact" if exact else "nearest"):
        errors.append(f"{label} profile_status does not match recorded versions")
    if environment != execution.get("acquisition_environment") or run.get("route") != execution.get("method"):
        errors.append(f"{label} method/acquisition_environment does not match session execution")
    android = profile.get("android_versions")
    if android is not None:
        android = _object(android, f"{label} android_versions", errors)
        release = run.get("android_version")
        if (type(android.get("min")) is not int or android["min"] < 1
                or ("max" in android and (type(android["max"]) is not int or android["max"] < android["min"]))
                or not isinstance(release, str) or re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", release.strip()) is None):
            errors.append(f"{label} invalid Android applicability metadata")
        else:
            major = int(release.strip().split(".")[0])
            if not android["min"] <= major <= android.get("max", major):
                errors.append(f"{label} android_version is outside profile applicability")
    targets = _index(_records(profile, "targets", errors), "target_id", errors)
    rules = {key: _target_rules(target, errors) for key, target in targets.items()}
    applicable = {}
    for key, target in targets.items():
        environments = target.get("acquisition_environments")
        if not _strings(environments) or any(value not in ("device_only", "controlled_online") for value in environments):
            errors.append(f"{key} invalid acquisition_environments")
        elif environment in environments:
            applicable[key] = target
        if target.get("method") not in ("materialize", "export"):
            errors.append(f"{key} invalid target method")
    planned_targets = [key for key, target in applicable.items() if target.get("method") == run.get("route")]
    if plan.get("target_ids") != planned_targets:
        errors.append(f"{label} target_ids do not match applicable profile targets")
    execution_interval = _interval({"started_at": execution.get("started_at"), "ended_at": execution.get("completed_at")}, f"{label} execution", errors)
    run_interval = _interval(acquisition, label, errors, execution_interval)
    if verification_at and run_interval and not run_interval[0] <= verification_at <= run_interval[1]:
        errors.append(f"{label} verified_state observed_at must lie inside the actual run interval")
    items, attempts = indexes["items"], indexes["attempts"]
    artifacts, observations, actions = indexes["artifacts"], indexes["observations"], indexes["actions"]
    if outcome.get("action_id") is not None:
        _reference(actions, outcome["action_id"], f"{label} outcome unknown action", errors)
    for kind in ("items", "attempts", "artifacts", "outcomes"):
        for identifier, record in indexes[kind].items():
            context = _object(record.get("context"), f"{identifier} context", errors)
            for field in ("run_id", "device_session_id", "app_id", "package_name", "installed_app_version", "profile_app_version", "android_version", "route"):
                if field in context and context[field] != run.get(field):
                    errors.append(f"{identifier} context {field} does not match same run")
    for item_id, item in items.items():
        target = _reference(applicable, item.get("target_id"), f"{item_id} unknown/inapplicable target", errors)
        if not _strings(target.get("item_types")) or item.get("item_type") not in target["item_types"]:
            errors.append(f"{item_id} item_type is not declared by target")
        _object(item.get("source"), f"{item_id} source", errors)
    identifications = _index(_records(acquisition, "identifications", errors), "target_id", errors)
    if set(identifications) != set(applicable):
        errors.append(f"{label} identifications must cover exactly the environment-applicable targets")
    for target_id, record in identifications.items():
        rule = rules.get(target_id, {})
        if not rule:
            continue
        count = sum(item.get("target_id") == target_id and item.get("item_type") in rule["identification"]["parameters"]["counted_item_types"] for item in items.values())
        if type(record.get("identified_item_count")) is not int or record.get("identified_item_count") != count:
            errors.append(f"{target_id} identified_item_count does not match counted items ({count})")
        if record.get("acquisition_environment") != environment:
            errors.append(f"{target_id} identification acquisition_environment mismatch")
        status = record.get("identification_status")
        if status == "not_attempted":
            if (record.get("started_at") is not None or record.get("ended_at") is not None
                    or record.get("traversal_complete") is not False or count or not record.get("reason")
                    or record.get("applied_rules") != {} or record.get("completion_condition") is not None):
                errors.append(f"{target_id} invalid not_attempted identification")
        elif status in ("completed", "interrupted"):
            _interval(record, f"{target_id} identification", errors, run_interval)
            expected = {name: rule[name]["id"] for name in ("identification", "traversal_completion", "error_handling")}
            if record.get("applied_rules") != expected:
                errors.append(f"{target_id} identification applied_rules mismatch")
            if status == "completed":
                if record.get("traversal_complete") is not True or record.get("completion_condition") not in rule["traversal_completion"]["parameters"]["completion_conditions"]:
                    errors.append(f"{target_id} completed identification has no declared completion_condition")
            elif record.get("traversal_complete") is not False or record.get("completion_condition") is not None or not record.get("reason"):
                errors.append(f"{target_id} interrupted identification requires a reason and no completion_condition")
        else:
            errors.append(f"{target_id} invalid identification_status")
    intervals = {}
    for attempt_id, attempt in attempts.items():
        item = _lookup(items, attempt.get("acquisition_item_id"))
        target = _lookup(targets, item.get("target_id"))
        rule = _lookup(rules, item.get("target_id"))
        if attempt.get("acquisition_environment") != environment or attempt.get("method") != target.get("method"):
            errors.append(f"{attempt_id} method/acquisition_environment mismatch")
        if rule and attempt.get("applied_rules") != {name: value["id"] for name, value in rule.items()}:
            errors.append(f"{attempt_id} applied_rules mismatch")
        status, acquired = attempt.get("procedure_status"), attempt.get("acquisition_status")
        owned = [artifact for artifact in artifacts.values() if artifact.get("attempt_id") == attempt_id]
        if status == "not_attempted":
            if acquired is not None or attempt.get("started_at") is not None or attempt.get("ended_at") is not None or not attempt.get("reason") or owned:
                errors.append(f"{attempt_id} invalid not_attempted status/times/artifacts")
        elif status in ("completed", "interrupted"):
            intervals[attempt_id] = _interval(attempt, attempt_id, errors, run_interval)
            if verification_at and intervals[attempt_id] and verification_at > intervals[attempt_id][0]:
                errors.append(f"{attempt_id} verified_state observed_at must precede actual attempt started_at")
            if acquired not in ("acquired", "partial", "not_acquired"):
                errors.append(f"{attempt_id} invalid acquisition_status")
            if (status == "interrupted" or acquired != "acquired") and not attempt.get("reason"):
                errors.append(f"{attempt_id} incomplete procedure/acquisition requires reason")
        else:
            errors.append(f"{attempt_id} invalid procedure_status")
        if not rule:
            continue
        kinds = {artifact.get("kind") for artifact in owned if isinstance(artifact.get("kind"), str)}
        required = rule["result"]["parameters"]["required_artifacts"]
        fulfilled = all(set(kinds_allowed) <= kinds if group == "ui_observation" else bool(kinds.intersection(kinds_allowed)) for group, kinds_allowed in required.items())
        data = {kind for group, kinds_allowed in required.items() if group != "ui_observation" for kind in kinds_allowed}
        details = _object(attempt.get("details"), f"{attempt_id} details", errors)
        fallback = details.get("fallback")
        if fallback is not None:
            declared = _lookup(rule["error_handling"]["parameters"]["fallbacks"], fallback)
            if (not declared or details.get("fallback_condition") not in declared.get("conditions", [])
                    or not set(declared.get("required_artifacts", [])) <= kinds
                    or acquired != declared.get("acquisition_status") or status != "completed"):
                errors.append(f"{attempt_id} fallback rule/condition/evidence mismatch")
        elif acquired == "acquired" and not fulfilled:
            errors.append(f"{attempt_id} acquired without required artifacts")
        if acquired == "partial" and not kinds.intersection(data) and fallback is None:
            errors.append(f"{attempt_id} partial without declared data artifacts")
    for kind in ("artifacts", "observations", "outcomes"):
        for identifier, record in indexes[kind].items():
            for field, known in (("action_id", actions), ("observation_id", observations)):
                reference = record.get(field)
                linked = _lookup(known, reference)
                if linked and linked.get("attempt_id") != record.get("attempt_id"):
                    errors.append(f"{identifier} {field} must reference the same attempt")
    for artifact_id, artifact in artifacts.items():
        context = artifact.get("context")
        source = context.get("source_snapshot_id") if isinstance(context, dict) else None
        if source is None:
            continue
        matches = [record for record in observations.values() if record.get("source_snapshot_id") == source]
        if not matches:
            errors.append(f"{artifact_id} context unknown source_snapshot reference {source}")
        elif artifact.get("observation_id") is not None and all(
            record.get("observation_id") != artifact["observation_id"] for record in matches
        ):
            errors.append(f"{artifact_id} context source_snapshot does not map to canonical observation")
    for observation_id, record in observations.items():
        for field, kind in (("screen_artifact_id", "screen_image"), ("hierarchy_artifact_id", "ui_hierarchy")):
            artifact = _lookup(artifacts, record.get(field))
            if (artifact.get("kind") != kind or artifact.get("attempt_id") != record.get("attempt_id")
                    or artifact.get("action_id") != record.get("action_id") or artifact.get("observation_id") != observation_id):
                errors.append(f"{observation_id} PNG/XML/action must reference the same attempt and observation")
        source = record.get("source_snapshot_id")
        if source is not None and (not isinstance(source, str) or not source.startswith(str(run.get("app_id")) + "-snapshot:") or source == observation_id):
            errors.append(f"{observation_id} invalid distinct source_snapshot_id")
    for outcome_id, outcome in indexes["outcomes"].items():
        attempt = _reference(attempts, outcome.get("attempt_id"), f"{outcome_id} unknown attempt", errors)
        if outcome.get("acquisition_status") != attempt.get("acquisition_status") or outcome.get("reason") != attempt.get("reason") or outcome.get("details") != attempt.get("details"):
            errors.append(f"{outcome_id} outcome does not match attempt status/reason/details")
    for artifact_id, artifact in artifacts.items():
        action = _lookup(actions, artifact.get("action_id"))
        if action.get("status") != "success" or action.get("action") != artifact.get("action"):
            errors.append(f"{artifact_id} must match its successful retention action")
    _index(events, "event_id", errors)
    retained_events = {}
    finished_events = {}
    identification_events = {}
    for event in events:
        event_id = str(event.get("event_id"))
        context = _object(event.get("context"), f"{event_id} context", errors)
        for field in ("run_id", "device_session_id", "app_id", "package_name", "installed_app_version", "profile_app_version", "android_version", "device_model", "route"):
            if context.get(field) != run.get(field):
                errors.append(f"{event_id} {field} does not match same run")
        if context.get("condition") != run.get("condition"):
            errors.append(f"{event_id} condition does not match same run")
        timestamp = _timestamp(event.get("timestamp"), f"{event_id} timestamp", errors)
        if timestamp and run_interval and not run_interval[0] <= timestamp <= run_interval[1]:
            errors.append(f"{event_id} outside run interval")
        details = _object(event.get("details"), f"{event_id} details", errors)
        attempt_id = event.get("attempt_id", details.get("attempt_id"))
        if attempt_id is not None:
            attempt = _reference(attempts, attempt_id, f"{event_id} unknown attempt", errors)
            interval = intervals.get(attempt_id) if isinstance(attempt_id, str) else None
            if event.get("event_type") in ("action", "artifact_retained") and attempt:
                if not interval or (timestamp and not interval[0] <= timestamp <= interval[1]):
                    errors.append(f"{event_id} outside actual attempt interval")
        if event.get("event_type") == "artifact_retained":
            if isinstance(details.get("artifact_id"), str):
                retained_events.setdefault(details["artifact_id"], []).append(event)
            artifact = _reference(artifacts, details.get("artifact_id"), f"{event_id} unknown artifact", errors)
            for field in ("kind", "size", "sha256", "action_id", "attempt_id", "observation_id"):
                if details.get(field) != artifact.get(field):
                    errors.append(f"{event_id} artifact {field} mismatch")
            if details.get("path") != artifact.get("relative_path"):
                errors.append(f"{event_id} artifact path mismatch")
        elif event.get("event_type") == "attempt_finished":
            if isinstance(attempt_id, str):
                finished_events.setdefault(attempt_id, []).append(event)
            attempt = _lookup(attempts, attempt_id)
            if event.get("status") != attempt.get("procedure_status") or any(details.get(field) != attempt.get(field) for field in ("started_at", "ended_at", "acquisition_status", "reason")):
                errors.append(f"{event_id} attempt_finished does not match actual attempt status/times")
        elif event.get("event_type") in ("identification_started", "identification_completed", "identification_interrupted"):
            target_id = details.get("target_id")
            if isinstance(target_id, str):
                identification_events.setdefault((target_id, event["event_type"]), []).append(event)
            identification = _reference(identifications, target_id, f"{event_id} unknown identification", errors)
            started = event["event_type"] == "identification_started"
            if event.get("timestamp") != identification.get("started_at" if started else "ended_at"):
                errors.append(f"{event_id} {event['event_type']} timestamp does not match identification")
            fields = ("applied_rules",) if started else ("completion_condition", "reason")
            if any(details.get(field) != identification.get(field) for field in fields):
                errors.append(f"{event_id} {event['event_type']} details do not match identification")
    for artifact_id in artifacts:
        if len(retained_events.get(artifact_id, [])) != 1:
            errors.append(f"{artifact_id} must have exactly one artifact_retained event")
    for attempt_id in attempts:
        if len(finished_events.get(attempt_id, [])) != 1:
            errors.append(f"{attempt_id} must have exactly one attempt_finished event")
    for target_id, record in identifications.items():
        for status in ("started", "completed", "interrupted"):
            expected = int(record.get("identification_status") in ("completed", "interrupted") and (status == "started" or status == record.get("identification_status")))
            if len(identification_events.get((target_id, "identification_" + status), [])) != expected:
                errors.append(f"{target_id} identification_{status} event count mismatch")
    _validate_app_json(indexes, members, run, errors)


def _validate_archive(
    session_path: Path,
    session_id: str,
    execution: dict,
    errors: list[str],
    session: dict,
):
    run_id = execution.get("run_id")
    archive = _archive_path(session_path, execution.get("archive_path"))
    if archive is None:
        errors.append(f"{run_id} has no archive path: incomplete validation scope")
        return None
    label = archive.name
    if not archive.is_file():
        errors.append(f"{label} does not exist: incomplete validation scope")
        return None
    try:
        archive_value = archive.read_bytes()
    except OSError:
        errors.append(f"{label} is unreadable: incomplete validation scope")
        return None
    if type(execution.get("archive_size")) is not int or execution.get("archive_size") != len(archive_value):
        errors.append(f"{label} archive size mismatch")
    if execution.get("sha256") != _sha256(archive_value):
        errors.append(f"{label} archive SHA-256 mismatch")
    try:
        with ZipFile(archive) as package:
            names = package.namelist()
            if len(names) != len(set(names)):
                errors.append(f"{label} contains duplicate member names")
            for name in names:
                if not _safe_path(name):
                    errors.append(f"{label} contains unsafe member path {name!r}")
            if "files.json" not in names:
                errors.append(f"{label} is missing files.json")
                return None
            manifest = _load_json(package.read("files.json"), f"{label}/files.json", errors)
            if not isinstance(manifest, dict):
                errors.append(f"{label}/files.json must contain an object")
                return None
            members = {
                name: package.read(name)
                for name in names
                if name != "files.json" and not name.endswith("/")
            }
    except (BadZipFile, KeyError, OSError, RuntimeError, NotImplementedError):
        errors.append(f"{label} is not a readable ZIP package")
        return None

    manifest_records = manifest.get("members")
    if not isinstance(manifest_records, list) or not all(
        isinstance(record, dict) for record in manifest_records
    ):
        errors.append(f"{label}/files.json members must be a list of objects")
        return None
    recorded = {}
    for record in manifest_records:
        path = record.get("path")
        if not _safe_path(path):
            errors.append(f"{label}/files.json contains an unsafe path {path!r}")
        elif path in recorded:
            errors.append(f"{label}/files.json contains duplicate path {path}")
        else:
            recorded[path] = record
    for path in sorted(recorded.keys() - members.keys()):
        errors.append(f"{label} is missing member {path}")
    for path in sorted(members.keys() - recorded.keys()):
        errors.append(f"{label} contains unrecorded member {path}")
    for path in sorted(recorded.keys() & members.keys()):
        value = members[path]
        if type(recorded[path].get("size")) is not int or recorded[path].get("size") != len(value):
            errors.append(f"{label}/{path} size mismatch")
        if recorded[path].get("sha256") != _sha256(value):
            errors.append(f"{label}/{path} SHA-256 mismatch")
    if "acquisition.json" not in members or "events.jsonl" not in members:
        errors.append(f"{label} is missing acquisition records")
        return None
    acquisition = _load_json(
        members["acquisition.json"], f"{label}/acquisition.json", errors
    )
    if not isinstance(acquisition, dict):
        errors.append(f"{label}/acquisition.json must contain an object")
        return None
    run = acquisition.get("run")
    if not isinstance(run, dict):
        errors.append(f"{label}/acquisition.json has no run record")
        return None
    if run.get("run_id") != run_id:
        errors.append(f"{label} run_id does not match session.json")
    if run.get("device_session_id") != session_id:
        errors.append(f"{label} device_session_id does not match session.json")
    event_records = _read_events(
        members["events.jsonl"], f"{label}/events.jsonl", errors
    )
    actions = _index(
        [record for record in event_records if record.get("event_type") == "action"],
        "action_id",
        errors,
    )
    indexes = _validate_references(acquisition, actions, members, errors)
    if "schema_version" in acquisition and (type(acquisition.get("schema_version")) is not int or acquisition.get("schema_version") not in (1, 2)):
        errors.append(f"{label} unsupported acquisition schema_version")
    elif session.get("schema_version") == 2:
        if acquisition.get("schema_version") != 2:
            errors.append(f"{label} schema_version must match schema2 session")
        else:
            _validate_schema2(acquisition, indexes, event_records, members, execution, session, errors)
    return {
        "execution": execution,
        "run": run,
        "indexes": indexes,
    }


def validate_session(session_path: Path):
    errors: list[str] = []
    try:
        session = json.loads(session_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None, [], ["session.json is not readable JSON"]
    if not isinstance(session, dict):
        return None, [], ["session.json must contain an object"]
    schema = session.get("schema_version", 1)
    if type(schema) is not int or schema not in (1, 2):
        return session, [], ["unsupported session schema_version"]
    if schema == 2:
        try:
            actual = session_path.with_name("session.sha256").read_bytes()
            expected = f"{_sha256(session_path.read_bytes())}  session.json\n".encode("ascii")
            if actual != expected:
                errors.append("session.sha256 format or SHA-256 mismatch")
        except OSError:
            errors.append("session.sha256 is missing or unreadable")
        _validate_session_records(session, errors)
    session_id = session.get("device_session_id")
    if not isinstance(session_id, str) or not session_id:
        errors.append("session.json has no device_session_id")
        session_id = ""
    executions = session.get("executions")
    if not isinstance(executions, list) or not all(
        isinstance(execution, dict) for execution in executions
    ):
        return session, [], [*errors, "session.json executions must be a list"]
    run_ids = set()
    validated = []
    for execution in executions:
        run_id = execution.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            errors.append("session execution has no run_id")
            continue
        if run_id in run_ids:
            errors.append(f"duplicate run_id {run_id}")
            continue
        run_ids.add(run_id)
        result = _validate_archive(
            session_path.resolve(), session_id, execution, errors, session
        )
        if result is not None:
            validated.append(result)
    return session, validated, errors


def _action_chain(action: dict | None, actions: dict[str, dict]) -> list[dict]:
    chain = []
    seen = set()
    current = action
    while current is not None and current.get("action_id") not in seen:
        chain.append(current)
        seen.add(current.get("action_id"))
        source = current.get("context", {}).get("source_action_id")
        current = actions.get(source)
    return list(reversed(chain))


def build_trace(
    session: dict,
    runs: list[dict],
    identifier: str,
    selected_run: str | None,
    errors: list[str],
):
    if selected_run is not None and not any(
        row["run"].get("run_id") == selected_run for row in runs
    ):
        errors.append(f"unknown run {selected_run}")
        return None
    matches = []
    for row in runs:
        if selected_run is not None and row["run"].get("run_id") != selected_run:
            continue
        indexes = row["indexes"]
        if identifier in indexes["artifacts"]:
            matches.append((row, "artifact", indexes["artifacts"][identifier]))
        if identifier in indexes["outcomes"]:
            matches.append((row, "outcome", indexes["outcomes"][identifier]))
    if not matches:
        errors.append(f"trace identifier {identifier} was not found")
        return None
    if len(matches) > 1:
        errors.append(f"trace identifier {identifier} is ambiguous; pass --run")
        return None
    row, kind, target = matches[0]
    indexes = row["indexes"]
    attempt = indexes["attempts"].get(target.get("attempt_id"))
    item = (
        indexes["items"].get(attempt.get("acquisition_item_id"))
        if attempt is not None
        else None
    )
    action = indexes["actions"].get(target.get("action_id"))
    observation = indexes["observations"].get(target.get("observation_id"))
    trace = {
        "session": {
            "device_session_id": session.get("device_session_id"),
            "status": session.get("status"),
        },
        "execution": row["execution"],
        "run": row["run"],
        "item": item,
        "attempt": attempt,
        "action": action,
        "action_chain": _action_chain(action, indexes["actions"]),
        "observation": observation,
        kind: target,
    }
    if kind == "artifact":
        associated = indexes.get("associated_items", {}).get(identifier, [])
    else:
        associated = [item for rows in indexes.get("associated_items", {}).values() for item in rows
                      if item["source"].get("collection_attempt_id") == target.get("attempt_id")]
    if associated:
        trace["associated_items"] = associated
    return trace


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session", type=Path, help="path to session.json")
    parser.add_argument("--trace", help="artifact_id or outcome_id to trace")
    parser.add_argument("--run", help="run_id used to disambiguate --trace")
    arguments = parser.parse_args(argv)
    session_path = arguments.session.resolve()
    session, runs, errors = validate_session(session_path)
    trace = None
    if not errors and arguments.trace:
        trace = build_trace(session, runs, arguments.trace, arguments.run, errors)
    result = {
        "valid": not errors,
        "session_path": str(session_path),
        "execution_count": len(runs),
        "validation_scope": ("schema2_record_consistency" if session and session.get("schema_version") == 2
                             else "legacy_integrity_and_references"),
    }
    if trace is not None:
        result["trace"] = trace
    if errors:
        result["errors"] = errors
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())

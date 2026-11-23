#!/usr/bin/env python3
"""Summarize timed sessions and compare independent or explicitly labeled baseline references."""

import argparse
import csv
import hashlib
import json
import math
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from zipfile import ZipFile

from validate_aura_results import _archive_path, validate_session


DATA_FILE_KINDS = {"file", "acquired_file", "downloaded_file", "saved_file", "export_file",
                   "original_artifact", "app_materialized_file", "app_export"}


def subset(expected, actual):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(k in actual and subset(v, actual[k]) for k, v in expected.items())
    return type(expected) is type(actual) and expected == actual


def evidence_index(acquisition):
    attempts_by_item, artifacts_by_attempt = defaultdict(list), defaultdict(list)
    for attempt in acquisition["attempts"]:
        attempts_by_item[attempt["acquisition_item_id"]].append(attempt)
    for artifact in acquisition["artifacts"]:
        artifacts_by_attempt[artifact["attempt_id"]].append(artifact)
    return {"attempts_by_item": attempts_by_item, "artifacts_by_attempt": artifacts_by_attempt,
            "attempts": {a["attempt_id"]: a for a in acquisition["attempts"]},
            "artifacts": {a["artifact_id"]: a for a in acquisition["artifacts"]}, "documents": {}}


def item_evidence(item, acquisition, members, index=None):
    if index is None:
        index = evidence_index(acquisition)
    source = item.get("source", {})
    attempts = index["attempts_by_item"].get(item["acquisition_item_id"], [])
    if source.get("collection_attempt_id"):
        attempt = index["attempts"].get(source["collection_attempt_id"])
        attempts = [attempt] if attempt is not None else []
    if source.get("artifact_id"):
        artifact = index["artifacts"].get(source["artifact_id"])
        artifacts = [artifact] if artifact is not None and any(artifact["attempt_id"] == a["attempt_id"] for a in attempts) else []
    else:
        artifacts = [artifact for attempt in attempts for artifact in index["artifacts_by_attempt"].get(attempt["attempt_id"], [])]
    records = []
    for artifact in artifacts:
        path = artifact["relative_path"]
        if artifact["kind"] not in ("ui_record", "structured_data", "structured_json") or not path.endswith(".json"):
            continue
        if path not in index["documents"]:
            index["documents"][path] = json.loads(members[path])
        document = index["documents"][path]
        if source.get("artifact_id"):
            record_index = source.get("record_index")
            rows = document.get("records", document.get("accounts", []))
            if type(record_index) is int and 0 <= record_index < len(rows):
                records.append(rows[record_index])
            elif record_index is None:
                records.append(document)
        else:
            records.append(document)
    return {"item": item, "source": source, "records": records, "attempts": attempts, "artifacts": artifacts}


def identity_matches(identity, evidence):
    if "source" in identity and not subset(identity["source"], evidence["source"]):
        return False
    if not any(key in identity for key in ("record", "record_contains")):
        return True
    return any(subset(identity.get("record", {}), record) and all(
        isinstance(record.get(k), str) and value in record[k]
        for k, value in identity.get("record_contains", {}).items()) for record in evidence["records"] if isinstance(record, dict))


def _identity_terms(identity):
    def leaves(value, path):
        if isinstance(value, dict):
            for key, child in value.items():
                yield from leaves(child, (*path, key))
        elif type(value) in (str, int, float, bool, type(None)):
            yield path, (type(value), value)
    for section in ("source", "record"):
        for path, value in leaves(identity.get(section, {}), ()):
            yield (section, path), value


def match_identities(expected, candidates):
    terms = [list(_identity_terms(item["identity"])) for item in expected]
    fields = {field: defaultdict(set) for row in terms for field, _ in row}
    for (section, path), values in fields.items():
        for number, candidate in enumerate(candidates):
            documents = [candidate["source"]] if section == "source" else candidate["records"]
            for value in documents:
                for key in path:
                    if not isinstance(value, dict) or key not in value:
                        break
                    value = value[key]
                else:
                    if type(value) in (str, int, float, bool, type(None)):
                        values[(type(value), value)].add(number)
    result = []
    for item, row in zip(expected, terms):
        buckets = [fields[field].get(value, ()) for field, value in row]
        # ponytail: substring/list-only identities still scan; exact scalar identities use the smallest bucket.
        possible = sorted(min(buckets, key=len)) if buckets else range(len(candidates))
        result.append([i for i in possible if identity_matches(item["identity"], candidates[i])])
    return result


def normalize_content(value, mode):
    if isinstance(value, dict):
        return {k: normalize_content(v, mode) for k, v in value.items()}
    if mode == "whitespace" and isinstance(value, str):
        return " ".join(value.split())
    return value


def validate_identity(identity):
    if not isinstance(identity, dict) or not identity or set(identity) - {"source", "record", "record_contains"}:
        raise ValueError("identity must specify source and/or record fields")
    if any(not isinstance(v, dict) or not v for v in identity.values()):
        raise ValueError("identity predicates must be nonempty objects")
    if any(not isinstance(v, str) or not v for v in identity.get("record_contains", {}).values()):
        raise ValueError("record_contains requires nonempty string values")


def validate_group(group):
    if not isinstance(group, dict):
        raise ValueError("Reference groups must be objects")
    if group.get("content_normalization", "exact") not in ("exact", "whitespace"):
        raise ValueError("content_normalization must be exact or whitespace")
    if group.get("reference_status") not in ("confirmed", "partial", "pending"):
        raise ValueError("reference_status must be confirmed, partial or pending")
    if group["reference_status"] != "pending" and not group.get("reference_evidence"):
        raise ValueError("reference_evidence is required")
    if not isinstance(group.get("expected"), list):
        raise ValueError("expected must be a list")
    if group["reference_status"] == "pending" and group["expected"]:
        raise ValueError("pending references must have no expected items; use partial with documented evidence")
    if group.get("reference_basis", "independent") not in ("independent", "acquired_baseline"):
        raise ValueError("reference_basis must be independent or acquired_baseline")
    if "device_model" in group and (not isinstance(group["device_model"], str) or not group["device_model"]):
        raise ValueError("device_model must be a nonempty string")
    if not isinstance(group.get("scope_label", "all"), str) or not group.get("scope_label", "all"):
        raise ValueError("scope_label must be a nonempty string")
    if not isinstance(group.get("excluded_identities", []), list):
        raise ValueError("excluded_identities must be a list")
    if group.get("excluded_identities") and group.get("scope_label", "all") == "all":
        raise ValueError("exclusions require an explicit scope_label")
    for identity in group.get("excluded_identities", []):
        validate_identity(identity)
    ids = set()
    for item in group["expected"]:
        if not isinstance(item, dict):
            raise ValueError("Expected items must be objects")
        identifier, identity = item.get("id"), item.get("identity")
        if not isinstance(identifier, str) or not identifier or identifier in ids:
            raise ValueError("Reference IDs must be nonempty and unique within a group")
        ids.add(identifier)
        validate_identity(identity)
        if type(item.get("occurrences", 1)) is not int or item.get("occurrences", 1) < 1:
            raise ValueError("occurrences must be a positive integer")
        if "content" in item and (not isinstance(item["content"], dict) or not item["content"]):
            raise ValueError("content must be a nonempty object")
        if "original" in item:
            original = item["original"]
            if not isinstance(original, dict) or type(original.get("size")) is not int or original["size"] < 0 or not re.fullmatch(r"[0-9a-f]{64}", str(original.get("sha256", ""))):
                raise ValueError("original requires a non-negative size and SHA-256")


def compare_group(acquisition, members, group, index=None):
    validate_group(group)
    items = [i for i in acquisition["items"] if i["target_id"] == group["target_id"] and i["item_type"] == group["item_type"]]
    if group["expected"] or group.get("excluded_identities"):
        if index is None:
            index = evidence_index(acquisition)
        candidates = [item_evidence(i, acquisition, members, index) for i in items]
        matches = match_identities(group["expected"], candidates)
    else:
        candidates, matches = items, []
    claimed = [i for match in matches for i in match]
    if len(set(claimed)) != len(claimed):
        raise ValueError("Reference identities overlap: an observed item matches multiple expected items")
    excluded = {i for identity in group.get("excluded_identities", []) for i, candidate in enumerate(candidates)
                if i not in claimed and identity_matches(identity, candidate)}
    observed_count = len(candidates) - len(excluded)
    occurrences = [e.get("occurrences", 1) for e in group["expected"]]
    expected_count = sum(occurrences)
    result = {"target_id": group["target_id"], "item_type": group["item_type"],
              "reference_status": group["reference_status"], "reference_evidence": group.get("reference_evidence"),
              "content_normalization": group.get("content_normalization", "exact"),
              "reference_basis": group.get("reference_basis", "independent"), "scope_label": group.get("scope_label", "all"),
              "total_observed_count": len(candidates), "excluded_count": len(excluded),
              "excluded_items": [candidates[i]["item"]["acquisition_item_id"] for i in sorted(excluded)],
              "expected_count": expected_count, "observed_count": observed_count,
              "identified_count": sum(min(n, len(m)) for n, m in zip(occurrences, matches)),
              "missing_count": sum(max(0, n - len(m)) for n, m in zip(occurrences, matches)),
              "duplicate_count": sum(max(0, len(m) - n) for n, m in zip(occurrences, matches)),
              "unexpected_count": observed_count - len(set(claimed)),
              "recorded_acquired_count": 0, "recorded_partial_count": 0, "recorded_not_acquired_count": 0,
              "unattempted_count": 0, "ambiguous_count": 0, "verified_count": 0, "items": []}
    for expected, match in zip(group["expected"], matches):
        row = {"id": expected["id"], "matches": [candidates[i]["item"]["acquisition_item_id"] for i in match],
               "content_verified": None, "original_verified": None}
        row["expected_occurrences"] = expected.get("occurrences", 1)
        observations = []
        if 0 < len(match) <= row["expected_occurrences"]:
            for number in match:
                observation = {"item_id": candidates[number]["item"]["acquisition_item_id"],
                               "content_verified": None, "original_verified": None}
                evidence = candidates[number]
                statuses = {a.get("acquisition_status") for a in evidence["attempts"] if a.get("acquisition_status")}
                # Multiple attempts with different results need examination, not an optimistic best outcome.
                status = next(iter(statuses)) if len(statuses) == 1 else "ambiguous" if statuses else "not_attempted"
                observation["recorded_acquisition_status"] = status
                if status in ("acquired", "partial", "not_acquired"):
                    result[f"recorded_{status}_count"] += 1
                else:
                    result["ambiguous_count" if status == "ambiguous" else "unattempted_count"] += 1
                if "content" in expected:
                    mode = group.get("content_normalization", "exact")
                    observation["content_verified"] = any(
                        identity_matches(expected["identity"], {**evidence, "records": [r]})
                        and subset(normalize_content(expected["content"], mode), normalize_content(r, mode))
                        for r in evidence["records"])
                if "original" in expected:
                    observation["original_verified"] = any(
                        len(members[a["relative_path"]]) == expected["original"]["size"]
                        and hashlib.sha256(members[a["relative_path"]]).hexdigest() == expected["original"]["sha256"]
                        for a in evidence["artifacts"]
                        if (not evidence["source"].get("artifact_id") or evidence["source"]["artifact_id"] == a["artifact_id"])
                        and a["kind"] in DATA_FILE_KINDS)
                checks = [v for k, v in observation.items() if k in ("content_verified", "original_verified") and v is not None]
                if status == "acquired" and checks and all(checks):
                    result["verified_count"] += 1
                observations.append(observation)
            for key in ("content_verified", "original_verified"):
                values = [o[key] for o in observations if o[key] is not None]
                row[key] = all(values) if values else None
            statuses = {o["recorded_acquisition_status"] for o in observations}
            row["recorded_acquisition_status"] = next(iter(statuses)) if len(statuses) == 1 else "mixed"
        elif match:
            result["ambiguous_count"] += row["expected_occurrences"]
        row["observations"] = observations
        result["items"].append(row)
    complete = group["reference_status"] == "confirmed" and group.get("reference_basis", "independent") == "independent"
    result["identification_precision"] = result["identified_count"] / observed_count if complete and observed_count else None
    result["identification_recall"] = result["identified_count"] / expected_count if complete and expected_count else None
    content_covered = all("content" in e or "original" in e for e in group["expected"])
    result["verified_recall"] = result["verified_count"] / expected_count if complete and expected_count and content_covered else None
    if group["reference_status"] == "pending":
        for key in result:
            if key.endswith("_count") and key not in ("observed_count", "total_observed_count", "excluded_count"):
                result[key] = None
    return result


def execution_timing(execution):
    elapsed = execution.get("elapsed_seconds")
    measured = type(elapsed) in (int, float) and math.isfinite(elapsed) and elapsed >= 0 and execution.get("timing_clock") == "perf_counter"
    wall = None
    if not measured and execution.get("started_at") and execution.get("completed_at"):
        wall = (datetime.fromisoformat(execution["completed_at"]) - datetime.fromisoformat(execution["started_at"])).total_seconds()
    return {"elapsed_seconds": elapsed if measured else None, "timing_scope": execution.get("timing_scope"), "legacy_wall_seconds": wall}


def evaluate_session(path, reference):
    for group in reference.get("groups", []):
        validate_group(group)
        if any(not isinstance(group.get(k), str) or not group[k] for k in ("app_id", "method", "acquisition_environment", "target_id", "item_type")):
            raise ValueError("Reference groups require app, method, environment, target and item type")
    session, validated, errors = validate_session(path)
    report = {"session_path": str(path), "integrity_valid": not errors, "integrity_errors": errors, "runs": []}
    if session is None:
        return report
    report.update(device_model=session.get("device_model"), android_version=session.get("android_version"),
                  preparation=session.get("preparation"), environment_transitions=session.get("environment_transitions"))
    for execution in session.get("executions", []):
        row = {k: execution.get(k) for k in ("run_id", "app_id", "method", "acquisition_environment", "status", "archive_size")}
        row.update(execution_timing(execution))
        row["groups"] = []
        report["runs"].append(row)
        if errors:  # Do not score untrusted/malformed packages as valid acquisition data.
            continue
        archive = _archive_path(path, execution.get("archive_path"))
        with ZipFile(archive) as package:
            acquisition = json.loads(package.read("acquisition.json"))
            members = {n: package.read(n) for n in package.namelist() if not n.endswith("/")}
        row["installed_app_version"] = acquisition["run"].get("installed_app_version")
        row["profile_app_version"] = acquisition["run"].get("profile_app_version")
        row["procedure_status_counts"] = dict(Counter(a.get("procedure_status") for a in acquisition["attempts"]))
        row["acquisition_status_counts"] = dict(Counter(a.get("acquisition_status") or "not_attempted" for a in acquisition["attempts"]))
        row["identification_status_counts"] = dict(Counter(a.get("identification_status") for a in acquisition.get("identifications", [])))
        row["artifact_count"] = len(acquisition["artifacts"])
        row["artifact_bytes"] = sum(a["size"] for a in acquisition["artifacts"])
        row["artifact_kind_counts"] = dict(Counter(a["kind"] for a in acquisition["artifacts"]))
        files = [a for a in acquisition["artifacts"] if a["kind"] in DATA_FILE_KINDS]
        row["data_file_count"] = len(files)
        row["data_file_bytes"] = sum(a["size"] for a in files)
        groups = [g for g in reference.get("groups", []) if all(g.get(k) == row[k] for k in ("app_id", "method", "acquisition_environment"))]
        row["inapplicable_reference_groups"] = [
            {"target_id": g["target_id"], "item_type": g["item_type"], "device_model": g["device_model"]}
            for g in groups if "device_model" in g and g["device_model"] != session.get("device_model")]
        groups = [g for g in groups if "device_model" not in g or g["device_model"] == session.get("device_model")]
        observed = {(i["target_id"], i["item_type"]) for i in acquisition["items"]}
        targets = acquisition.get("profile", {}).get("targets", [])
        planned = {(t["target_id"], kind) for t in targets
                   if t.get("method") == row["method"] and row["acquisition_environment"] in t.get("acquisition_environments", [])
                   for kind in t["item_types"]}
        covered = {(g["target_id"], g["item_type"]) for g in groups}
        if len(covered) != len(groups):
            raise ValueError("Duplicate reference groups for the same run/target/item type")
        row["unreferenced_groups"] = [{"target_id": t, "item_type": k} for t, k in sorted((observed | planned) - covered)]
        index = evidence_index(acquisition) if any(g["expected"] for g in groups) else None
        for group in groups:
            row["groups"].append(compare_group(acquisition, members, group, index))
        row["reference_complete"] = bool(groups and planned) and not row["unreferenced_groups"] and all(g["reference_status"] == "confirmed" and g.get("reference_basis", "independent") == "independent"
                and g.get("scope_label", "all") == "all" for g in groups)
    report["unexecuted_plan"] = [p for p in session.get("execution_plan", []) if p["run_id"] not in {e["run_id"] for e in session.get("executions", [])}]
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sessions", nargs="+", type=Path)
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--json", type=Path, dest="json_path")
    parser.add_argument("--csv", type=Path, dest="csv_path")
    args = parser.parse_args(argv)
    try:
        reference_bytes = args.reference.read_bytes() if args.reference else None
        reference = json.loads(reference_bytes) if reference_bytes is not None else {"groups": []}
        if not isinstance(reference, dict) or not isinstance(reference.get("groups"), list):
            raise ValueError("Reference must contain a groups list")
        reports = [evaluate_session(p.resolve(), reference) for p in args.sessions]
        result = {"dataset_id": reference.get("dataset_id"), "reference_path": str(args.reference) if args.reference else None,
                  "reference_sha256": hashlib.sha256(reference_bytes).hexdigest() if reference_bytes is not None else None, "sessions": reports}
        encoded = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        if args.json_path:
            args.json_path.write_text(encoded)
        else:
            print(encoded, end="")
        if args.csv_path:
            rows = []
            for report in reports:
                for run in report["runs"]:
                    base = {"session_path": report["session_path"], "integrity_valid": report["integrity_valid"],
                            "dataset_id": result["dataset_id"], "reference_sha256": result["reference_sha256"],
                            **{k: v for k, v in run.items() if not isinstance(v, (dict, list))}}
                    for name, counts in run.items():
                        if name.endswith("_counts") and isinstance(counts, dict):
                            base.update({f"{name}.{status}": count for status, count in counts.items()})
                    for group in run["groups"] or [{}]:
                        rows.append({**base, **{k: v for k, v in group.items() if not isinstance(v, (dict, list))}})
            fields = list(dict.fromkeys(k for row in rows for k in row))
            for row in rows:
                if row["integrity_valid"]:
                    for field in fields:
                        if "_counts." in field:
                            row.setdefault(field, 0)
            with args.csv_path.open("w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=fields)
                writer.writeheader()
                writer.writerows(rows)
        return 0 if all(r["integrity_valid"] for r in reports) else 1
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"Evaluation failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

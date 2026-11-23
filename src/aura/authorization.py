"""Examiner declarations and exact execution grants, not legal adjudication."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from dataclasses import asdict
from pathlib import Path
import re


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def profile_digest(profile) -> str:
    return hashlib.sha256(json.dumps(asdict(profile), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def validate_authorization(document: dict) -> None:
    fields = {"basis_type", "reference", "scope", "confirmed_by", "confirmed_at", "remote_acquisition", "grants"}
    if not isinstance(document, dict) or not fields <= document.keys() or document.keys() - fields - {"network_control"}:
        raise ValueError("authorization has missing or unsupported fields")
    if not all(_text(document[key]) for key in fields - {"grants"}):
        raise ValueError("authorization requires non-empty basis, reference, scope and confirmation")
    try:
        confirmed = datetime.fromisoformat(document["confirmed_at"])
        if confirmed.utcoffset() is None or confirmed > datetime.now(timezone.utc):
            raise ValueError()
    except ValueError as error:
        raise ValueError("authorization confirmed_at requires an ISO timestamp with timezone") from error
    if document["remote_acquisition"] not in {"allowed", "denied", "unconfirmed"}:
        raise ValueError("authorization remote_acquisition must be allowed, denied or unconfirmed")
    grants = document["grants"]
    if not isinstance(grants, list) or not grants:
        raise ValueError("authorization grants must be a non-empty list")
    grant_fields = {"app_id", "profile_app_version", "profile_sha256", "method", "acquisition_environment", "target_ids", "scope_matches_profile"}
    seen = set()
    for grant in grants:
        if not isinstance(grant, dict) or set(grant) != grant_fields:
            raise ValueError("authorization grant fields invalid; account/date/item filters are not supported")
        if not all(_text(grant[key]) for key in grant_fields - {"target_ids", "scope_matches_profile"}):
            raise ValueError("authorization grant identity must be non-empty")
        if (grant["method"] not in {"materialize", "export"}
                or grant["acquisition_environment"] not in {"device_only", "controlled_online"}
                or grant["scope_matches_profile"] is not True):
            raise ValueError("authorization requires confirmation of the entire profile execution scope")
        if re.fullmatch(r"[0-9a-f]{64}", grant["profile_sha256"]) is None:
            raise ValueError("authorization profile_sha256 must be a lowercase SHA-256")
        targets = grant["target_ids"]
        if not isinstance(targets, list) or not targets or not all(_text(x) for x in targets) or len(set(targets)) != len(targets):
            raise ValueError("authorization target_ids must be unique non-empty strings")
        key = tuple(grant[x] for x in ("app_id", "profile_app_version", "method", "acquisition_environment"))
        if key in seen:
            raise ValueError("authorization contains duplicate execution grants")
        seen.add(key)
    if "network_control" in document:
        control = document["network_control"]
        if (not isinstance(control, dict) or set(control) != {"external_traffic_restriction", "reference"}
                or control["external_traffic_restriction"] not in ("not_applied", "configured", "not_reported")
                or not isinstance(control["reference"], str)
                or (control["external_traffic_restriction"] == "configured" and not _text(control["reference"]))):
            raise ValueError("authorization network_control declaration is invalid")


def authorization_record(document: dict) -> dict:
    validate_authorization(document)
    encoded = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return {"document": json.loads(encoded), "document_sha256": hashlib.sha256(encoded).hexdigest()}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate authorization key: {key}")
        result[key] = value
    return result


def load_authorization(path: Path) -> dict:
    return authorization_record(json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object))


def grant_for(entry: dict, authority: dict) -> dict | None:
    document = authority["document"]
    if entry.get("installed_app_version", entry["profile_app_version"]) != entry["profile_app_version"]:
        return None
    if entry["acquisition_environment"] == "controlled_online" and document["remote_acquisition"] != "allowed":
        return None
    return next((grant for grant in document["grants"]
                 if all(grant[key] == entry[key] for key in ("app_id", "profile_app_version", "profile_sha256", "method", "acquisition_environment"))
                 and set(grant["target_ids"]) == set(entry["target_ids"])), None)


def authorize_plan(plan: tuple[dict, ...], authority: dict) -> tuple[dict, ...]:
    result = []
    for entry in plan:
        grant = grant_for(entry, authority)
        reason = None
        if grant is None:
            remote = authority["document"]["remote_acquisition"]
            reason = f"remote_acquisition_{remote}" if entry["acquisition_environment"] == "controlled_online" and remote != "allowed" else "no_matching_full_scope_grant"
        result.append({**entry, "status": "planned" if grant else "excluded",
                       "authorization_grant": grant, "authorization_document_sha256": authority["document_sha256"],
                       "exclusion_reason": reason})
    return tuple(result)

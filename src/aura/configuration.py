"""Session input configuration, reusing the existing authority/scope checks."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .authorization import _unique_object, authorization_record, validate_authorization


AUTHORIZATION_FIELDS = {"basis_type", "reference", "scope", "confirmed_by", "confirmed_at", "remote_acquisition"}


def authorization_from_configuration(document: dict) -> dict:
    if isinstance(document, dict) and "basis_type" in document:
        validate_authorization(document)  # Historical flat input; preserve its original hash.
        return document
    fields = {"schema_version", "execution_scopes", "authorization", "network_control"}
    if (not isinstance(document, dict) or set(document) != fields
            or type(document["schema_version"]) is not int or document["schema_version"] != 1
            or not isinstance(document["authorization"], dict)
            or set(document["authorization"]) != AUTHORIZATION_FIELDS):
        raise ValueError("session configuration fields or schema_version invalid")
    authority = {**document["authorization"], "grants": document["execution_scopes"],
                 "network_control": document["network_control"]}
    validate_authorization(authority)
    return authority


def configuration_record(document: dict) -> dict:
    authorization_from_configuration(document)
    encoded = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return {"document": json.loads(encoded), "document_sha256": hashlib.sha256(encoded).hexdigest()}


def load_configuration(path: Path) -> dict:
    return configuration_record(json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object))


def validate_configuration_link(configuration: dict, authority: dict) -> None:
    if (not isinstance(configuration, dict)
            or configuration_record(configuration.get("document")) != configuration
            or authorization_record(authorization_from_configuration(configuration["document"])) != authority):
        raise ValueError("session configuration is altered or differs from its authorization projection")

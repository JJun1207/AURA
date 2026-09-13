from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .device import COORDINATE_SELECTOR_KEYS
from .models import Route


class ProfileError(ValueError):
    pass


@dataclass(frozen=True)
class AcquisitionTarget:
    target_id: str
    item_types: tuple[str, ...]
    method: Route
    acquisition_environments: tuple[str, ...]
    expected_artifacts: tuple[str, ...]
    rules: dict[str, dict[str, Any]] = field(default_factory=dict)

    def validate_rules(self) -> None:
        expected = {
            "identification": "registered_ui_items",
            "traversal_completion": "explicit_ui_boundary",
            "result": "required_artifacts",
            "error_handling": "preserve_partial",
        }
        bound_suffix = {"traversal_completion": "verified_ui_boundary", "result": "required_artifacts", "error_handling": "preserve_partial"}
        if set(self.rules) != set(expected) or any(
            not isinstance(rule, dict)
            or not isinstance(rule.get("id"), str)
            or (rule.get("id") != expected[name] and (
                not rule["id"].startswith(self.target_id.split(".")[0] + ".") if name == "identification"
                else rule["id"] != self.target_id + "." + bound_suffix[name]
            ))
            or not isinstance(rule.get("parameters"), dict)
            for name, rule in self.rules.items()
        ):
            raise ProfileError(f"{self.target_id}: missing or unknown rules")
        counted = self.rules["identification"]["parameters"].get("counted_item_types")
        boundaries = self.rules["traversal_completion"]["parameters"].get("completion_conditions")
        required = self.rules["result"]["parameters"].get("required_artifacts")
        errors = self.rules["error_handling"]["parameters"]
        if self.target_id == "telegram.account":
            implemented_boundaries = {"account_profile_verified"}
        elif self.target_id in {"telegram.conversations", "telegram.attachments"}:
            implemented_boundaries = {"chat_list_and_histories_exhausted"}
        elif self.target_id == "chrome.account":
            implemented_boundaries = {"single_screen_parsed"}
        elif self.target_id == "chrome.bookmarks":
            implemented_boundaries = {"recursive_bookmarks_exhausted"}
        elif self.target_id.startswith(("chrome.", "samsung_browser.")):
            implemented_boundaries = {"repeated_hierarchy"}
        elif self.target_id.startswith("google_drive."):
            implemented_boundaries = {"recursive_listing_exhausted"}
        elif self.target_id == "notesnook.export":
            implemented_boundaries = {"export_selection_verified"}
        elif self.target_id.startswith("notesnook."):
            implemented_boundaries = {"notes_and_trash_exhausted"}
        elif self.target_id in {"whatsapp.conversations", "whatsapp.attachments"}:
            implemented_boundaries = {"chat_history_exhausted", "active_chat_list_exhausted"}
        elif self.target_id == "whatsapp.chat_export":
            implemented_boundaries = {"export_target_identified", "active_chat_list_exhausted"}
        else:
            implemented_boundaries = {"all_workspaces_visited"}
        if (
            not isinstance(counted, list) or not counted
            or not set(counted) <= set(self.item_types)
            or not isinstance(boundaries, list) or not boundaries
            or not all(isinstance(value, str) and value for value in boundaries)
            or not set(boundaries) <= implemented_boundaries
            or not isinstance(required, dict)
            or set(required) != set(self.expected_artifacts)
            or any(not isinstance(kinds, list) or not kinds or not all(isinstance(kind, str) and kind for kind in kinds) for kinds in required.values())
            or errors.get("on_interruption") != "preserve_partial"
            or not isinstance(errors.get("fallbacks"), dict)
        ):
            raise ProfileError(f"{self.target_id}: inapplicable rules parameters")
        allowed_kinds = {
            "structured_data": {"structured_data", "ui_record"} | ({"structured_json"} if self.target_id.startswith("whatsapp.") else set()),
            "ui_observation": {"screen_image", "ui_hierarchy"},
            "downloaded_file": {"downloaded_file", "original_artifact", "app_materialized_file"},
            "saved_file": {"saved_file", "original_artifact", "app_materialized_file"},
            "export_file": {"export_file", "original_artifact", "app_export"},
            "file": {"file", "acquired_file"},
        }
        if any(name not in allowed_kinds or not set(kinds) <= allowed_kinds[name]
               or (name == "ui_observation" and set(kinds) != allowed_kinds[name])
               for name, kinds in required.items()):
            raise ProfileError(f"{self.target_id}: unsupported result rules artifact kinds")
        implemented_fallbacks = ({"display_fallback": {
            "conditions": ["save_action_unavailable"],
            "required_artifacts": ["ui_record", "screen_image", "ui_hierarchy", "audit_record"],
            "acquisition_status": "partial",
        }} if self.target_id == "telegram.attachments" else {})
        if errors["fallbacks"] != implemented_fallbacks:
            raise ProfileError(f"{self.target_id}: unsupported fallback rules")


@dataclass(frozen=True)
class AppProfile:
    app_id: str
    package_name: str
    app_version: str
    routes: tuple[Route, ...]
    targets: tuple[AcquisitionTarget, ...]
    selectors: dict[str, dict[str, Any]]
    timings: dict[str, float]
    parameters: dict[str, Any]
    android_versions: dict[str, int] | None = None

    def require_identification_rules(self, contracts: dict[str, tuple[str, dict[str, Any]]]) -> None:
        for target in self.targets:
            expected = contracts.get(target.target_id)
            rule = target.rules["identification"]
            if expected is None or rule["id"] != expected[0] or rule["parameters"].get("ui_criteria") != expected[1]:
                raise ProfileError(f"{target.target_id}: identification rule or UI criteria do not match the collector")

    def supports_android(self, version: str | None) -> bool:
        if self.android_versions is None:
            return True
        if not isinstance(version, str) or re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", version.strip()) is None:
            return False
        major = int(version.strip().split(".")[0])
        return self.android_versions["min"] <= major <= self.android_versions.get("max", major)


@dataclass(frozen=True)
class SystemUIProfile:
    name: str
    description: str
    manufacturer_match: tuple[str, ...]
    operations: dict[str, dict[str, Any]]

    @property
    def profile_id(self) -> str:
        return self.name

    def supports(self, manufacturer: str) -> bool:
        normalized = manufacturer.strip().casefold()
        return any(token.casefold() in normalized for token in self.manufacturer_match)


class ProfileStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def load_app(self, app_id: str, app_version: str) -> AppProfile:
        parts = self._safe_segments(app_id, app_version)
        document = self._read(self.root / "apps" / parts[0] / f"{parts[1]}.json")
        self._match(
            document,
            app_id=app_id,
            app_version=app_version,
        )

        package_name = self._string(document, "package_name")
        routes_value = document.get("acquisition_methods")
        if not isinstance(routes_value, list) or not routes_value:
            raise ProfileError("acquisition_methods must be a non-empty list")
        try:
            routes = tuple(Route(value) for value in routes_value)
        except (TypeError, ValueError) as error:
            raise ProfileError(
                "acquisition_methods contain an unknown method"
            ) from error
        if len(routes) != len(set(routes)):
            raise ProfileError(
                "acquisition_methods must not contain duplicates"
            )

        targets_value = document.get("targets")
        if not isinstance(targets_value, list) or not targets_value:
            raise ProfileError("targets must be a non-empty list")
        targets = tuple(
            self._target(value, routes) for value in targets_value
        )
        target_ids = [target.target_id for target in targets]
        if len(target_ids) != len(set(target_ids)):
            raise ProfileError("target_id must not contain duplicates")
        for target in targets:
            target.validate_rules()

        selectors_value = document.get("selectors")
        if not isinstance(selectors_value, dict) or not all(
            isinstance(key, str) and isinstance(value, dict)
            for key, value in selectors_value.items()
        ):
            raise ProfileError("selectors must be an object of selector objects")
        self._reject_coordinate_keys(selectors_value)

        timings_value = document.get("timings")
        if not isinstance(timings_value, dict) or not all(
            isinstance(key, str)
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
            and value >= 0
            for key, value in timings_value.items()
        ):
            raise ProfileError("timings must contain non-negative numbers")

        parameters_value = document.get("parameters", {})
        if not isinstance(parameters_value, dict):
            raise ProfileError("parameters must be an object")
        self._reject_coordinate_keys(parameters_value)

        android_versions = document.get("android_versions")
        if "android_versions" in document and (
            not isinstance(android_versions, dict)
            or "min" not in android_versions
            or not android_versions.keys() <= {"min", "max"}
            or any(type(value) is not int or value <= 0 for value in android_versions.values())
            or android_versions.get("max", android_versions["min"]) < android_versions["min"]
        ):
            raise ProfileError("android_versions must contain positive integer min and optional max >= min")

        return AppProfile(
            app_id=app_id,
            package_name=package_name,
            app_version=app_version,
            routes=routes,
            targets=targets,
            selectors={key: dict(value) for key, value in selectors_value.items()},
            timings={key: float(value) for key, value in timings_value.items()},
            parameters=dict(parameters_value),
            android_versions=None if android_versions is None else dict(android_versions),
        )

    @classmethod
    def _target(
        cls, value: Any, acquisition_methods: tuple[Route, ...]
    ) -> AcquisitionTarget:
        if not isinstance(value, dict):
            raise ProfileError("targets must contain objects")
        target_id = cls._string(value, "target_id")
        item_types = cls._string_list(value, "item_types")
        expected_artifacts = cls._string_list(value, "expected_artifacts")
        acquisition_environments = cls._string_list(value, "acquisition_environments")
        if not set(acquisition_environments) <= {"device_only", "controlled_online"}:
            raise ProfileError("acquisition_environments contain an unknown acquisition_environment")
        try:
            method = Route(value.get("method"))
        except (TypeError, ValueError) as error:
            raise ProfileError("method is unknown") from error
        if method not in acquisition_methods:
            raise ProfileError("method is not declared in acquisition_methods")
        target = AcquisitionTarget(
            target_id=target_id,
            item_types=item_types,
            method=method,
            acquisition_environments=acquisition_environments,
            expected_artifacts=expected_artifacts,
            rules=value.get("rules", {}),
        )
        return target

    def load_system_ui(self, name: str) -> SystemUIProfile:
        (safe_name,) = self._safe_segments(name)
        document = self._read(self.root / "system_ui" / f"{safe_name}.json")
        self._match(document, name=name)

        description = document.get("description", "")
        if not isinstance(description, str):
            raise ProfileError("description must be a string")

        matches = document.get("manufacturer_match")
        if (
            not isinstance(matches, list)
            or not matches
            or not all(isinstance(value, str) and value for value in matches)
        ):
            raise ProfileError(
                "manufacturer_match must be a non-empty list of strings"
            )

        operations = document.get("operations")
        if not operations or not all(
            isinstance(key, str) and isinstance(value, dict)
            for key, value in operations.items()
        ):
            raise ProfileError("System UI operations must be objects")
        self._reject_coordinate_keys(operations)

        return SystemUIProfile(
            name=name,
            description=description,
            manufacturer_match=tuple(matches),
            operations={key: dict(value) for key, value in operations.items()},
        )

    @staticmethod
    def _safe_segments(*values: str) -> tuple[str, ...]:
        for value in values:
            if (
                not isinstance(value, str)
                or not value
                or value in {".", ".."}
                or "/" in value
                or "\\" in value
                or Path(value).is_absolute()
            ):
                raise ProfileError(f"unsafe Profile path segment: {value!r}")
        return values

    @staticmethod
    def _read(path: Path) -> dict[str, Any]:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ProfileError(f"cannot read Profile: {path}") from error
        if not isinstance(document, dict):
            raise ProfileError("Profile must be a JSON object")
        return document

    @classmethod
    def _match(cls, document: dict[str, Any], **expected: str) -> None:
        for key, value in expected.items():
            actual = cls._string(document, key)
            if actual != value:
                raise ProfileError(
                    f"{key} does not match Profile path: {actual!r} != {value!r}"
                )

    @staticmethod
    def _string(document: dict[str, Any], key: str) -> str:
        value = document.get(key)
        if not isinstance(value, str) or not value:
            raise ProfileError(f"{key} must be a non-empty string")
        return value

    @classmethod
    def _string_list(cls, document: dict[str, Any], key: str) -> tuple[str, ...]:
        value = document.get(key)
        if (
            not isinstance(value, list)
            or not value
            or not all(isinstance(item, str) and item for item in value)
            or len(value) != len(set(value))
        ):
            raise ProfileError(f"{key} must be a non-empty list of unique strings")
        return tuple(value)

    @classmethod
    def _reject_coordinate_keys(cls, value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if str(key).strip().casefold() in COORDINATE_SELECTOR_KEYS:
                    raise ProfileError(
                        f"coordinate-based selection is not allowed: {key}"
                    )
                cls._reject_coordinate_keys(child)
        elif isinstance(value, list):
            for child in value:
                cls._reject_coordinate_keys(child)

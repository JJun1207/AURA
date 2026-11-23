from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Mapping, Protocol
from .geometry import intersect_bounds, parse_bounds


Selector = Mapping[str, object]
Bounds = tuple[int, int, int, int]
COORDINATE_SELECTOR_KEYS = frozenset(
    {
        "x",
        "y",
        "x1",
        "y1",
        "x2",
        "y2",
        "point",
        "position",
        "bounds",
        "coordinate",
        "coordinates",
    }
)
LONG_CLICK_SECONDS = 0.8


@dataclass(frozen=True)
class InstalledPackage:
    package_name: str
    version_name: str | None


def validate_selector(selector: Selector) -> None:
    for key in selector:
        if str(key).strip().casefold() in COORDINATE_SELECTOR_KEYS:
            raise ValueError(f"coordinate-based selection is not allowed: {key}")


def validate_bounds(bounds: object, window_size: tuple[int, int]) -> Bounds:
    if not isinstance(bounds, (list, tuple)) or len(bounds) != 4:
        raise ValueError("bounds must contain exactly four integers")
    if any(type(value) is not int for value in bounds):
        raise ValueError("bounds must contain exactly four integers")
    left, top, right, bottom = bounds
    width, height = window_size
    if (
        left < 0
        or top < 0
        or right <= left
        or bottom <= top
        or right > width
        or bottom > height
    ):
        raise ValueError("bounds must be a positive rectangle inside the window")
    return left, top, right, bottom


class Device(Protocol):
    def interaction_bounds(self) -> Bounds: ...

    def installed_packages(self) -> tuple[InstalledPackage, ...]: ...

    def app_start(self, package_name: str) -> None: ...

    def app_stop(self, package_name: str) -> None: ...

    def exists(self, selector: Selector) -> bool: ...

    def count(self, selector: Selector) -> int: ...

    def click(self, selector: Selector) -> None: ...

    def click_xpath(self, xpath: str) -> None: ...

    def click_bounds(self, bounds: Bounds, *, anchor: str = "center") -> None: ...

    def long_click(self, selector: Selector) -> None: ...

    def long_click_bounds(self, bounds: Bounds) -> None: ...

    def back(self) -> None: ...

    def swipe(
        self,
        direction: str,
        duration: float = 0.2,
        distance_ratio: float = 0.2,
    ) -> None: ...

    def swipe_bounds(
        self,
        bounds: Bounds,
        direction: str,
        duration: float = 0.2,
        distance_ratio: float = 0.8,
    ) -> None: ...

    def hierarchy(self) -> str: ...

    def screenshot(self) -> bytes: ...

    def read_clipboard_text(self) -> str | None: ...

    def shell(self, command: str) -> str: ...

    def pull(self, remote_path: str, local_path: str | Path) -> None: ...

    def window_size(self) -> tuple[int, int]: ...


class AndroidDevice:
    def __init__(self, ui_client: Any, adb_client: Any, *, serial: str | None):
        self._ui = ui_client
        self._adb = adb_client
        self.serial = serial
        self.capture_journal = None
        self._window_dimensions = None
        self._window_rotation = None
        self._static_properties: dict[str, str] = {}

    @classmethod
    def connect(cls, serial: str | None = None) -> AndroidDevice:
        import adbutils
        import uiautomator2
        from .automation import host_automation, observe_automation

        adb_client = (
            adbutils.adb.device(serial=serial)
            if serial
            else adbutils.adb.device()
        )
        evidence = {"host": host_automation(), "before": observe_automation(adb_client)}
        try:
            ui_client = uiautomator2.connect(serial)
        except (Exception, KeyboardInterrupt) as error:
            evidence["after"] = observe_automation(adb_client)
            evidence["connection_error"] = f"{type(error).__name__}: {error}"
            error.automation_evidence = evidence
            raise
        evidence["after"] = observe_automation(adb_client)
        device = cls(ui_client, adb_client, serial=serial)
        device.connection_evidence = evidence
        return device

    def automation_snapshot(self) -> dict:
        from .automation import observe_automation
        return observe_automation(self._adb)

    def app_start(self, package_name: str) -> None:
        self._ui.app_start(package_name)

    def installed_packages(self) -> tuple[InstalledPackage, ...]:
        user_id = self.shell("am get-current-user").strip()
        if not user_id.isdigit():
            raise RuntimeError("installed package inventory is invalid")
        active_paths = {}
        for line in self.shell(
            f"pm list packages -f --user {user_id}"
        ).splitlines():
            payload = line.removeprefix("package:")
            apk_path, separator, package_name = payload.rpartition("=")
            if (
                not line.startswith("package:")
                or not separator
                or not apk_path
                or not package_name
                or package_name in active_paths
            ):
                raise RuntimeError("installed package inventory is invalid")
            active_paths[package_name] = apk_path.rsplit("/", 1)[0]
        blocks = re.split(
            r"(?m)^  Package \[([^\]]+)\] \([^)]*\):\s*$",
            self.shell("dumpsys package packages"),
        )
        if not active_paths or len(blocks) == 1:
            raise RuntimeError("installed package inventory is invalid")
        versions = dict.fromkeys(active_paths)
        for package_name, body in zip(blocks[1::2], blocks[2::2]):
            code_path = re.search(
                r"(?m)^\s+codePath=([^\n]+)$",
                body,
            )
            if (
                package_name not in active_paths
                or code_path is None
                or code_path.group(1).strip() != active_paths[package_name]
            ):
                continue
            match = re.search(r"(?m)^\s+versionName=([^\n]*)$", body)
            version_name = None if match is None else match.group(1).strip()
            if version_name in {"", "null"}:
                version_name = None
            versions[package_name] = version_name
        return tuple(
            InstalledPackage(package_name, versions[package_name])
            for package_name in sorted(versions)
        )

    def app_stop(self, package_name: str) -> None:
        self._ui.app_stop(package_name)

    def exists(self, selector: Selector) -> bool:
        return bool(self._element(selector).exists(timeout=0))

    def count(self, selector: Selector) -> int:
        return int(self._element(selector).count)

    def click(self, selector: Selector) -> None:
        element = self._element(selector)
        element.must_wait()
        self.click_bounds(element.bounds())

    def click_xpath(self, xpath: str) -> None:
        matches = self._ui.xpath(xpath).all()
        if len(matches) != 1:
            raise RuntimeError(
                f"expected exactly one XPath target, found {len(matches)}"
            )
        self.click_bounds(matches[0].bounds)

    def click_bounds(self, bounds: Bounds, *, anchor: str = "center") -> None:
        self._touch(bounds, anchor=anchor)

    def _touch(self, bounds, *, anchor="center", duration=None):
        area = self.interaction_bounds()
        bounds = validate_bounds(bounds, self.window_size())
        visible = intersect_bounds(bounds, area)
        if visible is None:
            raise ValueError("target is outside the interaction area")
        if anchor == "center":
            ratio = .5
        elif anchor == "lower_third":
            ratio = .65
        else:
            raise ValueError(f"unsupported bounds anchor: {anchor}")
        def point(rect):
            return ((rect[0] + rect[2]) // 2, rect[1] + int((rect[3] - rect[1]) * ratio))
        x, y = point(bounds)
        if not (area[0] <= x < area[2] and area[1] <= y < area[3]):
            x, y = point(visible)
        self._record_input("long_click" if duration is not None else "click", area,
                           bounds=bounds, coordinates=[x, y])
        if duration is None:
            self._ui.click(x, y)
        else:
            self._ui.long_click(x, y, duration=duration)

    def long_click(self, selector: Selector) -> None:
        element = self._element(selector)
        element.must_wait()
        self._touch(element.bounds(), duration=.5)

    def long_click_bounds(self, bounds: Bounds) -> None:
        self._touch(bounds, duration=LONG_CLICK_SECONDS)

    def _record_input(self, kind, area, **details):
        if self.capture_journal is not None:
            self.capture_journal.append("input_prepared", status="observed", details={
                "input_kind": kind, "interaction_bounds": area,
                "geometry_source": "dumpsys window displays", **details})

    def interaction_bounds(self) -> Bounds:
        """Read current system insets; hidden bars and rotation can change per screen."""
        output = self.shell("dumpsys window displays")
        sections = re.split(r"(?m)^\s*Display: mDisplayId=", output)
        if len(sections) > 1:
            output = next((part for part in sections[1:] if re.match(r"0\b", part)), "")
        frame = re.search(r"DisplayFrames w=(\d+) h=(\d+) r=([0-3])", output)
        if frame:
            self._window_dimensions = (int(frame[1]), int(frame[2]))
            self._window_rotation = frame[3]
        width, height = self.window_size()
        area = [0, 0, width, height]
        bars = re.findall(r"type=(statusBars|navigationBars|ITYPE_STATUS_BAR|ITYPE_NAVIGATION_BAR)\s+frame=(\[-?\d+,-?\d+\]\[-?\d+,-?\d+\])[^\n]*?visible=(true|false)", output)
        kinds = {kind for kind, _, _ in bars}
        if not (kinds & {"statusBars", "ITYPE_STATUS_BAR"} and kinds & {"navigationBars", "ITYPE_NAVIGATION_BAR"}):
            # Older WindowManager output exposes the stable display rectangle.
            bracket_stable = re.search(r"\bmStable=(\[-?\d+,-?\d+\]\[-?\d+,-?\d+\])", output)
            if bracket_stable is not None:
                return validate_bounds(parse_bounds(bracket_stable[1]), (width, height))
            stable = re.search(r"\bmStable=(?:Rect\()?\(?(-?\d+),\s*(-?\d+)\)?\s*-\s*\(?(-?\d+),\s*(-?\d+)\)?", output)
            if stable is None:
                raise ValueError("system interaction area is unavailable")
            return validate_bounds(tuple(map(int, stable.groups())), (width, height))
        for _, value, visible in bars:
            if visible == 'false':
                continue
            rect = intersect_bounds(parse_bounds(value, positive_area=False), (0, 0, width, height))
            if rect is None:
                raise ValueError("system interaction area has unavailable bar geometry")
            left, top, right, bottom = rect
            if left == 0 and right == width and top == 0 and bottom < height:
                area[1] = max(area[1], bottom)
            elif left == 0 and right == width and bottom == height and top > 0:
                area[3] = min(area[3], top)
            elif top == 0 and bottom == height and left == 0 and right < width:
                area[0] = max(area[0], right)
            elif top == 0 and bottom == height and right == width and left > 0:
                area[2] = min(area[2], left)
            else:
                raise ValueError("system interaction area has unsupported bar geometry")
        return validate_bounds(tuple(area), (width, height))

    def _swipe_input(self, coordinates, duration, area):
        x1, y1, x2, y2 = coordinates
        def clamp(x, y):
            return (max(area[0], min(x, area[2] - 1)), max(area[1], min(y, area[3] - 1)))
        x1, y1 = clamp(x1, y1)
        x2, y2 = clamp(x2, y2)
        if (x1, y1) == (x2, y2):
            raise ValueError("swipe has no distance inside the interaction area")
        self._record_input("swipe", area, coordinates=[x1, y1, x2, y2], duration=duration)
        self._ui.swipe(x1, y1, x2, y2, duration)

    def back(self) -> None:
        self._ui.press("back")

    def swipe(
        self,
        direction: str,
        duration: float = 0.2,
        distance_ratio: float = 0.2,
    ) -> None:
        if (
            isinstance(distance_ratio, bool)
            or not isinstance(distance_ratio, (int, float))
            or not 0 < distance_ratio <= 1
        ):
            raise ValueError("swipe distance ratio must be within (0, 1]")
        area = self.interaction_bounds()
        width, height = self.window_size()
        horizontal = max(1, int(width * distance_ratio))
        vertical = max(1, int(height * distance_ratio))
        left = (width - horizontal) // 2
        right = left + horizontal
        top = (height - vertical) // 2
        bottom = top + vertical
        coordinates = {
            "up": (width // 2, bottom, width // 2, top),
            "down": (width // 2, top, width // 2, bottom),
            "left": (right, height // 2, left, height // 2),
            "right": (left, height // 2, right, height // 2),
        }
        try:
            start_x, start_y, end_x, end_y = coordinates[direction]
        except KeyError as error:
            raise ValueError(f"unsupported swipe direction: {direction}") from error
        self._swipe_input((start_x, start_y, end_x, end_y), duration, area)

    def swipe_bounds(
        self,
        bounds: Bounds,
        direction: str,
        duration: float = 0.2,
        distance_ratio: float = 0.8,
    ) -> None:
        area = self.interaction_bounds()
        left, top, right, bottom = validate_bounds(
            bounds, self.window_size()
        )
        area = intersect_bounds(bounds, area)
        if area is None:
            raise ValueError("swipe is outside the interaction area")
        if (
            isinstance(distance_ratio, bool)
            or not isinstance(distance_ratio, (int, float))
            or not 0 < distance_ratio <= 1
        ):
            raise ValueError("swipe distance ratio must be within (0, 1]")
        center_x = (left + right) // 2
        center_y = (top + bottom) // 2
        horizontal = max(1, int((right - left) * distance_ratio))
        vertical = max(1, int((bottom - top) * distance_ratio))
        x1 = center_x - horizontal // 2
        x2 = x1 + horizontal
        y1 = center_y - vertical // 2
        y2 = y1 + vertical
        coordinates = {
            "up": (center_x, y2, center_x, y1),
            "down": (center_x, y1, center_x, y2),
            "left": (x2, center_y, x1, center_y),
            "right": (x1, center_y, x2, center_y),
        }
        try:
            start_x, start_y, end_x, end_y = coordinates[direction]
        except KeyError as error:
            raise ValueError(
                f"unsupported swipe direction: {direction}"
            ) from error
        self._swipe_input((start_x, start_y, end_x, end_y), duration, area)

    def hierarchy(self) -> str:
        hierarchy = self._ui.dump_hierarchy(compressed=False)
        rotation = re.search(r"<hierarchy\b[^>]*\brotation=[\"']([0-3])[\"']", hierarchy)
        current = rotation.group(1) if rotation else None
        if current is None or current != self._window_rotation:
            self._window_dimensions = None
        self._window_rotation = current
        return hierarchy

    def screenshot(self) -> bytes:
        from PIL import Image

        def record(status, error=None):
            journal = self.capture_journal
            if journal is None:
                return
            source = journal.actions[-1] if journal.actions else None
            journal.append(
                "screen_capture", status=status,
                action_id=source.action_id if source else None,
                attempt_id=source.attempt_id if source else None,
                context=source.context if source else None,
                details={
                    "source": "uiautomator2.takeScreenshot(1, 80)",
                    "capture_attempt": attempt, "max_attempts": 3,
                    "error_type": type(error).__name__ if error else None,
                    "error": str(error) if error else None,
                },
            )

        for attempt in range(1, 4):
            # Old's default uiautomator2 RPC, without its ADB/black-image fallback.
            # RPC/transport errors and interrupts must not be retried as decoding errors.
            encoded = self._ui.jsonrpc.takeScreenshot(1, 80)
            try:
                raw = base64.b64decode(encoded) if encoded else b""
                with Image.open(BytesIO(raw)) as screenshot:
                    buffer = BytesIO()
                    screenshot.save(buffer, format="PNG")
            except (OSError, ValueError) as error:
                record("failed" if attempt == 3 else "retrying", error)
                if attempt == 3:
                    raise
                continue
            if attempt > 1:
                record("recovered")
            return buffer.getvalue()

    def read_clipboard_text(self) -> str | None:
        """Read the native clipboard without installing/changing an IME."""
        return self._ui.clipboard

    def shell(self, command: str) -> str:
        # Only these read-only properties are fixed for this device connection.
        static = command in {
            "getprop ro.product.model", "getprop ro.product.manufacturer",
            "getprop ro.build.version.release",
        }
        if static and command in self._static_properties:
            return self._static_properties[command]
        value = str(self._adb.shell(command))
        if static and value.strip():
            self._static_properties[command] = value
        return value

    def pull(self, remote_path: str, local_path: str | Path) -> None:
        self._adb.sync.pull(remote_path, str(local_path))

    def window_size(self) -> tuple[int, int]:
        if self._window_dimensions is None:
            width, height = self._ui.window_size()
            self._window_dimensions = int(width), int(height)
        return self._window_dimensions

    def _element(self, selector: Selector):
        validate_selector(selector)
        return self._ui(**dict(selector))

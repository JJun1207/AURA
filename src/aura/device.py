from __future__ import annotations

import re
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Mapping, Protocol


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

    def shell(self, command: str) -> str: ...

    def pull(self, remote_path: str, local_path: str | Path) -> None: ...

    def window_size(self) -> tuple[int, int]: ...


class AndroidDevice:
    def __init__(self, ui_client: Any, adb_client: Any, *, serial: str | None):
        self._ui = ui_client
        self._adb = adb_client
        self.serial = serial

    @classmethod
    def connect(cls, serial: str | None = None) -> AndroidDevice:
        import adbutils
        import uiautomator2

        ui_client = uiautomator2.connect(serial)
        adb_client = (
            adbutils.adb.device(serial=serial)
            if serial
            else adbutils.adb.device()
        )
        return cls(ui_client, adb_client, serial=serial)

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
        self._element(selector).click()

    def click_xpath(self, xpath: str) -> None:
        matches = self._ui.xpath(xpath).all()
        if len(matches) != 1:
            raise RuntimeError(
                f"expected exactly one XPath target, found {len(matches)}"
            )
        matches[0].click()

    def click_bounds(self, bounds: Bounds, *, anchor: str = "center") -> None:
        left, top, right, bottom = validate_bounds(bounds, self.window_size())
        if anchor == "center":
            y = (top + bottom) // 2
        elif anchor == "lower_third":
            y = top + int((bottom - top) * 0.65)
        else:
            raise ValueError(f"unsupported bounds anchor: {anchor}")
        self._ui.click((left + right) // 2, y)

    def long_click(self, selector: Selector) -> None:
        self._element(selector).long_click()

    def long_click_bounds(self, bounds: Bounds) -> None:
        left, top, right, bottom = validate_bounds(bounds, self.window_size())
        self._ui.long_click(
            (left + right) // 2,
            (top + bottom) // 2,
            duration=LONG_CLICK_SECONDS,
        )

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
        width, height = self._ui.window_size()
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
        self._ui.swipe(start_x, start_y, end_x, end_y, duration)

    def swipe_bounds(
        self,
        bounds: Bounds,
        direction: str,
        duration: float = 0.2,
        distance_ratio: float = 0.8,
    ) -> None:
        left, top, right, bottom = validate_bounds(
            bounds, self.window_size()
        )
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
        self._ui.swipe(start_x, start_y, end_x, end_y, duration)

    def hierarchy(self) -> str:
        return self._ui.dump_hierarchy(compressed=False)

    def screenshot(self) -> bytes:
        screenshot = self._ui.screenshot()
        if isinstance(screenshot, bytes):
            return screenshot
        if hasattr(screenshot, "save"):
            buffer = BytesIO()
            screenshot.save(buffer, format="PNG")
            return buffer.getvalue()
        raise TypeError("uiautomator2 screenshot did not return PNG bytes or an image")

    def shell(self, command: str) -> str:
        return str(self._adb.shell(command))

    def pull(self, remote_path: str, local_path: str | Path) -> None:
        self._adb.sync.pull(remote_path, str(local_path))

    def window_size(self) -> tuple[int, int]:
        width, height = self._ui.window_size()
        return int(width), int(height)

    def _element(self, selector: Selector):
        validate_selector(selector)
        return self._ui(**dict(selector))

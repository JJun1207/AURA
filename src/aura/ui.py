from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from typing import Any

from .device import Bounds, Device, Selector, validate_selector
from .journal import EventJournal
from .models import ActionRef


class UiTimeout(RuntimeError):
    pass


def apply_state(read: Callable[[], bool], target: bool, change: Callable[[], None],
                verify: Callable[[], bool], record: Callable[..., None]) -> bool:
    """Change a boolean setting only when needed; callers verify and record it."""
    before = read()
    try:
        if before is not target:
            change()
        after = verify()
    except Exception as error:
        record("failed", before=before, target=target, after=None, error=error)
        raise
    record("success", before=before, target=target, after=after)
    return after


def hierarchy_signature(hierarchy: str | bytes | ET.Element) -> tuple | None:
    """Compare interaction content, without changing the original evidence tree."""
    root = hierarchy if isinstance(hierarchy, ET.Element) else ET.fromstring(hierarchy)

    def content(node):
        system_ui = node.get("package") == "com.android.systemui"
        if system_ui and node.get("resource-id", "").rsplit("/", 1)[-1] in {
            "status_bar", "status_bar_container", "navigation_bar_frame", "navigation_bar",
        }:
            return None
        children = tuple(value for child in node if (value := content(child)) is not None)
        if system_ui and len(node) and not children:
            return None  # Also omit a window that contained only system bars.
        attributes = tuple(sorted((key, value) for key, value in node.attrib.items()
                                  if key != "bounds"))
        return node.tag, attributes, (node.text or "").strip(), children

    return content(root)


class UiRuntime:
    def __init__(
        self,
        device: Device,
        journal: EventJournal,
        *,
        default_timeout: float = 5.0,
        poll_interval: float = 0.2,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if default_timeout < 0 or poll_interval < 0:
            raise ValueError("timeouts must be non-negative")
        self.device = device
        self.journal = journal
        self.default_timeout = default_timeout
        self.poll_interval = poll_interval
        self._monotonic = monotonic
        self._sleep = sleep

    def wait_until(
        self,
        predicate: Callable[[], bool],
        *,
        timeout: float | None = None,
    ) -> bool:
        limit = self.default_timeout if timeout is None else timeout
        if limit < 0:
            raise ValueError("timeout must be non-negative")
        deadline = self._monotonic() + limit
        while True:
            if predicate():
                return True
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                return False
            self._sleep(min(self.poll_interval, remaining))

    def wait_for(
        self,
        selector: Selector,
        *,
        timeout: float | None = None,
    ) -> bool:
        validate_selector(selector)
        return self.wait_until(lambda: self.device.exists(selector), timeout=timeout)

    def click(
        self,
        name: str,
        selector: Selector,
        *,
        expected: Selector | None = None,
        context: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> ActionRef:
        validate_selector(selector)
        for attempt in range(2):
            if not self.wait_until(
                lambda: self.device.count(selector) == 1,
                timeout=timeout,
            ):
                match_count = self.device.count(selector)
                stage = "target_not_found" if match_count == 0 else "target_ambiguous"
                self._failed(
                    name,
                    context,
                    stage,
                    selector=dict(selector),
                    match_count=match_count,
                )
                if match_count == 0:
                    raise UiTimeout(f"{name}: target not found")
                raise UiTimeout(
                    f"{name}: expected exactly one target, found {match_count}"
                )
            try:
                self.device.click(selector)
            except Exception as error:
                # uiautomator2 3.5 reads objInfo before dispatching the tap.
                # Retry that read failure only; input/transport failures may
                # already have performed the action and must not be replayed.
                if (attempt == 0 and type(error).__name__ == "RPCUnknownError"
                        and "androidx.test.uiautomator.StaleObjectException" in str(error)
                        and "AutomatorServiceImpl.objInfo(" in str(error)):
                    self.journal.record_action(name, status="retrying", context=context, details={
                        "stage": "stale_target_before_click", "selector": dict(selector),
                        "error": str(error), "retry": 1, "settle_seconds": self.poll_interval,
                    })
                    self._sleep(self.poll_interval)
                    continue
                self._failed(
                    name,
                    context,
                    "device_action_failed",
                    selector=dict(selector),
                    error=type(error).__name__,
                )
                raise
            break
        if expected is not None and not self.wait_for(expected, timeout=timeout):
            self._failed(
                name,
                context,
                "expected_state_not_found",
                selector=dict(selector),
                expected=dict(expected),
            )
            raise UiTimeout(f"{name}: expected state not found")
        return self.journal.record_action(
            name,
            status="success",
            context=context,
            details={
                "selector": dict(selector),
                "expected": dict(expected) if expected is not None else None,
            },
        )

    def back(
        self,
        name: str,
        *,
        expected: Selector | None = None,
        context: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> ActionRef:
        try:
            self.device.back()
        except Exception as error:
            self._failed(
                name,
                context,
                "device_action_failed",
                error=type(error).__name__,
            )
            raise
        if expected is not None and not self.wait_for(expected, timeout=timeout):
            self._failed(
                name,
                context,
                "expected_state_not_found",
                expected=dict(expected),
            )
            raise UiTimeout(f"{name}: expected state not found")
        return self.journal.record_action(
            name,
            status="success",
            context=context,
            details={"expected": dict(expected) if expected is not None else None},
        )

    def click_bounds(
        self,
        name: str,
        bounds: Bounds,
        *,
        anchor: str = "center",
        context: dict[str, Any] | None = None,
    ) -> ActionRef:
        try:
            self.device.click_bounds(bounds, anchor=anchor)
        except Exception as error:
            self._failed(
                name,
                context,
                "device_action_failed",
                bounds=tuple(bounds),
                anchor=anchor,
                error=type(error).__name__,
            )
            raise
        return self.journal.record_action(
            name,
            status="success",
            context=context,
            details={"bounds": tuple(bounds), "anchor": anchor},
        )

    def long_click_bounds(
        self,
        name: str,
        bounds: Bounds,
        *,
        context: dict[str, Any] | None = None,
    ) -> ActionRef:
        try:
            self.device.long_click_bounds(bounds)
        except Exception as error:
            self._failed(
                name,
                context,
                "device_action_failed",
                bounds=tuple(bounds),
                error=type(error).__name__,
            )
            raise
        return self.journal.record_action(
            name,
            status="success",
            context=context,
            details={"bounds": tuple(bounds)},
        )

    def swipe(
        self,
        name: str,
        direction: str,
        *,
        duration: float = 0.2,
        distance_ratio: float | None = None,
        expect_change: bool = False,
        context: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> ActionRef:
        before_signature = hierarchy_signature(self.device.hierarchy()) if expect_change else None
        try:
            self.device.swipe(direction, duration, **(
                {"distance_ratio": distance_ratio} if distance_ratio is not None else {}))
        except Exception as error:
            self._failed(
                name,
                context,
                "device_action_failed",
                direction=direction,
                error=type(error).__name__,
            )
            raise
        if expect_change and not self.wait_until(
            lambda: hierarchy_signature(self.device.hierarchy()) != before_signature,
            timeout=timeout,
        ):
            self._failed(
                name,
                context,
                "hierarchy_unchanged",
                direction=direction,
            )
            raise UiTimeout(f"{name}: hierarchy did not change")
        return self.journal.record_action(
            name,
            status="success",
            context=context,
            details={"direction": direction, "duration": duration,
                     **({"distance_ratio": distance_ratio} if distance_ratio is not None else {})},
        )

    def swipe_bounds(
        self,
        name: str,
        bounds: Bounds,
        direction: str,
        *,
        duration: float = 0.2,
        distance_ratio: float = 0.8,
        context: dict[str, Any] | None = None,
    ) -> ActionRef:
        try:
            self.device.swipe_bounds(
                bounds,
                direction,
                duration,
                distance_ratio,
            )
        except Exception as error:
            self._failed(
                name,
                context,
                "device_action_failed",
                bounds=tuple(bounds),
                direction=direction,
                error=type(error).__name__,
            )
            raise
        return self.journal.record_action(
            name,
            status="success",
            context=context,
            details={
                "bounds": tuple(bounds),
                "direction": direction,
                "duration": duration,
                "distance_ratio": distance_ratio,
            },
        )

    def _failed(
        self,
        name: str,
        context: dict[str, Any] | None,
        stage: str,
        **details: Any,
    ) -> ActionRef:
        return self.journal.record_action(
            name,
            status="failed",
            context=context,
            details={"stage": stage, **details},
        )

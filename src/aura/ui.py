from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from typing import Any

from .device import Bounds, Device, Selector, validate_selector
from .journal import EventJournal
from .models import ActionRef


class UiTimeout(RuntimeError):
    pass


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
            self._failed(
                name,
                context,
                "device_action_failed",
                selector=dict(selector),
                error=type(error).__name__,
            )
            raise
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
        expect_change: bool = False,
        context: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> ActionRef:
        before_hash = self._hierarchy_hash() if expect_change else None
        try:
            self.device.swipe(direction, duration)
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
            lambda: self._hierarchy_hash() != before_hash,
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
            details={"direction": direction, "duration": duration},
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

    def _hierarchy_hash(self) -> str:
        return hashlib.sha256(self.device.hierarchy().encode("utf-8")).hexdigest()

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

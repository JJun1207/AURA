from __future__ import annotations

import os
import time


_IS_WINDOWS = os.name == "nt"
_WAITING_MARKERS = (
    "waiting for a connection",
    "waiting to receive",
    "수신 대기",
    "대기 중",
)
_RECEIVE_LABELS = ("receive files", "receive", "파일 받기", "파일 수신")
_NEXT_LABELS = ("next", "다음")
_FINISH_LABELS = ("finish", "마침", "완료")


def _window_text(window) -> str:
    values = [window.window_text()]
    values.extend(node.window_text() for node in window.descendants())
    return " | ".join(value.lower() for value in values if value)


def _click_receive(window) -> bool:
    for node in window.descendants():
        try:
            label = node.window_text().strip().lower()
            control_type = node.element_info.control_type
            if (
                control_type in {"Button", "Hyperlink"}
                and node.is_enabled()
                and any(value == label for value in _RECEIVE_LABELS)
            ):
                node.click_input()
                return True
        except Exception:
            continue
    return False


def _click_next(window) -> bool:
    for node in window.descendants():
        try:
            if (
                node.element_info.control_type == "Button"
                and node.is_enabled()
                and node.window_text().strip().lower() in _NEXT_LABELS
            ):
                node.click_input()
                return True
        except Exception:
            continue
    return False


def prepare_windows_bluetooth_receiver(
    timeout_sec: float = 10.0,
) -> dict[str, object]:
    if not _IS_WINDOWS:
        return {
            "ok": False,
            "reason": "windows_receiver_unsupported",
            "detail": os.name,
        }
    try:
        from pywinauto import Application
    except ImportError as error:
        return {
            "ok": False,
            "reason": "pywinauto_unavailable",
            "detail": str(error),
        }

    try:
        app = Application(backend="uia").start("fsquirt.exe -receive")
        window = app.window(title_re=".*Bluetooth.*")
    except Exception as error:
        return {
            "ok": False,
            "reason": "fsquirt_start_failed",
            "detail": str(error),
        }

    deadline = time.monotonic() + max(0.0, timeout_sec)
    receive_selected = False
    next_selected = False
    while True:
        try:
            if window.exists(timeout=0.2) and any(
                marker in _window_text(window)
                for marker in _WAITING_MARKERS
            ):
                return {
                    "ok": True,
                    "reason": "receive_waiting_screen_detected",
                    "detail": "",
                }
            if not receive_selected:
                receive_selected = _click_receive(window)
                if receive_selected:
                    continue
            if receive_selected and not next_selected:
                next_selected = _click_next(window)
                if next_selected:
                    continue
        except Exception:
            pass
        if time.monotonic() >= deadline:
            return {
                "ok": False,
                "reason": "receiver_waiting_screen_not_found",
                "detail": "",
            }
        time.sleep(0.2)


def finish_windows_bluetooth_receive(
    timeout_sec: float = 90.0,
    poll_interval: float = 0.2,
) -> dict[str, object]:
    if not _IS_WINDOWS:
        return {
            "ok": False,
            "reason": "windows_receiver_unsupported",
            "detail": os.name,
        }
    try:
        from pywinauto import Application, findwindows
    except ImportError as error:
        return {
            "ok": False,
            "reason": "pywinauto_unavailable",
            "detail": str(error),
        }

    deadline = time.monotonic() + max(0.0, timeout_sec)
    while True:
        try:
            windows = findwindows.find_elements(
                title_re=".*Bluetooth.*",
                backend="uia",
            )
            if windows:
                handle = windows[-1].handle
                window = Application(backend="uia").connect(
                    handle=handle
                ).window(handle=handle)
                for node in window.descendants():
                    if (
                        node.element_info.control_type == "Button"
                        and node.is_enabled()
                        and node.window_text().strip().lower()
                        in _FINISH_LABELS
                    ):
                        label = node.window_text().strip()
                        node.click_input()
                        return {
                            "ok": True,
                            "reason": "finish_button_clicked",
                            "detail": label,
                        }
        except Exception:
            pass
        if time.monotonic() >= deadline:
            return {
                "ok": False,
                "reason": "receive_completion_not_found",
                "detail": "",
            }
        time.sleep(max(0.0, poll_interval))


__all__ = [
    "finish_windows_bluetooth_receive",
    "prepare_windows_bluetooth_receiver",
]

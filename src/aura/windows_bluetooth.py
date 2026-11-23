from __future__ import annotations

import os
import time
import unicodedata
from collections.abc import Callable


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
_SAVE_MARKERS = ("save the received file", "받은 파일 저장", "수신한 파일 저장")
_FAILURE_MARKERS = (
    "file transfer not completed", "unable to transfer", "transfer failed",
    "transfer was cancelled", "transfer was canceled", "catastrophic failure",
    "fatal error", "fatal failure", "파일 전송이 완료되지", "파일 전송을 완료하지",
    "파일을 전송할 수 없", "전송이 취소", "심각한 오류",
)


def normalize_bluetooth_label(value: str) -> str:
    """Compare display names without UI formatting; keep raw labels for clicks."""
    return " ".join(
        "".join(
            character
            for character in value
            if unicodedata.category(character) != "Cf"
        ).split()
    )


def prepare_windows_bluetooth_pairing() -> dict[str, object]:
    """Expose host settings; matching-code approval remains with the operator."""
    if not _IS_WINDOWS:
        return {"ok": True, "reason": "external_host_pairing_required"}
    try:
        os.startfile("ms-settings:bluetooth")
    except OSError as error:
        return {"ok": False, "reason": "bluetooth_settings_open_failed", "detail": str(error)}
    return {"ok": True, "reason": "bluetooth_settings_opened"}


def _status_text(nodes) -> str:
    # Exclude filenames and buttons when classifying receiver status.
    return " | ".join(node.window_text() for node in nodes
                      if getattr(getattr(node, "element_info", None), "control_type", "Text") == "Text")


def _receive_failed(text: str) -> bool:
    return any(marker in text.casefold() for marker in _FAILURE_MARKERS)


def _receiver_handle_exists(handle: int) -> bool:
    # A closed native dialog may raise a COM error if queried through UIA.
    import ctypes
    is_window = ctypes.windll.user32.IsWindow
    is_window.argtypes = [ctypes.c_void_p]
    is_window.restype = ctypes.c_int
    return bool(is_window(handle))


def _click_receiver_control(nodes, labels) -> bool:
    for node in nodes:
        label = normalize_bluetooth_label(node.window_text()).replace("&", "").casefold()
        if (getattr(getattr(node, "element_info", None), "control_type", "") in {"Button", "Hyperlink"}
                and label in labels and node.is_enabled()):
            node.click_input()
            return True
    return False


def prepare_windows_bluetooth_receiver(
    timeout_sec: float = 10.0,
    *,
    capture_window: Callable | None = None,
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
    receiver = {}
    ui_errors = 0
    last_detail = ""
    while True:
        try:
            nodes = None
            if window.exists(timeout=0.2):
                receiver = {"receiver_handle": int(window.handle)}
                nodes = window.descendants()
                text = _status_text(nodes)
                if _receive_failed(text):
                    if capture_window:
                        capture_window(window, "error")
                    return {"ok": False, "reason": "receive_failed", "detail": text, **receiver}
                if any(marker in text.casefold() for marker in _WAITING_MARKERS):
                    if capture_window:
                        capture_window(window, "waiting")
                    return {"ok": True, "reason": "receive_waiting_screen_detected", "detail": "", **receiver}
            if nodes is None and (not receive_selected or not next_selected):
                nodes = window.descendants()
            if not receive_selected:
                receive_selected = _click_receiver_control(nodes, _RECEIVE_LABELS)
                if receive_selected:
                    continue
            if receive_selected and not next_selected:
                next_selected = _click_receiver_control(nodes, _NEXT_LABELS)
                if next_selected:
                    continue
            ui_errors = 0
        except Exception as error:
            last_detail = str(error)
            ui_errors += 1
            if ui_errors >= 3:
                if capture_window:
                    capture_window(window, "ui-error")
                return {"ok": False, "reason": "receiver_ui_error", "detail": last_detail, **receiver}
        if time.monotonic() >= deadline:
            if capture_window:
                capture_window(window, "timeout")
            return {
                "ok": False,
                "reason": "receiver_waiting_screen_not_found",
                "detail": last_detail,
                **receiver,
            }
        time.sleep(0.2)


def finish_windows_bluetooth_receive(
    timeout_sec: float = 90.0,
    poll_interval: float = 0.2,
    receiver_handle: int | None = None,
    capture_window: Callable | None = None,
    existing_names: tuple[str, ...] = (),
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
    last_detail = "receive_window_not_found"
    ui_errors = 0
    phase = "locate_receiver"
    progress_captured = False
    window = None
    connected_handle = None
    finish_clicked = False
    file_names = []
    occupied = {normalize_bluetooth_label(name).casefold() for name in existing_names}
    while True:
        try:
            phase = "locate_receiver"
            handle = receiver_handle
            if finish_clicked and not _receiver_handle_exists(handle):
                return {"ok": True, "reason": "receiver_save_completed", "detail": "Receiver closed after Finish",
                        "file_names": file_names}
            if handle is not None and not findwindows.find_elements(handle=handle, backend="uia"):
                return {"ok": False, "reason": "receiver_window_closed", "detail": "Receiver closed before Finish was confirmed"}
            if handle is None:
                windows = findwindows.find_elements(title_re=".*Bluetooth.*", backend="uia")
                handle = windows[-1].handle if windows else None
            if handle is not None:
                if window is None or connected_handle != handle:
                    window = Application(backend="uia").connect(handle=handle).window(handle=handle)
                    connected_handle = handle
                phase = "read_receiver_controls"
                nodes = window.descendants()
                phase = "read_receiver_status"
                text = _status_text(nodes)
                if _receive_failed(text):
                    if capture_window:
                        capture_window(window, "error")
                    return {"ok": False, "reason": "receive_failed", "detail": text}
                if not progress_captured and (any(
                    getattr(getattr(node, "element_info", None), "control_type", "") == "ProgressBar" for node in nodes
                ) or any(marker in text.casefold() for marker in ("receiving files", "transferring", "파일 받는 중", "전송 중"))):
                    if capture_window:
                        capture_window(window, "progress")
                    progress_captured = True
                if finish_clicked:
                    # Leave pending data visible. Do not click Finish repeatedly or
                    # accept an overwrite dialog while waiting for actual save.
                    if time.monotonic() >= deadline:
                        if capture_window:
                            capture_window(window, "save-timeout")
                        return {"ok": False, "reason": "receiver_save_unconfirmed", "detail": text}
                    time.sleep(max(0.0, poll_interval))
                    continue
                phase = "read_received_file_names"
                file_names = []
                for item in nodes:
                    if getattr(getattr(item, "element_info", None), "control_type", "") == "ListItem":
                        label = item.window_text().strip()
                        if label.casefold() not in {"", "file name", "size"}:
                            file_names.append(label)
                last_detail = "finish_button_not_available"
                phase = "find_finish_button"
                for node in nodes:
                    if getattr(getattr(node, "element_info", None), "control_type", "") != "Button":
                        continue
                    label = normalize_bluetooth_label(node.window_text()).replace("&", "").casefold()
                    if label not in _FINISH_LABELS:
                        continue
                    if node.is_enabled() and not file_names and not any(marker in text.casefold() for marker in _SAVE_MARKERS):
                        if capture_window:
                            capture_window(window, "error")
                        return {"ok": False, "reason": "receive_result_unrecognized", "detail": text or "Finish without a received-file screen"}
                    if not node.is_enabled():
                        # Old AURA selects a received file to activate Finish.
                        for item in nodes:
                            if (getattr(getattr(item, "element_info", None), "control_type", "") == "ListItem"
                                    and item.window_text().strip().casefold() not in {"", "file name", "size"}
                                    and item.is_enabled()):
                                item.click_input()
                                break
                    if node.is_enabled():
                        if not file_names or any(normalize_bluetooth_label(name).casefold() in occupied for name in file_names):
                            if capture_window:
                                capture_window(window, "save-error")
                            return {"ok": False, "reason": "receive_filename_conflict" if file_names else "receive_file_names_unavailable",
                                    "detail": "Existing files are preserved; Finish was not clicked.", "file_names": file_names}
                        label = node.window_text().strip()
                        if capture_window:
                            capture_window(window, "finish")
                        phase = "click_finish"
                        node.click_input()
                        finish_clicked = True
                        receiver_handle = handle
                        continue
                    last_detail = "finish_button_disabled_after_file_selection"
            ui_errors = 0
        except Exception as error:
            last_detail = f"{phase}: {type(error).__name__}: {error}"
            ui_errors += 1
            if ui_errors >= 3:
                if capture_window and window is not None:
                    capture_window(window, "ui-error")
                return {"ok": False, "reason": "receiver_ui_error", "detail": last_detail}
            connected_handle = None  # Reattach after a stale UI Automation wrapper error.
        if time.monotonic() >= deadline:
            if capture_window and window is not None:
                capture_window(window, "timeout")
            return {"ok": False, "reason": "receive_completion_not_found", "detail": last_detail}
        time.sleep(max(0.0, poll_interval))


def close_waiting_windows_bluetooth_receiver(receiver_handle: int) -> dict[str, object]:
    """Close this chat's idle/failed receiver; leave pending received data visible."""
    if not _IS_WINDOWS:
        return {"ok": True, "reason": "windows_receiver_unsupported"}
    try:
        from pywinauto import Application, findwindows
        if not findwindows.find_elements(handle=receiver_handle, backend="uia"):
            return {"ok": True, "reason": "receiver_already_closed"}
        window = Application(backend="uia").connect(handle=receiver_handle).window(handle=receiver_handle)
        text = _status_text(window.descendants())
        failed = _receive_failed(text)
        if not failed and not any(marker in text.casefold() for marker in _WAITING_MARKERS):
            return {"ok": True, "reason": "receiver_retained_for_pending_data"}
        window.close()
        deadline = time.monotonic() + 2.0
        while window.exists(timeout=0):
            if time.monotonic() >= deadline:
                return {"ok": False, "reason": "receiver_close_unconfirmed"}
            time.sleep(0.1)
        return {"ok": True, "reason": "failed_receiver_closed" if failed else "idle_receiver_closed"}
    except Exception as error:
        return {"ok": False, "reason": "idle_receiver_cleanup_failed", "detail": str(error)}


__all__ = [
    "normalize_bluetooth_label",
    "close_waiting_windows_bluetooth_receiver",
    "finish_windows_bluetooth_receive",
    "prepare_windows_bluetooth_pairing",
    "prepare_windows_bluetooth_receiver",
]

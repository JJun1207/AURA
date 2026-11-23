r"""Send fixed indexed text to an already-open Telegram chat using uiautomator2.

Example (Windows PowerShell, with a dedicated evaluation chat already open):
  .\.venv-win\Scripts\python.exe tools\seed_telegram_messages.py --serial DEVICE_SERIAL --chat "Example channel" --count 1000
  .\.venv-win\Scripts\python.exe tools\seed_telegram_messages.py --serial DEVICE_SERIAL --chat "Example channel" --check

Each message is 'Demo message #000001'. --start selects the first index.
Submission means the composer cleared and the message appeared in the chat;
it does not independently verify server delivery. Failed sends are not retried.
"""

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys
import time
import xml.etree.ElementTree as ET

PACKAGE = "org.telegram.messenger"
EMPTY_HINTS = {"", "Message", "Broadcast"}


def screen(device, chat):
    nodes = [n for n in ET.fromstring(device.dump_hierarchy()).iter("node")
             if n.get("package") == PACKAGE and n.get("visible-to-user") != "false"]
    headers = [n for n in nodes if n.get("class") == "android.widget.FrameLayout"
               and n.get("content-desc", "").split("\n", 1)[0] in {chat, chat + ", Muted"}]
    fields = [n for n in nodes if n.get("class") == "android.widget.EditText" and n.get("enabled") != "false"]
    if len(headers) != 1 or len(fields) != 1:
        raise RuntimeError(f"Expected the open chat {chat!r} and exactly one message composer")
    sends = [n for n in nodes if n.get("content-desc") == "Send"
             and n.get("clickable") == "true" and n.get("enabled") != "false"]
    return nodes, fields[0].get("text", ""), sends


def empty_composer(state):
    return state[1] in EMPTY_HINTS and not state[2]


def wait_screen(device, chat, ready, timeout):
    deadline = time.monotonic() + timeout
    while True:
        state = screen(device, chat)
        if ready(state):
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("UI confirmation timed out; inspect this index before resuming")
        time.sleep(min(.2, remaining))


def send_messages(device, chat, start, count, interval, timeout, log):
    if start < 1 or count < 1 or not math.isfinite(interval) or interval < 0 or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Use positive start/count/timeout and a finite nonnegative interval")
    last_submitted = start - 1
    for index in range(start, start + count):
        text = f"Demo message #{index:06d}"

        def record(event, **details):
            log.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "event": event,
                                  "index": index, "text": text, "last_submitted": last_submitted,
                                  **details}, ensure_ascii=False) + "\n")
            log.flush()

        try:
            if not empty_composer(screen(device, chat)):
                raise RuntimeError("The composer contains a draft; it was not overwritten")
            record("prepared")
            device(packageName=PACKAGE, className="android.widget.EditText").set_text(text)
            wait_screen(device, chat, lambda s: s[1] == text and len(s[2]) == 1, timeout)
            record("send_requested")
            # Telegram's wide Send view overlaps the composer; the button is on its right.
            device(packageName=PACKAGE, description="Send").click(offset=(.8, .5))
            wait_screen(device, chat, lambda s: empty_composer(s) and any(
                n.get("class") != "android.widget.EditText" and text in n.get(key, "").splitlines()
                for n in s[0] for key in ("text", "content-desc")), timeout)
            last_submitted = index
            record("submitted")
            print(f"[{index - start + 1}/{count}] {text}", flush=True)
            if index < start + count - 1:
                time.sleep(interval)
        except (Exception, KeyboardInterrupt) as error:
            record("stopped", error=f"{type(error).__name__}: {error}")
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--serial", required=True, help="Target ADB serial from adb devices")
    parser.add_argument("--chat", required=True, help="Expected open evaluation chat/channel title")
    parser.add_argument("--count", type=int, help="Number of messages to send; required except with --check")
    parser.add_argument("--start", type=int, default=1, help="First index (default: 1)")
    parser.add_argument("--interval", type=float, default=1.0, help="Pause after each confirmed UI submission, seconds")
    parser.add_argument("--timeout", type=float, default=10.0, help="Maximum wait for each UI transition, seconds")
    parser.add_argument("--log", type=Path, help="New JSONL log file; an existing file is never overwritten")
    parser.add_argument("--check", action="store_true", help="Check the current chat and empty composer without typing or sending")
    args = parser.parse_args()
    if (not args.chat.strip() or not args.serial.strip() or args.start < 1
            or (not args.check and (args.count is None or args.count < 1))
            or not math.isfinite(args.interval) or args.interval < 0
            or not math.isfinite(args.timeout) or args.timeout <= 0):
        parser.error("Specify --count >= 1, --start >= 1, valid chat/serial, interval >= 0 and timeout > 0")
    try:
        import uiautomator2 as u2
        device = u2.connect(args.serial)
        if not empty_composer(screen(device, args.chat)):
            raise RuntimeError("The composer contains a draft; it was not overwritten")
        if args.check:
            print(f"Ready: {args.serial} / {args.chat}. No input or Send action performed.")
            return 0
        path = args.log or Path("dataset-logs") / ("telegram-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as log:
            print(f"Log: {path.resolve()}", flush=True)
            log.write(json.dumps({"event": "batch", "serial": args.serial, "chat": args.chat,
                                  "start": args.start, "count": args.count, "interval": args.interval}) + "\n")
            log.flush()
            send_messages(device, args.chat, args.start, args.count, args.interval, args.timeout, log)
        return 0
    except KeyboardInterrupt:
        print("Stopped. Inspect the last logged index before resuming with --start.", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"Stopped: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

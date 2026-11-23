"""Read-only inventory around the pinned UI automation client's connection."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from importlib.metadata import distribution, version
import platform
import re
import shlex


def _now():
    return datetime.now(timezone.utc).isoformat()


def host_automation() -> dict:
    result = {"python": platform.python_version(), "platform": platform.system(), "dependencies": {}, "bundled_components": []}
    for name in ("uiautomator2", "adbutils"):
        try:
            result["dependencies"][name] = version(name)
        except Exception as error:
            result["dependencies"][name] = {"status": "unavailable", "error": str(error)}
    try:
        package = distribution("uiautomator2")
        for relative in package.files or ():
            if str(relative).endswith(("assets/u2.jar", "assets/app-uiautomator.apk")):
                data = package.locate_file(relative).read_bytes()
                result["bundled_components"].append({"path": str(relative), "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    except Exception as error:
        result["component_inventory_error"] = str(error)
    return result


def observe_automation(adb) -> dict:
    observations = {}

    def query(name, command, *, select=None):
        try:
            response = adb.shell2(command, timeout=10)
            output = response.output
            row = {"command": command, "status": "observed" if response.returncode == 0 else "unavailable",
                   "observed_at": _now(), "returncode": response.returncode, "output": output}
            if select is not None and response.returncode == 0:
                row["output"] = "\n".join(line for line in output.splitlines() if select(line))
                row["output_scope"] = "automation_component_lines_only"
        except Exception as error:
            row = {"command": command, "status": "unavailable", "queried_at": _now(), "error": f"{type(error).__name__}: {error}"}
        observations[name] = row
        return row

    query("jar", "test -r /data/local/tmp && test -x /data/local/tmp || exit 77; "
          "if [ -e /data/local/tmp/u2.jar ]; then sha256sum /data/local/tmp/u2.jar; else printf 'AURA_ABSENT\\n'; fi")
    packages = query("companion_packages", "pm list packages -f com.github.uiautomator")
    if packages["status"] == "observed":
        for line in packages["output"].splitlines():
            match = re.fullmatch(r"package:(/.+)=(com\.github\.uiautomator(?:\.test)?)", line)
            if match:
                path, package = match.groups()
                query(package + ".apk_sha256", "sha256sum " + shlex.quote(path))
                query(package + ".metadata", "dumpsys package " + package,
                      select=lambda line: any(key in line for key in ("versionName=", "versionCode=", "codePath=", "dataDir=", "firstInstallTime=", "lastUpdateTime=")))
    query("processes", "ps -A -o PID,USER,ARGS",
          select=lambda line: any(name in line for name in ("com.wetest.uia2.Main", "com.github.uiautomator", "uiautomator")))
    query("adb_enabled", "settings get global adb_enabled")
    query("usb_state", "getprop sys.usb.state")
    statuses = [row["status"] for row in observations.values()]
    return {"status": "observed" if all(s == "observed" for s in statuses) else "unavailable" if all(s == "unavailable" for s in statuses) else "partial",
            "observed_at": _now(), "observations": observations,
            "scope": "known_uiautomator2_components_and_adb_settings_not_full_device_footprint"}

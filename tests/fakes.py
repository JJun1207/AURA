from collections import Counter
from pathlib import Path
from xml.sax.saxutils import escape

from aura.device import validate_selector


def target_rules(item_types=("page",), expected_artifacts=("structured_data",), boundaries=("all_workspaces_visited",)):
    return {
        "identification": {"id": "registered_ui_items", "parameters": {"counted_item_types": list(item_types)}},
        "traversal_completion": {"id": "explicit_ui_boundary", "parameters": {"completion_conditions": list(boundaries)}},
        "result": {"id": "required_artifacts", "parameters": {"required_artifacts": {
            kind: ["structured_data", "ui_record"] if kind == "structured_data" else ["screen_image", "ui_hierarchy"] if kind == "ui_observation" else [kind]
            for kind in expected_artifacts
        }}},
        "error_handling": {"id": "preserve_partial", "parameters": {"on_interruption": "preserve_partial", "fallbacks": {}}},
    }


def selector_key(selector):
    return tuple(sorted(selector.items()))


class FakeDevice:
    def __init__(
        self,
        *,
        existing=None,
        appear_after_click=None,
        appear_after_back=None,
        hierarchy_before="<before/>",
        hierarchy_after_swipe="<after/>",
    ):
        self.existing = Counter(selector_key(selector) for selector in existing or [])
        self.appear_after_click = appear_after_click
        self.appear_after_back = appear_after_back
        self.hierarchy_value = hierarchy_before
        self.hierarchy_after_swipe = hierarchy_after_swipe
        self.clicks = []
        self.back_count = 0
        self.swipes = []
        self.boundary_swipes = []
        self.xpath_clicks = []
        self.boundary_long_clicks = []
        self.started = []
        self.stopped = []

    def installed_packages(self):
        return ()

    def app_start(self, package_name):
        self.started.append(package_name)
        self.existing[selector_key({"packageName": package_name})] = 1

    def app_stop(self, package_name):
        self.stopped.append(package_name)

    def exists(self, selector):
        return self.count(selector) > 0

    def count(self, selector):
        return self.existing[selector_key(selector)]

    def click(self, selector):
        self.clicks.append(dict(selector))
        if self.appear_after_click:
            self.existing[selector_key(self.appear_after_click)] = 1

    def long_click(self, selector):
        self.click(selector)

    def long_click_bounds(self, bounds):
        self.boundary_long_clicks.append(tuple(bounds))

    def click_xpath(self, xpath):
        self.xpath_clicks.append(xpath)

    def back(self):
        self.back_count += 1
        if self.appear_after_back:
            self.existing[selector_key(self.appear_after_back)] = 1

    def swipe(self, direction, duration=0.2):
        self.swipes.append((direction, duration))
        self.hierarchy_value = self.hierarchy_after_swipe

    def swipe_bounds(
        self,
        bounds,
        direction,
        duration=0.2,
        distance_ratio=0.8,
    ):
        self.boundary_swipes.append(
            (tuple(bounds), direction, duration, distance_ratio)
        )
        self.hierarchy_value = self.hierarchy_after_swipe

    def hierarchy(self):
        return self.hierarchy_value

    def screenshot(self):
        return b"\x89PNG\r\n\x1a\nfake"

    def shell(self, command):
        if command == "getprop ro.product.model":
            return "Fake phone\n"
        if command == "getprop ro.build.version.release":
            return "16\n"
        return f"ran:{command}"

    def pull(self, remote_path, local_path):
        Path(local_path).write_bytes(f"pulled:{remote_path}".encode())


class FakeBluetoothDevice(FakeDevice):
    def __init__(
        self,
        *,
        enabled=False,
        paired_targets=(),
        refuse_shell_toggle=False,
        bluetooth_ui=False,
    ):
        super().__init__()
        self.bluetooth_enabled = enabled
        self.paired_targets = tuple(paired_targets)
        self.refuse_shell_toggle = refuse_shell_toggle
        self.bluetooth_ui = bluetooth_ui
        self.screen = "home"

    def shell(self, command):
        if command == "settings get global bluetooth_on":
            return "1" if self.bluetooth_enabled else "0"
        if command == "cmd bluetooth_manager enable":
            if not self.refuse_shell_toggle:
                self.bluetooth_enabled = True
            return "enable: Success"
        if command == "cmd bluetooth_manager disable":
            if not self.refuse_shell_toggle:
                self.bluetooth_enabled = False
            return "disable: Success"
        if command == "am start -a android.settings.WIRELESS_SETTINGS":
            self.screen = "wireless"
            return ""
        if command == "dumpsys bluetooth_manager":
            bonded = "\n".join(
                f"    00:00:00:00:00:00 [{name}]"
                for name in self.paired_targets
            )
            return (
                "Bluetooth Status\n"
                f"  enabled: {str(self.bluetooth_enabled).lower()}\n"
                "  Bonded devices:\n"
                f"{bonded}\n\n"
                "  Devices in DB:\n"
            )
        return super().shell(command)

    def hierarchy(self):
        if self.bluetooth_ui and self.screen == "wireless":
            checked = str(self.bluetooth_enabled).lower()
            return (
                "<hierarchy>"
                '<node class="android.widget.LinearLayout" clickable="true">'
                '<node resource-id="android:id/title" text="Bluetooth"/>'
                '<node resource-id="android:id/switch_widget" '
                f'checkable="true" checked="{checked}" clickable="true"/>'
                "</node>"
                "</hierarchy>"
            )
        return super().hierarchy()

    def click_xpath(self, xpath):
        self.xpath_clicks.append(xpath)
        if '@text="Bluetooth"' in xpath and "switch_widget" in xpath:
            self.bluetooth_enabled = not self.bluetooth_enabled
            return
        raise AssertionError(f"unexpected xpath: {xpath}")


class FakeDndDevice(FakeDevice):
    def __init__(
        self,
        *,
        dnd_enabled=False,
        hide_all=False,
        missing_hide_all=False,
        duplicate_hide_all=False,
        refuse_hide_all=False,
        fail_restore=False,
        unreadable_hide_all=False,
        text_only_dnd_title=False,
        expanded_dnd_headers=False,
        duplicate_dnd_title=False,
        defer_first_hide_navigation=False,
        zen_mode_output=None,
        dnd_scroll_pages=0,
        hide_notifications_without_title=False,
        preserve_hide_screen_on_dnd_intent=False,
        legacy_dnd_main_row=False,
    ):
        super().__init__()
        self.dnd_enabled = dnd_enabled
        self.hide_all = hide_all
        self.screen = "home"
        self.missing_hide_all = missing_hide_all
        self.duplicate_hide_all = duplicate_hide_all
        self.refuse_hide_all = refuse_hide_all
        self.fail_restore = fail_restore
        self.unreadable_hide_all = unreadable_hide_all
        self.text_only_dnd_title = text_only_dnd_title
        self.expanded_dnd_headers = expanded_dnd_headers
        self.duplicate_dnd_title = duplicate_dnd_title
        self.defer_first_hide_navigation = defer_first_hide_navigation
        self.hide_navigation_attempts = 0
        self.zen_mode_output = zen_mode_output
        self.dnd_scroll_pages = dnd_scroll_pages
        self.hide_notifications_without_title = (
            hide_notifications_without_title
        )
        self.preserve_hide_screen_on_dnd_intent = (
            preserve_hide_screen_on_dnd_intent
        )
        self.legacy_dnd_main_row = legacy_dnd_main_row

    def shell(self, command):
        if command == "settings get global zen_mode":
            if self.zen_mode_output is not None:
                return self.zen_mode_output
            return "1" if self.dnd_enabled else "0"
        if command.startswith("am start -a android.settings.ZEN_MODE_SETTINGS"):
            if self.preserve_hide_screen_on_dnd_intent and self.screen == "hide":
                return ""
            self.screen = "dnd"
            return ""
        return super().shell(command)

    def hierarchy(self):
        if self.screen == "dnd":
            if self.hide_notifications_without_title:
                return (
                    "<hierarchy>"
                    '<node resource-id="android:id/switch_widget"/>'
                    '<node class="android.widget.LinearLayout" '
                    'clickable="true">'
                    '<node resource-id="android:id/title" '
                    'text="Hide notifications" clickable="false"/>'
                    "</node>"
                    "</hierarchy>"
                )
            if self.dnd_scroll_pages:
                return (
                    "<hierarchy>"
                    '<node text="Sleeping"/>'
                    '<node resource-id="android:id/switch_widget"/>'
                    "</hierarchy>"
                )
            checked = str(self.dnd_enabled).lower()
            title = (
                '<node resource-id="android:id/title" '
                'text="Do not disturb"/>'
            )
            text_only_title = (
                '<node text="Do not disturb"/>'
                if self.text_only_dnd_title
                else ""
            )
            expanded_headers = (
                '<node resource-id="com.android.settings:id/'
                'collapsing_appbar_extended_title" '
                'text="Do not disturb"/>'
                '<node text="Do not disturb"/>'
                if self.expanded_dnd_headers
                else ""
            )
            main_control = (
                '<node resource-id="com.android.settings:id/'
                'collpasing_app_bar_extended_title" '
                'text="Do not disturb"/>'
                '<node class="android.widget.LinearLayout" clickable="true">'
                '<node resource-id="android:id/title" '
                'text="Turn on now"/>'
                "</node>"
                if self.legacy_dnd_main_row
                else (
                    '<node class="android.widget.LinearLayout" clickable="true">'
                    f"{title * (2 if self.duplicate_dnd_title else 1)}"
                    "</node>"
                )
            )
            return (
                f"<hierarchy>{text_only_title}{expanded_headers}"
                f"{main_control}"
                '<node resource-id="android:id/switch_widget" '
                f'checked="{checked}"/>'
                '<node class="android.widget.LinearLayout" clickable="true">'
                '<node resource-id="android:id/title" '
                'text="Hide notifications" clickable="false"/>'
                "</node>"
                "</hierarchy>"
            )
        if self.screen == "hide":
            if self.missing_hide_all:
                return "<hierarchy/>"
            checked = (
                "unknown"
                if self.unreadable_hide_all
                else str(self.hide_all).lower()
            )
            group = (
                '<node resource-id="com.android.settings:id/card">'
                '<node resource-id="com.android.settings:id/switch_text" '
                'text="Hide all"/>'
                '<node resource-id="com.android.settings:id/switch_widget" '
                f'checked="{escape(checked)}"/>'
                "</node>"
            )
            return (
                f"<hierarchy>{group * (2 if self.duplicate_hide_all else 1)}"
                "</hierarchy>"
            )
        return "<hierarchy/>"

    def click(self, selector):
        validate_selector(selector)
        selector = dict(selector)
        self.clicks.append(selector)
        if selector == {
            "resourceId": "android:id/title",
            "text": "Do not disturb",
        }:
            self.dnd_enabled = not self.dnd_enabled
            return
        if selector == {"text": "Hide notifications"}:
            self.screen = "hide"
            return
        if selector == {
            "resourceId": "com.android.settings:id/switch_text",
            "text": "Hide all",
        }:
            if self.refuse_hide_all:
                return
            if self.fail_restore and self.hide_all:
                return
            self.hide_all = not self.hide_all
            return
        raise AssertionError(f"unexpected selector: {selector}")

    def click_xpath(self, xpath):
        self.xpath_clicks.append(xpath)
        if (
            '@text="Do not disturb"' in xpath
            or '@text="Turn on now"' in xpath
        ):
            self.dnd_enabled = not self.dnd_enabled
            return
        if '@text="Hide notifications"' in xpath:
            self.hide_navigation_attempts += 1
            if (
                self.defer_first_hide_navigation
                and self.hide_navigation_attempts == 1
            ):
                return
            self.screen = "hide"
            return
        if '@text="Hide all"' in xpath:
            if self.refuse_hide_all:
                return
            if self.fail_restore and self.hide_all:
                return
            self.hide_all = not self.hide_all
            return
        raise AssertionError(f"unexpected xpath: {xpath}")

    def swipe(self, direction, duration=0.2):
        self.swipes.append((direction, duration))
        if (
            self.screen == "dnd"
            and direction == "down"
            and self.dnd_scroll_pages
        ):
            self.dnd_scroll_pages -= 1

    def back(self):
        super().back()
        self.screen = "dnd"


class FakeEnvironmentDevice(FakeDndDevice):
    def __init__(
        self,
        *,
        airplane_enabled=False,
        wifi_enabled=False,
        wifi_connected=True,
        wifi_status_output=None,
        connectivity_output=None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.airplane_enabled = airplane_enabled
        self.wifi_enabled = wifi_enabled
        self.wifi_connected = wifi_connected
        self.wifi_status_output = wifi_status_output
        self.connectivity_output = connectivity_output

    def shell(self, command):
        if command == "cmd connectivity airplane-mode":
            return "enabled" if self.airplane_enabled else "disabled"
        if command == "cmd connectivity airplane-mode enable":
            self.airplane_enabled = True
            return ""
        if command == "cmd connectivity airplane-mode disable":
            self.airplane_enabled = False
            return ""
        if command == "settings get global wifi_on":
            return "1" if self.wifi_enabled else "0"
        if command in {
            "svc wifi enable",
            "cmd wifi set-wifi-enabled enabled",
            "settings put global wifi_on 1",
        }:
            self.wifi_enabled = True
            return ""
        if command in {
            "svc wifi disable",
            "cmd wifi set-wifi-enabled disabled",
            "settings put global wifi_on 0",
        }:
            self.wifi_enabled = False
            return ""
        if command == "cmd wifi status":
            if self.wifi_status_output is not None:
                return self.wifi_status_output
            if self.wifi_enabled and self.wifi_connected:
                return "Wifi is connected to TestAP"
            if self.wifi_enabled:
                return "Wifi is not connected"
            return "Wifi is disabled"
        if command == "dumpsys connectivity":
            return self.connectivity_output or ""
        return super().shell(command)

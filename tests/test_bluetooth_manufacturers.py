"""Old Pixel/Huawei navigation shapes through the common Bluetooth lifecycle."""
from xml.sax.saxutils import escape

import pytest

from aura.profiles import ProfileStore
from aura.system_ui import SystemUIError
from fakes import FakeBluetoothDevice
from test_system_ui import make_system_ui_runtime, read_actions


class ManufacturerDevice(FakeBluetoothDevice):
    def __init__(self, manufacturer, *, existing=False, direct_pairing=False,
                 refuse_shell=False, outcome='approved', hidden=False):
        super().__init__(paired_targets=('Keyboard', 'Lab Receiver') if existing else ('Keyboard',),
                         refuse_shell_toggle=refuse_shell)
        self.manufacturer = manufacturer
        self.direct_pairing = direct_pairing
        self.outcome = outcome
        self.hidden = hidden
        self.scan_started = False
        self.commands = []
        self.phone_pair_clicks = 0
        self.forget_clicks = 0

    def shell(self, command):
        self.commands.append(command)
        if 'android.settings.BLUETOOTH_PAIRING_SETTINGS' in command:
            self.screen = 'scan' if self.direct_pairing else 'base'
            return '' if self.direct_pairing else 'Error: unavailable intent'
        if 'android.settings.BLUETOOTH_SETTINGS' in command:
            self.screen = 'base' if self.bluetooth_enabled else 'power'
            return ''
        return super().shell(command)

    @staticmethod
    def node(text='', resource='', **attrs):
        attributes = {'package': 'com.android.settings', 'text': text, 'resource-id': resource, **attrs}
        return '<node ' + ' '.join(f'{k}="{escape(str(v), {chr(34): "&quot;"})}"' for k,v in attributes.items()) + '/>'

    def rows(self):
        rows = []
        for name in ('Keyboard', 'Lab Receiver Other', 'Lab Receiver'):
            button_id = ('com.android.settings:id/settings_button' if self.manufacturer == 'generic'
                         else 'com.android.settings:id/konw_more')
            button = self.node(resource=button_id, clickable='true', enabled='true', **{'content-desc': 'Device settings'})
            rows.append('<node package="com.android.settings" clickable="true">' +
                        self.node(name, 'android:id/title') + button + '</node>')
        return ''.join(rows)

    def hierarchy(self):
        n = self.node
        if self.screen == 'power':
            return '<hierarchy>' + n('Bluetooth') + n(resource='com.android.settings:id/switch_widget',
                clickable='true', checkable='true', checked=str(self.bluetooth_enabled).lower()) + '</hierarchy>'
        if self.screen == 'dialog':
            target = 'Someone else' if self.outcome == 'wrong_target' else 'Lab Receiver'
            owner = 'other.app' if self.outcome == 'wrong_owner' else 'com.android.settings'
            return ('<hierarchy>' + f'<node package="{owner}" resource-id="android:id/parentPanel">' +
                    n(f'Pair with {target}?', package=owner) +
                    n('Pair', 'android:id/button1', package=owner, clickable='true', enabled='true') + '</node></hierarchy>')
        if self.screen == 'details':
            button = 'Forget' if self.manufacturer == 'generic' else 'Unpair'
            return '<hierarchy>' + n('Lab Receiver', 'android:id/title') + n(button, clickable='true') + '</hierarchy>'
        if self.screen == 'forget_dialog':
            return ('<hierarchy><node package="com.android.settings" resource-id="android:id/parentPanel">' +
                    n('Lab Receiver') + n('Forget' if self.manufacturer == 'generic' else 'Unpair',
                    'android:id/button1', clickable='true', enabled='true') + '</node></hierarchy>')
        if self.screen == 'scan':
            return '<hierarchy>' + n('Available devices') + n('Device name') + self.rows() + '</hierarchy>'
        if self.screen in ('base', 'saved'):
            title = 'Connected devices' if self.manufacturer == 'generic' else 'Bluetooth'
            body = n(title)
            if self.manufacturer == 'generic':
                body += n('Bluetooth')
            body += n(resource='com.android.settings:id/switch_widget', clickable='true',
                      checkable='true', checked=str(self.bluetooth_enabled).lower())
            if self.manufacturer == 'generic' and self.screen == 'base':
                body += n('Pair new device', 'android:id/title', clickable='true')
                body += n('See all', 'android:id/title', clickable='true')
            elif self.manufacturer == 'huawei' and 'Lab Receiver' not in self.paired_targets:
                body += n('Scan', 'android:id/title', clickable='true')
                if self.scan_started: body += self.rows()
            elif not self.hidden:
                body += self.rows()
            return '<hierarchy>' + body + '</hierarchy>'
        return super().hierarchy()

    def click(self, selector):
        self.clicks.append(selector)
        text = selector.get('text')
        if selector.get('resourceId') == 'com.android.settings:id/switch_widget':
            self.bluetooth_enabled = not self.bluetooth_enabled
            self.screen = 'base' if self.bluetooth_enabled else 'power'
        elif text == 'Pair new device': self.screen = 'scan'
        elif text == 'See all': self.screen = 'saved'
        elif text == 'Scan': self.scan_started = True
        elif text == 'Pair':
            self.phone_pair_clicks += 1
            if self.outcome == 'approved':
                self.paired_targets += ('Lab Receiver',)
                self.screen = 'base'
        elif text in ('Unpair', 'Forget'):
            self.forget_clicks += 1
            if self.screen == 'forget_dialog':
                self.paired_targets = tuple(x for x in self.paired_targets if x != 'Lab Receiver')
                self.screen = 'base'
            else: self.screen = 'forget_dialog'
        else:
            raise AssertionError(f'unexpected click {selector}')

    def click_xpath(self, xpath):
        from lxml import etree
        matched = etree.fromstring(self.hierarchy().encode()).xpath(xpath)
        assert len(matched) == 1, (xpath, self.hierarchy())
        self.xpath_clicks.append(xpath)
        assert 'Lab Receiver' in xpath and 'Lab Receiver Other' not in xpath, xpath
        if 'settings_button' in xpath or 'konw_more' in xpath or 'widget_frame' in xpath:
            self.screen = 'details'
        else: self.screen = 'dialog'

    def swipe(self, direction, duration=0.2):
        self.swipes.append((direction, duration))
        if direction == 'up': self.hidden = False


def runtime_for(tmp_path, device):
    runtime = make_system_ui_runtime(tmp_path, device)
    runtime.profile = ProfileStore('profiles').load_system_ui(device.manufacturer)
    if 'bluetooth' in runtime.profile.operations:
        pairing = runtime.profile.operations['bluetooth'].get('pairing')
        if pairing: pairing['timeout_sec'] = .5
    ticks = iter(i / 100 for i in range(1000))
    runtime._monotonic = lambda: next(ticks)
    return runtime


@pytest.mark.parametrize('manufacturer', ['generic', 'huawei'])
@pytest.mark.parametrize('existing', [False, True])
@pytest.mark.parametrize('refuse_shell', [False, True])
def test_manufacturer_preparation_confirmation_and_cleanup(tmp_path, manufacturer, existing, refuse_shell):
    device = ManufacturerDevice(manufacturer, existing=existing, refuse_shell=refuse_shell, hidden=True)
    runtime = runtime_for(tmp_path, device)
    snapshot = runtime.prepare_bluetooth_delivery('Lab Receiver')
    assert device.bluetooth_enabled and 'Lab Receiver' in device.paired_targets
    assert device.phone_pair_clicks == (0 if existing else 1)
    runtime.restore_bluetooth_delivery(snapshot)
    assert device.bluetooth_enabled is False
    assert device.paired_targets == (('Keyboard', 'Lab Receiver') if existing else ('Keyboard',))
    assert device.forget_clicks == (0 if existing else 2)
    if not existing and manufacturer == 'generic':
        assert any(s.get('text') == 'Pair new device' for s in device.clicks)
        assert any(s.get('text') == 'See all' for s in device.clicks)
    if not existing:
        assert any(action['action'] == 'system_ui_bluetooth_unpair' and action['status'] == 'success'
                   for action in read_actions(tmp_path))


def test_pixel_direct_pairing_intent_skips_pair_new_device_click(tmp_path):
    device = ManufacturerDevice('generic', direct_pairing=True)
    runtime = runtime_for(tmp_path, device)
    snapshot = runtime.prepare_bluetooth_delivery('Lab Receiver')
    assert not any(s.get('text') == 'Pair new device' for s in device.clicks)
    runtime.restore_bluetooth_delivery(snapshot)


@pytest.mark.parametrize('manufacturer', ['generic', 'huawei'])
@pytest.mark.parametrize('outcome', ['waiting_on_pc', 'wrong_target', 'wrong_owner'])
def test_manufacturer_timeout_preserves_other_pairings_and_restores_power(tmp_path, manufacturer, outcome):
    device = ManufacturerDevice(manufacturer, outcome=outcome)
    with pytest.raises(SystemUIError, match='timeout'):
        runtime_for(tmp_path, device).prepare_bluetooth_delivery('Lab Receiver')
    assert device.paired_targets == ('Keyboard',)
    assert device.bluetooth_enabled is False
    assert device.phone_pair_clicks == (1 if outcome == 'waiting_on_pc' else 0)
    assert device.forget_clicks == 0


@pytest.mark.parametrize('manufacturer', ['generic', 'huawei'])
def test_shell_exception_uses_profile_power_switch(tmp_path, manufacturer):
    device = ManufacturerDevice(manufacturer, existing=True)
    shell = device.shell
    def unavailable_command(command):
        if command.startswith('cmd bluetooth_manager'):
            raise RuntimeError('command unavailable on this Android version')
        return shell(command)
    device.shell = unavailable_command
    runtime = runtime_for(tmp_path, device)
    snapshot = runtime.prepare_bluetooth_delivery('Lab Receiver')
    runtime.restore_bluetooth_delivery(snapshot)
    assert device.bluetooth_enabled is False
    assert device.paired_targets == ('Keyboard', 'Lab Receiver')


@pytest.mark.parametrize('manufacturer', ['generic', 'huawei'])
@pytest.mark.parametrize('case', ['sibling_icon', 'duplicate_rows', 'foreign_owner'])
def test_device_settings_lookup_stays_in_named_receiver_row(tmp_path, manufacturer, case):
    device = ManufacturerDevice(manufacturer)
    runtime = runtime_for(tmp_path, device)
    spec = runtime.profile.operations['bluetooth']
    button_id = 'com.android.settings:id/settings_button' if manufacturer == 'generic' else 'com.android.settings:id/konw_more'
    def row(name, with_button=True):
        return ('<node package="com.android.settings">' + device.node(name, 'android:id/title') +
                (device.node(resource=button_id, clickable='true') if with_button else '') + '</node>')
    xml = '<hierarchy>' + row('Keyboard') + row('Lab Receiver', with_button=case != 'sibling_icon')
    if case == 'duplicate_rows': xml += row('Lab Receiver')
    xml += '</hierarchy>'
    if case == 'foreign_owner': xml = xml.replace('package="com.android.settings"', 'package="other.app"')
    device.hierarchy = lambda: xml
    selectors = runtime._bluetooth_device_settings_selectors('Lab Receiver', spec['pairing'], spec['unpair'])
    assert len(selectors) == (2 if case == 'duplicate_rows' else 0)
    assert not device.clicks and not device.xpath_clicks


@pytest.mark.parametrize('manufacturer', ['generic', 'huawei'])
@pytest.mark.parametrize('case', ['foreign_owner', 'two_switches'])
def test_power_fallback_rejects_unrelated_or_ambiguous_switches(tmp_path, manufacturer, case):
    device = ManufacturerDevice(manufacturer, refuse_shell=True)
    hierarchy = device.hierarchy
    def bad_power_page():
        xml = hierarchy()
        if case == 'foreign_owner':
            return xml.replace('package="com.android.settings"', 'package="other.app"')
        return xml.replace('</hierarchy>', device.node(resource='com.android.settings:id/switch_widget',
            clickable='true', checked='false') + '</hierarchy>')
    device.hierarchy = bad_power_page
    with pytest.raises(SystemUIError, match='Bluetooth preparation failed'):
        runtime_for(tmp_path, device).prepare_bluetooth_delivery('Lab Receiver')
    assert device.bluetooth_enabled is False
    assert not device.clicks and not device.xpath_clicks

class SettingsFallbackDevice(ManufacturerDevice):
    def __init__(self, manufacturer, *, intent_failure, refuse_shell=False):
        super().__init__(manufacturer, refuse_shell=refuse_shell)
        self.intent_failure = intent_failure
        self.connection_label = 'Connected devices' if manufacturer == 'generic' else 'Device connectivity'

    def shell(self, command):
        if command.startswith('am start -a android.settings.BLUETOOTH'):
            self.commands.append(command)
            self.screen = 'settings_home'
            if self.intent_failure == 'exception':
                raise RuntimeError('intent unsupported')
            return 'Starting: Intent (Settings home)'
        if command.startswith('am start -a android.settings.SETTINGS'):
            self.commands.append(command)
            self.screen = 'settings_home'
            return ''
        return super().shell(command)

    def hierarchy(self):
        if self.screen in ('settings_home', 'connections'):
            title = 'Settings' if self.screen == 'settings_home' else self.connection_label
            row = self.connection_label if self.screen == 'settings_home' else 'Bluetooth'
            return '<hierarchy>' + self.node(title) + self.node(row, 'android:id/title', clickable='true') + '</hierarchy>'
        return super().hierarchy()

    def click(self, selector):
        if self.screen == 'settings_home' and selector.get('text') == self.connection_label:
            self.clicks.append(selector)
            self.screen = 'connections'
        elif self.screen == 'connections' and selector.get('text') == 'Bluetooth':
            self.clicks.append(selector)
            self.screen = 'base' if self.bluetooth_enabled else 'power'
        else:
            return super().click(selector)


@pytest.mark.parametrize('manufacturer', ['generic', 'huawei'])
@pytest.mark.parametrize('intent_failure', ['wrong_page', 'exception'])
@pytest.mark.parametrize('refuse_shell', [False, True])
def test_settings_fallback_restores_pairing_cleanup_and_power(tmp_path, manufacturer, intent_failure, refuse_shell):
    device = SettingsFallbackDevice(manufacturer, intent_failure=intent_failure, refuse_shell=refuse_shell)
    runtime = runtime_for(tmp_path, device)
    snapshot = runtime.prepare_bluetooth_delivery('Lab Receiver')
    assert device.phone_pair_clicks == 1
    runtime.restore_bluetooth_delivery(snapshot)
    assert device.paired_targets == ('Keyboard',)
    assert device.bluetooth_enabled is False
    assert device.forget_clicks == 2
    assert sum(s.get('text') == device.connection_label for s in device.clicks) >= 2
    assert sum(s.get('text') == 'Bluetooth' for s in device.clicks) >= 2


def test_settings_heading_alone_is_not_bluetooth_destination(tmp_path):
    device = SettingsFallbackDevice('huawei', intent_failure='wrong_page')
    device.hierarchy = lambda: '<hierarchy>' + device.node('Settings') + device.node('Device connectivity') + '</hierarchy>'
    runtime = runtime_for(tmp_path, device)
    with pytest.raises(SystemUIError, match='could not be opened'):
        runtime._open_bluetooth_settings('Lab Receiver', runtime._bluetooth_operation()['pairing'])
    assert not device.clicks

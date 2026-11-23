from types import SimpleNamespace

import adbutils
import uiautomator2

from aura.device import AndroidDevice


def test_connection_observes_before_agent_deployment_and_after(monkeypatch):
    installed = False
    seen = []

    class Adb:
        def shell2(self, command, **kwargs):
            seen.append((installed, command))
            if 'u2.jar' in command:
                return SimpleNamespace(returncode=0, output=('a' * 64 + '  /data/local/tmp/u2.jar\n') if installed else 'AURA_ABSENT\n')
            if command.startswith('pm list'):
                return SimpleNamespace(returncode=0, output='')
            if command.startswith('ps '):
                return SimpleNamespace(returncode=0, output='123 shell com.wetest.uia2.Main' if installed else 'PID USER ARGS')
            return SimpleNamespace(returncode=0, output='1')

    adb = Adb()
    monkeypatch.setattr(adbutils.adb, 'device', lambda **kwargs: adb)

    def connect(serial):
        nonlocal installed
        assert seen and not any(state for state, _ in seen)
        installed = True
        return object()

    monkeypatch.setattr(uiautomator2, 'connect', connect)
    device = AndroidDevice.connect('SERIAL')
    evidence = device.connection_evidence
    assert evidence['before']['observations']['jar']['output'] == 'AURA_ABSENT\n'
    assert evidence['after']['observations']['jar']['output'].startswith('a' * 64)
    assert evidence['after']['observations']['companion_packages']['output'] == ''
    assert evidence['host']['dependencies']['uiautomator2']
    device.automation_snapshot()  # Raw ADB only; another connect would fail the assertion above.


def test_failed_inventory_is_unavailable_not_absent():
    from aura.automation import observe_automation

    class Adb:
        def shell2(self, command, **kwargs):
            return SimpleNamespace(returncode=1, output='Permission denied')

    evidence = observe_automation(Adb())
    assert evidence['status'] == 'unavailable'
    assert all(row['status'] == 'unavailable' for row in evidence['observations'].values())
    assert all(row.get('output') != 'AURA_ABSENT\n' for row in evidence['observations'].values())

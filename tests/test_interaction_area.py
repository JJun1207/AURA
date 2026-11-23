"""Input coordinates must not land on system bars, even for visible XML nodes."""
from types import SimpleNamespace

import pytest

from aura.device import AndroidDevice


DISPLAY = '''Display: mDisplayId=0
  DisplayFrames w=1080 h=2400 r=0
  InsetsSource id=1 type=statusBars frame=[0,0][1080,80] visible=true
  InsetsSource id=2 type=navigationBars frame=[0,2256][1080,2400] visible=true
'''


def input_device(bounds=(918, 2228, 978, 2288), dump=DISPLAY):
    taps, swipes, reads = [], [], []
    element = SimpleNamespace(bounds=lambda: bounds, must_wait=lambda **kw: None,
                              click=lambda: taps.append((948, 2258)),
                              long_click=lambda: taps.append((948, 2258, .5)))
    class Client:
        def __call__(self, **selector):
            return element
        window_size = staticmethod(lambda: (1080, 2400))
        click = staticmethod(lambda *args: taps.append(args))
        long_click = staticmethod(lambda *args, **kw: taps.append((*args, kw['duration'])))
        swipe = staticmethod(lambda *args: swipes.append(args))
        xpath = staticmethod(lambda query: SimpleNamespace(all=lambda: [SimpleNamespace(bounds=bounds, click=element.click)]))
    def shell(command):
        reads.append(command)
        return dump
    return AndroidDevice(Client(), SimpleNamespace(shell=shell), serial='test'), taps, swipes, reads


@pytest.mark.parametrize('route', ['selector', 'xpath', 'bounds', 'long_selector', 'long_bounds'])
def test_all_tap_paths_clip_target_to_user_area(route):
    device, taps, _, reads = input_device()
    if route == 'selector': device.click({'text': 'Expand'})
    elif route == 'xpath': device.click_xpath('//*[@text="Expand"]')
    elif route == 'bounds': device.click_bounds((918, 2228, 978, 2288))
    elif route == 'long_selector': device.long_click({'text': 'Expand'})
    else: device.long_click_bounds((918, 2228, 978, 2288))
    assert taps[0][:2] == (948, 2242)
    assert len(reads) == 1


@pytest.mark.parametrize('bounds', [(918, 2256, 978, 2300), (0, 0, 100, 70)])
def test_completely_obscured_target_never_dispatches_input(bounds):
    device, taps, _, _ = input_device(bounds)
    with pytest.raises(ValueError, match='interaction area'):
        device.click_bounds(bounds)
    assert not taps


def test_safe_coordinates_and_swipe_timing_stay_unchanged():
    device, taps, swipes, _ = input_device()
    device.click_bounds((918, 2220, 978, 2280))
    device.swipe('up', .2, .3)
    assert taps == [(948, 2250)]
    assert swipes == [(540, 1560, 540, 840, .2)]
    device.swipe_bounds((0, 0, 1080, 2400), 'up', .2, 1)
    assert swipes[-1] == (540, 2255, 540, 80, .2)


def test_missing_system_geometry_fails_before_input():
    device, taps, _, _ = input_device(dump='permission denied')
    with pytest.raises(ValueError, match='interaction area'):
        device.click_bounds((100, 100, 200, 200))
    assert not taps


def test_current_bar_visibility_and_rotation_are_used():
    device, taps, _, _ = input_device()
    device.click_bounds((918, 2228, 978, 2288))
    device._adb.shell = lambda command: DISPLAY.replace('visible=true', 'visible=false')
    device.click_bounds((918, 2228, 978, 2288))
    assert taps[-1] == (948, 2258)
    device._adb.shell = lambda command: '''Display: mDisplayId=0
      DisplayFrames w=2400 h=1080 r=1
      InsetsSource id=1 type=statusBars frame=[0,0][2400,80] visible=true
      InsetsSource id=2 type=navigationBars frame=[2256,0][2400,1080] visible=true'''
    device.click_bounds((2228, 500, 2288, 560))
    assert taps[-1] == (2242, 530)


@pytest.mark.parametrize('dump', [
    'DisplayFrames w=1080 h=2400 r=0\n mStable=Rect(0, 80 - 1080, 2256)',
    'DisplayFrames w=1080 h=2400 r=0\n mStable=(0,80)-(1080,2256)',
    DISPLAY + '\n Display: mDisplayId=2\n DisplayFrames w=300 h=400 r=0\n'
              'InsetsSource type=navigationBars frame=[0,350][300,400] visible=true',
])
def test_display_area_uses_primary_display_and_legacy_stable_rectangles(dump):
    device, _, _, _ = input_device(dump=dump)
    assert device.interaction_bounds() == (0, 80, 1080, 2256)


def test_missing_navigation_bar_information_cannot_assume_full_screen():
    device, taps, _, _ = input_device(dump=DISPLAY.split('  InsetsSource id=2')[0])
    with pytest.raises(ValueError, match='interaction area'):
        device.click_bounds((918, 2228, 978, 2288))
    assert not taps


def test_android9_stable_rectangle_keeps_taps_and_swipes_inside_user_area():
    device, taps, swipes, _ = input_device(dump='''Display: mDisplayId=0
      DisplayFrames w=1080 h=2220 r=0
      mStable=[0,72][1080,2175]''')
    assert device.interaction_bounds() == (0, 72, 1080, 2175)
    device.click_bounds((900, 2150, 1080, 2220))
    assert taps == [(990, 2162)]
    device.swipe_bounds((0, 0, 1080, 2220), 'up', .2, 1)
    assert swipes == [(540, 2174, 540, 72, .2)]
    with pytest.raises(ValueError, match='interaction area'):
        device.click_bounds((0, 0, 100, 70))
    assert len(taps) == 1

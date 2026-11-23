"""Rectangle operations shared by XML parsing and native input."""
import re


def parse_bounds(value, *, positive_area=True, allow_negative=True):
    match = re.fullmatch(r'\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]', value)
    if match is None or (not allow_negative and '-' in value):
        raise ValueError('invalid bounds')
    rect = tuple(map(int, match.groups()))
    if positive_area and (rect[0] >= rect[2] or rect[1] >= rect[3]):
        raise ValueError('invalid bounds')
    return rect


def contains_bounds(outer, inner):
    return (outer[0] <= inner[0] and outer[1] <= inner[1]
            and outer[2] >= inner[2] and outer[3] >= inner[3])


def overlaps_bounds(first, second):
    return (first[0] < second[2] and first[2] > second[0]
            and first[1] < second[3] and first[3] > second[1])


def intersect_bounds(rect, area, *, minimum_height=1):
    visible = (max(rect[0], area[0]), max(rect[1], area[1]),
               min(rect[2], area[2]), min(rect[3], area[3]))
    return visible if visible[0] < visible[2] and visible[3] - visible[1] >= minimum_height else None

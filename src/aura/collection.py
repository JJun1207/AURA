"""Small collection functions composed by app bindings; no app dispatch here."""
from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from contextlib import contextmanager

from .models import AcquisitionStatus, ProcedureStatus

_SELECTOR_KEYS = {
    'xml': {'resource_id': 'resource-id', 'text': 'text', 'content_description': 'content-desc', 'class_name': 'class'},
    'uiautomator': {'resource_id': 'resourceId', 'text': 'text', 'content_description': 'description', 'class_name': 'className'},
}
for _keys in _SELECTOR_KEYS.values():
    _keys.update({key: key for key in ('checked', 'clickable', 'focusable', 'scrollable', 'selected')})


def xml_selector_value(value):
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, str):
        return value
    raise ValueError('selector constraint is invalid')


def selector_conditions(values, *, backend, allowed=None, transform=None):
    """Translate field names once; each binding supplies its accepted vocabulary."""
    keys = _SELECTOR_KEYS[backend]
    if allowed is not None:
        keys = {key: keys[key] for key in allowed}
    if transform is None:
        transform = xml_selector_value if backend == 'xml' else lambda value: value
    return {keys[key]: transform(value) for key, value in values.items()}


def selector_entry(profile, name):
    entry = profile.selectors[name]
    return {**entry['selector'], 'constraints': dict(entry.get('constraints', {}))}


def one_candidate(candidates):
    """Require one occurrence, preserving equal-valued duplicates as ambiguity."""
    candidates = tuple(candidates)
    if len(candidates) != 1:
        raise ValueError('unavailable' if not candidates else 'ambiguous')
    return candidates[0]


def ui_selector(profile, name):
    entry = profile.selectors[name]
    spec = entry['selector']
    allowed = {'resource_id', 'text', 'content_description', 'class_name'}
    return {**selector_conditions({spec['kind']: spec['value']}, backend='uiautomator', allowed=allowed),
            'packageName': spec['owner_package'],
            **selector_conditions(entry.get('constraints', {}), backend='uiautomator', allowed=allowed)}


def read_fields(row, fields, *, resource_prefix=None, suffix=False):
    result = {}
    for name, spec in fields.items():
        def matches(child):
            value = child.get('resource-id', '')
            if suffix:
                value = value.rsplit('/', 1)[-1]
            elif resource_prefix:
                value = value.removeprefix(resource_prefix)
            return value == spec['resource_id']
        nodes = row if spec.get('scope') == 'children' else row.iter()
        node = next((child for child in nodes if matches(child)), None)
        attributes = spec.get('attribute', 'text')
        if isinstance(attributes, str):
            attributes = [attributes]
        result[name] = '' if node is None else next((value for attribute in attributes
            if (value := node.get(attribute, '').strip())), '')
    return result


def iter_windows(observe, advance, signature, *, seen=None, include_boundary=False, adjacent=False, boundary_repeats=1):
    """Enumerate windows with app-supplied readiness and content identity.

    Content collectors retain the boundary observation and compare after visits;
    list collectors omit the repeated window. None defers boundary judgement.
    """
    if type(boundary_repeats) is not int or boundary_repeats < 1:
        raise ValueError('boundary_repeats must be positive')
    seen = set() if seen is None else seen
    repeats = 0
    def repeated(window):
        nonlocal repeats
        key = signature(window)
        if key is None:
            repeats = 0
            return False
        if key in seen:
            repeats += 1
            return repeats >= boundary_repeats
        repeats = 0
        if adjacent:
            seen.clear()
        seen.add(key)
        return False
    while True:
        window = observe()
        if not include_boundary and repeated(window):
            return
        yield window
        if include_boundary and repeated(window):
            return
        if advance() is False:
            return


def seek_window(observe, advance, signature, match, *, adjacent=False, include_boundary=True):
    """Search windows and return the matched value plus the last visited window."""
    last = None
    for last in iter_windows(observe, advance, signature, adjacent=adjacent, include_boundary=include_boundary):
        result = match(last)
        if result is not None:
            return result, last
    return None, last


def visit_candidates(candidates, reacquire):
    """Reacquire each candidate *after* the preceding visit has returned."""
    for candidate in candidates:
        yield reacquire(candidate)


def pending_candidates(rows, processed, *, key, eligible=lambda row: True):
    """Keep source order and consult the caller's live processed set lazily."""
    for row in rows:
        if eligible(row) and key(row) not in processed:
            yield row


def matching_candidates(rows, expected, *, key):
    """Expose all matches in source order; callers own uniqueness/error policy."""
    expected_key = key(expected)
    for row in rows:
        if key(row) == expected_key:
            yield row


def ordered_candidate(expected, current, index, *, key, allow_removed_prefix=False):
    """Reidentify an occurrence only within the same ordered remaining window."""
    removed = len(expected) - len(current)
    if (not 0 <= index < len(expected) or not 0 <= removed <= index
            or (removed and not allow_removed_prefix)
            or tuple(map(key, current)) != tuple(map(key, expected[removed:]))):
        raise ValueError('candidate window changed')
    return current[index - removed]


@contextmanager
def visit_scope(enter, restore, *, restore_on_error=False):
    """Compose entry/return around ordinary work or a hierarchy child yield.

    Entry failure never restores. The caller explicitly chooses success-only
    restoration or finally restoration, including cancellation.
    """
    entered = enter()
    if restore_on_error:
        try:
            yield entered
        finally:
            restore()
    else:
        yield entered
        restore()


def walk_hierarchy(node, expand):
    """Drive a level generator: yield child context, receive its result.

    A level owns its UI-specific entry/return checks. Traversal and exception
    propagation are shared; throwing into the parent preserves its recovery.
    """
    frame = expand(node)
    result = None
    failure = None
    try:
        while True:
            try:
                child = frame.throw(failure) if failure is not None else frame.send(result)
            except StopIteration as done:
                return done.value
            failure = None
            try:
                result = walk_hierarchy(child, expand)
            except BaseException as error:
                failure = error
    finally:
        frame.close()


def content_signature(xml, package_name):
    root = ET.fromstring(xml)
    for parent in root.iter():
        parent.attrib.pop('focused', None)
        parent.attrib.pop('bounds', None)
        for child in list(parent):
            if child.get('package') not in (None, '', package_name):
                parent.remove(child)
    return hashlib.sha256(ET.tostring(root, encoding='utf-8')).hexdigest()


def overlap_candidates(previous, current, *, same=lambda left, right: left == right):
    # ponytail: O(n²) on bounded UI windows; optimize only if measured.
    return [size for size in range(1, min(len(previous), len(current)) + 1)
            if all(same(left, right) for left, right in zip(previous[-size:], current[:size]))]


def unique_overlap_anchor(older, newer, size, *, same):
    return any(all(sum(same(row, other) for other in window) == 1
                   for row in pair for window in (older, newer))
               for pair in zip(older[-size:], newer[:size]))


def reconcile_overlap(older, newer, size, reconcile, *, copy_newer=lambda row: row):
    """Keep occurrences and order; the binding owns one pair's reconciliation."""
    resolved = [copy_newer(row) for row in newer]
    for index in range(size):
        resolved[index] = reconcile(older[len(older) - size + index], resolved[index])
    return [*older[:len(older) - size], *resolved]


def _record_compatible(old, new, missing_keys=()):
    optional = {"date_group", "displayed_address", "displayed_time", "caption", "group", "domain",
                "source", "displayed_size", "description"}
    optional.update(missing_keys)
    # Counts and recycled accessibility text do not identify an occurrence.
    ignored = optional | {"child_count", "accessibility_description"}

    return {k: v for k, v in old.items() if k not in ignored} == {
        k: v for k, v in new.items() if k not in ignored
    } and all(not old.get(key) or not new.get(key) or old[key] == new[key]
              for key in optional)


def window_overlap(previous, visible, *, missing_keys=()) -> int:
    def compatible(old, new):
        return _record_compatible(old, new, missing_keys)

    # Adjacent scrolling windows share their longest compatible suffix/prefix.
    # Preserve repeated rows inside that sequence; never deduplicate globally.
    return max(overlap_candidates(previous, visible, same=compatible), default=0)


def merge_history_windows(collected, visible, *, previous_count=None, carry_groups=False):
    """Preserve observed list rows unless their adjacent overlap is confirmed."""
    previous = collected
    if previous_count is not None:
        previous = collected[-previous_count:] if previous_count else []
    overlap = window_overlap(previous, visible)
    result = [dict(row) for row in collected]
    for index, row in enumerate(visible[:overlap], len(result) - overlap):
        result[index].update({key: value for key, value in row.items()
                              if value and not result[index].get(key)})
    labels = {key: result[-1].get(key, "") if overlap else "" for key in ("date_group", "group")}
    if carry_groups and collected and not overlap:
        for key in labels:
            label = collected[-1].get(key, '')
            boundary = next((row[key] for row in visible if row.get(key)), None)
            if boundary == label:
                # A boundary seen again below the leading rows still separates
                # those rows from its group; use the preceding observed group.
                label = next((row.get(key, '') for row in reversed(collected)
                              if row.get(key) != boundary), '')
            labels[key] = label
    for row in visible[overlap:]:
        for key in labels:
            labels[key] = row.get(key, "") or labels[key]
        result.append({**row, **{key: labels[key] for key in labels if key in row}})
    return result, bool(overlap or not previous or not visible)



def retain_ui_observation(context, attempt_id, prefix, hierarchy, *, name, source_action, linked):
    """Link and preserve a supplied XML view and its screen; neither decides flow."""
    action = context.linked_action(name, attempt_id, source_action=source_action, context=linked)
    screen, _ = context.retain_observation(
        prefix, context.device.screenshot(), hierarchy.encode('utf-8'),
        attempt_id=attempt_id, action=action, context=linked,
    )
    return action, screen


def retain_failure_observation(context, attempt_id, prefix, error):
    source = context.journal.actions[-1] if context.journal.actions else None
    linked = {"error_type": type(error).__name__, "error": str(error)}
    try:
        hierarchy = context.device.hierarchy()
        screen = context.device.screenshot()
        action = context.linked_action("retain_browser_failure_observation", attempt_id,
                                       source_action=source, context=linked)
        artifact, _ = context.retain_observation(
            prefix, screen, hierarchy.encode(), attempt_id=attempt_id, action=action, context=linked,
        )
        return action, artifact.observation_id, {}
    except (Exception, KeyboardInterrupt) as capture_error:
        # The original error and retained rows survive a disconnected device.
        return None, None, {"failure_evidence_error": f"{type(capture_error).__name__}: {capture_error}"}


def collect_record_list(context, *, target_id, item_key, item_type, surface, prefix,
                        record_kind, retain_name, completion, opener, collect,
                        initial_state=None, metadata=None):
    """Run the shared identification/attempt lifecycle around composed list work."""
    context.begin_identification(target_id)
    attempt_id = context.item_attempt(item_key, target_id, item_type, source={'surface': surface})
    state = {'action': None, 'records': [], **(initial_state or {})}
    error = None
    try:
        state['action'] = opener()
        collect(attempt_id, state)
    except (Exception, KeyboardInterrupt) as failure:
        error = failure
    return finish_record_collection(context, attempt_id, target_id, state['records'], state['action'],
        surface=surface, prefix=prefix, record_kind=record_kind, retain_name=retain_name,
        completion=completion, error=error, metadata=(metadata or window_merge_metadata)(state))


def finish_record_collection(context, attempt_id, target_id, records, source_action, *,
                             surface, prefix, record_kind, retain_name, completion,
                             error=None, metadata=None, success_status=None):
    """Preserve collected rows before recording completion or rethrowing failure."""
    metadata = metadata or {}
    overlaps = metadata.get('unconfirmed_overlaps', [])
    reason = ((str(error) or type(error).__name__) if error
              else overlaps[0]['reason'] if overlaps else None)
    failure_action, failure_observation_id, failure_details = (
        retain_failure_observation(context, attempt_id, f'{prefix}/failure', error)
        if error else (None, None, {})
    )
    linked = {'surface': surface, 'record_count': len(records)}
    action = context.linked_action(retain_name, attempt_id, source_action=source_action, context=linked)
    payload = {'record_kind': record_kind, 'record_count': len(records), 'records': records, **metadata}
    artifact = context.artifacts.write_bytes(
        f'{prefix}.json', (json.dumps(payload, ensure_ascii=False, indent=2) + '\n').encode(),
        kind='structured_data', attempt_id=attempt_id, action=action, context=linked,
    )
    context.register_collection_records(target_id, artifact, records)
    context.finish_identification(target_id, completion_condition=None if error else completion, reason=reason)
    context.finish_attempt(
        attempt_id,
        ProcedureStatus.INTERRUPTED if error else ProcedureStatus.COMPLETED,
        AcquisitionStatus.PARTIAL if error or overlaps else (
            context.result_status(attempt_id) if success_status is None else success_status),
        reason=reason, action=failure_action or action, observation_id=failure_observation_id,
        details={'record_count': len(records), **metadata, **failure_details},
    )
    if error:
        raise error
    return len(records), action



def collect_record_windows(context, attempt_id, state, *, surface, prefix, observe_name,
                           scroll_name, parser, read=None, scroll=True, settle,
                           overlap_error, carry_groups=False):
    """Compose readiness, window enumeration, evidence, overlap and scrolling."""
    import time
    previous_count = 0
    previous_observation_id = None
    def observe():
        if read is not None:
            return read()
        return context.device.hierarchy()
    def advance():
        if not scroll or state.get('empty'):
            return False
        state['action'] = context.ui.swipe(scroll_name, 'up', context=state['linked'])
        time.sleep(settle)
    windows = iter_windows(observe, advance, lambda xml: content_signature(
        xml, context.app_profile.package_name), adjacent=True)
    for number, hierarchy in enumerate(windows, 1):
        linked = {'surface': surface, 'window': number}
        state['linked'] = linked
        action, screen = retain_ui_observation(
            context, attempt_id, f'{prefix}/window-{number:04d}', hierarchy,
            name=observe_name, source_action=state['action'], linked=linked)
        state['action'] = action
        visible = parser(hierarchy)
        records, confirmed = merge_history_windows(state['records'], visible, previous_count=previous_count,
                                                  carry_groups=carry_groups)
        state['records'][:] = records
        if not confirmed:
            # Without a compatible adjacent overlap, preserve the entire window
            # and continue rather than losing later visits.
            warning = {**linked, 'reason': overlap_error,
                       'previous_observation_id': previous_observation_id,
                       'observation_id': screen.observation_id,
                       'record_start_index': len(records) - len(visible),
                       'record_count': len(visible)}
            state.setdefault('unconfirmed_overlaps', []).append(warning)
            state['action'] = context.linked_action('preserve_unconfirmed_list_overlap',
                attempt_id, source_action=action, context=linked, details=warning)
        previous_observation_id = screen.observation_id
        previous_count = len(visible)


def window_merge_metadata(state):
    warnings = state.get('unconfirmed_overlaps', [])
    return ({'unconfirmed_overlaps': warnings,
             'record_count_semantics': 'retained_rows_may_include_window_overlap'}
            if warnings else {})


def poll_samples(read, *, probes=None, timeout=None, interval, sleep=None, clock=None):
    """Bounded observations, with the first observation made immediately."""
    import time
    sleep = time.sleep if sleep is None else sleep
    clock = time.monotonic if clock is None else clock
    if probes is None and timeout is None:
        raise ValueError('polling requires a probe count or timeout')
    if probes is not None and (type(probes) is not int or probes < 1):
        raise ValueError('probes must be positive')
    if interval < 0 or (timeout is not None and timeout < 0):
        raise ValueError('polling times must be non-negative')
    deadline = None if timeout is None else clock() + timeout
    number = 0
    while True:
        yield read()
        number += 1
        if (probes is not None and number >= probes) or (deadline is not None and clock() >= deadline):
            return
        sleep(interval)


def stability_probe(read, *, key=lambda value: value, retry_errors=(), reset_on_error=True,
                    clear_error_on_success=False, consecutive=2, stable_for=0.0,
                    immediate=lambda value, identity: False, clock=None):
    """Stateful predicate usable with either existing polling scheduler.

    Reading errors propagate; only explicitly selected interpretation errors
    are retried. The last observation/error remains available for diagnostics.
    """
    import time
    clock = time.monotonic if clock is None else clock
    state = {'value': None, 'key': None, 'error': None, 'count': 0, 'since': None}

    def probe():
        value = state['value'] = read()
        try:
            identity = key(value)
        except retry_errors as error:
            state['error'] = error
            if reset_on_error:
                state.update(key=None, count=0, since=None)
            return False
        if clear_error_on_success:
            state['error'] = None
        now = clock() if stable_for else 0.0
        if identity is None:
            state.update(key=None, count=0, since=None)
            return False
        if identity == state['key'] and state['since'] is not None:
            state['count'] += 1
        else:
            state.update(key=identity, count=1, since=now)
        return immediate(value, identity) or (state['count'] >= consecutive and now - state['since'] >= stable_for)

    return probe, state


def first_stable(samples, *, key=lambda value: value, stable_for=0.0, clock=None):
    """Two consecutive nonempty candidates, optionally stable for a duration."""
    probe, _ = stability_probe(lambda: sample, key=key, stable_for=stable_for, clock=clock)
    for sample in samples:
        if probe():
            return sample
    return None


def field_id(profile, name):
    """Resolve a logical field; already concrete resource names remain literal."""
    return profile.parameters['ui_fields'].get(name, name)


def bind_ui_fields(profile, contracts):
    """Resolve concrete resource references in identification criteria too."""
    def resolve(value):
        if isinstance(value, list):
            return [resolve(item) for item in value]
        prefix, separator, name = value.rpartition('/')
        attribute = next((suffix for suffix in ('.text', '.content-desc') if name.endswith(suffix)), '')
        logical = name.removesuffix(attribute) if attribute else name
        return prefix + separator + field_id(profile, logical) + attribute
    return {target: (rule, {key: resolve(value) if key.endswith(('_resource_id', '_resource_ids'))
                           or key in ('fields', 'title_sources') else value
                           for key, value in criteria.items()})
            for target, (rule, criteria) in contracts.items()}


def device_profile(device):
    return device.context.app_profile


def display_pattern(profile, name, *, flags=0):
    import re
    return re.compile(profile.parameters['display_patterns'][name], flags)


def display_text(profile, name):
    return profile.parameters['display_text'][name]


def visit_item(enter, collect, restore):
    """Compose an item visit; callbacks own evidence and verified UI recovery.

    Unhandled errors propagate at the failing step. An app wishing to continue
    must explicitly record/recover its collection failure before returning.
    """
    enter()
    result = collect()
    parent = restore()
    return result, parent


def matching_nodes(root, owner, attributes, *, visible=None):
    return tuple(node for node in root.iter()
                 if node.get('package') == owner
                 and (visible is None or visible(node, root))
                 and all(node.get(key) == value for key, value in attributes.items()))

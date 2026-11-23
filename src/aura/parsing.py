"""Pure, composable XML extraction. UI actions and item identity live elsewhere."""
from collections import Counter
import hashlib
import json
import re

from .collection import read_fields, bind_ui_fields
from .geometry import parse_bounds, contains_bounds, overlaps_bounds


def node_values(nodes, attribute='text', *, exclude=(), prefix=None, unique=False):
    """Read nonempty attributes in source order; duplicates are kept by default."""
    values = [value for node in nodes if (value := node.get(attribute, '').strip())
              and value not in exclude and (prefix is None or value.startswith(prefix))]
    return list(dict.fromkeys(values)) if unique else values


def parsing_spec(profile, name):
    """Read only the explicitly selected profile; never infer a version's rules."""
    return profile.parameters['list_parsing'][name]


def matches_node(node, selector, *, ui_fields, resource_prefix=None, suffix=False):
    for key, expected in selector.items():
        if key == 'resource_id':
            actual = node.get('resource-id', '')
            if suffix:
                actual = actual.rsplit('/', 1)[-1]
            elif resource_prefix:
                actual = actual.removeprefix(resource_prefix)
            expected = ui_fields.get(expected, expected)
        else:
            actual = node.get(key, '')
        if actual != expected:
            return False
    return True


def first_node(root, selector, **options):
    return next((node for node in root.iter() if matches_node(node, selector, **options)), None)


def field_value(row, source, *, ui_fields, resource_prefix=None, suffix=False):
    """Fallbacks use the first nonempty value; each source uses its first node."""
    if isinstance(source, list):
        return next((value for item in source if (value := field_value(
            row, item, ui_fields=ui_fields, resource_prefix=resource_prefix, suffix=suffix))), '')
    if isinstance(source, str):
        source = {'resource_id': source}
    if 'resource_id' not in source:
        attributes = source.get('attribute', 'text')
        if isinstance(attributes, str):
            attributes = [attributes]
        for attribute in attributes:
            value = row.get(attribute, '')
            value = value if attribute == 'resource-id' else value.strip()
            if value:
                return value
        return ''
    return read_fields(row, {'value': {**source, 'resource_id': ui_fields.get(source['resource_id'], source['resource_id'])}},
                       resource_prefix=resource_prefix, suffix=suffix)['value']


def labelled_values(values, labels):
    """Read each label's following value and expose positions for ordering policy."""
    try:
        positions = {key: values.index(label) for key, label in labels.items()}
        return {key: values[index + 1] for key, index in positions.items()}, positions
    except (ValueError, IndexError):
        raise ValueError('labelled values unavailable') from None


def row_values(row, fields, **options):
    return {key: field_value(row, source, **options) for key, source in fields.items()}


def read_rows(root, spec, *, ui_fields, resource_prefix=None, suffix=False, group_values=None):
    """Return record/node pairs in XML order, retaining equal row occurrences."""
    options = dict(ui_fields=ui_fields, resource_prefix=resource_prefix, suffix=suffix)
    if 'container' in spec:
        root = first_node(root, spec['container'], **options)
    if root is None:
        return []
    result = []
    group = ''
    grouping = spec.get('group')
    for node in root.iter():
        if grouping and group_values is not None and node in group_values:
            group = group_values[node]
        if grouping and matches_node(node, grouping['selector'], **options):
            value = field_value(node, grouping['source'], **options)
            if value or grouping.get('keep_empty', False):
                group = value
            if grouping.get('exclusive', False):
                continue
        if not matches_node(node, spec['row'], **options):
            continue
        record = row_values(node, spec['fields'], **options)
        if any(not record[key] for key in spec.get('required', ())):
            continue
        exclusion = spec.get('exclude')
        if (exclusion and record[exclusion['field']] == exclusion['equals']
                and first_node(node, {'resource_id': exclusion['absent']}, **options) is None):
            continue
        if grouping:
            record = {grouping['field']: group, **record}
        rule = spec.get('kind')
        if rule:
            found = first_node(node, {'resource_id': rule['exists']}, **options) is not None
            kind = rule['present' if found else 'absent']
            record = {'kind': kind, **record}
            record.update({key: value for key, value in row_values(
                node, spec['variants'][kind], **options).items() if value})
        for key in spec.get('optional', ()):
            if not record[key]:
                del record[key]
        result.append((record, node))
        if spec.get('first', False):
            break
    return result


def read_sections(root, spec, *, initial='', ui_fields, resource_prefix=None, suffix=False):
    """A section applies only to direct item children of the selected container."""
    options = dict(ui_fields=ui_fields, resource_prefix=resource_prefix, suffix=suffix)
    section_spec = spec['sections']
    container = first_node(root, section_spec['container'], **options)
    groups = {}
    label = initial
    for node in (() if container is None else container):
        values = row_values(node, spec['fields'], **options)
        if matches_node(node, spec['row'], **options) and all(values[k] for k in spec.get('required', ())):
            groups[node] = label
        else:
            label = field_value(node, section_spec['source'], **options) or label
    return groups


def bind_row_actions(rows, spec):
    """Bind current selectors; occurrence numbers are not persistent identities."""
    labels = [node.get(spec['attribute'], '').strip() for _, node in rows]
    totals = Counter(labels)
    occurrences = Counter()
    result = []
    for (record, _), label in zip(rows, labels, strict=True):
        selector = None
        if record['kind'] == spec['kind']:
            if not label:
                raise RuntimeError(spec['missing_reason'])
            selector = {'description': label, 'packageName': spec['package_name']}
            if totals[label] > 1:
                selector.update(clickable=True, instance=occurrences[label])
        result.append((record, selector))
        occurrences[label] += 1
    return result


def validate_list_parsing(views, contracts, *, package_name):
    """Validate only the finite data vocabulary consumed above, never executable code."""
    def require(condition):
        if not condition:
            raise ValueError('invalid list_parsing')

    def selector(value):
        require(isinstance(value, dict))
        require(all(k in {'resource_id', 'resource-id', 'class', 'clickable', 'text',
                          'content-desc', 'enabled', 'visible-to-user', 'focusable'}
                    and isinstance(v, str) for k, v in value.items()))

    def source(value):
        if isinstance(value, str):
            require(bool(value))
        elif isinstance(value, list):
            require(bool(value))
            for item in value:
                require(not isinstance(item, list))
                source(item)
        else:
            require(isinstance(value, dict) and bool(value)
                    and set(value) <= {'resource_id', 'attribute', 'scope'})
            require(all(isinstance(v, str) and bool(v) for k, v in value.items() if k != 'attribute'))
            require(value.get('scope', 'descendants') in {'children', 'descendants'})
            attributes = value.get('attribute', 'text')
            attributes = [attributes] if isinstance(attributes, str) else attributes
            require(isinstance(attributes, list) and bool(attributes))
            require(all(isinstance(v, str) and v in {'text', 'content-desc', 'checked', 'selected', 'resource-id'} for v in attributes))

    def fields(value):
        require(isinstance(value, dict) and all(isinstance(k, str) and k for k in value))
        for value in value.values():
            source(value)

    require(isinstance(views, dict) and set(views) == set(contracts))
    for name, spec in views.items():
        require(isinstance(name, str) and name and isinstance(spec, dict))
        require({'row', 'fields'} <= set(spec) <= {
            'container', 'row', 'fields', 'required', 'optional', 'first',
            'group', 'exclude', 'kind', 'variants', 'sections', 'action'})
        contract = contracts[name]
        require(set(contract.get('required_parts', ())) <= set(spec))
        require(isinstance(spec['fields'], dict) and set(spec['fields']) == set(contract['fields']))
        require(('group' in spec) == ('group' in contract))
        if 'group' in contract:
            require(isinstance(spec['group'], dict) and spec['group'].get('field') == contract['group'])
        require(('kind' in spec) == ('variants' in spec) == ('kinds' in contract))
        if 'kinds' in contract:
            require(isinstance(spec['kind'], dict) and
                    tuple(spec['kind'].get(k) for k in ('present', 'absent')) == contract['kinds'])
            require(isinstance(spec['variants'], dict) and set(spec['variants']) == set(contract['variants']))
            for kind, values in spec['variants'].items():
                require(isinstance(values, dict) and set(values) == set(contract['variants'][kind]))
        require(not (set(spec) & {'sections', 'action'}) - set(contract.get('required_parts', ())))
        selector(spec['row'])
        fields(spec['fields'])
        if 'container' in spec:
            selector(spec['container'])
        for key in ('required', 'optional'):
            values = spec.get(key, [])
            require(isinstance(values, list) and all(isinstance(v, str) and v in spec['fields'] for v in values))
            require(len(values) == len(set(values)))
        require(set(spec.get('optional', ())) <= set(contract.get('optional', ())))
        require(type(spec.get('first', False)) is bool)
        if 'group' in spec:
            g = spec['group']
            require(isinstance(g, dict) and {'selector', 'source', 'field'} <= set(g)
                    <= {'selector', 'source', 'field', 'keep_empty', 'exclusive'})
            selector(g['selector']); source(g['source'])
            require(isinstance(g['field'], str) and g['field'] and g['field'] not in spec['fields'])
            require(all(type(g.get(k, False)) is bool for k in ('keep_empty', 'exclusive')))
        if 'exclude' in spec:
            e = spec['exclude']
            require(isinstance(e, dict) and set(e) == {'field', 'equals', 'absent'})
            require(all(isinstance(v, str) for v in e.values()) and e['field'] in spec['fields'] and e['absent'])
        require(('kind' in spec) == ('variants' in spec))
        if 'kind' in spec:
            k = spec['kind']; variants = spec['variants']
            require(isinstance(k, dict) and set(k) == {'exists', 'present', 'absent'})
            require(all(isinstance(v, str) and v for v in k.values()) and k['present'] != k['absent'])
            require(isinstance(variants, dict) and set(variants) == {k['present'], k['absent']})
            for values in variants.values():
                fields(values)
        if 'sections' in spec:
            s = spec['sections']
            require(isinstance(s, dict) and set(s) == {'container', 'source'})
            selector(s['container']); source(s['source'])
        if 'action' in spec:
            a = spec['action']
            require(isinstance(a, dict) and set(a) == {'kind', 'attribute', 'package_name', 'missing_reason'})
            require(all(isinstance(v, str) and v for v in a.values()))
            require(a['attribute'] == 'content-desc' and a['kind'] in spec.get('variants', {}))
            require(a['kind'] == contract['kinds'][0] and a['package_name'] == package_name)


def bind_parsing_contracts(profile, contracts):
    """Reference the actual parser definition instead of duplicating its selectors.

    Legacy profiles retain their original criteria. New profiles pin both the
    definition and resource mappings so an edited parser requires an updated rule.
    """
    contracts = bind_ui_fields(profile, contracts)
    parameters = profile.parameters
    specs = parameters.get('list_parsing', {})
    extracted = {'row_resource_id', 'row_clickable', 'fields', 'required_child_resource_id',
                 'required_child_resource_ids', 'optional_child_resource_ids', 'title_sources',
                 'date_marker', 'date_resource_id', 'group_resource_id'}
    for target, (rule, criteria) in list(contracts.items()):
        view = target.rsplit('.', 1)[-1]
        if view not in specs:
            continue
        payload = {'definition': specs[view], 'ui_fields': parameters.get('ui_fields', {})}
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':'),
                                           ensure_ascii=False).encode('utf-8')).hexdigest()
        contracts[target] = (rule, {**{key: value for key, value in criteria.items() if key not in extracted},
            'parser_spec': {'parameter': 'list_parsing.' + view, 'sha256': digest}})
    return contracts

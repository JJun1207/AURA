import importlib.util
from pathlib import Path


def test_report_distinguishes_reuse_code_profile_and_measured_time(tmp_path):
    spec = importlib.util.spec_from_file_location('adaptation', Path(__file__).parents[1] / 'tools/measure_aura_adaptation.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    before, after = tmp_path/'before', tmp_path/'after'
    for root in (before, after):
        (root/'src/aura/apps/example').mkdir(parents=True)
        (root/'profiles/apps/example').mkdir(parents=True)
    (before/'src/aura/apps/example/collector.py').write_text('def collect():\n    return 1\n')
    (after/'src/aura/apps/example/collector.py').write_text('from ...collection import iter_windows as windows\nfrom ...parsing import read_rows\ndef collect():\n    return windows(None, None, None)\ndef parse():\n    return read_rows(None, {})\n')
    (before/'profiles/apps/example/1.json').write_text('{"parameters":{"title":"old"}}')
    (after/'profiles/apps/example/1.json').write_text('{"parameters":{"title":"new","timeout":2}}')
    report = module.measure(before, after)
    app = report['apps']['example']
    assert app['shared_collection_calls'] == {'iter_windows': 1}
    assert app['shared_parsing_calls'] == {'read_rows': 1}
    assert app['functions']['added'] == ['src/aura/apps/example/collector.py:parse']
    assert app['functions']['changed'] == ['src/aura/apps/example/collector.py:collect']
    assert app['profiles'][0]['changed'] == ['parameters.title']
    assert app['profiles'][0]['added'] == ['parameters.timeout']
    assert app['code_lines']['added'] > 0
    assert report['effort'] is None
    assert report['scope'] == 'static_source_comparison_not_runtime_coverage_or_time_estimate'

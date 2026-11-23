import os
import pytest
from aura import artifacts


def test_inventory_delta_preserves_order_and_duplicate_after_records():
    before = [('same', 10, 1), ('changed', 20, 1), ('gone', 2, 1)]
    after = [('new', 3, 2), ('same', 10, 1), ('changed', 21, 1), ('new', 3, 2)]
    assert artifacts.changed_inventory(before, after) == (after[0], after[2], after[3])
    assert artifacts.changed_inventory(before, []) == ()


def test_membership_delta_keeps_notesnook_semantics_even_with_duplicate_baseline_paths():
    before = [('same', 10, 1), ('same', 20, 2)]
    after = [('same', 10, 1), ('same', 30, 3)]
    assert artifacts.changed_inventory(before, after, key=lambda row: row, signature=lambda row: True) == (after[1],)


def test_host_inventory_pairs_use_full_size_and_timestamp_signature():
    before = [('chat.zip', (10, 100)), ('other.zip', (5, 100))]
    after = [('other.zip', (5, 101)), ('chat.zip', (10, 100))]
    assert artifacts.changed_inventory(before, after, signature=lambda row: row[1]) == (after[0],)


@pytest.mark.skipif(os.name != 'posix', reason='Executes the Android-side POSIX producer with a stat shim')
@pytest.mark.parametrize('fail_stat', [False, True])
def test_batched_nul_scan_preserves_special_paths_and_rejects_stat_failure(tmp_path, fail_stat):
    import json, os, subprocess, sys
    from pathlib import PurePosixPath
    root = tmp_path/'download root'
    root.mkdir()
    paths = [root/name for name in ('plain.txt', 'space name.txt', 'tab\tname', 'line\nbreak', "quote'$`name")]
    for index, path in enumerate(paths):
        path.write_bytes(b'x' * (index + 1))
        os.utime(path, (1000, 1000))
    binary = tmp_path/'bin'; binary.mkdir()
    calls = tmp_path/'stat-calls.jsonl'
    stat = binary/'stat'
    stat.write_text('#!' + sys.executable + '\n' + '''import json, os, sys
from pathlib import Path
with open(os.environ['AURA_STAT_CALLS'], 'a') as out:
    out.write(json.dumps(sys.argv[1:])+'\\n')
assert sys.argv[1] == '-c'
format = sys.argv[2]
for name in sys.argv[3:]:
    if os.environ.get('AURA_STAT_FAIL') and Path(name).name == 'plain.txt':
        print('stat failed', file=sys.stderr)
        sys.exit(1)
    info = Path(name).stat()
    print(format.replace('%s', str(info.st_size)).replace('%Y', str(int(info.st_mtime))))
''')
    stat.chmod(0o755)
    result = subprocess.run(artifacts.nul_inventory_command((PurePosixPath(root),)), shell=True,
        env={**os.environ, 'PATH': str(binary)+os.pathsep+os.environ['PATH'],
             'AURA_STAT_CALLS': str(calls), 'AURA_STAT_FAIL': '1' if fail_stat else ''},
        capture_output=True, text=True)
    if fail_stat:
        with pytest.raises(ValueError):
            artifacts.parse_inventory(result.stdout, (str(root),), format='nul_triples')
    else:
        assert artifacts.parse_inventory(result.stdout, (str(root),), format='nul_triples') == tuple(sorted(
            (str(path), index+1, 1000) for index, path in enumerate(paths)))
        assert len(calls.read_text().splitlines()) == 1

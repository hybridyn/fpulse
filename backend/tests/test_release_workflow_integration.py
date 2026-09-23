"""Guard the release gates when integrating distribution validation."""
from pathlib import Path

import yaml


class UniqueKeyLoader(yaml.SafeLoader):
    pass


def _mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if key in result:
            raise ValueError(f"Duplicate YAML key: {key}")
        result[key] = loader.construct_object(value_node)
    return result


UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def test_release_retains_org_publish_gates_and_adds_package_validation():
    path = Path(__file__).resolve().parents[2] / '.github/workflows/release.yml'
    workflow = yaml.load(path.read_text(encoding='utf-8'), Loader=UniqueKeyLoader)
    jobs = workflow['jobs']
    assert 'PUBLISH_DOCKER' in jobs['docker']['if']
    assert 'PUBLISH_PYPI' in jobs['pypi']['if']
    assert 'environment' not in jobs['pypi']
    assert jobs['pypi']['needs'] == 'package'
    assert jobs['docker']['needs'] == 'package'
    assert set(jobs['github-release']['needs']) == {'package', 'linux-installers'}
    # PyYAML's YAML 1.1 loader parses the GitHub Actions key "on" as True.
    events = workflow.get('on', workflow.get(True))
    assert 'version_suffix' in events['workflow_dispatch']['inputs']

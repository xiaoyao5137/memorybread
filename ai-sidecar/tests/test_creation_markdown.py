import json
from pathlib import Path

import pytest

from creation.markdown_format import normalize_creation_markdown

CASES = json.loads((Path(__file__).parents[2] / 'shared/creation-markdown/cases.json').read_text())


@pytest.mark.parametrize('case', CASES)
def test_format_contract(case):
    actual = normalize_creation_markdown(case['source'])
    assert actual == case['expected']
    assert normalize_creation_markdown(actual) == actual

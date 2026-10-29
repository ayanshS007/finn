import importlib

import pytest

SUBPACKAGES = [
    "sources",
    "normalise",
    "parse",
    "classify",
    "score",
    "enrich",
    "summarise",
    "pull",
    "llm",
    "db",
    "jobs",
    "api",
]


def test_version():
    import finn

    assert finn.__version__


@pytest.mark.parametrize("name", SUBPACKAGES)
def test_subpackage_imports(name):
    assert importlib.import_module(f"finn.{name}")

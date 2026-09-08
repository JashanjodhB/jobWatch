"""Marks the browser tier as a package.

Without this, pytest imports `tests/e2e/conftest.py` under the bare name
`conftest` and it shadows `tests/conftest.py` for every `from conftest import
...` in the rest of the suite. The package name also makes the helper import
unambiguous: `from e2e.helpers import ...`.
"""

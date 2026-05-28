"""Guard: `symposium.__version__` MUST equal the pyproject distribution version.

The release workflow (`release.yml`) verifies the pushed *tag* against
`pyproject.toml`, but nothing checked the in-package ``__version__``
attribute — so a release could (and did, in v1.11.0) ship a stale
``__version__`` while the distribution metadata was correct. That makes
``get_version()`` (and any consumer reading the attribute) lie about what
is running. This test fails fast when the two drift.
"""

from __future__ import annotations

import pathlib
import tomllib

import symposium


def test_dunder_version_matches_pyproject():
    pyproject = pathlib.Path(__file__).resolve().parents[1] / "pyproject.toml"
    declared = tomllib.loads(pyproject.read_text())["project"]["version"]
    assert symposium.__version__ == declared, (
        f"symposium.__version__ ({symposium.__version__!r}) != pyproject "
        f"version ({declared!r}); bump symposium/__init__.py at every release"
    )

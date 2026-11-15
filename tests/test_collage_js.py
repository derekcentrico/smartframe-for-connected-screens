"""Run the collage layout tests (tests/js) under node."""

import os
import shutil
import subprocess

import pytest

JS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "js")


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_collage_layout_js() -> None:
    files = sorted(
        os.path.join(JS_DIR, name) for name in os.listdir(JS_DIR) if name.endswith(".test.js")
    )
    result = subprocess.run(  # nosec B603 B607: fixed argv, test files from this repo
        ["node", "--test", *files],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

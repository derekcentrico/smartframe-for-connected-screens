"""Run the collage layout tests (tests/js) under node."""

import os
import re
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


def test_frame_page_can_refetch_layout_script(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import create_app, faces

    # A face worker started here would import numpy in the background while
    # later tests run.
    monkeypatch.setattr(faces, "ENABLE_FACES", False)
    os.makedirs(os.path.join(os.environ["MEDIA_BASE"], "upstairs"), exist_ok=True)
    client = create_app().test_client()
    page = client.get("/frame/upstairs").get_data(as_text=True)
    match = re.search(r'var COLLAGE_JS\s*=\s*"([^"]+)";', page)
    assert match, "frame page lost the layout script url"
    assert "requestCollageScript(false)" in page
    resp = client.get(match.group(1))
    assert resp.status_code == 200
    assert b"SmartFrameCollage" in resp.data
    resp.close()

"""Image cache decoding and upload handling, one file at a time."""

import io
import os
import shutil
import threading
import time
import zipfile

import pytest
from PIL import Image

from app import faces, media, upload
from app.config import CACHE_BASE, MEDIA_BASE


def _drain_precache() -> None:
    # One worker, FIFO: once this no-op runs, every earlier pre-cache job is done.
    upload._cache_pool.submit(lambda: None).result(timeout=30)


@pytest.fixture(autouse=True)
def clean_dirs(monkeypatch: pytest.MonkeyPatch):
    # A pre-cache job left over from the previous test would recreate the
    # cache directory mid-cleanup.
    _drain_precache()
    for d in (MEDIA_BASE, CACHE_BASE):
        # conftest points both at a fresh mkdtemp; never clear anything else.
        if "smartframe-test-" not in d:
            pytest.fail(f"refusing to clear {d}: not the test scratch directory")
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d)
    media._media_cache.clear()
    upload._hash_cache.clear()
    monkeypatch.setattr(faces, "ENABLE_FACES", False)
    yield


def _jpeg(size, color=(10, 120, 200), orientation=None) -> bytes:
    im = Image.new("RGB", size, color)
    buf = io.BytesIO()
    if orientation:
        exif = Image.Exif()
        exif[0x0112] = orientation
        im.save(buf, "JPEG", exif=exif)
    else:
        im.save(buf, "JPEG")
    return buf.getvalue()


def _write(frame: str, name: str, data: bytes) -> str:
    folder = os.path.join(MEDIA_BASE, frame)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def _client():
    from app import create_app

    return create_app().test_client()


def _post(client, frame: str, files, xhr: bool = True):
    data = {"frame": frame, "files": [(io.BytesIO(b), n) for n, b in files]}
    headers = {"X-Requested-With": "XMLHttpRequest"} if xhr else {}
    return client.post("/upload", data=data, content_type="multipart/form-data",
                       headers=headers)


def test_cache_image_applies_exif_rotation_after_shrinking():
    src = _write("test", "rot.jpg", _jpeg((4000, 3000), orientation=6))
    out = media.get_cached_image("test", src, "rot.jpg")
    with Image.open(out) as im:
        assert im.size == (1080, 1440)
        assert im.mode == "RGB"


@pytest.mark.parametrize("mode", ["P", "RGBA", "LA"])
def test_cache_image_converts_other_modes(mode: str):
    buf = io.BytesIO()
    Image.new(mode, (3000, 2000)).save(buf, "PNG")
    src = _write("test", f"m{mode}.png", buf.getvalue())
    out = media.get_cached_image("test", src, f"m{mode}.png")
    with Image.open(out) as im:
        assert im.size == (1080, 720)
        assert im.mode == "RGB"


@pytest.mark.skipif(not media.HEIC_SUPPORTED, reason="pillow-heif is not installed")
def test_cache_image_reads_heic():
    buf = io.BytesIO()
    Image.new("RGB", (1200, 1600), (200, 80, 40)).save(buf, format="HEIF")
    src = _write("test", "IMG_0001.HEIC", buf.getvalue())
    out = media.get_cached_image("test", src, "IMG_0001.HEIC")
    with Image.open(out) as im:
        assert im.size == (1080, 1440)


def test_cache_decodes_run_one_at_a_time(monkeypatch: pytest.MonkeyPatch):
    active = {"now": 0, "max": 0}
    lock = threading.Lock()
    real = media._render_cache_image

    def slow_render(src: str, dst: str) -> None:
        with lock:
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
        time.sleep(0.05)
        real(src, dst)
        with lock:
            active["now"] -= 1

    monkeypatch.setattr(media, "_render_cache_image", slow_render)
    paths = [_write("test", f"p{i}.jpg", _jpeg((800, 600))) for i in range(4)]
    threads = [
        threading.Thread(target=media.get_cached_image, args=("test", p, os.path.basename(p)))
        for p in paths
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert active["max"] == 1
    for p in paths:
        assert os.path.isfile(media._cache_path("test", os.path.basename(p)))


def test_cache_is_reused_after_another_thread_builds_it(monkeypatch: pytest.MonkeyPatch):
    calls = []
    real = media._render_cache_image
    monkeypatch.setattr(media, "_render_cache_image",
                        lambda s, d: (calls.append(s), real(s, d)))
    src = _write("test", "once.jpg", _jpeg((800, 600)))
    first = media.get_cached_image("test", src, "once.jpg")
    assert media.get_cached_image("test", src, "once.jpg") == first
    assert len(calls) == 1


def test_upload_many_files_reports_counts():
    client = _client()
    files = [(f"IMG_{i:04d}.jpg", _jpeg((640, 480), (i * 9, 50, 90))) for i in range(6)]
    resp = _post(client, "test", files)
    body = resp.get_json()
    assert resp.status_code == 200
    assert (body["saved"], body["duplicates"], body["skipped"]) == (6, 0, 0)
    assert sorted(os.listdir(os.path.join(MEDIA_BASE, "test"))) == sorted(n for n, _ in files)

    again = _post(client, "test", files[:2]).get_json()
    assert (again["saved"], again["duplicates"]) == (0, 2)


def test_upload_to_all_frames_saves_each_file_per_frame():
    for frame in ("upstairs", "basement"):
        os.makedirs(os.path.join(MEDIA_BASE, frame))
    body = _post(_client(), "_all", [("a.jpg", _jpeg((640, 480)))]).get_json()
    assert body["saved"] == 2
    for frame in ("upstairs", "basement"):
        assert os.listdir(os.path.join(MEDIA_BASE, frame)) == ["a.jpg"]


def test_zip_members_are_extracted_and_junk_skipped():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("trip/one.jpg", _jpeg((640, 480), (1, 2, 3)))
        zf.writestr("trip/two.jpg", _jpeg((640, 480), (4, 5, 6)))
        zf.writestr("trip/notes.txt", b"hello")
        zf.writestr("__MACOSX/trip/._one.jpg", b"meta")
        zf.writestr("trip/", b"")
    body = _post(_client(), "test", [("trip.zip", buf.getvalue())]).get_json()
    assert (body["saved"], body["skipped"]) == (2, 1)
    assert sorted(os.listdir(os.path.join(MEDIA_BASE, "test"))) == ["one.jpg", "two.jpg"]


def test_zip_member_over_cap_is_skipped(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(upload, "_MAX_ZIP_MEMBER_BYTES", 1000)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("big.jpg", _jpeg((640, 480)))
        zf.writestr("small.jpg", b"\xff\xd8" + b"\0" * 100)
    body = _post(_client(), "test", [("set.zip", buf.getvalue())]).get_json()
    assert (body["saved"], body["skipped"]) == (1, 1)


def test_bad_zip_is_reported_as_skipped():
    resp = _post(_client(), "test", [("broken.zip", b"not a zip")])
    body = resp.get_json()
    assert resp.status_code == 400
    assert body["error"] and body["skipped"] == 1


def test_invalid_frame_name_saves_nothing():
    resp = _post(_client(), "../etc", [("a.jpg", _jpeg((640, 480)))])
    assert resp.status_code == 400
    assert resp.get_json()["saved"] == 0


def test_upload_page_carries_size_cap():
    page = _client().get("/upload").get_data(as_text=True)
    assert "var MAX_BYTES   = 200 * 1024 * 1024;" in page


def test_jpeg_decodes_at_reduced_scale(monkeypatch: pytest.MonkeyPatch):
    from PIL import JpegImagePlugin

    seen = []
    real_draft = JpegImagePlugin.JpegImageFile.draft

    def spy(self, mode, size):
        result = real_draft(self, mode, size)
        seen.append(self.size)
        return result

    monkeypatch.setattr(JpegImagePlugin.JpegImageFile, "draft", spy)
    src = _write("test", "land.jpg", _jpeg((4032, 3024)))
    out = media.get_cached_image("test", src, "land.jpg")
    assert seen and seen[0] == (2016, 1512)
    with Image.open(out) as im:
        assert im.size == (1080, 810)


def test_hash_cache_survives_per_file_requests(monkeypatch: pytest.MonkeyPatch):
    builds = []
    real = upload._build_existing_hashes
    monkeypatch.setattr(upload, "_build_existing_hashes",
                        lambda d: (builds.append(d), real(d))[1])
    client = _client()
    first = _jpeg((640, 480), (1, 1, 1))
    for i, data in enumerate([first, _jpeg((640, 480), (2, 2, 2)), first]):
        body = _post(client, "test", [(f"f{i}.jpg", data)]).get_json()
    assert body["duplicates"] == 1
    assert len(builds) == 1


def test_corrupt_zip_member_does_not_stop_the_rest():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("bad.jpg", _jpeg((640, 480), (7, 7, 7)))
        zf.writestr("good.jpg", _jpeg((640, 480), (8, 8, 8)))
    raw = bytearray(buf.getvalue())
    with zipfile.ZipFile(io.BytesIO(bytes(raw))) as zf:
        info = zf.getinfo("bad.jpg")
    name_len = int.from_bytes(raw[info.header_offset + 26:info.header_offset + 28], "little")
    extra_len = int.from_bytes(raw[info.header_offset + 28:info.header_offset + 30], "little")
    raw[info.header_offset + 30 + name_len + extra_len] = 0xFF  # invalid deflate block type
    body = _post(_client(), "test", [("set.zip", bytes(raw))]).get_json()
    assert (body["saved"], body["skipped"]) == (1, 1)
    assert os.listdir(os.path.join(MEDIA_BASE, "test")) == ["good.jpg"]


def test_unsupported_zip_method_is_skipped():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("odd.jpg", _jpeg((640, 480), (3, 3, 3)))
        zf.writestr("fine.jpg", _jpeg((640, 480), (4, 4, 4)))
    raw = bytearray(buf.getvalue())
    with zipfile.ZipFile(io.BytesIO(bytes(raw))) as zf:
        off = zf.getinfo("odd.jpg").header_offset
    raw[off + 8:off + 10] = (97).to_bytes(2, "little")  # local header method
    central = raw.index(b"PK\x01\x02")  # first central entry is odd.jpg
    raw[central + 10:central + 12] = (97).to_bytes(2, "little")
    body = _post(_client(), "test", [("set.zip", bytes(raw))]).get_json()
    assert (body["saved"], body["skipped"]) == (1, 1)


def test_write_failure_is_reported_as_failed(monkeypatch: pytest.MonkeyPatch):
    def no_space(*_args, **_kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(upload.tempfile, "mkstemp", no_space)
    resp = _post(_client(), "test", [("a.jpg", _jpeg((640, 480)))])
    body = resp.get_json()
    assert resp.status_code == 400
    assert body["error"] and (body["saved"], body["failed"], body["skipped"]) == (0, 1, 0)
    assert "Could not save 1 file" in body["message"]


def test_media_list_refreshes_even_if_a_later_file_errors(monkeypatch: pytest.MonkeyPatch):
    refreshed = []
    monkeypatch.setattr(upload, "invalidate_media_cache", refreshed.append)
    real = upload._save_file
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("boom")
        return real(*args, **kwargs)

    monkeypatch.setattr(upload, "_save_file", flaky)
    client = _client()
    client.application.config["PROPAGATE_EXCEPTIONS"] = False
    resp = _post(client, "test", [("a.jpg", _jpeg((640, 480), (1, 0, 0))),
                                  ("b.jpg", _jpeg((640, 480), (0, 1, 0)))])
    assert resp.status_code == 500
    assert os.listdir(os.path.join(MEDIA_BASE, "test")) == ["a.jpg"]
    assert refreshed == ["test"]


def _zip_of(members, method=zipfile.ZIP_DEFLATED) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", method) as zf:
        for name, data in members:
            zf.writestr(name, data)
    return buf.getvalue()


def test_zip_total_inflated_size_is_capped(monkeypatch: pytest.MonkeyPatch):
    photos = [(f"p{i}.jpg", _jpeg((640, 480), (i, i, i))) for i in range(4)]
    cap = len(photos[0][1]) + len(photos[1][1]) + 10
    monkeypatch.setattr(upload, "_MAX_ZIP_TOTAL_BYTES", cap)
    body = _post(_client(), "test", [("bomb.zip", _zip_of(photos))]).get_json()
    assert body["saved"] == 2
    assert sorted(os.listdir(os.path.join(MEDIA_BASE, "test"))) == ["p0.jpg", "p1.jpg"]


def test_zip_lying_about_size_still_hits_the_total_cap(monkeypatch: pytest.MonkeyPatch):
    photos = [(f"p{i}.jpg", _jpeg((640, 480), (i, 9, 9))) for i in range(3)]
    raw = bytearray(_zip_of(photos, zipfile.ZIP_STORED))
    # Claim every member is 1 byte in the central directory.
    pos = raw.find(b"PK\x01\x02")
    while pos != -1:
        raw[pos + 24:pos + 28] = (1).to_bytes(4, "little")
        pos = raw.find(b"PK\x01\x02", pos + 4)
    monkeypatch.setattr(upload, "_MAX_ZIP_TOTAL_BYTES", len(photos[0][1]) + 10)
    body = _post(_client(), "test", [("liar.zip", bytes(raw))]).get_json()
    assert body["saved"] <= 1


def test_zip_entry_count_is_capped(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(upload, "_MAX_ZIP_ENTRIES", 3)
    photos = [(f"p{i}.jpg", _jpeg((64, 48), (i, 2, 2))) for i in range(6)]
    body = _post(_client(), "test", [("many.zip", _zip_of(photos))]).get_json()
    assert body["saved"] == 3


def test_undecodable_zip_member_is_skipped(monkeypatch: pytest.MonkeyPatch):
    real_open = zipfile.ZipFile.open

    def picky_open(self, name, *args, **kwargs):
        target = name.filename if isinstance(name, zipfile.ZipInfo) else name
        if target == "bad.jpg":
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        return real_open(self, name, *args, **kwargs)

    data = _zip_of([("bad.jpg", _jpeg((64, 48))), ("good.jpg", _jpeg((64, 48), (5, 5, 5)))])
    monkeypatch.setattr(zipfile.ZipFile, "open", picky_open)
    body = _post(_client(), "test", [("names.zip", data)]).get_json()
    assert (body["saved"], body["skipped"]) == (1, 1)


def test_deleted_photo_is_not_reported_as_duplicate():
    client = _client()
    photo = _jpeg((640, 480), (11, 22, 33))
    assert _post(client, "test", [("a.jpg", photo)]).get_json()["saved"] == 1
    os.remove(os.path.join(MEDIA_BASE, "test", "a.jpg"))
    body = _post(client, "test", [("a.jpg", photo)]).get_json()
    assert (body["saved"], body["duplicates"]) == (1, 0)


def test_members_failing_crc_still_count_toward_the_total_cap(monkeypatch: pytest.MonkeyPatch):
    photos = [(f"p{i}.jpg", _jpeg((640, 480), (i, 40, 40))) for i in range(3)]
    raw = bytearray(_zip_of(photos, zipfile.ZIP_STORED))
    # Break the CRC of the first two members in the central directory.
    pos = raw.find(b"PK\x01\x02")
    for _ in range(2):
        raw[pos + 16:pos + 20] = b"\0\0\0\0"
        pos = raw.find(b"PK\x01\x02", pos + 4)
    size = max(len(d) for _n, d in photos)
    monkeypatch.setattr(upload, "_MAX_ZIP_TOTAL_BYTES", int(size * 2.5))
    body = _post(_client(), "test", [("crc.zip", bytes(raw))]).get_json()
    # Two corrupt members use most of the budget, so the third is not reached.
    assert body["saved"] == 0
    assert os.listdir(os.path.join(MEDIA_BASE, "test")) == []


def test_member_larger_than_declared_is_not_inflated_past_its_claim():
    raw = bytearray(_zip_of([("bomb.jpg", b"\0" * (8 * 1024 * 1024)),
                             ("ok.jpg", _jpeg((64, 48), (9, 1, 1)))]))
    pos = raw.find(b"PK\x01\x02")
    raw[pos + 24:pos + 28] = (1000).to_bytes(4, "little")  # claims 1000 bytes
    seen = []
    real_read = zipfile.ZipExtFile.read

    def spy(self, n=-1):
        data = real_read(self, n)
        seen.append(n)
        return data

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(zipfile.ZipExtFile, "read", spy)
        body = _post(_client(), "test", [("liar.zip", bytes(raw))]).get_json()
    assert (body["saved"], body["skipped"]) == (1, 1)
    assert max(seen) <= 1001


@pytest.mark.parametrize("method", [zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA])
def test_zip_methods_without_bounded_inflate_are_skipped(method: int):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(zipfile.ZipInfo("odd.jpg"), _jpeg((64, 48), (1, 2, 3)), compress_type=method)
        zf.writestr(zipfile.ZipInfo("fine.jpg"), _jpeg((64, 48), (3, 2, 1)),
                    compress_type=zipfile.ZIP_DEFLATED)
    body = _post(_client(), "test", [("mixed.zip", buf.getvalue())]).get_json()
    assert (body["saved"], body["skipped"]) == (1, 1)
    assert os.listdir(os.path.join(MEDIA_BASE, "test")) == ["fine.jpg"]

from pathlib import Path

import pytest

from claimagent.core.cache import DiskCache
from claimagent.core.sweep import SweepDir, SweepNotFound, frame_name, parse_frame_name


def test_frame_name_round_trip():
    name = frame_name(123, 30.75)
    assert name == "f000123_t030.75.jpg"
    ref = parse_frame_name(name)
    assert (ref.index, ref.t) == (123, 30.75)


def test_parse_rejects_other_files():
    with pytest.raises(ValueError):
        parse_frame_name("photo.jpg")


def test_sweep_lists_frames_in_order(sweep: SweepDir):
    assert [f.index for f in sweep.frames()] == [0, 1, 2]
    assert sweep.read_manifest().sweep_id == "test"


def test_create_refuses_to_overwrite_unless_asked(sweep: SweepDir):
    runs = sweep.root.parent
    with pytest.raises(FileExistsError):
        SweepDir.create(runs, "test")
    fresh = SweepDir.create(runs, "test", replace=True)
    assert fresh.frames() == []


def test_open_missing_sweep(tmp_path: Path):
    with pytest.raises(SweepNotFound, match="claim ingest"):
        SweepDir.open(tmp_path, "nothing")


def test_invalid_sweep_id(tmp_path: Path):
    with pytest.raises(ValueError):
        SweepDir.create(tmp_path, "../escape")


def test_cache_round_trip(tmp_path: Path):
    cache = DiskCache(tmp_path / "c.sqlite")
    key = DiskCache.key("model", b"\x00image")
    assert cache.get("llm", key) is None
    cache.set("llm", key, '{"a": 1}')
    assert cache.get("llm", key) == '{"a": 1}'
    assert cache.get("web", key) is None  # namespaces are separate


def test_cache_key_is_unambiguous():
    assert DiskCache.key("ab", "c") != DiskCache.key("a", "bc")

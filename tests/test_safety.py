from __future__ import annotations

import hashlib

import pytest

from ofc_media.safety import (
    UnsafeMediaError,
    decode_metainfo,
    encode_bencode,
    is_video_name,
    metainfo_files,
    safe_relative_path,
)


def test_only_explicit_video_extensions_are_accepted():
    assert is_video_name("Season 1/episode.mkv")
    assert is_video_name("movie.mp4")
    assert not is_video_name("movie.mkv.exe")
    assert not is_video_name("installer.exe")
    assert not is_video_name("../escape.mkv")


def test_relative_paths_reject_traversal_and_drives():
    with pytest.raises(UnsafeMediaError):
        safe_relative_path("../outside.mkv")
    with pytest.raises(UnsafeMediaError):
        safe_relative_path("C:/outside.mkv")


def test_metainfo_hash_and_file_order_are_revalidated():
    info = {
        b"files": [
            {b"length": 1000, b"path": [b"season", b"episode.mkv"]},
            {b"length": 20, b"path": [b"readme.txt"]},
        ],
        b"name": b"release",
        b"piece length": 16384,
        b"pieces": b"x" * 20,
    }
    payload = encode_bencode({b"announce": b"https://tracker.invalid", b"info": info})
    infohash = hashlib.sha1(encode_bencode(info)).hexdigest()
    root = decode_metainfo(payload, infohash)
    assert metainfo_files(root) == [
        (0, "season/episode.mkv", 1000),
        (1, "readme.txt", 20),
    ]
    with pytest.raises(UnsafeMediaError):
        decode_metainfo(payload, "0" * 40)

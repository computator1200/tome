"""Ingest helpers that run before anything touches the database.

These guard the two things that break real uploads: archives whose extension
lies about their format, and filenames from Windows archives in legacy
encodings.
"""

import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.ingest import decode_zip_name, sniff_archive  # noqa: E402

ZIP_MAGIC = b"PK\x03\x04"
RAR_MAGIC = b"Rar!\x1a\x07\x00"
SEVEN_ZIP_MAGIC = b"7z\xbc\xaf\x27\x1c"


def test_sniffs_zip_by_magic_bytes(tmp_path: Path):
    p = tmp_path / "archive.bin"  # deliberately wrong extension
    p.write_bytes(ZIP_MAGIC + b"\x00" * 64)
    assert sniff_archive(p) == "zip"


def test_sniffs_rar_by_magic_bytes(tmp_path: Path):
    p = tmp_path / "archive.zip"  # claims to be a zip
    p.write_bytes(RAR_MAGIC + b"\x00" * 64)
    assert sniff_archive(p) == "rar"


def test_sniffs_7z_by_magic_bytes(tmp_path: Path):
    p = tmp_path / "archive.zip"
    p.write_bytes(SEVEN_ZIP_MAGIC + b"\x00" * 64)
    assert sniff_archive(p) == "7z"


def test_unknown_format_returns_none(tmp_path: Path):
    p = tmp_path / "not-an-archive.zip"
    p.write_bytes(b"just some text, definitely not an archive")
    assert sniff_archive(p) is None


def _info(name: str, flag: int) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo("x")
    info.filename = name
    info.flag_bits = flag
    return info


def test_decodes_utf8_flag_names_normally():
    name, repaired = decode_zip_name(_info("第01巻/001.jpg", 0x800))
    assert name.endswith("001.jpg")
    assert repaired is False


def test_repairs_cp932_names_that_were_not_flagged_utf8():
    # A Japanese filename encoded as cp932 but written without the UTF-8 flag,
    # which is exactly what Windows archivers produce.
    raw = "第01巻/001.jpg".encode("cp932").decode("cp437")
    name, repaired = decode_zip_name(_info(raw, 0))
    assert "第01巻" in name
    assert repaired is True

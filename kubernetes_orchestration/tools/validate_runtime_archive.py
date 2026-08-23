"""Validate the external automation_system archive without extracting it."""

import argparse
import hashlib
import json
import pathlib
import stat
import zipfile


EXPECTED_ROOT = "automation_system_v0101/"
EXPECTED_VERSION = "automation_system_v0101"
EXPECTED_ARCHIVE_SHA256 = "c1e62ead807817930cd8323852e0ba421d6d70baa336b37f89d86692f1939467"
EXPECTED_MANIFEST_SHA256 = "699c29f9cf58b1bd6bd7407e879b3b5b3f41ad978343037a1dc33a75ec25026d"
MAX_ENTRIES = 10000
MAX_UNCOMPRESSED_BYTES = 256 * 1024 * 1024


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path):
    state = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            state.update(block)
    return state.hexdigest()


def _safe_name(name):
    path = pathlib.PurePosixPath(name)
    return bool(name) and not name.startswith("/") and ".." not in path.parts and "\\" not in name


def _manifest_entries(data):
    result = {}
    for raw_line in data.decode("utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        digest, relative = line.split(None, 1)
        relative = relative.strip()
        if relative.startswith("./"):
            relative = relative[2:]
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ValueError("invalid internal SHA256SUMS digest")
        if not _safe_name(relative) or relative in result:
            raise ValueError("invalid or duplicate internal manifest path")
        result[relative] = digest
    return result


def validate_archive(path, expected_digest=None):
    archive_path = pathlib.Path(path)
    archive_digest = _sha256_file(archive_path)
    requested_digest = (expected_digest or EXPECTED_ARCHIVE_SHA256).removeprefix("sha256:")
    if requested_digest != EXPECTED_ARCHIVE_SHA256:
        raise ValueError("runtime expected digest differs from the v0101 release binding")
    if archive_digest != requested_digest:
        raise ValueError("runtime archive digest mismatch")
    names = set()
    with zipfile.ZipFile(archive_path, "r") as archive:
        entries = archive.infolist()
        if len(entries) > MAX_ENTRIES:
            raise ValueError("runtime archive contains too many entries")
        uncompressed = sum(info.file_size for info in entries)
        if uncompressed > MAX_UNCOMPRESSED_BYTES:
            raise ValueError("runtime archive exceeds the uncompressed size limit")
        for info in entries:
            if not _safe_name(info.filename):
                raise ValueError("unsafe ZIP path: " + info.filename)
            if not info.filename.startswith(EXPECTED_ROOT):
                raise ValueError("runtime member is outside the expected release root")
            if info.filename in names:
                raise ValueError("duplicate ZIP path: " + info.filename)
            names.add(info.filename)
            if info.flag_bits & 0x1:
                raise ValueError("encrypted ZIP entries are forbidden")
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise ValueError("symbolic links are forbidden")
        manifest_name = EXPECTED_ROOT + "SHA256SUMS"
        version_name = EXPECTED_ROOT + "VERSION"
        if manifest_name not in names or version_name not in names:
            raise ValueError("runtime archive identity files are missing")
        version = archive.read(version_name).decode("utf-8").strip()
        if version != EXPECTED_VERSION:
            raise ValueError("runtime version mismatch: " + version)
        manifest_data = archive.read(manifest_name)
        if _sha256(manifest_data) != EXPECTED_MANIFEST_SHA256:
            raise ValueError("runtime internal SHA256SUMS binding mismatch")
        manifest = _manifest_entries(manifest_data)
        expected_files = {EXPECTED_ROOT + relative for relative in manifest}
        expected_files.add(manifest_name)
        actual_files = {info.filename for info in entries if not info.is_dir()}
        extras = sorted(actual_files - expected_files)
        if extras:
            raise ValueError("unmanifested runtime member: " + extras[0])
        missing = sorted(expected_files - actual_files)
        if missing:
            raise ValueError("manifest member missing: " + missing[0])
        for relative, expected in manifest.items():
            member = EXPECTED_ROOT + relative
            if member not in names:
                raise ValueError("manifest member missing: " + relative)
            observed = _sha256(archive.read(member))
            if observed != expected:
                raise ValueError("manifest digest mismatch: " + relative)
    return {
        "ok": True,
        "archive": archive_path.name,
        "sha256": archive_digest,
        "version": EXPECTED_VERSION,
        "entries": len(names),
        "manifest_files": len(manifest),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("archive")
    parser.add_argument("--expected-digest")
    arguments = parser.parse_args()
    print(json.dumps(validate_archive(arguments.archive, arguments.expected_digest),
                     indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

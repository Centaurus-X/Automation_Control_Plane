"""Create the deterministic v0027-r3 release ZIP and sidecar."""

import argparse
import hashlib
import pathlib
import subprocess
import sys
import zipfile

sys.dont_write_bytecode = True

from build_file_audit import build as build_file_audit
from vendor_helm_dependencies import vendor as vendor_helm_dependencies


def _sha256(path):
    state = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            state.update(block)
    return state.hexdigest()


def _manifest(root):
    release_manifest = root / "SHA256SUMS"
    files = sorted(
        path for path in root.rglob("*")
        if path.is_file() and path != release_manifest)
    lines = []
    for path in files:
        relative = path.relative_to(root).as_posix()
        lines.append(_sha256(path) + "  ./" + relative)
    release_manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(files)


def _zip_info(archive_name, mode):
    info = zipfile.ZipInfo(archive_name, date_time=(1980, 1, 1, 0, 0, 0))
    info.create_system = 3
    info.external_attr = (mode & 0xFFFF) << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    return info


def _write_zip(root, output):
    files = sorted(path for path in root.rglob("*") if path.is_file())
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED,
                         compresslevel=9, strict_timestamps=True) as archive:
        for path in files:
            relative = path.relative_to(root.parent).as_posix()
            mode = path.stat().st_mode
            archive.writestr(_zip_info(relative, mode), path.read_bytes(), compresslevel=9)
    return len(files)


def _run_verifier(root, tool):
    result = subprocess.run(
        [sys.executable, "-B", str(tool), "--root", str(root), "--quiet"],
        check=False)
    if result.returncode != 0:
        raise RuntimeError("release verifier failed")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--output", required=True)
    arguments = parser.parse_args()
    root = pathlib.Path(arguments.root).resolve()
    output = pathlib.Path(arguments.output).resolve()
    tools = root / "kubernetes_orchestration" / "tools"
    audit = root / "kubernetes_orchestration" / "documentation" / "FILE_AUDIT_V0027_R3.json"
    vendor_helm_dependencies(root / "kubernetes_orchestration")
    build_file_audit(root, audit)
    _run_verifier(root, tools / "verify_release.py")
    manifest_files = _manifest(root)
    _run_verifier(root, tools / "verify_release.py")
    output.parent.mkdir(parents=True, exist_ok=True)
    archive_files = _write_zip(root, output)
    digest = _sha256(output)
    sidecar = output.with_suffix(".sha256")
    sidecar.write_text(digest + "  " + output.name + "\n", encoding="utf-8")
    print("Release ZIP: " + str(output))
    print("SHA-256: " + digest)
    print("Manifest files: " + str(manifest_files))
    print("Archive files: " + str(archive_files))


if __name__ == "__main__":
    main()

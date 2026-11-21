"""Verify and unpack a Retro Library backup into a NEW destination directory.

Usage: python restore_backup.py archive.tar new-directory
Existing installations and Apple TV are never modified by this helper.
"""
import sys
from pathlib import Path
import shutil
import tarfile
from service import verify_archive, safe_path


def main():
    if len(sys.argv) != 3:
        raise SystemExit('Usage: python restore_backup.py archive.tar NEW-DIRECTORY')
    archive_path, destination = Path(sys.argv[1]), Path(sys.argv[2])
    if destination.exists():
        raise SystemExit('Destination must not exist: choose a new directory.')
    manifest = verify_archive(archive_path)
    # Validate every member before creating anything.
    for relative in manifest['files']:
        safe_path(destination, relative)
    destination.mkdir(parents=True)
    with tarfile.open(archive_path) as archive:
        for member in archive:
            path = safe_path(destination, member.name)
            path.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(member) as source, path.open('xb') as target:
                shutil.copyfileobj(source, target)
    print(f'Verified and unpacked {len(manifest["files"])} files to {destination}')


if __name__ == '__main__':
    main()

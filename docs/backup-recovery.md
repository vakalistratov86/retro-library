# Backup and recovery

## Archive contents

- `library/`: managed downloads, BIOS, complete NAS playlists and thumbnails.
- `state/`: metadata, synchronization selection and capture bookkeeping when present.
- `apple-tv/`: the most recent successfully captured saves, states, core configuration, remaps and exposed `retroarch.cfg`.
- `service/`: available application source/configuration files.
- `manifest.json`: creation information and a size/SHA-256 entry for every file.

RetroArch binaries, tvOS data and paths not exposed by RetroArch's web server are not backed up. A successful archive does not imply that Apple TV was online: its record explicitly shows the capture timestamp or that no capture exists.

The archive is read back and every file is hashed before it becomes downloadable. Backups contain private games and save data; do not commit or attach them to public issues. Archives are not automatically deleted. Choose a filesystem supporting files larger than 4 GB for large collections.

## Restoring

Click **Восстановить** beside an archive, or import a TAR with **Загрузить TAR с внешнего диска**. A preflight verifies integrity and displays added/replaced/unchanged counts. Restoration verifies the archive again, stages its data and preserves replaced files under `state/restore-history/`.

Files added after the backup are kept. Service code from an archive is never executed or installed. Device ROMs/BIOS that differ are not silently overwritten by the normal sync. Explicit save restoration first captures current device data; if that fails, restoration aborts before changes. A partial transfer is reported as an error, with its recovery-history path retained.

For offline recovery, keep `restore_backup.py` beside `service.py` and run:

```sh
python restore_backup.py archive.tar NEW-DIRECTORY
```

The destination must not exist. All checksums are verified before extraction, and unsafe paths/non-regular archive entries are rejected.

## Deleting an archive

**Удалить** opens a confirmation dialog naming the archive. Only registered backup files can be deleted; arbitrary server paths are rejected. Deletion is blocked while a backup, import, integrity check or restoration is active. It does not remove the live library or device saves and cannot be undone through the UI.

## Direct NAS drive destinations

Browser download is available immediately and can be saved to an external drive attached to the browser computer. For a drive mounted on the NAS, an administrator must add its specific backup directory as a writable bind mount and set, for example:

```yaml
environment:
  BACKUP_DESTINATIONS: '{"usb":{"label":"External USB","path":"/backup-usb"}}'
volumes:
  - /mnt/EXTERNAL/retro-backups:/backup-usb:rw
```

Merge these entries into the existing service configuration. The application does not mount, format or change permissions on physical disks. Only configured, available directories appear in the destination selector.

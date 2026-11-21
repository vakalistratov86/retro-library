# Retro Library

[![CI](https://github.com/vakalistratov86/retro-library/actions/workflows/ci.yml/badge.svg)](https://github.com/vakalistratov86/retro-library/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

A self-hosted library manager for **RetroArch on Apple TV**, designed for TrueNAS SCALE and Docker. Keep your collection on a NAS, choose what belongs in the Apple TV playlist, restore missing files, and make portable backups through a Russian-language web UI.

**[Русская инструкция](docs/README.ru.md)** · [TrueNAS installation](docs/truenas.md) · [Backup and recovery](docs/backup-recovery.md) · [Security](SECURITY.md)

## Features

- Upload ROMs, BIOS and custom PNG covers from a browser. Supported systems: NES, SNES, Mega Drive / Genesis, PlayStation and DOS.
- Select individual games, or select/deselect the currently filtered list. Device playlists are generated separately; the NAS keeps the complete collection.
- Recover missing files when RetroArch is reachable. Uploads are staged, downloaded for SHA-256 verification and then moved to their final destination.
- Automatically look up game information and box art after upload, with screenshots and title screens when available. Libretro CRC32 matching takes priority over exact normalized titles; fuzzy matches require a manual choice.
- View year, genre, developer, publisher, source links and an attributed Wikipedia description when a sufficiently clear match exists. Canonical names are applied to cards and playlists together with matching artwork; language/variant markers remain distinguishable. Existing custom covers are preserved.
- Create SHA-256-verified TAR backups, download them to an external drive, import them, preview restoration, restore data and delete individual archives.
- Restore saves/settings to Apple TV only through a separate opt-in. Current versions are captured before replacement.

**No ROMs, BIOS, saves, artwork packs or personal library data are distributed in this repository.** Supply your own files and respect their licenses.

## Requirements and limits

- Docker with Compose, or a TrueNAS SCALE release supporting custom Docker Compose applications. The original deployment was tested on SCALE 24.10.2.4.
- RetroArch running in the foreground on Apple TV, exposing its HTTP uploader on port 80 and WebDAV on port 8080. Tested with RetroArch 1.22.2; other builds may differ.
- Network access from the container to Apple TV. Metadata lookup additionally uses GitHub/Libretro, Wikipedia and Wikidata over HTTPS. No metadata API key is required.
- A trusted LAN. The UI has no login; it uses same-origin POST checks and CSRF tokens. The example binds to localhost. Do not expose it directly to the Internet.

**Deselecting a game does not delete files already stored on Apple TV.** It removes that entry from the generated device playlist and stops future recovery of its exclusive ROM/artwork files. Shared disc tracks remain eligible while a selected game needs them. BIOS remain shared system files. Manual edits to device playlists are protected as conflicts rather than silently overwritten.

## Quick start

```sh
git clone https://github.com/vakalistratov86/retro-library.git
cd retro-library
cp .env.example .env
mkdir -p data/library data/state
```

Edit `.env`: set `APPLE_TV_HOST`, and set `PUID`/`PGID` to the owner of the two data directories. For access from another computer, set `BIND_ADDRESS` to your server's LAN address. Keep credentials and real deployment settings out of Git.

```sh
docker compose up -d --build
docker compose logs --tail=100 retro-library
```

Open `http://localhost:30808`, or the configured LAN address. Open RetroArch on Apple TV and upload files through **Добавить файлы**. Use the **На Apple TV** checkbox on each card to control synchronization. Reopen the playlist in RetroArch if it has cached an earlier version.

For PlayStation, select BIN and CUE together: the uploader sends tracks first and checks CUE references. CHD, ISO, PBP and M3U are also supported. RAR/7z must be extracted before uploading. DOS uses `.dosz` or `.zip`. Known `scph5500/5501/5502.bin` images are checked against their expected MD5 values.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `APPLE_TV_HOST` | Required by Compose | Apple TV IP/hostname; HTTP ports 80 and 8080 |
| `BIND_ADDRESS` | `127.0.0.1` | Host address for the web UI |
| `WEB_PORT` | `30808` | Published UI port |
| `PUID`, `PGID` | `1000` | Container user/group; match data-directory permissions |
| `LIBRARY_PATH` | `./data/library` | Persistent ROM/BIOS/playlist/artwork storage |
| `STATE_PATH` | `./data/state` | Metadata, selection, backups and recovery history |
| `SYNC_INTERVAL` | `60` | Seconds between synchronization passes |
| `METADATA_ENABLED` | `true` | Background lookup for newly uploaded/unprocessed games |

The Python process also accepts `LIBRARY_ROOT`, `STATE_ROOT`, `PORT` and `BACKUP_DESTINATIONS`. See [direct backup destinations](docs/backup-recovery.md#direct-nas-drive-destinations).

## Metadata and privacy

Metadata lookup downloads cached Libretro catalogs, computes ROM checksums **locally**, and uses exact matches or explicit manual selection. ROM/BIOS contents are never uploaded to providers. Wikipedia receives a game-title search; GitHub receives catalog/image requests. Cache data stays in the private state directory and is not part of this repository.

Missing year, genre, developer and publisher are filled from the Wikidata entity linked to the matched article. Platform-qualified claims take priority, claims for other platforms are excluded, and existing Libretro values are preserved. The year is the earliest available release year among eligible claims. Wikidata receives entity IDs only; source links are retained. Unavailable facts remain empty. Existing library entries are enriched automatically after upgrading, without re-uploading ROMs.

Uncertain matches remain **Уточните игру** rather than receiving a guessed cover. Open a card to select a candidate or retry. Translations, hacks, homebrew and unusually named files may need manual matching. Provider outages/rate limits do not block ROM upload or normal recovery. Existing information and covers are retained.

Artwork follows RetroArch's `thumbnails/<playlist>/Named_Boxarts|Named_Snaps|Named_Titles/<label>.png` layout. The service decodes source images with Pillow and writes PNG. Canonical playlist labels and artwork filenames are updated together. ROM filenames and save paths stay unchanged; existing custom artwork is preserved.

See [NOTICE](NOTICE) for third-party rights and attribution. Wikipedia text retains a source link and CC BY-SA 4.0 attribution in the UI; it may be shortened. The project's MIT license applies to project code, not downloaded content.

## Development and checks

Python 3.12+ and Node.js are used for the checks. Node is not required at runtime.

```sh
python -m venv .venv
# Activate the environment for your shell, then:
python -m pip install -r requirements.txt
python -m unittest -v
node --check app.js
node --check library.js
```

Tests cover recovery, overwrite protection, filtered playlists, shared disc dependencies, backup integrity/restoration/deletion, archive import, CSRF/path checks, metadata matching and image conversion. Network providers are mocked in tests; no ROM collection or Apple TV is needed.

The application uses Python's standard-library HTTP server and a background worker, plus Pillow for image validation. It is intentionally a small LAN utility, not a hardened Internet-facing service. See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution rules and [CHANGELOG.md](CHANGELOG.md) for releases.

## Upgrading

Create a backup in the UI first, retain both data directories, then:

```sh
git pull --ff-only
docker compose up -d --build
```

Do not run `docker compose down -v` against data you want to keep. Restoring a TAR through the UI restores library data; it never installs executable code from that archive. This project is independent of Apple, TrueNAS, Libretro and Wikipedia.

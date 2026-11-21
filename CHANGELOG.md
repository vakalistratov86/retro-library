# Changelog

## 1.1.2 — 2026-11-21

- Resolve missing facts independently of Wikipedia descriptions using exact Wikidata titles/aliases and platform evidence. Ambiguous matches remain unselected.
- Accept genre-based introductory wording and try the next Wikipedia language after a provider failure.
- Add sourced, platform-scoped corrections for known aliases, port credits and the Ultimate Challenge mission pack.
- Mark incomplete metadata explicitly and retry automatically: weekly for unavailable facts; exponential backoff from one hour to one day for errors.
- Preserve retry schedules across restarts, retain manual choices and show missing fields/next retry time in the UI.

## 1.1.1 — 2026-11-21

- Fill missing year, genre, developer and publisher from the Wikidata entity linked to the matched Wikipedia article.
- Respect platform qualifiers and preserve existing Libretro values and cached facts during provider outages.
- Automatically enrich previously processed library entries once; display source links for additional facts.
- Add regression coverage for metadata migration, matching, merging and retries.

## 1.1.0 — 2026-11-21

First public release.

- ROM/BIOS uploads, library cards and Apple TV file status.
- Missing-file recovery with staged uploads and SHA-256 verification.
- Per-game sync selection, filtered device playlists and shared-disc dependencies.
- Cached Libretro metadata/artwork lookup, conservative matching and manual candidates.
- Attributed Wikipedia descriptions when a clear match is available.
- Verified TAR backup, import, restore preview, protected restoration and archive deletion.
- Separate opt-in for restoring device saves/settings.
- Docker/TrueNAS examples, CI, security policy and contribution documentation.

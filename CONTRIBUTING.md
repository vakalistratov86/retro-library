# Contributing

Open an issue describing the problem before proposing a large change. Include the
application version, RetroArch version, deployment type, expected behavior and a
minimal reproduction. Replace personal addresses and paths with placeholders.

1. Fork the repository and create a focused branch.
2. Install `requirements.txt` in a virtual environment.
3. Add meaningful tests for behavior changes. Mock external providers and Apple TV.
4. Run `python -m unittest -v`, `node --check app.js` and `node --check library.js`.
5. Update relevant documentation and the changelog.
6. Submit a pull request explaining the user-visible change and validation.

Do not attach ROMs, BIOS, saves, artwork packs, private configuration or credentials.
Use synthetic fixtures. Preserve existing file-protection and backup semantics.
Avoid adding dependencies without a clear need. Contributions are under the MIT
license; third-party material must retain its own applicable license/attribution.

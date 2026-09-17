# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/) and the project follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Verified chlorine automation status and auto/off control (`5.154`, `19.17`/`19.18`).
- Persistent two-hour post-dose automation guard that disables both dosing automations and restores only the channels that were previously automatic.

### Changed

- Hardened MQTT write confirmation against stale cached mode responses.
- Standardized the public documentation and issue templates in English.

## [2.0.0] - 2026-09-09

### Added

- First documented public release.
- BAYROL pool-data polling, local cache/API, MQTT status, and verified pH auto/off commands.
- Example configuration files without production credentials.
- Loxone integration and troubleshooting documentation.
- MIT License.

# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-10-04

### Added
- Auto-discovery of Pangolin resources through the Integration API (`public-resources`, legacy `resources` fallback).
- One Gatus endpoint per enabled resource with an active health check, plus a `Pangolin API` endpoint.
- Alerts for any configured Gatus provider, include/exclude filters, `FAIL_ON_UNKNOWN` mode.
- Atomic, change-only writes; the existing file is kept when the API fails or returns nothing.
- `--once`, `--dry-run` and `--healthcheck` modes, hardened container, unit and end-to-end tests.

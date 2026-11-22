# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Compatibility

Tabularium is **alpha**. Until a `1.0` release, any release may change
commands, record formats under `.ai/`, schemas and Python interfaces without a
deprecation period. Changes of that kind are listed under **Changed** or
**Removed** for the release that introduces them. Linux is the supported
platform. The version string lives in [`VERSION`](VERSION).

## [Unreleased]

### Added

- `CONTRIBUTING.md` with development commands, check scope, pull request
  expectations and a release checklist.
- `SECURITY.md` with the private vulnerability reporting route.
- CI: a bounded Ruff lint baseline and a `pip-audit` dependency audit job.

## [0.3.0.dev0] - 2026-11-01

### Added

- Initial public release of the source tree (alpha). See the
  [README](README.md) for scope, guarantees and known limits.

[Unreleased]: https://github.com/kaaan11/Tabularium/compare/0ef36ec...HEAD
[0.3.0.dev0]: https://github.com/kaaan11/Tabularium/commit/0ef36ec

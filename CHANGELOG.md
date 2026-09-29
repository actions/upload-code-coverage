# Changelog

## Unreleased

### Fixed

- Non-default branch pushes no longer associate coverage with an unrelated fork pull request that uses the same branch name.

## v1.4.3 - 2026-09-28

### Fixed

- Non-default branch pushes without an open pull request now skip successfully instead of attempting an upload that the API rejects.
- GitHub CLI failures during pull request discovery now produce an actionable error instead of being silently treated as no matching pull request.

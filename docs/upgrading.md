# Upgrading

Review the [changelog](../CHANGELOG.md) before upgrading.

## Version references

Each release uses a complete semantic version tag such as `v1.4.3`. The corresponding major tag remains a supported movable alias:

```yaml
- uses: actions/upload-code-coverage@v1
```

Workflows using `v1` receive new `v1.x.x` releases when the major tag advances. Workflows pinned to a complete version or commit SHA must update that reference explicitly.

## Compatibility

Existing workflows can upgrade without changing their triggers, job layout, action inputs, or permissions.

Push-only workflows should keep `pull-requests: read` so the action can discover an open pull request for a pushed branch. For a non-default branch push without an open pull request, the action now skips successfully instead of attempting an upload that the API rejects.

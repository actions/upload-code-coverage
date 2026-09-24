# Upload Code Coverage

Upload a Cobertura XML coverage report to GitHub's code coverage API.

## Usage

```yaml
- uses: actions/upload-code-coverage@v1
  with:
    file: cobertura.xml
    language: Java
    label: code-coverage/jacoco
```

The action handles everything else automatically: event routing, gzip/base64 encoding, resolving the correct commit SHA and ref, detecting PR numbers from both `pull_request` and `push` events, and calling the upload API.

## Inputs

| Input | Required | Default | Description |
|-------|----------|---------|-------------|
| `file` | Yes | | Path to the Cobertura XML coverage report |
| `language` | Yes | | Linguist language name (e.g. `Java`, `Go`, `Python`) |
| `label` | Yes | | Label for the report (e.g. `code-coverage/jacoco`) |
| `fail-on-error` | No | `true` | Whether to fail the workflow step if the upload fails |
| `wait-for-processing-timeout` | No | `160` | How many seconds to wait for processing before failing the step. Set to 0 to disable waiting |
| `token` | No | `github.token` | GitHub token with `code-quality:write` permission |

## Permissions

The calling workflow or job must grant `code-quality: write`. The action cannot declare this itself.

```yaml
permissions:
  contents: read
  code-quality: write
```

For push-only workflows where the action looks up PR numbers, also add `pull-requests: read`.
The runner must have the GitHub CLI installed. GitHub-hosted runners include it by default.

## Error handling

By default, the action fails the workflow step (exits with code 1) when the upload is unsuccessful. This ensures you notice when coverage data is not being stored.

By default, the action also waits for GitHub to finish processing a successful upload. If processing reports `failed`, or if it never reaches a terminal status before the timeout, the step fails the same way as an upload error.

If the uploaded report is for a commit that is no longer the latest commit on its branch, GitHub skips the upload. The action logs a warning, does not wait for processing, and exits successfully.

If coverage upload is best-effort in your workflow and you don't want a transient API failure to block CI, set `fail-on-error: false`:

```yaml
- uses: actions/upload-code-coverage@v1
  with:
    file: cobertura.xml
    language: Java
    label: code-coverage/jacoco
    fail-on-error: false
```

When `fail-on-error` is `false`, upload errors are still surfaced as `::error::` annotations in the workflow log, but the step exits 0.

If you want the action to return immediately after the upload request instead of waiting for processing, set `wait-for-processing-timeout: 0`:

```yaml
- uses: actions/upload-code-coverage@v1
  with:
    file: cobertura.xml
    language: Java
    label: code-coverage/jacoco
    wait-for-processing-timeout: 0
```

## Event handling

The action auto-detects the event type and resolves the correct behavior:

- **`pull_request` / `pull_request_target`**: Uses the PR head SHA (not the merge commit) and includes the PR number.
- **Default-branch `push`**: Uses `github.sha` and `github.ref`.
- **Non-default branch `push` with an open PR**: Looks up the PR number and uploads coverage for the pushed commit.
- **Non-default branch `push` without an open PR**: Skips successfully instead of sending an upload request the API cannot associate with a PR.

Configure workflows to run for pull requests and pushes to the default branch:

```yaml
on:
  push:
    branches: [main]
  pull_request:
    branches: [main]
```

Push-only workflows can upload PR coverage by granting `pull-requests: read`. Repositories may also use broader `push` triggers for other CI requirements; pushes without an open PR are skipped safely.

## Full example (separate upload job)

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:

jobs:
  build:
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@v6
        with:
          ref: ${{ github.event.pull_request.head.sha || github.sha }}

      # ... build and generate cobertura.xml ...

      - uses: actions/upload-artifact@v4
        with:
          name: cobertura-report
          path: cobertura.xml

  upload-coverage:
    needs: build
    runs-on: ubuntu-latest
    permissions:
      contents: read
      code-quality: write
    steps:
      - uses: actions/download-artifact@v4
        with:
          name: cobertura-report

      - uses: actions/upload-code-coverage@v1
        with:
          file: cobertura.xml
          language: Java
          label: code-coverage/jacoco
```

## Caveats

- **Fork PRs are not supported.** Pull requests from forks don't have write access to the base repository, so the action cannot upload coverage. When a fork PR is detected, the action exits gracefully with a notice — it won't fail your CI.
- **Merge queue runs are skipped.** Coverage should be uploaded for PRs and the default branch, making merge queue uploads unnecessary. The action logs a warning and exits successfully.

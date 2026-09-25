# Choosing a workflow strategy

The action supports different trigger and job layouts. Choose the one that best fits your CI workflow.

## Pull requests and default-branch pushes

For most repositories, run coverage for pull requests and pushes to the default branch:

```yaml
on:
  pull_request:
  push:
    branches: [main]
```

This avoids running coverage for branches that do not have a pull request. Pull request events also provide the PR number directly, so the upload job does not need `pull-requests: read`.

## Push-only workflows

Some workflows use `push` events to run tests on every branch, without a `pull_request` trigger. This trigger strategy works with either the separate-job or same-job layout described below.

In a separate-job layout, the test job generates the report and the upload job receives it through an artifact on every push. The action then determines whether the report can be uploaded:

- Default-branch pushes upload normally.
- Pushes with an open pull request upload using the discovered PR number.
- Pushes without an open pull request skip successfully.

If a branch is pushed before its pull request is opened, coverage is not uploaded until the next push. To upload coverage when a pull request is opened without requiring another push, use a `pull_request` trigger.

Push-only workflows have an existing requirement to grant the upload job `pull-requests: read`:

```yaml
permissions:
  contents: read
  code-quality: write
  pull-requests: read
```

See the [README permissions section](../README.md#permissions) for the complete requirements, including GitHub CLI availability.

## Separate upload job

A separate upload job keeps `code-quality: write` away from the job that runs repository code and third-party dependencies. Because jobs have isolated filesystems, pass the coverage report between them with `actions/upload-artifact` and `actions/download-artifact`.

It also allows the test job to run independently when the upload job is skipped. This is the recommended layout.

## Same-job upload

For a simpler workflow, generate and upload the report in the same job:

```yaml
permissions:
  contents: read
  code-quality: write

steps:
  # ... generate cobertura.xml ...

  - uses: actions/upload-code-coverage@v1
    with:
      file: cobertura.xml
      language: Java
      label: code-coverage/jacoco
```

No artifact transfer is needed, but the job that runs tests also receives `code-quality: write`. If the job runs on every push, tests still run before the action decides whether to upload or skip. Applying an event condition to the entire job instead would skip both testing and uploading.

## Avoiding duplicate runs

A workflow with broad `push` and `pull_request` triggers may run twice for the same pull request commit. To avoid that, either:

- Use `pull_request` plus default-branch `push`; or
- Use a push-only workflow with `pull-requests: read`.

## Multiple reports

Call the action once for each report and use a distinct `label`:

```yaml
- uses: actions/upload-code-coverage@v1
  with:
    file: backend.xml
    language: Python
    label: backend

- uses: actions/upload-code-coverage@v1
  with:
    file: frontend.xml
    language: JavaScript
    label: frontend
```

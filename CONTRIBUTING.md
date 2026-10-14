# Contributing

Thanks for your interest in contributing to Upload Code Coverage.

Contributions are released under the project's [MIT license](LICENSE). By
participating in this project, you agree to abide by the
[GitHub Community Code of Conduct](https://github.com/actions/.github/blob/main/CODE_OF_CONDUCT.md).

## Development

The project requires Python 3.9 or later and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --locked --group dev
uv run python -m unittest \
  test_upload_coverage.py \
  test_status_report.py \
  test_post_upload_coverage.py \
  test_resolve_upload_context.py \
  test_action_security.py \
  -v
uv run ruff check .
uv run ruff format --check .
```

## Submitting a pull request

1. Fork and clone the repository.
2. Create a branch for your change.
3. Make a focused change and add or update tests where appropriate.
4. Run the tests, lint checks, and formatting check.
5. Open a pull request describing the change and why it is needed.

Before starting a substantial change, open an issue so the approach can be
discussed with the maintainers.

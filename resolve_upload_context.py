#!/usr/bin/env python3
import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse


@dataclass(frozen=True)
class UploadContext:
    should_upload: bool
    commit_oid: str = ""
    ref: str = ""
    pr_number: str = ""
    annotation_level: str = ""
    message: str = ""


@dataclass(frozen=True)
class PullRequestLookup:
    number: str = ""
    error: str = ""


def _find_open_pull_request(environ: Mapping[str, str]) -> PullRequestLookup:
    repository = environ.get("GITHUB_REPOSITORY", "")
    ref_name = environ.get("GITHUB_REF_NAME", "")
    command_environment = dict(environ)
    server_url = environ.get("GITHUB_SERVER_URL", "")
    if server_url:
        command_environment["GH_HOST"] = urlparse(server_url).netloc

    try:
        result = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                repository,
                "--head",
                ref_name,
                "--state",
                "open",
                "--json",
                "number",
                "--jq",
                ".[0].number // empty",
            ],
            capture_output=True,
            check=False,
            env=command_environment,
            text=True,
        )
    except OSError as error:
        return PullRequestLookup(error=f"unable to run gh: {error}")

    if result.returncode != 0:
        return PullRequestLookup(
            error=result.stderr.strip() or f"gh exited with status {result.returncode}"
        )

    return PullRequestLookup(number=result.stdout.strip())


def resolve_upload_context(
    environ: Optional[Mapping[str, str]] = None,
    pull_request_lookup: Optional[Callable[[Mapping[str, str]], PullRequestLookup]] = None,
) -> UploadContext:
    env = dict(os.environ if environ is None else environ)
    lookup = _find_open_pull_request if pull_request_lookup is None else pull_request_lookup
    event_name = env.get("GITHUB_EVENT_NAME", "")
    repository = env.get("GITHUB_REPOSITORY", "")
    ref = env.get("GITHUB_REF", "")

    if event_name == "merge_group":
        return UploadContext(
            should_upload=False,
            annotation_level="warning",
            message=(
                "Skipping coverage upload for merge queue. Coverage should be uploaded "
                "for pull requests and the default branch instead."
            ),
        )

    if event_name in {"pull_request", "pull_request_target"}:
        head_repository = env.get("COVERAGE_PR_HEAD_REPOSITORY", "")
        if head_repository and head_repository != repository:
            return UploadContext(
                should_upload=False,
                annotation_level="notice",
                message=f"Skipping coverage upload for fork PR from {head_repository}.",
            )

        return UploadContext(
            should_upload=True,
            commit_oid=env.get("COVERAGE_PR_HEAD_SHA", ""),
            pr_number=env.get("COVERAGE_PR_NUMBER", ""),
        )

    if event_name == "push":
        default_branch = env.get("COVERAGE_DEFAULT_BRANCH", "")
        is_non_default_branch = (
            default_branch
            and ref.startswith("refs/heads/")
            and ref != f"refs/heads/{default_branch}"
        )
        if is_non_default_branch:
            pull_request = lookup(env)
            if pull_request.number:
                return UploadContext(
                    should_upload=True,
                    commit_oid=env.get("GITHUB_SHA", ""),
                    ref=ref,
                    pr_number=pull_request.number,
                )

            if pull_request.error:
                return UploadContext(
                    should_upload=False,
                    annotation_level="error",
                    message=(
                        "Failed to query pull requests using the GitHub CLI: "
                        f"{pull_request.error}. Ensure gh is installed and the workflow "
                        "grants pull-requests: read permission."
                    ),
                )

            return UploadContext(
                should_upload=False,
                annotation_level="notice",
                message=(
                    "Skipping coverage upload for non-default branch push because no "
                    "open pull request was found."
                ),
            )

    return UploadContext(
        should_upload=True,
        commit_oid=env.get("GITHUB_SHA", ""),
        ref=ref,
    )


def _append_lines(file_path: str, lines: list[str]) -> None:
    if not file_path:
        return
    with Path(file_path).open("a", encoding="utf-8") as stream:
        for line in lines:
            stream.write(f"{line}\n")


def main(environ: Optional[Mapping[str, str]] = None) -> int:
    env = dict(os.environ if environ is None else environ)
    context = resolve_upload_context(env)
    output_file_path = env.get("GITHUB_OUTPUT", "")
    summary_file_path = env.get("GITHUB_STEP_SUMMARY", "")

    _append_lines(
        output_file_path,
        [
            f"should_upload={str(context.should_upload).lower()}",
            f"commit_oid={context.commit_oid}",
            f"ref={context.ref}",
            f"pr_number={context.pr_number}",
        ],
    )

    if not context.should_upload:
        print(f"::{context.annotation_level}::{context.message}")
        outcome = "failed" if context.annotation_level == "error" else "skipped"
        _append_lines(
            summary_file_path,
            [f"### Code coverage upload {outcome}", "", context.message],
        )

    return 1 if context.annotation_level == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())

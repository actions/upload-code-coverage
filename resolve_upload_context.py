#!/usr/bin/env python3
import json
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
    warning: str = ""


@dataclass(frozen=True)
class PullRequestCandidate:
    number: int
    base_ref_name: str


def _parse_pull_request_lookup(
    output: str,
    repository: str,
    ref_name: str,
    default_branch: str,
) -> PullRequestLookup:
    try:
        pull_requests = json.loads(output)
    except json.JSONDecodeError as error:
        return PullRequestLookup(error=f"gh returned invalid JSON: {error.msg}")

    if not isinstance(pull_requests, list):
        return PullRequestLookup(error="gh returned invalid JSON: expected a list")

    matching_pull_requests: list[PullRequestCandidate] = []
    for pull_request in pull_requests:
        if not isinstance(pull_request, dict):
            return PullRequestLookup(
                error="gh returned invalid JSON: expected pull request objects"
            )

        head_repository = pull_request.get("headRepository")
        if not isinstance(head_repository, dict):
            continue

        head_repository_name = head_repository.get("nameWithOwner")
        if (
            pull_request.get("headRefName") != ref_name
            or not isinstance(head_repository_name, str)
            or head_repository_name.casefold() != repository.casefold()
        ):
            continue

        number = pull_request.get("number")
        if not isinstance(number, int):
            return PullRequestLookup(
                error="gh returned invalid JSON: expected a pull request number"
            )
        base_ref_name = pull_request.get("baseRefName")
        if not isinstance(base_ref_name, str):
            return PullRequestLookup(
                error="gh returned invalid JSON: expected a pull request base branch"
            )

        if base_ref_name == default_branch:
            return PullRequestLookup(number=str(number))

        matching_pull_requests.append(
            PullRequestCandidate(number=number, base_ref_name=base_ref_name)
        )

    if len(matching_pull_requests) == 1:
        return PullRequestLookup(number=str(matching_pull_requests[0].number))

    if len(matching_pull_requests) > 1:
        numbers = ", ".join(
            str(candidate.number)
            for candidate in sorted(
                matching_pull_requests,
                key=lambda candidate: candidate.number,
            )
        )
        return PullRequestLookup(
            warning=(
                f"multiple open pull requests from {repository}:{ref_name} matched: "
                f"{numbers}, but none uniquely targets the default branch "
                f"{default_branch}"
            )
        )

    return PullRequestLookup()


def _find_open_pull_request(environ: Mapping[str, str]) -> PullRequestLookup:
    repository = environ.get("GITHUB_REPOSITORY", "")
    ref_name = environ.get("GITHUB_REF_NAME", "")
    default_branch = environ.get("COVERAGE_DEFAULT_BRANCH", "")
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
                "--limit",
                "1000",
                "--json",
                "number,headRefName,headRepository,baseRefName",
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

    return _parse_pull_request_lookup(
        result.stdout,
        repository,
        ref_name,
        default_branch,
    )


def resolve_upload_context(
    environ: Mapping[str, str],
    pull_request_lookup: Optional[Callable[[Mapping[str, str]], PullRequestLookup]] = None,
) -> UploadContext:
    env = dict(environ)
    lookup_pull_request = (
        _find_open_pull_request if pull_request_lookup is None else pull_request_lookup
    )
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
        default_branch_ref = f"refs/heads/{default_branch}"
        is_branch_push = ref.startswith("refs/heads/")
        is_non_default_branch = default_branch and is_branch_push and ref != default_branch_ref
        if is_non_default_branch:
            pull_request = lookup_pull_request(env)
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

            if pull_request.warning:
                return UploadContext(
                    should_upload=False,
                    annotation_level="warning",
                    message=(
                        f"Skipping coverage upload because {pull_request.warning}. "
                        "Use a pull_request workflow trigger for unambiguous per-PR "
                        "uploads."
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

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import resolve_upload_context


class ResolveUploadContextTests(unittest.TestCase):
    def setUp(self):
        self.base_env = {
            "GITHUB_EVENT_NAME": "push",
            "GITHUB_REPOSITORY": "octo-org/octo-repo",
            "GITHUB_REF": "refs/heads/main",
            "GITHUB_REF_NAME": "main",
            "GITHUB_SHA": "deadbeef",
            "GITHUB_SERVER_URL": "https://github.com",
            "COVERAGE_DEFAULT_BRANCH": "main",
            "COVERAGE_PR_HEAD_REPOSITORY": "",
            "COVERAGE_PR_HEAD_SHA": "",
            "COVERAGE_PR_NUMBER": "",
        }

    def test_default_branch_push_uploads_without_pr_number(self):
        context = resolve_upload_context.resolve_upload_context(self.base_env)

        self.assertTrue(context.should_upload)
        self.assertEqual("deadbeef", context.commit_oid)
        self.assertEqual("refs/heads/main", context.ref)
        self.assertEqual("", context.pr_number)

    def test_non_default_branch_push_with_open_pr_uploads(self):
        env = dict(
            self.base_env,
            GITHUB_REF="refs/heads/feature",
            GITHUB_REF_NAME="feature",
        )

        context = resolve_upload_context.resolve_upload_context(
            env,
            lambda _: resolve_upload_context.PullRequestLookup(number="42"),
        )

        self.assertTrue(context.should_upload)
        self.assertEqual("deadbeef", context.commit_oid)
        self.assertEqual("refs/heads/feature", context.ref)
        self.assertEqual("42", context.pr_number)

    def test_non_default_branch_push_without_open_pr_skips(self):
        env = dict(
            self.base_env,
            GITHUB_REF="refs/heads/feature",
            GITHUB_REF_NAME="feature",
        )

        context = resolve_upload_context.resolve_upload_context(
            env,
            lambda _: resolve_upload_context.PullRequestLookup(),
        )

        self.assertFalse(context.should_upload)
        self.assertEqual("notice", context.annotation_level)
        self.assertIn("no open pull request", context.message)

    def test_non_default_branch_push_with_lookup_failure_fails(self):
        env = dict(
            self.base_env,
            GITHUB_REF="refs/heads/feature",
            GITHUB_REF_NAME="feature",
        )

        context = resolve_upload_context.resolve_upload_context(
            env,
            lambda _: resolve_upload_context.PullRequestLookup(error="HTTP 403"),
        )

        self.assertFalse(context.should_upload)
        self.assertEqual("error", context.annotation_level)
        self.assertIn("HTTP 403", context.message)
        self.assertIn("gh is installed", context.message)
        self.assertIn("pull-requests: read", context.message)

    def test_default_branch_push_does_not_look_up_pull_request(self):
        lookup = mock.Mock()

        context = resolve_upload_context.resolve_upload_context(
            self.base_env,
            lookup,
        )

        self.assertTrue(context.should_upload)
        lookup.assert_not_called()

    def test_renamed_default_branch_push_uploads(self):
        env = dict(
            self.base_env,
            GITHUB_REF="refs/heads/trunk",
            GITHUB_REF_NAME="trunk",
            COVERAGE_DEFAULT_BRANCH="trunk",
        )

        context = resolve_upload_context.resolve_upload_context(env)

        self.assertTrue(context.should_upload)
        self.assertEqual("refs/heads/trunk", context.ref)

    def test_tag_push_preserves_existing_upload_behavior(self):
        env = dict(
            self.base_env,
            GITHUB_REF="refs/tags/v1.0.0",
            GITHUB_REF_NAME="v1.0.0",
        )

        context = resolve_upload_context.resolve_upload_context(env)

        self.assertTrue(context.should_upload)
        self.assertEqual("refs/tags/v1.0.0", context.ref)

    def test_same_repository_pull_request_uploads(self):
        env = dict(
            self.base_env,
            GITHUB_EVENT_NAME="pull_request",
            GITHUB_REF="refs/pull/42/merge",
            COVERAGE_PR_HEAD_REPOSITORY="octo-org/octo-repo",
            COVERAGE_PR_HEAD_SHA="cafebabe",
            COVERAGE_PR_NUMBER="42",
        )

        context = resolve_upload_context.resolve_upload_context(env)

        self.assertTrue(context.should_upload)
        self.assertEqual("cafebabe", context.commit_oid)
        self.assertEqual("", context.ref)
        self.assertEqual("42", context.pr_number)

    def test_fork_pull_request_skips(self):
        env = dict(
            self.base_env,
            GITHUB_EVENT_NAME="pull_request",
            COVERAGE_PR_HEAD_REPOSITORY="contributor/octo-repo",
        )

        context = resolve_upload_context.resolve_upload_context(env)

        self.assertFalse(context.should_upload)
        self.assertIn("fork PR", context.message)

    def test_merge_group_skips(self):
        env = dict(self.base_env, GITHUB_EVENT_NAME="merge_group")

        context = resolve_upload_context.resolve_upload_context(env)

        self.assertFalse(context.should_upload)
        self.assertEqual("warning", context.annotation_level)

    def test_other_events_preserve_existing_ref_upload_behavior(self):
        env = dict(
            self.base_env,
            GITHUB_EVENT_NAME="workflow_dispatch",
            GITHUB_REF="refs/heads/feature",
            GITHUB_REF_NAME="feature",
        )

        context = resolve_upload_context.resolve_upload_context(env)

        self.assertTrue(context.should_upload)
        self.assertEqual("refs/heads/feature", context.ref)

    def test_resolver_uses_process_environment_by_default(self):
        with mock.patch.dict("os.environ", self.base_env, clear=True):
            context = resolve_upload_context.resolve_upload_context()

        self.assertTrue(context.should_upload)
        self.assertEqual("deadbeef", context.commit_oid)

    def test_skipped_upload_writes_outputs_notice_and_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "output"
            summary_path = Path(directory) / "summary"
            env = dict(
                self.base_env,
                GITHUB_REF="refs/heads/feature",
                GITHUB_REF_NAME="feature",
                GITHUB_OUTPUT=str(output_path),
                GITHUB_STEP_SUMMARY=str(summary_path),
            )
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                with mock.patch.object(
                    resolve_upload_context,
                    "_find_open_pull_request",
                    return_value=resolve_upload_context.PullRequestLookup(),
                ):
                    exit_code = resolve_upload_context.main(env)

            self.assertEqual(0, exit_code)
            self.assertIn("should_upload=false", output_path.read_text())
            self.assertIn("::notice::", stdout.getvalue())
            self.assertIn("Code coverage upload skipped", summary_path.read_text())

    def test_lookup_failure_writes_error_and_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "output"
            summary_path = Path(directory) / "summary"
            env = dict(
                self.base_env,
                GITHUB_REF="refs/heads/feature",
                GITHUB_REF_NAME="feature",
                GITHUB_OUTPUT=str(output_path),
                GITHUB_STEP_SUMMARY=str(summary_path),
            )
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                with mock.patch.object(
                    resolve_upload_context,
                    "_find_open_pull_request",
                    return_value=resolve_upload_context.PullRequestLookup(error="HTTP 403"),
                ):
                    exit_code = resolve_upload_context.main(env)

            self.assertEqual(1, exit_code)
            self.assertIn("should_upload=false", output_path.read_text())
            self.assertIn("::error::", stdout.getvalue())
            self.assertIn("Code coverage upload failed", summary_path.read_text())

    def test_allowed_upload_writes_context_outputs_without_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "output"
            summary_path = Path(directory) / "summary"
            env = dict(
                self.base_env,
                GITHUB_OUTPUT=str(output_path),
                GITHUB_STEP_SUMMARY=str(summary_path),
            )

            exit_code = resolve_upload_context.main(env)

            self.assertEqual(0, exit_code)
            output = output_path.read_text()
            self.assertIn("should_upload=true", output)
            self.assertIn("commit_oid=deadbeef", output)
            self.assertFalse(summary_path.exists())

    def test_open_pull_request_lookup_uses_existing_gh_contract(self):
        completed_process = subprocess_result(stdout="42\n")

        with mock.patch.object(
            resolve_upload_context.subprocess,
            "run",
            return_value=completed_process,
        ) as run:
            result = resolve_upload_context._find_open_pull_request(
                dict(
                    self.base_env,
                    GITHUB_REF_NAME="feature",
                    GH_TOKEN="token",
                )
            )

        self.assertEqual("42", result.number)
        self.assertEqual("", result.error)
        self.assertEqual(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                "octo-org/octo-repo",
                "--head",
                "feature",
                "--state",
                "open",
                "--json",
                "number",
                "--jq",
                ".[0].number // empty",
            ],
            run.call_args.args[0],
        )
        self.assertEqual("github.com", run.call_args.kwargs["env"]["GH_HOST"])
        self.assertEqual("token", run.call_args.kwargs["env"]["GH_TOKEN"])

    def test_open_pull_request_lookup_reports_command_failure(self):
        completed_process = subprocess_result(returncode=1, stderr="HTTP 403\n")

        with mock.patch.object(
            resolve_upload_context.subprocess,
            "run",
            return_value=completed_process,
        ):
            result = resolve_upload_context._find_open_pull_request(self.base_env)

        self.assertEqual("", result.number)
        self.assertEqual("HTTP 403", result.error)

    def test_open_pull_request_lookup_reports_missing_gh(self):
        with mock.patch.object(
            resolve_upload_context.subprocess,
            "run",
            side_effect=FileNotFoundError("gh"),
        ):
            result = resolve_upload_context._find_open_pull_request(self.base_env)

        self.assertEqual("", result.number)
        self.assertEqual("unable to run gh: gh", result.error)


def subprocess_result(
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> mock.Mock:
    return mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)


if __name__ == "__main__":
    unittest.main()

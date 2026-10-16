import base64
import gzip
import io
import json
import re
import shutil
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import mkdtemp
from unittest import mock
from unittest.mock import patch
from urllib.error import URLError

import upload_coverage


class FakeResponse:
    def __init__(self, status=201, body=b""):
        self.status = status
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self.body

    def getcode(self):
        return self.status


class FakeTime:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class UploadCoverageTests(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(mkdtemp(dir=Path(__file__).parent))
        self.coverage_file = self.test_dir / "coverage.xml"
        self.coverage_contents = b'<coverage branch-rate="0.5" />\n'
        self.coverage_file.write_bytes(self.coverage_contents)
        self.base_env = {
            "INPUT_FILE": str(self.coverage_file),
            "INPUT_LANGUAGE": "Python",
            "INPUT_LABEL": "code-coverage/test",
            "COMMIT_OID": "deadbeef",
            "REF": "refs/heads/main",
            "PR_NUMBER": "",
            "GITHUB_REPOSITORY": "octo-org/octo-repo",
            "GITHUB_API_URL": "https://api.github.com",
            "GH_TOKEN": "test-token",
            "FAIL_ON_ERROR": "true",
            "WAIT_FOR_PROCESSING_TIMEOUT": "0",
        }

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def successful_opener(self):
        return mock.Mock(
            side_effect=[
                FakeResponse(status=201, body=b'{"id":"b814ebc9-8d00-47b7-b08e-575796cccd03"}'),
                FakeResponse(status=200, body=b'{"processing_status":"succeeded","errors":[]}'),
            ]
        )

    def run_main(self, env=None, opener=None, status_opener=None):
        stdout = io.StringIO()
        opener = opener or self.successful_opener()
        status_opener = status_opener or mock.Mock(return_value=FakeResponse())
        with redirect_stdout(stdout):
            fake_time = FakeTime()
            with (
                patch("upload_coverage.time.monotonic", side_effect=fake_time.monotonic),
                patch("upload_coverage.time.sleep", side_effect=fake_time.sleep),
            ):
                exit_code = upload_coverage.main(
                    environ=env or self.base_env,
                    opener=opener,
                    status_opener=status_opener,
                )
        return exit_code, stdout.getvalue(), opener

    def request_payload(self, opener):
        request = opener.call_args.args[0]
        return json.loads(request.data.decode("utf-8"))

    # --- File validation ---

    def test_file_not_found_exits_with_error_annotation(self):
        env = dict(self.base_env, INPUT_FILE=str(self.test_dir / "missing.xml"))

        exit_code, output, opener = self.run_main(env=env)

        self.assertEqual(1, exit_code)
        self.assertIn("::error::Coverage file not found", output)
        opener.assert_not_called()

    # --- Successful uploads ---

    def test_successful_with_pr_number_uses_pull_request_number(self):
        env = dict(self.base_env, REF="", PR_NUMBER="42")
        exit_code, _, opener = self.run_main(env=env)

        payload = self.request_payload(opener)
        self.assertEqual(0, exit_code)
        self.assertEqual(42, payload["pull_request_number"])
        self.assertNotIn("ref", payload)

    def test_successful_with_ref_uses_ref_and_omits_pull_request_number(self):
        exit_code, _, opener = self.run_main()

        payload = self.request_payload(opener)
        self.assertEqual("refs/heads/main", payload["ref"])
        self.assertNotIn("pull_request_number", payload)

    # --- Waiting for processing ---

    def test_200_without_coverage_id_skips_processing(self):
        env = dict(self.base_env, WAIT_FOR_PROCESSING_TIMEOUT="10")
        opener = mock.Mock(
            return_value=FakeResponse(
                status=200,
                body=b'{"message":"commit is not the latest commit on the branch"}',
            )
        )

        exit_code, output, _ = self.run_main(opener=opener, env=env)

        self.assertEqual(0, exit_code)
        self.assertIn("::warning::Skipped coverage processing", output)
        self.assertIn("commit is not the latest commit on the branch", output)
        self.assertNotIn("Waiting for processing to finish", output)
        opener.assert_called_once()

    def test_200_without_coverage_id_warns_when_waiting_is_disabled(self):
        opener = mock.Mock(return_value=FakeResponse(status=200, body=b"{}"))

        exit_code, output, _ = self.run_main(opener=opener)

        self.assertEqual(0, exit_code)
        self.assertIn("::warning::Skipped coverage processing", output)
        opener.assert_called_once()

    def test_201_without_coverage_id_fails(self):
        opener = mock.Mock(return_value=FakeResponse(status=201, body=b"{}"))

        exit_code, output, _ = self.run_main(opener=opener)

        self.assertEqual(1, exit_code)
        self.assertIn("response did not include an upload id", output)

    def test_waits_for_processing_after_successful_upload(self):
        env = dict(self.base_env, WAIT_FOR_PROCESSING_TIMEOUT="10")
        opener = mock.Mock(
            side_effect=[
                FakeResponse(status=201, body=b'{"id":"b814ebc9-8d00-47b7-b08e-575796cccd03"}'),
                FakeResponse(status=200, body=b'{"processing_status":"pending","errors":[]}'),
                FakeResponse(status=200, body=b'{"processing_status":"succeeded","errors":[]}'),
            ]
        )

        exit_code, output, _ = self.run_main(env=env, opener=opener)

        self.assertRegex(
            output,
            re.compile(
                r"Starting\ coverage\ upload\.\."
                r".*Coverage\ report\ uploaded\ successfully\."
                r".*Waiting\ for\ processing\ to\ finish"
                r".*Coverage\ report\ processing\ status:\ pending\."
                r".*Coverage\ report\ processing\ status:\ succeeded\."
                r".*Coverage\ report\ processing\ finished\ successfully\.",
                re.DOTALL,
            ),
        )
        self.assertEqual(0, exit_code)
        self.assertEqual(3, opener.call_count)

    def test_processing_failure_exits_with_error_by_default(self):
        env = dict(self.base_env, WAIT_FOR_PROCESSING_TIMEOUT="10")
        opener = mock.Mock(
            side_effect=[
                FakeResponse(status=201, body=b'{"id":"b814ebc9-8d00-47b7-b08e-575796cccd03"}'),
                FakeResponse(
                    status=200,
                    body=b'{"processing_status":"failed","errors":["invalid coverage payload"]}',
                ),
            ]
        )

        exit_code, output, _ = self.run_main(opener=opener, env=env)

        self.assertIn("Coverage report processing failed: invalid coverage payload", output)
        self.assertEqual(1, exit_code)

    def test_processing_failure_respects_fail_on_error_false(self):
        env = dict(self.base_env, FAIL_ON_ERROR="false", WAIT_FOR_PROCESSING_TIMEOUT="10")
        opener = mock.Mock(
            side_effect=[
                FakeResponse(status=201, body=b'{"id":"b814ebc9-8d00-47b7-b08e-575796cccd03"}'),
                FakeResponse(
                    status=200,
                    body=b'{"processing_status":"failed","errors":["invalid coverage payload"]}',
                ),
            ]
        )

        exit_code, output, _ = self.run_main(env=env, opener=opener)

        self.assertEqual(0, exit_code)
        self.assertIn("::error::Coverage report processing failed", output)

    def test_waiting_for_processing_can_be_disabled(self):
        env = dict(self.base_env, WAIT_FOR_PROCESSING_TIMEOUT="0")
        opener = mock.Mock(
            return_value=FakeResponse(
                status=201, body=b'{"id":"b814ebc9-8d00-47b7-b08e-575796cccd03"}'
            )
        )

        exit_code, output, _ = self.run_main(env=env, opener=opener)

        self.assertEqual(0, exit_code)
        self.assertNotIn("Waiting for processing to finish", output)
        opener.assert_called_once()

    def test_processing_timeout_exits_with_error(self):
        env = dict(self.base_env, WAIT_FOR_PROCESSING_TIMEOUT="40")
        opener = mock.Mock(
            side_effect=[
                FakeResponse(status=201, body=b'{"id":"b814ebc9-8d00-47b7-b08e-575796cccd03"}'),
                FakeResponse(status=200, body=b'{"processing_status":"pending","errors":[]}'),
                FakeResponse(status=200, body=b'{"processing_status":"pending","errors":[]}'),
                FakeResponse(status=200, body=b'{"processing_status":"pending","errors":[]}'),
                FakeResponse(status=200, body=b'{"processing_status":"pending","errors":[]}'),
            ]
        )

        exit_code, output, _ = self.run_main(env=env, opener=opener)

        self.assertEqual(1, exit_code)
        self.assertIn("Timed out waiting 40 seconds", output)

    def test_invalid_wait_timeout_exits_with_user_error(self):
        env = dict(self.base_env, WAIT_FOR_PROCESSING_TIMEOUT="zero")

        exit_code, output, opener = self.run_main(env=env)

        self.assertEqual(1, exit_code)
        self.assertIn("WAIT_FOR_PROCESSING_TIMEOUT must be a non-negative integer", output)
        opener.assert_not_called()

    # --- HTTP 403 (permissions) ---

    def test_403_not_authorized_exits_with_permissions_error(self):
        opener = mock.Mock(
            return_value=FakeResponse(status=403, body=b'{"message":"not authorized"}')
        )

        exit_code, output, _ = self.run_main(opener=opener)

        self.assertEqual(1, exit_code)
        self.assertIn("code-quality: write", output)
        self.assertIn("HTTP 403", output)

    def test_403_other_message_exits_with_generic_error(self):
        opener = mock.Mock(return_value=FakeResponse(status=403, body=b'{"message":"forbidden"}'))

        exit_code, output, _ = self.run_main(opener=opener)

        self.assertEqual(1, exit_code)
        self.assertIn("Coverage upload failed (HTTP 403)", output)
        self.assertIn("forbidden", output)

    # --- Other error codes ---

    def test_400_bad_request_exits_with_status_and_body(self):
        opener = mock.Mock(return_value=FakeResponse(status=400, body=b'{"message":"bad request"}'))

        exit_code, output, _ = self.run_main(opener=opener)

        self.assertEqual(1, exit_code)
        self.assertIn("HTTP 400", output)
        self.assertIn("bad request", output)

    def test_500_server_error_exits_with_status_and_body(self):
        opener = mock.Mock(return_value=FakeResponse(status=500, body=b'{"message":"boom"}'))

        exit_code, output, _ = self.run_main(opener=opener)

        self.assertEqual(1, exit_code)
        self.assertIn("HTTP 500", output)
        self.assertIn("boom", output)

    # --- Network errors ---

    def test_network_error_exits_with_error(self):
        opener = mock.Mock(side_effect=URLError("connection refused"))

        exit_code, output, _ = self.run_main(opener=opener)

        self.assertEqual(1, exit_code)
        self.assertIn("::error::", output)
        self.assertIn("could not reach the API", output)

    # --- fail-on-error: true (default) ---

    def test_fail_on_error_true_exits_1_on_4xx(self):
        env = dict(self.base_env, FAIL_ON_ERROR="true")
        opener = mock.Mock(return_value=FakeResponse(status=400, body=b'{"message":"nope"}'))

        exit_code, output, _ = self.run_main(env=env, opener=opener)

        self.assertEqual(1, exit_code)
        self.assertIn("fail-on-error", output)

    def test_fail_on_error_true_exits_1_on_5xx(self):
        env = dict(self.base_env, FAIL_ON_ERROR="true")
        opener = mock.Mock(return_value=FakeResponse(status=502, body=b"bad gateway"))

        exit_code, output, _ = self.run_main(env=env, opener=opener)

        self.assertEqual(1, exit_code)

    def test_fail_on_error_true_exits_1_on_network_error(self):
        env = dict(self.base_env, FAIL_ON_ERROR="true")
        opener = mock.Mock(side_effect=URLError("timeout"))

        exit_code, output, _ = self.run_main(env=env, opener=opener)

        self.assertEqual(1, exit_code)

    # --- fail-on-error: false ---

    def test_fail_on_error_false_exits_0_on_4xx(self):
        env = dict(self.base_env, FAIL_ON_ERROR="false")
        opener = mock.Mock(return_value=FakeResponse(status=400, body=b'{"message":"nope"}'))

        exit_code, output, _ = self.run_main(env=env, opener=opener)

        self.assertEqual(0, exit_code)
        self.assertIn("::error::", output)
        self.assertIn("fail-on-error", output)

    def test_fail_on_error_false_exits_0_on_5xx(self):
        env = dict(self.base_env, FAIL_ON_ERROR="false")
        opener = mock.Mock(return_value=FakeResponse(status=500, body=b"error"))

        exit_code, output, _ = self.run_main(env=env, opener=opener)

        self.assertEqual(0, exit_code)
        self.assertIn("::error::", output)

    def test_fail_on_error_false_exits_0_on_network_error(self):
        env = dict(self.base_env, FAIL_ON_ERROR="false")
        opener = mock.Mock(side_effect=URLError("connection refused"))

        exit_code, output, _ = self.run_main(env=env, opener=opener)

        self.assertEqual(0, exit_code)
        self.assertIn("::error::", output)

    def test_fail_on_error_false_exits_0_on_403(self):
        env = dict(self.base_env, FAIL_ON_ERROR="false")
        opener = mock.Mock(
            return_value=FakeResponse(status=403, body=b'{"message":"not authorized"}')
        )

        exit_code, output, _ = self.run_main(env=env, opener=opener)

        self.assertEqual(0, exit_code)
        self.assertIn("::error::", output)
        self.assertIn("code-quality: write", output)

    def test_fail_on_error_false_still_exits_1_on_missing_file(self):
        """File-not-found is a local config error, not an upload failure."""
        env = dict(self.base_env, FAIL_ON_ERROR="false", INPUT_FILE="/nonexistent")
        opener = mock.Mock()

        exit_code, output, _ = self.run_main(env=env, opener=opener)

        self.assertEqual(1, exit_code)
        opener.assert_not_called()

    def test_fail_on_error_false_still_exits_1_on_missing_ref_and_pr(self):
        """Missing ref/PR is a config error, not an upload failure."""
        env = dict(self.base_env, FAIL_ON_ERROR="false", REF="", PR_NUMBER="")
        opener = mock.Mock()

        exit_code, output, _ = self.run_main(env=env, opener=opener)

        self.assertEqual(1, exit_code)
        opener.assert_not_called()

    # --- Diagnostic logging ---

    def test_upload_parameters_logged_in_group(self):
        exit_code, output, _ = self.run_main()

        self.assertIn("::group::Upload parameters", output)
        self.assertIn("commit_oid: deadbeef", output)
        self.assertIn("ref: refs/heads/main", output)
        self.assertIn("pr_number: <not set>", output)
        self.assertIn("language: Python", output)
        self.assertIn("label: code-coverage/test", output)
        self.assertIn("::endgroup::", output)

    def test_upload_parameters_shows_pr_number_when_set(self):
        env = dict(self.base_env, REF="", PR_NUMBER="99")

        exit_code, output, _ = self.run_main(env=env)

        self.assertIn("pr_number: 99", output)
        self.assertIn("ref: <not set>", output)

    # --- Payload structure ---

    def test_payload_structure_and_encoding(self):
        exit_code, _, opener = self.run_main()

        request = opener.call_args_list[0].args[0]
        payload = self.request_payload(opener)
        decoded = gzip.decompress(base64.b64decode(payload["coverage_report"]))

        self.assertEqual(0, exit_code)
        self.assertEqual("PUT", request.get_method())
        self.assertEqual("Bearer test-token", request.headers["Authorization"])
        self.assertEqual("deadbeef", payload["commit_oid"])
        self.assertEqual("Python", payload["language_name"])
        self.assertEqual("code-coverage/test", payload["label"])
        self.assertEqual(self.coverage_contents, decoded)

    def test_neither_ref_nor_pr_number_exits_with_error(self):
        env = dict(self.base_env, REF="", PR_NUMBER="")

        exit_code, output, opener = self.run_main(env=env)

        self.assertEqual(1, exit_code)
        self.assertIn("::error::", output)
        self.assertIn("Either PR_NUMBER or REF must be provided", output)
        opener.assert_not_called()

    # --- Error annotations include fail-on-error hint ---

    def test_error_annotations_include_fail_on_error_hint(self):
        opener = mock.Mock(return_value=FakeResponse(status=500, body=b"oops"))

        exit_code, output, _ = self.run_main(opener=opener)

        self.assertIn("fail-on-error: false", output)

    def test_network_error_annotation_includes_fail_on_error_hint(self):
        opener = mock.Mock(side_effect=URLError("dns failure"))

        exit_code, output, _ = self.run_main(opener=opener)

        self.assertIn("fail-on-error: false", output)

    def test_403_permissions_error_includes_fail_on_error_hint(self):
        opener = mock.Mock(
            return_value=FakeResponse(status=403, body=b'{"message":"not authorized"}')
        )

        exit_code, output, _ = self.run_main(opener=opener)

        self.assertIn("fail-on-error: false", output)

    # --- Only warnings and error annotations include the docs link ---

    def test_successful_upload_omits_docs_url(self):
        exit_code, output, _ = self.run_main()

        self.assertEqual(0, exit_code)
        self.assertNotIn(upload_coverage.DOCS_URL, output)

    def test_errors_responses_includes_docs_url(self):
        opener = mock.Mock(side_effect=URLError("dns failure"))

        exit_code, output, _ = self.run_main(opener=opener)

        self.assertIn(upload_coverage.DOCS_URL, output)

    def test_warning_annotation_includes_docs_url(self):
        env = dict(self.base_env, WAIT_FOR_PROCESSING_TIMEOUT="10")
        opener = mock.Mock(return_value=FakeResponse(status=201, body=b'{"id":"abc"}'))
        exit_code, output, _ = self.run_main(opener=opener, env=env)

        warning_lines = [line for line in output.splitlines() if line.startswith("::warning::")]
        self.assertTrue(warning_lines)
        self.assertIn(upload_coverage.DOCS_URL, warning_lines[0])

    # --- Telemetry integration ---

    def test_telemetry_sends_starting_and_success_reports(self):
        opener = self.successful_opener()
        status_opener = mock.Mock(return_value=FakeResponse())
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            upload_coverage.main(environ=self.base_env, opener=opener, status_opener=status_opener)

        # Two telemetry calls: starting + success
        self.assertEqual(2, status_opener.call_count)
        starting_request = status_opener.call_args_list[0].args[0]
        starting_body = json.loads(starting_request.data)
        self.assertEqual("starting", starting_body["status"])

        completed_request = status_opener.call_args_list[1].args[0]
        completed_body = json.loads(completed_request.data)
        self.assertEqual("success", completed_body["status"])
        self.assertIn("completed_at", completed_body)
        self.assertIn("upload_duration_ms", completed_body)
        self.assertIn("payload_size_bytes", completed_body)

    def test_telemetry_reports_skipped_processing_as_success(self):
        opener = mock.Mock(
            return_value=FakeResponse(
                status=200,
                body=b'{"message":"commit is not the latest commit on the branch"}',
            )
        )
        status_opener = mock.Mock(return_value=FakeResponse())

        exit_code, _, _ = self.run_main(
            env=dict(self.base_env, WAIT_FOR_PROCESSING_TIMEOUT="10"),
            opener=opener,
            status_opener=status_opener,
        )

        self.assertEqual(0, exit_code)
        completed_request = status_opener.call_args_list[1].args[0]
        completed_body = json.loads(completed_request.data)
        self.assertEqual("success", completed_body["status"])
        self.assertNotIn("error_type", completed_body)

    def test_telemetry_sends_failure_report_on_upload_error(self):
        opener = mock.Mock(return_value=FakeResponse(status=500, body=b'{"message":"boom"}'))
        status_opener = mock.Mock(return_value=FakeResponse())
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            upload_coverage.main(environ=self.base_env, opener=opener, status_opener=status_opener)

        completed_request = status_opener.call_args_list[1].args[0]
        completed_body = json.loads(completed_request.data)
        self.assertEqual("failure", completed_body["status"])
        self.assertEqual("http_500", completed_body["error_type"])

    def test_telemetry_sends_user_error_on_4xx_upload_response(self):
        """4xx HTTP responses report user-error even when fail-on-error is false."""
        env = dict(self.base_env, FAIL_ON_ERROR="false")
        opener = mock.Mock(
            return_value=FakeResponse(status=403, body=b'{"message":"Code quality is not enabled"}')
        )
        status_opener = mock.Mock(return_value=FakeResponse())
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            exit_code = upload_coverage.main(
                environ=env, opener=opener, status_opener=status_opener
            )

        self.assertEqual(0, exit_code)
        completed_request = status_opener.call_args_list[1].args[0]
        completed_body = json.loads(completed_request.data)
        self.assertEqual("user-error", completed_body["status"])
        self.assertEqual("http_403", completed_body["error_type"])

    def test_telemetry_sends_user_error_on_missing_file(self):
        env = dict(self.base_env, INPUT_FILE="/nonexistent")
        status_opener = mock.Mock(return_value=FakeResponse())
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            upload_coverage.main(environ=env, opener=mock.Mock(), status_opener=status_opener)

        completed_request = status_opener.call_args_list[1].args[0]
        completed_body = json.loads(completed_request.data)
        self.assertEqual("user-error", completed_body["status"])
        self.assertEqual("file_not_found", completed_body["error_type"])

    def test_telemetry_failure_does_not_affect_action_exit_code(self):
        """Status reporting errors must never cause the action to fail."""
        opener = self.successful_opener()
        status_opener = mock.Mock(side_effect=Exception("telemetry boom"))
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            exit_code = upload_coverage.main(
                environ=self.base_env,
                opener=opener,
                status_opener=status_opener,
            )

        self.assertEqual(0, exit_code)

    @mock.patch("upload_coverage.threading.Thread")
    def test_telemetry_thread_daemon_and_timeout_on_success(self, mock_thread_class):
        mock_thread_instance = mock.Mock()
        mock_thread_class.return_value = mock_thread_instance

        opener = mock.Mock(return_value=FakeResponse())
        status_opener = mock.Mock(return_value=FakeResponse())
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            upload_coverage.main(
                environ=self.base_env, opener=opener, status_opener=status_opener,
            )

        # Verify thread constructor arguments
        mock_thread_class.assert_called_once()
        kwargs = mock_thread_class.call_args.kwargs
        self.assertEqual("telemetry-starting", kwargs.get("name"))
        self.assertTrue(kwargs.get("daemon"))

        # Verify join was called with the configured timeout
        from status_report import STATUS_TIMEOUT_SECONDS
        mock_thread_instance.join.assert_called_once_with(timeout=STATUS_TIMEOUT_SECONDS)

    @mock.patch("upload_coverage.threading.Thread")
    def test_telemetry_thread_timeout_on_missing_file(self, mock_thread_class):
        mock_thread_instance = mock.Mock()
        mock_thread_class.return_value = mock_thread_instance

        env = dict(self.base_env, INPUT_FILE="/nonexistent")
        status_opener = mock.Mock(return_value=FakeResponse())
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            upload_coverage.main(environ=env, opener=mock.Mock(), status_opener=status_opener)

        from status_report import STATUS_TIMEOUT_SECONDS
        mock_thread_instance.join.assert_called_once_with(timeout=STATUS_TIMEOUT_SECONDS)

    @mock.patch("upload_coverage.threading.Thread")
    def test_telemetry_thread_timeout_on_invalid_input(self, mock_thread_class):
        mock_thread_instance = mock.Mock()
        mock_thread_class.return_value = mock_thread_instance

        env = dict(self.base_env, INPUT_LANGUAGE="", REF="", PR_NUMBER="")  # will trigger ValueError in build_payload
        status_opener = mock.Mock(return_value=FakeResponse())
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            upload_coverage.main(environ=env, opener=mock.Mock(), status_opener=status_opener)

        from status_report import STATUS_TIMEOUT_SECONDS
        mock_thread_instance.join.assert_called_once_with(timeout=STATUS_TIMEOUT_SECONDS)


if __name__ == "__main__":
    unittest.main()

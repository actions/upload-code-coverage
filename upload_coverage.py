#!/usr/bin/env python3
import base64
import gzip
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Mapping, Optional, Tuple

import status_report


PERMISSIONS_ERROR = (
    "Coverage upload returned HTTP {status}. Ensure the calling job has "
    "'code-quality: write' permission. See https://github.com/actions/upload-code-coverage#permissions"
)

FAIL_ON_ERROR_HINT = (
    "To treat upload errors as warnings, add 'fail-on-error: false' to the action inputs."
)

STATUS_CHECK_INITIAL_BACKOFF_SECONDS = 5
STATUS_CHECK_BACKOFF_MULTIPLIER = 2
DEFAULT_WAIT_FOR_PROCESSING_TIMEOUT_SECONDS = 155


def emit_annotation(level: str, message: str) -> None:
    print(f"::{level}::{message}")


def log_upload_parameters(
    *,
    commit_oid: str,
    ref: str,
    pr_number: str,
    language: str,
    label: str,
    file_path: str,
) -> None:
    file_size = Path(file_path).stat().st_size
    print("::group::Upload parameters")
    print(f"  commit_oid: {commit_oid}")
    print(f"  ref: {ref or '<not set>'}")
    print(f"  pr_number: {pr_number or '<not set>'}")
    print(f"  language: {language}")
    print(f"  label: {label}")
    print(f"  file: {file_path} ({file_size} bytes)")
    print("::endgroup::")


def _extract_message(body: str) -> str:
    """Extract the human-readable message from an API JSON response.

    Falls back to the raw body if parsing fails or no message field exists.
    We intentionally strip documentation_url and other fields because the
    docs URL currently 404s (pre-GA).
    TODO(GA): Once docs are live, consider including documentation_url in output.
    """
    try:
        data = json.loads(body)
        message = data.get("message", "")
        if message:
            return message
    except (json.JSONDecodeError, AttributeError, TypeError):
        pass
    return body


def _load_json_object(body: str) -> dict:
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _parse_positive_int(raw_value: str, *, env_name: str) -> int:
    try:
        value = int(raw_value)
    except ValueError as error:
        raise ValueError(f"{env_name} must be a positive integer") from error
    if value <= 0:
        raise ValueError(f"{env_name} must be a positive integer")
    return value


def encode_coverage_report(file_path: str) -> str:
    data = Path(file_path).read_bytes()
    return base64.b64encode(gzip.compress(data)).decode("ascii")


def build_payload(
    *,
    file_path: str,
    language: str,
    label: str,
    commit_oid: str,
    ref: str = "",
    pr_number: str = "",
) -> dict:
    payload = {
        "commit_oid": commit_oid,
        "coverage_report": encode_coverage_report(file_path),
        "language_name": language,
        "label": label,
    }

    if pr_number:
        payload["pull_request_number"] = int(pr_number)
    elif ref:
        payload["ref"] = ref
    else:
        raise ValueError("Either PR_NUMBER or REF must be provided")

    return payload


def upload_report(
    *,
    payload: dict,
    repository: str,
    api_url: str,
    token: str,
    opener=urllib.request.urlopen,
) -> Tuple[int, str]:
    """Upload the coverage report. Returns (status_code, response_body)."""
    request = urllib.request.Request(
        url=f"{api_url.rstrip('/')}/repos/{repository}/code-coverage/report",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        },
        method="PUT",
    )

    try:
        with opener(request) as response:
            body = response.read().decode("utf-8", errors="replace")
            return response.getcode(), body
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        return error.code, body
    except urllib.error.URLError as error:
        return 0, str(error.reason)


def fetch_upload_status(
    *,
    coverage_report_id: str,
    repository: str,
    api_url: str,
    token: str,
    opener=urllib.request.urlopen,
) -> Tuple[int, str]:
    request = urllib.request.Request(
        url=f"{api_url.rstrip('/')}/repos/{repository}/code-coverage/reports/{coverage_report_id}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
        method="GET",
    )

    try:
        with opener(request) as response:
            body = response.read().decode("utf-8", errors="replace")
            return response.getcode(), body
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        return error.code, body
    except urllib.error.URLError as error:
        return 0, str(error.reason)


def wait_for_processing(
    *,
    coverage_report_id: str,
    repository: str,
    api_url: str,
    token: str,
    timeout_seconds: int,
    opener=urllib.request.urlopen,
    sleep=time.sleep,
    monotonic=time.monotonic,
) -> Tuple[bool, Optional[str], Optional[str]]:
    print("::group::Waiting for processing to finish")
    try:
        deadline = monotonic() + timeout_seconds
        status_check_backoff = STATUS_CHECK_INITIAL_BACKOFF_SECONDS

        while True:
            remaining = deadline - monotonic()
            if remaining <= 0:
                return (
                    False,
                    "processing_timeout",
                    f"Timed out waiting {timeout_seconds} seconds for coverage report processing to finish",
                )

            sleep(min(status_check_backoff, remaining))

            status_code, body = fetch_upload_status(
                coverage_report_id=coverage_report_id,
                repository=repository,
                api_url=api_url,
                token=token,
                opener=opener,
            )

            if status_code == 200:
                data = _load_json_object(body)
                processing_status = data.get("processing_status", "")
                print(f"Coverage upload processing status: {processing_status or '<missing>'}.")

                if processing_status in ("pending", "processing"):
                    status_check_backoff *= STATUS_CHECK_BACKOFF_MULTIPLIER
                    continue

                if processing_status == "succeeded":
                    print("Coverage report processing finished successfully.")
                    return True, None, None

                if processing_status == "failed":
                    errors = data.get("errors")
                    message = "Coverage report processing failed"
                    if isinstance(errors, list) and errors:
                        message = f"{message}: {'; '.join(str(error) for error in errors)}"
                    return False, "processing_failed", message

                emit_annotation(
                    "warning",
                    "Coverage upload status response did not include a valid processing_status. Retrying until timeout.",
                )
                status_check_backoff *= STATUS_CHECK_BACKOFF_MULTIPLIER
                continue

            if status_code and 400 <= status_code < 500:
                return (
                    False,
                    f"status_check_http_{status_code}",
                    f"Checking coverage upload status failed (HTTP {status_code}): {_extract_message(body)}",
                )

            if status_code == 0:
                emit_annotation(
                    "warning",
                    "Checking coverage upload status failed: could not reach the API. Retrying until timeout.",
                )
            else:
                emit_annotation(
                    "warning",
                    f"Checking coverage upload status returned HTTP {status_code}. Retrying until timeout.",
                )
            status_check_backoff *= STATUS_CHECK_BACKOFF_MULTIPLIER
    finally:
        print("::endgroup::")


def handle_response(status: int, body: str, fail_on_error: bool) -> int:
    """Process the upload response. Returns the process exit code."""
    if status == 0:
        # Network error (could not reach the API)
        emit_annotation("error", f"Coverage upload failed: could not reach the API. {FAIL_ON_ERROR_HINT}")
        return 1 if fail_on_error else 0

    if status == 201:
        print("Coverage report uploaded successfully.")
        return 0

    if status == 200:
        # API accepted but did not store (e.g. commit not latest on branch)
        try:
            message = json.loads(body).get("message", "")
        except (json.JSONDecodeError, AttributeError):
            message = ""
        if message:
            emit_annotation("warning", f"Coverage upload returned HTTP 200 (report not stored): {message}")
        else:
            emit_annotation("warning", "Coverage upload returned HTTP 200 but expected 201. The report may not have been stored.")
        return 0

    if status >= 400:
        if status == 403 and "not authorized" in body.lower():
            emit_annotation("error", f"{PERMISSIONS_ERROR.format(status=status)}. {FAIL_ON_ERROR_HINT}")
        else:
            display_body = _extract_message(body)
            emit_annotation("error", f"Coverage upload failed (HTTP {status}): {display_body}. {FAIL_ON_ERROR_HINT}")
        return 1 if fail_on_error else 0

    # Unexpected status code
    emit_annotation("notice", f"Coverage upload returned unexpected HTTP {status}: {body}")
    return 0


def main(
    environ: Optional[Mapping[str, str]] = None,
    opener=urllib.request.urlopen,
    status_opener=urllib.request.urlopen,
    sleep=time.sleep,
    monotonic=time.monotonic,
) -> int:
    env = dict(os.environ if environ is None else environ)

    repository = env.get("GITHUB_REPOSITORY", "")
    api_url = env.get("GITHUB_API_URL", "https://api.github.com")
    token = env.get("GH_TOKEN", "")

    # Send "starting" telemetry report
    starting_report = status_report.build_starting_report(env)
    status_report.save_state("started_at", starting_report.get("started_at", ""))
    status_report.save_state("starting_report", json.dumps(starting_report))
    status_report.send_status_report(
        starting_report,
        repository=repository,
        api_url=api_url,
        token=token,
        opener=status_opener,
    )

    upload_start = time.monotonic()

    file_path = env.get("INPUT_FILE", "")
    if not file_path or not Path(file_path).is_file():
        emit_annotation("error", f"Coverage file not found: {file_path}")
        _send_completed_report(
            starting_report, "user-error",
            error_type="file_not_found", error_message=f"Coverage file not found: {file_path}",
            repository=repository, api_url=api_url, token=token, opener=status_opener,
        )
        return 1

    fail_on_error = env.get("FAIL_ON_ERROR", "true").lower() != "false"
    wait_for_processing_enabled = env.get("WAIT_FOR_PROCESSING", "true").lower() != "false"

    commit_oid = env.get("COMMIT_OID", "")
    ref = env.get("REF", "")
    pr_number = env.get("PR_NUMBER", "")
    language = env.get("INPUT_LANGUAGE", "")
    label = env.get("INPUT_LABEL", "")

    try:
        wait_for_processing_timeout = _parse_positive_int(
            env.get(
                "WAIT_FOR_PROCESSING_TIMEOUT",
                str(DEFAULT_WAIT_FOR_PROCESSING_TIMEOUT_SECONDS),
            ),
            env_name="WAIT_FOR_PROCESSING_TIMEOUT",
        )
    except ValueError as error:
        emit_annotation("error", str(error))
        _send_completed_report(
            starting_report, "user-error",
            error_type="invalid_input", error_message=str(error),
            repository=repository, api_url=api_url, token=token, opener=status_opener,
        )
        return 1

    log_upload_parameters(
        commit_oid=commit_oid,
        ref=ref,
        pr_number=pr_number,
        language=language,
        label=label,
        file_path=file_path,
    )

    try:
        payload = build_payload(
            file_path=file_path,
            language=language,
            label=label,
            commit_oid=commit_oid,
            ref=ref,
            pr_number=pr_number,
        )
    except ValueError as error:
        emit_annotation("error", str(error))
        _send_completed_report(
            starting_report, "user-error",
            error_type="invalid_input", error_message=str(error),
            repository=repository, api_url=api_url, token=token, opener=status_opener,
        )
        return 1

    payload_size_bytes = len(json.dumps(payload).encode("utf-8"))

    http_status, body = upload_report(
        payload=payload,
        repository=repository,
        api_url=api_url,
        token=token,
        opener=opener,
    )

    upload_duration_ms = int((time.monotonic() - upload_start) * 1000)
    exit_code = handle_response(http_status, body, fail_on_error)
    telemetry_status = None
    error_type = None
    error_message = None

    if http_status == 201 and wait_for_processing_enabled:
        coverage_report_id = _load_json_object(body).get("id", "")
        if not coverage_report_id:
            error_type = "missing_report_id"
            error_message = "Coverage upload succeeded but the response did not include an upload id"
            emit_annotation("error", f"{error_message}. {FAIL_ON_ERROR_HINT}")
            exit_code = 1 if fail_on_error else 0
            telemetry_status = "failure"
        else:
            processed, processing_error_type, processing_error_message = wait_for_processing(
                coverage_report_id=str(coverage_report_id),
                repository=repository,
                api_url=api_url,
                token=token,
                timeout_seconds=wait_for_processing_timeout,
                opener=opener,
                sleep=sleep,
                monotonic=monotonic,
            )
            if processed:
                telemetry_status = "success"
            else:
                error_type = processing_error_type
                error_message = processing_error_message
                emit_annotation("error", f"{processing_error_message}. {FAIL_ON_ERROR_HINT}")
                exit_code = 1 if fail_on_error else 0
                telemetry_status = (
                    "user-error" if processing_error_type == "processing_failed" else "failure"
                )

    # Derive telemetry status from http_status (not exit_code) so that
    # fail-on-error:false still reports failures/user-errors to Datadog.
    if telemetry_status is None and http_status and 200 <= http_status < 300:
        telemetry_status = "success"
    elif telemetry_status is None and http_status and 400 <= http_status < 500:
        telemetry_status = "user-error"
        error_type = f"http_{http_status}"
        error_message = _extract_message(body)
    elif telemetry_status is None:
        telemetry_status = "failure"
        error_type = f"http_{http_status}" if http_status else "network_error"
        error_message = _extract_message(body)

    _send_completed_report(
        starting_report, telemetry_status,
        upload_duration_ms=upload_duration_ms,
        payload_size_bytes=payload_size_bytes,
        error_type=error_type, error_message=error_message,
        repository=repository, api_url=api_url, token=token, opener=status_opener,
    )

    return exit_code


def _send_completed_report(
    starting_report: dict,
    telemetry_status: str,
    *,
    repository: str,
    api_url: str,
    token: str,
    upload_duration_ms: Optional[int] = None,
    payload_size_bytes: Optional[int] = None,
    error_type: Optional[str] = None,
    error_message: Optional[str] = None,
    opener=urllib.request.urlopen,
) -> None:
    """Build and send a completed status report, then mark state as sent."""
    completed = status_report.build_completed_report(
        starting_report,
        status=telemetry_status,
        upload_duration_ms=upload_duration_ms,
        payload_size_bytes=payload_size_bytes,
        error_type=error_type,
        error_message=error_message,
    )
    status_report.save_state("completed_report", json.dumps(completed))
    sent = status_report.send_status_report(
        completed,
        repository=repository,
        api_url=api_url,
        token=token,
        opener=opener,
    )
    if sent:
        status_report.save_state("status_sent", "true")


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
import base64
import gzip
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Optional

import status_report
from categorised_error import CategorisedError

PERMISSIONS_ERROR = (
    "Coverage upload returned HTTP {status}. Ensure the calling job has "
    "'code-quality: write' permission. See https://github.com/actions/upload-code-coverage#permissions"
)

FAIL_ON_ERROR_HINT = (
    "To treat upload errors as warnings, add 'fail-on-error: false' to the action inputs."
)

DOCS_URL = (
    "https://docs.github.com/en/code-security/how-tos/maintain-quality-code/set-up-code-coverage"
)

STATUS_CHECK_INITIAL_BACKOFF_SECONDS = 5
STATUS_CHECK_BACKOFF_MULTIPLIER = 2
STATUS_CHECK_MAX_BACKOFF_SECONDS = 60


def emit_annotation(level: str, message: str) -> None:
    if level in {"error", "warning"}:
        trimmed = message.rstrip()
        sep = "" if trimmed.endswith((".", "!", "?")) else "."
        message = f"{trimmed}{sep} See {DOCS_URL} for more information."
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
    """
    data = _load_json_object(body)
    message = data.get("message", "")
    if message:
        return message
    return body


def _parse_coverage_report_id(data: dict) -> str:
    coverage_report_id = data.get("id")
    if not coverage_report_id:
        error_message = "Coverage upload succeeded but the response did not include an upload id"
        raise CategorisedError(error_message, "missing_upload_id")
    return coverage_report_id


def _load_json_object(body: str) -> dict:
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _parse_wait_for_processing_timeout(raw_value: str) -> int:
    error_message = "WAIT_FOR_PROCESSING_TIMEOUT must be a non-negative integer"
    try:
        value = int(raw_value)
    except (ValueError, TypeError) as error:
        raise ValueError(error_message) from error
    if value < 0:
        raise ValueError(error_message)
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
) -> tuple[int, str]:
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
) -> tuple[int, str]:
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


def _handle_processing_status_response(body: str) -> tuple[bool, Optional[str]]:
    """
    Handle the processing status response from the coverage upload API.

    Returns a tuple (completed, error_message), where completed is a boolean
    indicating whether processing has finished, and error_message is an optional
    string containing an error message if processing failed.
    """
    data = _load_json_object(body)
    processing_status = data.get("processing_status", "<missing>")
    print(f"Coverage report processing status: {processing_status}.")

    if processing_status in ("pending", "processing"):
        return False, None
    if processing_status == "succeeded":
        print("Coverage report processing finished successfully.")
        return True, None
    if processing_status == "failed":
        errors = data.get("errors")
        message = "Coverage report processing failed"
        if isinstance(errors, list) and errors:
            message = f"{message}: {'; '.join(str(error) for error in errors)}"
        return True, message

    emit_annotation(
        "warning",
        "Coverage report processing status response did not include a valid "
        "processing_status. Retrying until timeout.",
    )
    return False, None


def _check_processing_status(
    *,
    coverage_report_id: str,
    repository: str,
    api_url: str,
    token: str,
    opener=urllib.request.urlopen,
) -> tuple[bool, Optional[str]]:
    status_code, body = fetch_upload_status(
        coverage_report_id=coverage_report_id,
        repository=repository,
        api_url=api_url,
        token=token,
        opener=opener,
    )

    if 200 <= status_code < 300:
        return _handle_processing_status_response(body)
    if status_code and 400 <= status_code < 500:
        raise CategorisedError(
            "Checking coverage report processing status failed "
            f"(HTTP {status_code}): {_extract_message(body)}",
            f"status_check_http_{status_code}",
        )

    emit_annotation(
        "warning",
        "Checking coverage report processing status failed with HTTP status code "
        f"'{status_code}'. Retrying until timeout.",
    )
    return False, None


def wait_for_processing(
    *,
    coverage_report_id: str,
    repository: str,
    api_url: str,
    token: str,
    timeout_seconds: int,
    opener=urllib.request.urlopen,
) -> Optional[str]:
    """
    Wait for the coverage report processing to finish, up to a timeout.
    Returns None if processing succeeded, or a string error message if it failed.
    """
    print("::group::Waiting for processing to finish")
    try:
        deadline = time.monotonic() + timeout_seconds
        status_check_backoff = STATUS_CHECK_INITIAL_BACKOFF_SECONDS

        while (remaining := deadline - time.monotonic()) > 0:
            # Also sleep initially since processing is guaranteed to take at least a few seconds
            sleep_time = min(status_check_backoff, remaining)
            print(f"Sleeping for {sleep_time} seconds before checking processing status...")
            time.sleep(sleep_time)

            completed, error_message = _check_processing_status(
                coverage_report_id=coverage_report_id,
                repository=repository,
                api_url=api_url,
                token=token,
                opener=opener,
            )
            if completed:
                return error_message
            status_check_backoff = min(
                status_check_backoff * STATUS_CHECK_BACKOFF_MULTIPLIER,
                STATUS_CHECK_MAX_BACKOFF_SECONDS,
            )
        raise CategorisedError(
            f"Timed out waiting {timeout_seconds} seconds for coverage report processing to finish",
            "processing_timeout",
        )
    finally:
        print("::endgroup::")


def handle_response(status: int, body: str) -> Optional[str]:
    """Process the upload response.

    Returns the upload ID, or None when the API intentionally skips processing.
    Raises CategorisedError for failures and malformed upload responses.
    """
    if status == 0:
        raise CategorisedError("could not reach the API", "network_error")
    elif 200 <= status < 300:
        data = _load_json_object(body)
        coverage_report_id = data.get("id")
        if status == 200 and not coverage_report_id:
            raw_message = data.get("message")
            message = raw_message if isinstance(raw_message, str) else ""
            warning = "Skipped coverage processing"
            if message:
                warning = f"{warning}: {message}"
            emit_annotation("warning", warning)
            return None

        coverage_report_id = _parse_coverage_report_id(data)
        print("Coverage report uploaded successfully.")
        return coverage_report_id
    elif status == 403 and "not authorized" in body.lower():
        raise CategorisedError(PERMISSIONS_ERROR.format(status=status), "permissions_error")
    else:
        display_body = _extract_message(body)
        raise CategorisedError(
            f"Coverage upload failed (HTTP {status}): {display_body}", f"http_{status}"
        )


def main(
    environ: Optional[Mapping[str, str]] = None,
    opener=urllib.request.urlopen,
    status_opener=urllib.request.urlopen,
) -> int:
    env = dict(os.environ if environ is None else environ)

    repository = env.get("GITHUB_REPOSITORY", "")
    api_url = env.get("GITHUB_API_URL", "https://api.github.com")
    token = env.get("GH_TOKEN", "")

    # Send "starting" telemetry report in a background thread
    starting_report = status_report.build_starting_report(env)
    status_report.save_state("started_at", starting_report.get("started_at", ""))
    status_report.save_state("starting_report", json.dumps(starting_report))
    
    telemetry_thread = threading.Thread(
        target=status_report.send_status_report,
        args=(starting_report,),
        kwargs={
            "repository": repository,
            "api_url": api_url,
            "token": token,
            "opener": status_opener,
        },
        name="telemetry-starting",
        daemon=True,
    )
    telemetry_thread.start()

    upload_start = time.monotonic()

    file_path = env.get("INPUT_FILE", "")
    if not file_path or not Path(file_path).is_file():
        emit_annotation("error", f"Coverage file not found: {file_path}")
        telemetry_thread.join(timeout=status_report.STATUS_TIMEOUT_SECONDS)
        _send_completed_report(
            starting_report,
            "user-error",
            error_type="file_not_found",
            error_message=f"Coverage file not found: {file_path}",
            repository=repository,
            api_url=api_url,
            token=token,
            opener=status_opener,
        )
        return 1

    fail_on_error = env.get("FAIL_ON_ERROR", "true").lower() != "false"

    commit_oid = env.get("COMMIT_OID", "")
    ref = env.get("REF", "")
    pr_number = env.get("PR_NUMBER", "")
    language = env.get("INPUT_LANGUAGE", "")
    label = env.get("INPUT_LABEL", "")

    try:
        wait_for_processing_timeout = _parse_wait_for_processing_timeout(
            env.get("WAIT_FOR_PROCESSING_TIMEOUT")
        )
    except ValueError as error:
        emit_annotation("error", str(error))
        _send_completed_report(
            starting_report,
            "user-error",
            error_type="invalid_input",
            error_message=str(error),
            repository=repository,
            api_url=api_url,
            token=token,
            opener=status_opener,
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
        telemetry_thread.join(timeout=status_report.STATUS_TIMEOUT_SECONDS)
        _send_completed_report(
            starting_report,
            "user-error",
            error_type="invalid_input",
            error_message=str(error),
            repository=repository,
            api_url=api_url,
            token=token,
            opener=status_opener,
        )
        return 1

    payload_size_bytes = len(json.dumps(payload).encode("utf-8"))

    print("Starting coverage upload..")
    http_status, body = upload_report(
        payload=payload,
        repository=repository,
        api_url=api_url,
        token=token,
        opener=opener,
    )

    upload_duration_ms = int((time.monotonic() - upload_start) * 1000)
    try:
        coverage_id = handle_response(http_status, body)
    except CategorisedError as error:
        emit_annotation("error", f"Coverage upload failed: {error}. {FAIL_ON_ERROR_HINT}")
        telemetry_status = "user-error" if 400 <= http_status < 500 else "failure"
        _send_completed_report(
            starting_report,
            telemetry_status,
            error_type=error.type,
            error_message=str(error),
            repository=repository,
            api_url=api_url,
            token=token,
            opener=status_opener,
        )
        return 1 if fail_on_error else 0

    if coverage_id is not None and wait_for_processing_timeout > 0:
        try:
            error_msg = wait_for_processing(
                coverage_report_id=coverage_id,
                timeout_seconds=wait_for_processing_timeout,
                repository=repository,
                api_url=api_url,
                token=token,
                opener=opener,
            )
            if error_msg:
                emit_annotation("error", f"{error_msg}. {FAIL_ON_ERROR_HINT}")
                _send_completed_report(
                    starting_report,
                    "failure",
                    error_type="processing_failed",
                    error_message=error_msg,
                    repository=repository,
                    api_url=api_url,
                    token=token,
                    opener=status_opener,
                )
                return 1 if fail_on_error else 0
        except CategorisedError as error:
            emit_annotation(
                "error",
                f"Waiting for coverage report processing failed: {error}. {FAIL_ON_ERROR_HINT}",
            )
            _send_completed_report(
                starting_report,
                "failure",
                error_type=error.type,
                error_message=str(error),
                repository=repository,
                api_url=api_url,
                token=token,
                opener=status_opener,
            )
            return 1 if fail_on_error else 0

    telemetry_thread.join(timeout=status_report.STATUS_TIMEOUT_SECONDS)
    _send_completed_report(
        starting_report,
        "success",
        upload_duration_ms=upload_duration_ms,
        payload_size_bytes=payload_size_bytes,
        repository=repository,
        api_url=api_url,
        token=token,
        opener=status_opener,
    )

    return 0


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

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
            starting_report, "user-error",
            error_type="file_not_found", error_message=f"Coverage file not found: {file_path}",
            repository=repository, api_url=api_url, token=token, opener=status_opener,
        )
        return 1

    fail_on_error = env.get("FAIL_ON_ERROR", "true").lower() != "false"

    commit_oid = env.get("COMMIT_OID", "")
    ref = env.get("REF", "")
    pr_number = env.get("PR_NUMBER", "")
    language = env.get("INPUT_LANGUAGE", "")
    label = env.get("INPUT_LABEL", "")

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

    # Derive telemetry status from http_status (not exit_code) so that
    # fail-on-error:false still reports failures/user-errors to Datadog.
    if http_status and 200 <= http_status < 300:
        telemetry_status = "success"
        error_type = None
        error_message = None
    elif http_status and 400 <= http_status < 500:
        telemetry_status = "user-error"
        error_type = f"http_{http_status}"
        error_message = _extract_message(body)
    else:
        telemetry_status = "failure"
        error_type = f"http_{http_status}" if http_status else "network_error"
        error_message = _extract_message(body)

    telemetry_thread.join(timeout=status_report.STATUS_TIMEOUT_SECONDS)
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

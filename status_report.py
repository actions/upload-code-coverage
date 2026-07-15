#!/usr/bin/env python3
"""Telemetry status reporting for the upload-code-coverage Action.

Reports action status to the monolith endpoint so we can track usage,
errors, and performance. All public functions are safe to call in any
context — they swallow exceptions and log warnings so telemetry never
causes the action to fail.
"""
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, Optional


STATUS_ENDPOINT = "/repos/{repository}/code-coverage/action/status"
STATUS_TIMEOUT_SECONDS = 30


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_int(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def _emit_warning(message: str) -> None:
    print(f"::warning::{message}")


def _gather_runner_info(env: Dict[str, str]) -> Dict[str, Any]:
    """Collect runner environment details."""
    info: Dict[str, Any] = {}
    for key, field in [
        ("RUNNER_OS", "runner_os"),
        ("RUNNER_ARCH", "runner_arch"),
        ("ImageVersion", "runner_image_version"),
    ]:
        value = env.get(key)
        if value:
            info[field] = value
    return info


def _gather_workflow_context(env: Dict[str, str]) -> Dict[str, Any]:
    """Collect workflow/job context."""
    ctx: Dict[str, Any] = {}
    for key, field in [
        ("GITHUB_WORKFLOW", "workflow_name"),
        ("GITHUB_JOB", "job_name"),
        ("GITHUB_EVENT_NAME", "actions_event_name"),
        ("GITHUB_RUN_ID", "workflow_run_id"),
        ("GITHUB_RUN_ATTEMPT", "workflow_run_attempt"),
    ]:
        value = env.get(key)
        if value:
            ctx[field] = _safe_int(value) if field in ("workflow_run_id", "workflow_run_attempt") else value

    # job_run_uuid: unique identifier for this job run
    job_run_uuid = env.get("JOB_RUN_UUID", "")
    if job_run_uuid:
        ctx["job_run_uuid"] = job_run_uuid

    return ctx


def _gather_action_metadata(env: Dict[str, str]) -> Dict[str, Any]:
    """Collect action identity metadata."""
    meta: Dict[str, Any] = {
        "action_name": "upload-code-coverage",
        "action_version": env.get("ACTION_VERSION", "unknown"),
    }
    action_ref = env.get("GITHUB_ACTION_REF", "")
    if action_ref:
        meta["action_ref"] = action_ref

    return meta


def build_starting_report(env: Dict[str, str]) -> Dict[str, Any]:
    """Build the initial "starting" status report.

    This captures input parameters, runner info, and workflow context
    at the start of execution.
    """
    report: Dict[str, Any] = {
        "status": "starting",
        "started_at": _now_iso(),
    }

    report.update(_gather_action_metadata(env))
    report.update(_gather_runner_info(env))
    report.update(_gather_workflow_context(env))

    # Git context (commit_oid is required by the endpoint)
    report["commit_oid"] = env.get("COMMIT_OID", "")
    ref = env.get("REF", "")
    if ref:
        report["ref"] = ref

    # User-supplied parameters (top-level fields to match endpoint schema)
    if env.get("INPUT_LANGUAGE"):
        report["language_name"] = env["INPUT_LANGUAGE"]
    if env.get("INPUT_LABEL"):
        report["category"] = env["INPUT_LABEL"]

    return report


def build_completed_report(
    starting_report: Dict[str, Any],
    *,
    status: str,
    upload_duration_ms: Optional[int] = None,
    payload_size_bytes: Optional[int] = None,
    error_type: Optional[str] = None,
    error_message: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the completion status report from a starting report.

    Copies fields from the starting report and adds completion-specific
    data like timing, outcome, and error information.
    """
    report = dict(starting_report)
    report["status"] = status
    report["completed_at"] = _now_iso()

    if upload_duration_ms is not None:
        report["upload_duration_ms"] = upload_duration_ms
    if payload_size_bytes is not None:
        report["payload_size_bytes"] = payload_size_bytes
    if error_type:
        report["error_type"] = error_type
    if error_message:
        report["error_message"] = error_message[:1000]

    return report


def send_status_report(
    report: Dict[str, Any],
    *,
    repository: str,
    api_url: str,
    token: str,
    opener=urllib.request.urlopen,
) -> bool:
    """Send a status report to the telemetry endpoint.

    Returns True if the report was sent successfully, False otherwise.
    Never raises — all exceptions are caught and logged as warnings.
    """
    try:
        url = f"{api_url.rstrip('/')}{STATUS_ENDPOINT.format(repository=repository)}"
        data = json.dumps(report).encode("utf-8")

        request = urllib.request.Request(
            url=url,
            data=data,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
            },
            method="PUT",
        )

        with opener(request, timeout=STATUS_TIMEOUT_SECONDS) as response:
            response.read()
            code = getattr(response, "getcode", lambda: None)()
            if code is not None and not (200 <= int(code) < 300):
                _emit_warning(f"Status report request returned HTTP {code}")
                return False
            return True
    except Exception as exc:
        _emit_warning(f"Failed to send status report: {exc}")
        return False


def save_state(key: str, value: str) -> None:
    """Save state for the post step via $GITHUB_ENV.

    In composite actions, state must be passed between steps using
    environment variables (not $GITHUB_STATE which is for JS/Docker
    action pre/post hooks).
    """
    env_file = os.environ.get("GITHUB_ENV", "")
    if env_file:
        # Prefix keys to avoid collisions with user env vars
        env_key = f"_COVERAGE_TELEMETRY_{key.upper()}"
        try:
            with open(env_file, "a") as f:
                f.write(f"{env_key}<<EOF\n{value}\nEOF\n")
        except OSError as exc:
            _emit_warning(f"Failed to save state '{key}': {exc}")


def get_state(key: str) -> str:
    """Read state saved by the main step.

    Reads from environment variables set via $GITHUB_ENV in the main step.
    """
    env_key = f"_COVERAGE_TELEMETRY_{key.upper()}"
    return os.environ.get(env_key, "")

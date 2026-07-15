#!/usr/bin/env python3
"""Post step for upload-code-coverage Action telemetry.

Runs after the main step (even on cancellation/failure) to ensure we
always report a final status. If the main step already sent a completion
report, this is a no-op.
"""
import json
import os
import sys

import status_report


def main() -> int:
    if status_report.get_state("status_sent") == "true":
        return 0

    env = os.environ
    repository = env.get("GITHUB_REPOSITORY", "")
    api_url = env.get("GITHUB_API_URL", "https://api.github.com")
    token = env.get("GH_TOKEN", "")

    # Try to retry the completed report if it was built but failed to send
    completed_report_json = status_report.get_state("completed_report")
    if completed_report_json:
        try:
            report = json.loads(completed_report_json)
            status_report.send_status_report(
                report,
                repository=repository,
                api_url=api_url,
                token=token,
            )
            return 0
        except (json.JSONDecodeError, TypeError):
            pass

    # Otherwise build an "aborted" report from the saved starting report
    starting_report_json = status_report.get_state("starting_report")
    if starting_report_json:
        try:
            starting_report = json.loads(starting_report_json)
        except (json.JSONDecodeError, TypeError):
            starting_report = status_report.build_starting_report(env)
    else:
        starting_report = status_report.build_starting_report(env)

    report = status_report.build_completed_report(starting_report, status="aborted")

    status_report.send_status_report(
        report,
        repository=repository,
        api_url=api_url,
        token=token,
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())

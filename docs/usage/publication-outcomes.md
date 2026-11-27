# Coverage publication outcomes

Give the action step an `id` to inspect its `outcome`, `report-id`, and
`skip-reason` outputs in subsequent steps.

| Outcome | Meaning |
| --- | --- |
| `skipped` | The event is ineligible, or the API declined to process this commit. `skip-reason` contains the explanation. |
| `uploaded` | The API accepted a report and returned its ID. Processing was not awaited because `wait-for-processing-timeout` was zero. |
| `processed` | The API accepted the report and confirmed successful processing. |
| `failed` | Input validation, upload, or processing failed. An accepted report keeps its `report-id` when processing later fails. |

`fail-on-error` controls the exit code independently of these outputs. A successful
step with `fail-on-error: false` can still have `outcome: failed`. To require
completed coverage processing, check for `outcome: processed` in an `always()`
completion step. An empty output, for example after runner termination, does not
establish successful publication.

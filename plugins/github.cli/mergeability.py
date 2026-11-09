"""PR mergeability decisions from check_pr_mergeability.py, without workflow I/O."""

from __future__ import annotations

PR_VIEW_FIELDS = [
    "number",
    "title",
    "state",
    "url",
    "headRefName",
    "baseRefName",
    "isDraft",
    "reviewDecision",
    "mergedAt",
    "mergeStateStatus",
    "mergeable",
    "statusCheckRollup",
]
MAX_PR_VIEW_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 0.5
TRANSIENT_GH_FAILURE_MARKERS = (
    "unexpected end of json input",
    "unexpected end of file",
    "connection reset",
    "connection refused",
    "connection timed out",
    "eof",
    "http 502",
    "http 503",
    "http 504",
    "timeout",
)


def normalize_statuses(raw_statuses: list[str]) -> list[str]:
    """Normalize the explicit allowlist, defaulting an empty list to CLEAN."""
    statuses = [str(v).strip().upper() for v in raw_statuses if str(v).strip()]
    return statuses or ["CLEAN"]


def mergeable_value_ok(value: object) -> bool:
    """Accept exactly the helper's true/MERGEABLE/TRUE representations."""
    if value is True:
        return True
    return str(value or "").strip().upper() in {"MERGEABLE", "TRUE"}


def evaluate(pull_request: dict, allowed_statuses: list[str]) -> dict:
    """Return typed readiness and full reasons without inventing check/review policy."""
    reasons: list[str] = []
    state = str(pull_request.get("state") or "").strip().upper()
    merged = state == "MERGED" or bool(pull_request.get("mergedAt"))
    is_draft = bool(pull_request.get("isDraft"))
    merge_state_status = str(pull_request.get("mergeStateStatus") or "").strip().upper()
    mergeable_value = pull_request.get("mergeable")
    if not merged:
        if state != "OPEN":
            reasons.append(f"PR state is {state or 'UNKNOWN'}, expected OPEN")
        if is_draft:
            reasons.append("PR is still a draft")
        if merge_state_status not in allowed_statuses:
            reasons.append(
                "GitHub mergeStateStatus is "
                f"{merge_state_status or 'UNKNOWN'}, expected one of "
                f"{', '.join(allowed_statuses)}"
            )
        if mergeable_value not in (None, "") and not mergeable_value_ok(
            mergeable_value
        ):
            reasons.append(
                f"GitHub mergeable is {mergeable_value!r}, expected MERGEABLE"
            )
    return {
        "ok": not reasons,
        "merge_ready": not reasons,
        "allowed_merge_state_statuses": allowed_statuses,
        "pull_request": pull_request,
        "merge_state_status": merge_state_status,
        "mergeable": mergeable_value,
        "state": state,
        "is_draft": is_draft,
        "review_decision": str(pull_request.get("reviewDecision") or ""),
        "status_check_rollup": pull_request.get("statusCheckRollup"),
        "reasons": reasons,
    }

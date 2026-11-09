"""Fictional golden inputs shared by package, installed and extraction checks."""

from __future__ import annotations


def comment(identity: str, **extra: object) -> dict:
    """Return one complete normalized request with exact body and location."""
    return {
        "id": identity,
        "source": "pr_conversation",
        "author": "fixture-reviewer",
        "outdated": False,
        "diff_hunk": "",
        "body": "  Fix edge case café\n",
        "created_at": "2026-01-01T10:00:00Z",
        "path": "src/example.py",
        "line": 12,
        "url": "https://example.test/comment",
        **extra,
    }


def fetch(entries: list[dict], **overrides: object) -> dict:
    """Build complete normalized fetch/checkpoint evidence without a provider."""
    return {
        "ok": True,
        "exit_code": 0,
        "comments": entries,
        "count": len(entries),
        "review_thread_comment_count": sum(
            isinstance(c, dict) and c.get("source") == "review_thread" for c in entries
        ),
        "pr_conversation_comment_count": sum(
            isinstance(c, dict) and c.get("source") == "pr_conversation"
            for c in entries
        ),
        "pr_conversation_comments_after": "",
        "last_successful_fetch_completed_at": "2026-01-01T12:00:00Z",
        **overrides,
    }


def invalid_projected_locations() -> list[tuple[str, object]]:
    """Enumerate malformed output locations, including bools masquerading as ints."""
    return (
        [
            (field, value)
            for field in ("line", "original_line", "start_line")
            for value in ("12", True, False, 12.5, {}, [])
        ]
        + [("side", value) for value in (12, True, False, {}, [])]
        + [("diff_hunk_header", value) for value in (None, 12, True, False, {}, [])]
    )


def valid_projected_locations() -> list[dict]:
    """Keep schema-permitted nulls, integral numbers and unconstrained locations."""
    return [
        {"line": None, "original_line": None, "start_line": None, "side": None},
        {"line": -1, "original_line": 0, "start_line": 2147483648, "side": ""},
        {"line": 12.0, "original_line": 0.0, "start_line": -1.0, "side": "custom"},
        {
            "line": 12,
            "original_line": 13,
            "start_line": 10,
            "side": "RIGHT",
            "diff_hunk_header": "header preserved verbatim",
        },
    ]


def merge_cases() -> list[tuple[dict, list[str]]]:
    """Cover helper truth representations, merged bypass, allowlists and negatives."""
    clean = {"state": "OPEN", "mergeStateStatus": "CLEAN"}
    return (
        [
            ({**clean, "mergeable": value}, [])
            for value in (
                None,
                "",
                True,
                "MERGEABLE",
                " true ",
                False,
                0,
                1,
                "UNKNOWN",
                "CONFLICTING",
            )
        ]
        + [
            ({**clean, **extra}, statuses)
            for extra, statuses in (
                ({"state": " closed "}, []),
                ({"state": ""}, []),
                ({"isDraft": True}, []),
                ({"mergeStateStatus": "BLOCKED"}, []),
                ({"mergeStateStatus": " unstable "}, [" unstable ", "clean"]),
                ({"mergeStateStatus": "UNKNOWN"}, [""]),
                (
                    {
                        "reviewDecision": "CHANGES_REQUESTED",
                        "statusCheckRollup": [{"conclusion": "FAILURE"}],
                    },
                    [],
                ),
                ({"state": "MERGED", "isDraft": True, "mergeable": "CONFLICTING"}, []),
                ({"state": "CLOSED", "mergedAt": "2026-01-01"}, []),
            )
        ]
        + [({}, [])]
    )

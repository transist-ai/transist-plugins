"""Package-local UTF-8 budgets and bounded APP diagnostics (no runtime changes)."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Callable, NoReturn

MAX_HINT_BYTES = 256 * 1024
MAX_FRAME_BYTES = 1024 * 1024


def positive_limit(payload: dict, name: str) -> int | None:
    """Validate an optional byte limit; omission leaves the input unbounded."""
    value = payload.get(name)
    if name not in payload:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def output_mode(payload: dict) -> str:
    """Resolve the explicit output contract, defaulting to legacy."""
    mode = payload.get("output_mode", "legacy")
    if mode not in ("legacy", "compact"):
        raise ValueError("output_mode must be legacy or compact")
    return mode


def strict_bounded(payload: dict) -> bool:
    """Return whether supplied byte controls require declared contributors to exist."""
    return "max_prompt_bytes" in payload or "max_context_file_bytes" in payload


class ContributorReadError(ValueError):
    """Carry safe contributor labels and raw byte counts, never paths or content."""

    def __init__(
        self,
        label: str,
        reason: str,
        *,
        read_bytes: int | None = None,
        source_bytes: int | None = None,
        limit: int | None = None,
    ) -> None:
        self.label, self.reason = label, reason
        self.read_bytes, self.source_bytes, self.limit = read_bytes, source_bytes, limit
        message = f"{label}: {reason}"
        if limit is not None:
            message += f"; limit {limit} UTF-8 bytes"
        if read_bytes is not None:
            message += f"; read {read_bytes} bytes"
        if source_bytes is not None:
            message += f"; source {source_bytes} bytes"
        super().__init__(message)

    def diagnostic(self) -> dict:
        """Return only known raw sizes and safe labels for the composition artifact."""
        result = {"label": self.label, "reason": self.reason}
        if self.read_bytes is not None:
            result["read_bytes"] = self.read_bytes
        if self.source_bytes is not None:
            result["bytes"] = self.source_bytes
        return result


def bounded_text(
    path: str,
    limit: int | None = None,
    *,
    label: str = "prompt_file",
    strict: bool = False,
) -> str:
    """Read at most limit+1 raw bytes, reject overflow, then decode strict UTF-8."""
    strict = strict or limit is not None
    try:
        if any(p.is_symlink() for p in (Path(path), *Path(path).parents)):
            raise ContributorReadError(label, "symlink")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            source = os.fstat(handle.fileno())
            if not stat.S_ISREG(source.st_mode):
                raise ContributorReadError(label, "not a regular file")
            source_bytes = source.st_size if stat.S_ISREG(source.st_mode) else None
            data = handle.read() if limit is None else handle.read(limit + 1)
    except OSError:
        if strict:
            raise ContributorReadError(label, "unavailable") from None
        raise
    if limit is not None and len(data) > limit:
        raise ContributorReadError(
            label,
            "exceeds budget",
            read_bytes=len(data),
            source_bytes=source_bytes,
            limit=limit,
        )
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        if strict:
            raise ContributorReadError(
                label,
                "invalid UTF-8",
                read_bytes=len(data),
                source_bytes=source_bytes,
            ) from None
        raise


def save_artifact(request: dict, name: str, contents: str) -> None:
    """Preserve full produced bytes in the invocation artifact directory."""
    directory = request["paths"]["artifacts_dir"]
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, name), "w", encoding="utf-8") as handle:
        handle.write(contents)
    request.setdefault("_produced", {})[name] = len(contents.encode("utf-8"))


class PromptSections(list):
    """Compose labeled sections with incremental and final rendered byte checks."""

    def __init__(self, request: dict, payload: dict) -> None:
        super().__init__()
        self.request = request
        self.limit = positive_limit(payload, "max_prompt_bytes")
        self.file_limit = positive_limit(payload, "max_context_file_bytes")
        self.strict = strict_bounded(payload)
        self.total = 0
        self.labels: list[dict] = []

    def report(self, **extra: object) -> None:
        """Persist composition sizes/labels only, including on budget failure."""
        save_artifact(
            self.request,
            "prompt-composition.json",
            json.dumps(
                {"sections": self.labels, "total_bytes": self.total, **extra},
                ensure_ascii=False,
                sort_keys=True,
            ),
        )

    def append(self, section: str) -> None:
        size = len(section.encode("utf-8"))
        self.total += size + (2 if self else 0)
        # Labels are wrapper-generated headings, never section contents.
        self.labels.append({"label": section.split("\n", 1)[0], "bytes": size})
        self.report()
        if self.limit is not None and self.total > self.limit:
            self.report(exceeded=True)
            raise ValueError(f"composed prompt exceeds {self.limit} UTF-8 bytes")
        super().append(section)

    def fail_read(self, error: ContributorReadError) -> NoReturn:
        """Report a sanitized contributor failure and stop composition."""
        self.labels.append(error.diagnostic())
        self.report(read_failed=True, exceeded=error.reason == "exceeds budget")
        raise error

    def directory(self, path: str, label: str, *, allow_missing: bool = False) -> bool:
        """Require replay roots; only intentional fresh staging may be absent."""
        if not self.strict:
            return os.path.isdir(path)
        try:
            source = os.stat(path)
        except FileNotFoundError:
            if allow_missing:
                return False
            self.fail_read(ContributorReadError(label, "unavailable"))
        except OSError:
            self.fail_read(ContributorReadError(label, "unavailable"))
        if not stat.S_ISDIR(source.st_mode):
            self.fail_read(ContributorReadError(label, "not a directory"))
        return True

    def read(self, path: str, label: str = "context_file") -> str:
        """Bound every explicit and automatically replayed file by remaining budget."""
        limits = [value for value in (self.file_limit, self.limit) if value is not None]
        if self.limit is not None:
            limits.append(max(0, self.limit - self.total))
        try:
            return bounded_text(
                path,
                min(limits) if limits else None,
                label=label,
                strict=self.strict,
            )
        except ContributorReadError as error:
            self.fail_read(error)

    def finish(self, rendered: str) -> str:
        """Check template, separators and all substituted sections before dispatch."""
        rendered_bytes = len(rendered.encode("utf-8"))
        self.labels.append(
            {"label": "Template framing", "bytes": max(0, rendered_bytes - self.total)}
        )
        self.total = rendered_bytes
        exceeded = self.limit is not None and self.total > self.limit
        self.report(exceeded=exceeded)
        if exceeded:
            raise ValueError(f"composed prompt exceeds {self.limit} UTF-8 bytes")
        return rendered


def record_logs(request: dict, label: str, stdout: str, stderr: str) -> None:
    """Store each command's complete logs once and record logical names/byte sizes."""
    logs = request.setdefault("_logs", [])
    for stream, contents in (("stdout", stdout), ("stderr", stderr)):
        if isinstance(contents, bytes):
            contents = contents.decode("utf-8", errors="replace")
        name = f"{label}-{stream}.txt"
        save_artifact(request, name, contents)
        logs.append(
            {"name": name, "bytes": len(contents.encode("utf-8")), "contents": contents}
        )


def payload_hint(request: dict, name: str, contents: str, hint: Callable) -> dict:
    """Save complete business evidence before applying inline transport limits."""
    descriptor = hint(name, "")
    if name == "response.txt" and any(
        log["contents"].strip() == contents.strip() for log in request.get("_logs", [])
    ):
        return hint("response.txt", "")
    if name == "response.txt" and output_mode(request.get("input") or {}) == "compact":
        if not any(
            log["contents"].strip() == contents.strip()
            for log in request.get("_logs", [])
        ):
            save_artifact(request, "backend-response.txt", contents)
        return hint("response.txt", "")
    save_artifact(request, descriptor["name"], contents)
    if len(contents.encode("utf-8")) > MAX_HINT_BYTES:
        request["_output_limit"] = True
        return hint(
            descriptor["name"] + ".metadata.json",
            json.dumps(
                {
                    "name": descriptor["name"],
                    "bytes": len(contents.encode("utf-8")),
                    "inline": False,
                    "storage": "invocation_private",
                    "retention": "not_guaranteed",
                }
            ),
        )
    return hint(name, contents)


def finalize_result(request: dict, result: dict, hint: Callable) -> dict:
    """Select compact diagnostics; fail rather than truncate business data."""
    mode = output_mode(request.get("input") or {})
    output = result["output"]
    logs = request.get("_logs", [])
    if not logs and ("stdout" in output or "stderr" in output):
        record_logs(
            request, "command", output.get("stdout", ""), output.get("stderr", "")
        )
        logs = request["_logs"]
    if output and mode == "compact":
        output.pop("stdout", None)
        output.pop("stderr", None)
        output["output_mode"] = "compact"
        output["diagnostics"] = {"logs": []}
        if request.get("action") == "verify":
            output["findings"] = request.get("_findings", [])
    hints = [
        item
        for item in result["artifact_hints"]
        if item["name"] not in {"stdout.txt", "stderr.txt", "artifact-paths.json"}
        and not (mode == "compact" and item["name"] == "response.txt")
        and not (item["name"] == "response.txt" and not item["contents"])
    ]
    # Diagnostics alone may be shortened, with explicit metadata and distinct names.
    for log in logs:
        name = log["name"]
        contents = log["contents"]
        allowance = min(
            MAX_HINT_BYTES,
            max(
                0,
                MAX_FRAME_BYTES // 2
                - sum(len(json.dumps(item).encode("utf-8")) for item in hints),
            ),
        )
        truncated = log["bytes"] > allowance
        if truncated:
            contents = contents.encode("utf-8")[:allowance].decode(
                "utf-8", errors="ignore"
            )
            name += ".truncated.txt"
        offered = len(hints) < 29 and allowance > 0
        if offered:
            hints.append(hint(name, contents))
        else:
            truncated = True
        if output and mode == "compact":
            output["diagnostics"]["logs"].append(
                {
                    "name": log["name"],
                    "bytes": log["bytes"],
                    "truncated": truncated,
                    "full_log_storage": "invocation_private",
                    "full_log_hint_offered": offered and not truncated,
                }
            )
    if "prompt-composition.json" in request.get("_produced", {}):
        with open(
            os.path.join(request["paths"]["artifacts_dir"], "prompt-composition.json"),
            encoding="utf-8",
        ) as handle:
            composition = handle.read()
        if len(composition.encode("utf-8")) <= MAX_HINT_BYTES and len(hints) < 30:
            hints.append(hint("prompt-composition.json", composition))
    result["artifact_hints"] = hints
    associations = {
        item["name"]: item["original_path"]
        for item in hints
        if item.get("original_path")
    }
    if associations:
        hints.append(
            hint("artifact-paths.json", json.dumps(associations, sort_keys=True))
        )
    diagnostic_names = {log["name"] for log in logs}
    diagnostic_names.add("prompt-composition.json")
    while len(json.dumps(result).encode("utf-8")) > MAX_FRAME_BYTES:
        changed = False
        for item in hints:
            if (
                item["name"].removesuffix(".truncated.txt") not in diagnostic_names
                or not item["contents"]
            ):
                continue
            item["contents"] = (
                item["contents"]
                .encode("utf-8")[: len(item["contents"].encode("utf-8")) // 2]
                .decode("utf-8", errors="ignore")
            )
            if not item["name"].endswith(".truncated.txt"):
                item["name"] += ".truncated.txt"
            if output and mode == "compact":
                for diagnostic in output["diagnostics"]["logs"]:
                    if item["name"].startswith(diagnostic["name"]):
                        diagnostic["truncated"] = True
                        diagnostic["full_log_hint_offered"] = False
            changed = True
        if changed:
            for item in hints:
                if item["name"] == "artifact-paths.json":
                    item["contents"] = json.dumps(
                        {
                            entry["name"]: entry["original_path"]
                            for entry in hints
                            if entry.get("original_path")
                            and entry["name"] != "artifact-paths.json"
                        },
                        sort_keys=True,
                    )
        if not changed:
            break
    if (
        request.get("_output_limit")
        or len(hints) > 32
        or len(json.dumps(result).encode("utf-8")) > MAX_FRAME_BYTES
    ):
        save_artifact(
            request, "produced-output.json", json.dumps(output, ensure_ascii=False)
        )
        result["status"] = "failed"
        result["error"] = {
            "code": "validation_error",
            "class": "validation",
            "retryability": "none",
            "message": (
                "produced business output exceeds APP inline limits;"
                " complete evidence preserved in invocation-private staging;"
                " durable retention is not guaranteed"
            ),
        }
        if len(json.dumps(output).encode("utf-8")) > MAX_FRAME_BYTES // 2:
            result["output"] = {}
        result["artifact_hints"] = []
    if result["status"] == "failed" and request.get("_produced"):
        if result["output"]:
            result["output"]["ok"] = False
        inventory = json.dumps(
            {
                "storage": "invocation_private",
                "retention": "not_guaranteed",
                "files": request["_produced"],
            },
            sort_keys=True,
        )
        if (
            len(inventory.encode("utf-8")) <= MAX_HINT_BYTES
            and len(result["artifact_hints"]) < 32
        ):
            result["artifact_hints"].append(hint("produced-evidence.json", inventory))
    if len(json.dumps(result).encode("utf-8")) > MAX_FRAME_BYTES:
        result["output"] = {}
        result["artifact_hints"] = [
            hint(
                "produced-evidence.json",
                json.dumps(
                    {
                        "name": "produced-output.json",
                        "inline": False,
                        "storage": "invocation_private",
                        "retention": "not_guaranteed",
                    }
                ),
            )
        ]
    if result["output"] and mode == "compact":
        for log in result["output"]["diagnostics"]["logs"]:
            offered = any(
                item["name"] == log["name"]
                and len(item["contents"].encode("utf-8")) == log["bytes"]
                for item in result["artifact_hints"]
            )
            log["full_log_hint_offered"] = offered
            log["truncated"] = not offered
    return result

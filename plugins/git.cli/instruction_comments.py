"""Bounded tracked-source scanner ported from scan_tst_comments.py.

Only explicit leading marker comments are requests. Operational files, binary
source and the destination are excluded. Exhaustion never publishes a prefix.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import selectors
import stat
import subprocess
import sys
import time
import tokenize
from typing import BinaryIO, NamedTuple
import unicodedata

DEFAULT_MARKER = "tst:"
MAX_MARKER_LENGTH = 64
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_LINE_BYTES = 64 * 1024
MAX_COMMENT_BYTES = 4096
MAX_OUTPUT_BYTES = 32768
MAX_FILES = 10000
MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_GIT_OUTPUT_BYTES = 2 * 1024 * 1024
GIT_TIMEOUT_SECONDS = 30
MAX_SCAN_SECONDS = 60
QUOTED_SOURCE_SUFFIXES = {
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".mjs",
    ".cjs",
    ".ex",
    ".exs",
    ".sql",
    ".go",
    ".java",
    ".kt",
    ".c",
    ".cpp",
    ".h",
    ".rb",
    ".swift",
}
NETPBM_SPACE = re.compile(rb"(?:[ \t\r\n\v\f]|#[^\r\n]*(?:\r\n|[\r\n]))+")
NETPBM_NUMBER = re.compile(rb"[0-9]{1,10}(?=[ \t\r\n\v\f])")
SOURCE_FAILURES = {
    "invalid_utf8": "invalid UTF-8; repair the source encoding",
    "unreadable_source": "cannot read source; check permissions and nonsymlink paths",
    "unsafe_source": "source is not a regular file; check the tracked path",
    "tokenization": "cannot safely tokenize comments; check source syntax/encoding",
    "file_budget": "source exceeds 8 MiB scan budget; reduce the file size",
    "stream_budget": "source exceeds streaming scan budget; reduce file/line size",
    "scan_budget": "aggregate reviewer scan budget exceeded; reduce scan size/duration",
    "comment_budget": "reviewer comment exceeds 4 KiB; shorten the comment",
    "output_budget": "reviewer context exceeds 32 KiB; reduce requests (none dropped)",
    "hidden_index": (
        "tracked paths have assume-unchanged or skip-worktree flags; "
        "clear them before scanning"
    ),
    "changed_during_scan": (
        "Git state changed during reviewer scan; coordinate workspace writes and retry"
    ),
    "worktree_root": "changed scope requires the workspace to be the Git worktree root",
}


class CommentMarker:
    """Validate and compile one literal marker for the duration of a scan."""

    def __init__(self, value: object) -> None:
        """Reject invalid raw input before stripping surrounding whitespace."""
        if not isinstance(value, str):
            raise ValueError("marker must be a string; omit it to use the default")
        if len(value) > MAX_MARKER_LENGTH:
            raise ValueError(f"marker must be at most {MAX_MARKER_LENGTH} characters")
        if any(
            unicodedata.category(c) in {"Cc", "Cf", "Cs", "Zl", "Zp"} for c in value
        ):
            raise ValueError("marker must be a single line without control characters")
        literal = value.strip()
        if not literal:
            raise ValueError("marker must not be blank; omit it to use the default")
        self.is_default = literal.lower() == DEFAULT_MARKER
        self.pattern = re.compile(
            r"^\s*(?P<delimiter>//|#|--|/\*|<!--)\s*"
            rf"(?P<marker>{re.escape(literal)})\s*(?P<body>.*?)\s*$",
            re.IGNORECASE,
        )


DEFAULT_COMMENT_MARKER = CommentMarker(DEFAULT_MARKER)


class SourceScanError(ValueError):
    """Carry a controlled reason and bounded safe relative path, never raw errors."""

    def __init__(self, relative: Path | None, reason: str) -> None:
        """Build an actionable diagnostic from a relative path and known reason code."""
        detail = SOURCE_FAILURES[reason]
        name = relative.as_posix() if relative is not None else ""
        self.relative_path = (
            name
            if relative is not None
            and not relative.is_absolute()
            and ".." not in relative.parts
            and len(name) <= 240
            and re.fullmatch(r"[A-Za-z0-9_./ -]+", name)
            else None
        )
        self.reason = reason
        location = f" for `{self.relative_path}`" if self.relative_path else ""
        super().__init__(
            f"Reviewer scan failed{location}: {detail}; no partial context published"
        )


def binary_preview(preview: bytes) -> bool:
    """Identify binary content from the bounded probe, independent of file suffix."""
    if b"\0" in preview or preview.startswith(
        (
            b"\x89PNG\r\n\x1a\n",
            b"\xff\xd8\xff",
            b"GIF87a",
            b"GIF89a",
            b"II*\0",
            b"MM\0*",
            b"%PDF-",
            b"PK\x03\x04",
            b"PK\x05\x06",
            b"\x1f\x8b\x08",
            b"BZh",
            b"\xfd7zXZ\0",
            b"\x28\xb5\x2f\xfd",
            b"\x7fELF",
            b"wOFF",
            b"wOF2",
        )
    ):
        return True
    if preview[:2] not in (b"P4", b"P5", b"P6"):
        return False
    # Require a complete raw Netpbm header in the probe, not just a P4/P5/P6 prefix.
    # Comments (including tst: examples) are image metadata, never source requests.
    offset = 2
    values = []
    for _ in range(2 if preview[:2] == b"P4" else 3):
        space = NETPBM_SPACE.match(preview, offset)
        number = NETPBM_NUMBER.match(preview, space.end()) if space else None
        if number is None or int(number.group()) == 0:
            return False
        values.append(int(number.group()))
        offset = number.end()
    return len(values) == 2 or values[-1] <= 65535


def operational(path: Path) -> bool:
    """Exclude operational storage and interaction captures, even when tracked."""
    return ".transist" in path.parts or path.name.endswith(
        (".interaction.json", ".interaction.md")
    )


def _directory_flags() -> int:
    """Search pinned ancestors without requesting directory-listing permission."""
    search = getattr(os, "O_SEARCH", None)
    if search is None:
        # Darwin's public <sys/fcntl.h> O_SEARCH ABI (O_EXEC | O_DIRECTORY),
        # also exported as 0x40100000 by CPython 3.14; absent in older Pythons.
        search = (
            0x40100000
            if sys.platform == "darwin"
            else getattr(os, "O_PATH", os.O_RDONLY)
        )
    return search | os.O_DIRECTORY | os.O_NOFOLLOW


def open_directory(path: Path, create: bool = False) -> int:
    """Pin nonsymlink directory components; only output parents may be created."""
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("unsafe directory path")
    flags = _directory_flags()
    fd = os.open(path.anchor, flags)
    try:
        for part in path.parts[1:]:
            try:
                child = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, mode=0o700, dir_fd=fd)
                child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except Exception:
        os.close(fd)
        raise


def open_source(root: Path, path: Path) -> BinaryIO:
    """Open only contained regular source, with no symlink traversal."""
    path.relative_to(root)
    parent = open_directory(path.parent)
    try:
        fd = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
        )
    finally:
        os.close(parent)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise SourceScanError(path.relative_to(root), "unsafe_source")
    return os.fdopen(fd, "rb")


def git_output(repo_root: Path, arguments: list[str], budget: ScanBudget) -> bytes:
    """Capture bounded Git output with a fixed deadline and suppressed diagnostics."""
    command = ["git", *arguments]
    output = bytearray()
    entries = 0
    budget.check()
    deadline = min(time.monotonic() + GIT_TIMEOUT_SECONDS, budget.deadline)
    process = subprocess.Popen(
        command, cwd=repo_root, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    )
    assert process.stdout is not None
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise subprocess.TimeoutExpired(command, GIT_TIMEOUT_SECONDS)
                chunk = os.read(
                    process.stdout.fileno(),
                    min(65536, MAX_GIT_OUTPUT_BYTES + 1 - len(output)),
                )
                if not chunk:
                    break
                output.extend(chunk)
                entries += chunk.count(b"\0")
                if len(output) > MAX_GIT_OUTPUT_BYTES or entries > MAX_FILES:
                    raise ValueError("Git filename enumeration exceeds scan budget")
        process.wait(timeout=max(0.001, deadline - time.monotonic()))
        budget.check()
        if process.returncode:
            raise subprocess.CalledProcessError(process.returncode, command)
    finally:
        try:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        finally:
            process.stdout.close()
    return bytes(output)


def git_names(
    repo_root: Path,
    budget: ScanBudget,
    deleted: bool = False,
    arguments: list[str] | None = None,
) -> set[str]:
    """Read and validate bounded NUL-delimited repository-relative Git paths."""
    output = git_output(
        repo_root,
        arguments or ["ls-files", "-z", *(["--deleted"] if deleted else [])],
        budget,
    )
    if output and not output.endswith(b"\0"):
        raise ValueError("incomplete Git filename enumeration")
    names: set[str] = set()
    for name in output.decode("utf-8").split("\0"):
        if not name:
            continue
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("tracked path escapes repository")
        if any(ord(char) < 32 for char in name) or "`" in name:
            raise ValueError("tracked path cannot be represented safely")
        names.add(name)
    return names


class ChangedSnapshot(NamedTuple):
    """Capture selected paths and Git identities for best-effort revalidation."""

    paths: tuple[Path, ...]
    head: str
    base: str
    index: bytes
    changes: tuple[frozenset[str], ...]


def changed_files(
    repo_root: Path, base_ref: str, budget: ScanBudget
) -> ChangedSnapshot:
    """Select current files changed since the merge base or in the worktree."""
    if not isinstance(base_ref, str) or not base_ref.startswith("refs/"):
        raise ValueError("base must be a fully qualified ref")
    top = git_output(repo_root, ["rev-parse", "--show-toplevel"], budget)
    if not top.endswith(b"\n"):
        raise SourceScanError(None, "worktree_root")
    top_path = Path(os.fsdecode(top[:-1]))
    if not top_path.is_absolute() or top_path.resolve(strict=True) != repo_root.resolve(
        strict=True
    ):
        raise SourceScanError(None, "worktree_root")
    git_output(repo_root, ["check-ref-format", base_ref], budget)
    commit = (
        git_output(
            repo_root,
            ["rev-parse", "--verify", "--end-of-options", base_ref + "^{commit}"],
            budget,
        )
        .strip()
        .decode("ascii")
    )
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
        raise ValueError("invalid base commit")
    head = (
        git_output(repo_root, ["rev-parse", "--verify", "HEAD^{commit}"], budget)
        .strip()
        .decode("ascii")
    )
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head):
        raise ValueError("invalid HEAD commit")
    flags = git_output(repo_root, ["ls-files", "-v", "-z"], budget)
    if flags and not flags.endswith(b"\0"):
        raise ValueError("incomplete Git index flags")
    for entry in flags.split(b"\0"):
        if not entry:
            continue
        if len(entry) < 3 or entry[1:2] != b" ":
            raise ValueError("invalid Git index flags")
        if entry[:1] == b"S" or entry[:1].islower():
            raise SourceScanError(None, "hidden_index")
    index = git_output(repo_root, ["ls-files", "--stage", "-z"], budget)
    merge = (
        git_output(repo_root, ["merge-base", head, commit], budget)
        .strip()
        .decode("ascii")
    )
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", merge):
        raise ValueError("invalid merge base")
    diffs = [
        [
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            "--name-only",
            "-z",
            merge,
            head,
            "--",
        ],
        [
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            "--name-only",
            "-z",
            "--cached",
            "--",
        ],
        [
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            "--name-only",
            "-z",
            "--",
        ],
    ]
    names: set[str] = set()
    deleted: set[str] = set()
    changes: list[frozenset[str]] = []
    for args in diffs:
        selected = git_names(repo_root, budget, arguments=args)
        removed = git_names(
            repo_root, budget, arguments=args[:-1] + ["--diff-filter=D", "--"]
        )
        names.update(selected)
        deleted.update(removed)
        changes.extend((frozenset(selected), frozenset(removed)))
    untracked = git_names(
        repo_root,
        budget,
        arguments=["ls-files", "--others", "--exclude-standard", "-z"],
    )
    names.update(untracked)
    changes.append(frozenset(untracked))
    if (
        len(names) > MAX_FILES
        or sum(len(n.encode("utf-8")) + 1 for n in names) > MAX_GIT_OUTPUT_BYTES
    ):
        raise ValueError("Git filename enumeration exceeds scan budget")
    paths = []
    for name in sorted(names):
        budget.check()
        relative = Path(name)
        if operational(relative):
            continue
        path = repo_root / relative
        if name in deleted:
            try:
                if confirmed_missing(repo_root, path):
                    continue
            except OSError:
                raise SourceScanError(relative, "unreadable_source") from None
        paths.append(path)
    return ChangedSnapshot(tuple(paths), head, commit, index, tuple(changes))


def confirmed_missing(repo_root: Path, path: Path) -> bool:
    """Exclude pinned-parent Git-confirmed deletions, never symlink replacements."""
    relative = path.relative_to(repo_root)
    flags = _directory_flags()
    fd = open_directory(repo_root)
    try:
        for part in relative.parts[:-1]:
            try:
                child = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                return True
            except NotADirectoryError:
                mode = os.stat(part, dir_fd=fd, follow_symlinks=False).st_mode
                if stat.S_ISREG(mode):
                    return True
                raise SourceScanError(relative, "unsafe_source") from None
            os.close(fd)
            fd = child
        try:
            mode = os.stat(relative.name, dir_fd=fd, follow_symlinks=False).st_mode
        except FileNotFoundError:
            return True
        if stat.S_ISLNK(mode):
            raise SourceScanError(relative, "unsafe_source")
        return stat.S_ISDIR(mode)
    finally:
        os.close(fd)


def tracked_files(repo_root: Path, budget: ScanBudget) -> list[Path]:
    """Enumerate sorted tracked source; a later disappearance remains fatal."""
    tracked = git_names(repo_root, budget)
    deleted = git_names(repo_root, budget, deleted=True)
    paths = []
    for name in sorted(tracked):
        budget.check()
        relative = Path(name)
        path = repo_root / relative
        if operational(relative):
            continue
        if name in deleted:
            try:
                if confirmed_missing(repo_root, path):
                    continue
            except OSError:
                raise SourceScanError(relative, "unreadable_source") from None
        paths.append(path)
    return paths


class ScanBudget:
    """Bound file count, aggregate source size/growth and total scan duration."""

    def __init__(self) -> None:
        """Start an invocation-local scan budget without mutating repository state."""
        self.files = 0
        self.source_bytes = 0
        self.deadline = time.monotonic() + MAX_SCAN_SECONDS

    def check(self) -> None:
        """Fail on exhaustion instead of reporting partial/false-clean context."""
        if (
            self.files > MAX_FILES
            or self.source_bytes > MAX_SOURCE_BYTES
            or time.monotonic() >= self.deadline
        ):
            raise SourceScanError(None, "scan_budget")

    def reserve_file(self, size: int) -> None:
        """Charge the entire file size before reading, including binary source."""
        self.files += 1
        self.source_bytes += size
        self.check()

    def grow(self, size: int) -> None:
        """Charge observed concurrent growth beyond the original stat size."""
        self.source_bytes += size
        self.check()


def render_output(
    hits: list[tuple[str, int, str]], marker: CommentMarker = DEFAULT_COMMENT_MARKER
) -> bytes:
    """Render complete located requests or fail the 32-KiB UTF-8 context budget."""
    title = "## Inline Reviewer Comments"
    lines = [title + " (tst:)" if marker.is_default else title, ""]
    if hits:
        lines.extend(
            [f"{len(hits)} comment(s); address each and remove its marker.", ""]
        )
        lines.extend(f"- `{rel}:{line}` -- {body}" for rel, line, body in hits)
    else:
        lines.append(
            "No tst: comments found." if marker.is_default else "No comments found."
        )
    data = ("\n".join(lines) + "\n").encode("utf-8")
    if len(data) > MAX_OUTPUT_BYTES:
        raise SourceScanError(None, "output_budget")
    return data


def quote_state(
    line: str, active: str | None, multiline_strings: bool = False
) -> str | None:
    """Conservatively track source literals/block comments; this is not a parser."""
    index = 0
    while index < len(line):
        if active:
            if line.startswith(active, index):
                index += len(active)
                active = None
            elif active not in {"*/", "-->"} and line[index] == "\\":
                index += 2
            else:
                index += 1
        elif line.startswith(("//", "--", "#"), index):
            break
        elif line.startswith("/*", index):
            active = "*/"
            index += 2
        elif line.startswith("<!--", index):
            active = "-->"
            index += 4
        else:
            delimiter = next(
                (v for v in ('"""', "'''", '"', "'", "`") if line.startswith(v, index)),
                None,
            )
            if delimiter:
                active = delimiter
                index += len(delimiter)
            else:
                index += 1
    if (
        not multiline_strings
        and active in {"'", '"'}
        and not line.rstrip("\r\n").endswith("\\")
    ):
        return None
    return active


def scan_file(
    repo_root: Path,
    file_path: Path,
    budget: ScanBudget | None = None,
    *,
    marker: CommentMarker = DEFAULT_COMMENT_MARKER,
) -> list[tuple[str, int, str]]:
    """Scan contained source and translate expected failures into safe diagnostics."""
    relative = file_path.relative_to(repo_root)
    if ".." in relative.parts:
        raise ValueError("source path escapes repository")
    try:
        return _scan_file(repo_root, file_path, budget, marker)
    except SourceScanError as error:
        raise SourceScanError(relative, error.reason) from None
    except UnicodeError:
        raise SourceScanError(relative, "invalid_utf8") from None
    except (tokenize.TokenError, SyntaxError):
        raise SourceScanError(relative, "tokenization") from None
    except OSError:
        raise SourceScanError(relative, "unreadable_source") from None


def _scan_file(
    repo_root: Path, file_path: Path, budget: ScanBudget | None, marker: CommentMarker
) -> list[tuple[str, int, str]]:
    """Stream bounded text; keep only leading comment-only markers with exact bodies."""
    relative = file_path.relative_to(repo_root)
    if operational(relative):
        return []
    hits: list[tuple[str, int, str]] = []
    budget = budget if budget is not None else ScanBudget()
    with open_source(repo_root, file_path) as handle:
        reserved = os.fstat(handle.fileno()).st_size
        if reserved > MAX_FILE_BYTES:
            raise SourceScanError(None, "file_budget")
        budget.reserve_file(reserved)
        preview = handle.read(8192)
        if len(preview) > reserved:
            budget.grow(len(preview) - reserved)
            reserved = len(preview)
        if len(preview) > MAX_FILE_BYTES:
            raise SourceScanError(None, "file_budget")
        if binary_preview(preview):
            return []
        handle.seek(0)
        consumed = 0

        def readline() -> bytes:
            """Read a bounded line for streaming text or Python tokenization."""
            nonlocal consumed, reserved
            budget.check()
            raw = handle.readline(MAX_LINE_BYTES + 1)
            consumed += len(raw)
            if consumed > reserved:
                budget.grow(consumed - reserved)
                reserved = consumed
            if consumed > MAX_FILE_BYTES or len(raw) > MAX_LINE_BYTES:
                raise SourceScanError(None, "stream_budget")
            # Validate Python bytes too: coding cookies must not hide malformed UTF-8.
            if file_path.suffix == ".py":
                raw.decode("utf-8")
            return raw

        if file_path.suffix == ".py":
            lines = (
                (t.start[0], t.line)
                for t in tokenize.tokenize(readline)
                if t.type == tokenize.COMMENT
            )
        else:
            lines = (
                (number, raw.decode("utf-8"))
                for number, raw in enumerate(iter(readline, b""), 1)
            )
        try:
            quote = None
            for lineno, line in lines:
                quoted = quote is not None
                if file_path.suffix in QUOTED_SOURCE_SUFFIXES:
                    quote = quote_state(
                        line, quote, multiline_strings=file_path.suffix == ".sql"
                    )
                match = None if quoted else marker.pattern.fullmatch(line)
                if match and (body := match.group("body").strip()):
                    closing = {"/*": "*/", "<!--": "-->"}.get(match.group("delimiter"))
                    # A custom marker may contain all or part of a block terminator.
                    if closing and (
                        0
                        <= line.find(closing, match.end("delimiter"))
                        < match.end("marker")
                    ):
                        continue
                    if closing and closing in body:
                        body, trailing = body.split(closing, 1)
                        if trailing.strip():
                            continue
                        body = body.strip()
                    if not body:
                        continue
                    if len(body.encode("utf-8")) > MAX_COMMENT_BYTES:
                        raise SourceScanError(None, "comment_budget")
                    hits.append((relative.as_posix(), lineno, body))
                    render_output(hits, marker)
        except (tokenize.TokenError, SyntaxError):
            raise SourceScanError(None, "tokenization") from None
    return hits


def write_output(
    output_path: Path,
    hits: list[tuple[str, int, str]],
    marker: CommentMarker = DEFAULT_COMMENT_MARKER,
) -> None:
    """Publish complete bounded context through pinned regular output parents."""
    data = render_output(hits, marker)
    parent = open_directory(output_path.parent, create=True)
    try:
        fd = os.open(
            output_path.name,
            os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
            dir_fd=parent,
        )
        with os.fdopen(fd, "wb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ValueError("output is not a regular file")
            handle.truncate(0)
            handle.write(data)
    finally:
        os.close(parent)


def scan(
    root: Path,
    output: Path,
    base_ref: str | None = None,
    *,
    marker: CommentMarker = DEFAULT_COMMENT_MARKER,
) -> dict:
    """Scan the runtime-selected canonical workspace; output is data, not authority."""
    directory = open_directory(root)
    os.close(directory)
    hits: list[tuple[str, int, str]] = []
    budget = ScanBudget()
    snapshot = None if base_ref is None else changed_files(root, base_ref, budget)
    paths = tracked_files(root, budget) if snapshot is None else snapshot.paths
    for tracked in paths:
        if tracked == output or operational(tracked.relative_to(root)):
            continue
        hits.extend(scan_file(root, tracked, budget, marker=marker))
        render_output(hits, marker)
    if snapshot is not None and snapshot != changed_files(root, base_ref, budget):
        raise SourceScanError(None, "changed_during_scan")
    budget.check()
    write_output(output, hits, marker)
    return {"ok": True, "count": len(hits), "has_comments": bool(hits)}

"""TEST ONLY: exact fake HTTP peers inside owned Linux invocation namespaces.

No forwarding, routing, loopback configuration or sandbox grants. A private
fixture Python shim stops before exec; a birth/pidfd-checked controller binds
only registered peers, then resumes that invocation. Darwin keeps its peer/env
path; a native supervisor reclaims proved descendants and fails closed when
forked lineage cannot be completely classified. Unknown processes are not owned.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
import errno
from functools import lru_cache
import fcntl
import hashlib
import http.server
import json
import os
from pathlib import Path
import selectors
import select
import signal
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import tempfile
import types
from typing import Any, Callable

MAX_CHILDREN = 4096
CLEANUP_SECONDS = 5.0
MAX_CAPTURE_BYTES = 32 * 1024 * 1024
SOURCE_BYTES = globals().get("__namespace_peers_source__")
HELPER_SHA256 = (
    hashlib.sha256(SOURCE_BYTES).hexdigest()
    if isinstance(SOURCE_BYTES, bytes)
    else None
)
if SOURCE_BYTES is not None and (
    len(SOURCE_BYTES) > 262144
    or HELPER_SHA256 != globals().get("__namespace_peers_expected_sha256__")
):
    raise RuntimeError("fixture helper captured-source identity mismatch")


def load_from_source(
    source: bytes, expected_sha256: str, logical_path: str
) -> types.ModuleType:
    """Compile exactly caller-captured trusted bytes; logical_path is not reopened.

    Pinned workflow loaders must supply the same captured bytes and expected
    digest before exec. Plain imports have no verified source identity and may
    not dispatch; use this loader or load_file() for honest direct-source use.
    """
    if (
        not isinstance(source, bytes)
        or len(source) > 262144
        or hashlib.sha256(source).hexdigest() != expected_sha256
    ):
        raise ValueError("fixture source bytes do not match their expected digest")
    name = "_verified_namespace_peers_" + expected_sha256
    module = types.ModuleType(name)
    module.__dict__.update(
        __file__=logical_path,
        __namespace_peers_source__=source,
        __namespace_peers_expected_sha256__=expected_sha256,
    )
    sys.modules[name] = module
    exec(compile(source, logical_path, "exec"), module.__dict__)
    return module


def load_file(path: Path) -> types.ModuleType:
    """Capture and compile direct fixture bytes, without trusting pyc/path reuse."""
    with path.open("rb") as handle:
        source = handle.read(262145)
    return load_from_source(source, hashlib.sha256(source).hexdigest(), str(path))


def _source_snapshot(source: bytes, directory: Path) -> int:
    """Return an unlinked read-only snapshot; no writable handle is retained."""
    fd, name = tempfile.mkstemp(prefix=".namespace-source-", dir=directory)
    try:
        offset = 0
        while offset < len(source):
            offset += os.write(fd, source[offset : offset + 65536])
        os.fchmod(fd, 0o400)
        readonly = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    finally:
        os.close(fd)
        os.unlink(name)
    return readonly


_BOOTSTRAP = r"""
import hashlib, os, sys
fd = int(sys.argv[1])
try:
    chunks, size = [], 0
    while True:
        chunk = os.read(fd, 65536)
        if not chunk: break
        size += len(chunk)
        if size > 262144: raise RuntimeError('fixture source snapshot budget exceeded')
        chunks.append(chunk)
    source = b''.join(chunks)
finally:
    os.close(fd)
if hashlib.sha256(source).hexdigest() != sys.argv[2]:
    raise RuntimeError('fixture source snapshot digest mismatch')
logical = sys.argv[3]
sys.argv = [logical, '--fixture-supervise', *sys.argv[4:]]
namespace = {'__name__':'__main__', '__file__':logical,
             '__namespace_peers_source__':source,
             '__namespace_peers_expected_sha256__':hashlib.sha256(source).hexdigest()}
exec(compile(source, logical, 'exec'), namespace)
"""


class _BSDInfo(ctypes.Structure):
    """Public proc_bsdinfo ABI, verified against the host SDK in review-3 evidence."""

    _fields_ = (
        [
            (name, ctypes.c_uint32)
            for name in (
                "flags",
                "status",
                "xstatus",
                "pid",
                "ppid",
                "uid",
                "gid",
                "ruid",
                "rgid",
                "svuid",
                "svgid",
                "rfu1",
            )
        ]
        + [("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32)]
        + [
            (name, ctypes.c_uint32)
            for name in ("nfiles", "pgid", "pjobc", "tdev", "tpgid", "nice")
        ]
        + [("start_sec", ctypes.c_uint64), ("start_usec", ctypes.c_uint64)]
    )


class _UniqueInfo(ctypes.Structure):
    """XNU proc_uniqidentifierinfo (API size 56), verified with native orphan probe."""

    _fields_ = [
        ("uuid", ctypes.c_ubyte * 16),
        ("unique", ctypes.c_uint64),
        ("parent_unique", ctypes.c_uint64),
        ("version", ctypes.c_int32),
        ("original_parent_version", ctypes.c_int32),
        ("reserved", ctypes.c_uint64 * 2),
    ]


def _unique_info(pid: int) -> _UniqueInfo | None:
    """Read opaque native generations and the immutable fork-parent version."""
    lib = _darwin_lib()
    value = _UniqueInfo()
    ctypes.set_errno(0)
    size = lib.proc_pidinfo(pid, 17, 1, ctypes.byref(value), ctypes.sizeof(value))
    if size != ctypes.sizeof(value):
        error = ctypes.get_errno()
        if error == errno.ESRCH:
            return None
        raise OSError(error, "Darwin process generation ABI unavailable")
    return value


@lru_cache(maxsize=1)
def _darwin_lib() -> ctypes.CDLL:
    """Configure public libproc metadata APIs; no argv or environments are queried."""
    lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    lib.proc_pidinfo.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint64,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    lib.proc_pidinfo.restype = ctypes.c_int
    lib.proc_listpids.argtypes = [
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    lib.proc_listpids.restype = ctypes.c_int
    lib.proc_signal_with_audittoken.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.proc_signal_with_audittoken.restype = ctypes.c_int
    return lib


class _DarwinTree:
    """Native owned-descent ledger, watched before the real CLI is admitted."""

    def __init__(self) -> None:
        self.queue = select.kqueue()
        self.known: dict[int, Process] = {}
        self.exited: set[int] = set()
        self.forked: set[int] = set()
        self.uncertain: set[int] = set()
        self.unresolved: list[dict[str, int]] = []
        self.census_gaps = 0
        self.lib = _darwin_lib()
        self.versions: dict[int, int] = {}
        self.native: dict[int, _UniqueInfo] = {}
        own = _unique_info(os.getpid())
        self.floor = own.unique
        # launchd is outside the same-UID census. Its immutable generation can
        # exclude newly launched foreign services, never authorize their signals.
        init = _unique_info(1)
        self.foreign_versions = (
            {init.version} if init is not None and init.unique < self.floor else set()
        )

    def add(self, identity: Process) -> None:
        """Watch an already-proven native birth, never a numeric announcement."""
        prior = self.known.get(identity.pid)
        if prior is not None:
            if prior.birth != identity.birth:
                raise RuntimeError("Darwin owned PID birth changed")
            return
        current = process_identity(identity.pid)
        if current is None:
            return
        if current.birth != identity.birth:
            raise RuntimeError("Darwin birth changed before fork watch")
        unique = _unique_info(identity.pid)
        if unique is None or unique.unique != identity.birth:
            raise RuntimeError("Darwin owned generation changed while registering")
        self.native[identity.pid] = unique
        self.versions[unique.version] = unique.unique
        self.known[identity.pid] = identity
        if current.state == "Z":
            self.exited.add(identity.pid)
            return
        try:
            self.queue.control(
                [
                    select.kevent(
                        identity.pid,
                        filter=select.KQ_FILTER_PROC,
                        flags=select.KQ_EV_ADD
                        | select.KQ_EV_ENABLE
                        | select.KQ_EV_CLEAR,
                        fflags=select.KQ_NOTE_FORK | select.KQ_NOTE_EXIT,
                    )
                ],
                0,
                0,
            )
        except ProcessLookupError:
            self.exited.add(identity.pid)

    def census(self) -> list[Process]:
        """Revalidate parent and child births before extending the owned ledger."""
        deadline = time.monotonic() + 0.5
        events = self.queue.control(None, MAX_CHILDREN, 0)
        for event in events:
            if event.fflags & select.KQ_NOTE_FORK:
                self.forked.add(event.ident)
            if event.fflags & select.KQ_NOTE_EXIT:
                self.exited.add(event.ident)
        # Capture exec generations even for the unreaped direct CLI zombie.
        for pid, known in list(self.known.items()):
            unique = _unique_info(pid)
            if unique is not None and unique.unique == known.birth:
                self.native[pid] = unique
                self.versions[unique.version] = unique.unique
            elif unique is not None:
                self.uncertain.add(pid)
        # Only opaque same-UID identities are examined. XNU records the original
        # parent's global pid-version at fork and does not change it on setsid,
        # exec or reparenting. An unobserved, reaped intermediate still leaves a
        # gap: its descendants must remain unclassified, never silently unowned.
        buffer = (ctypes.c_int * (MAX_CHILDREN + 1))()
        count = self.lib.proc_listpids(4, os.geteuid(), buffer, ctypes.sizeof(buffer))
        if count < 0 or count >= ctypes.sizeof(buffer):
            raise RuntimeError("Darwin same-UID generation census truncated")
        candidates = []
        foreign_versions = set(self.foreign_versions)
        self.census_gaps = 0
        for pid in list(buffer)[: count // ctypes.sizeof(ctypes.c_int)]:
            if time.monotonic() > deadline:
                raise RuntimeError("Darwin identity census deadline: candidates")
            if pid > 0:
                unique = _unique_info(pid)
                if unique is None:
                    # A listed process could have forked after the snapshot and
                    # vanished before inspection. No empty-ownership proof then.
                    self.census_gaps += 1
                elif unique.unique < self.floor:
                    foreign_versions.add(unique.version)
                elif unique.unique > self.floor:
                    candidates.append((pid, unique))
        for _ in range(MAX_CHILDREN):
            changed = False
            for pid, unique in candidates:
                if time.monotonic() > deadline:
                    raise RuntimeError("Darwin identity census deadline: lineage")
                if (
                    pid in self.known
                    or unique.original_parent_version not in self.versions
                ):
                    continue
                current = process_identity(pid)
                final = _unique_info(pid)
                if (
                    current is not None
                    and final is not None
                    and current.birth == unique.unique == final.unique
                    and final.original_parent_version == unique.original_parent_version
                ):
                    before_count = len(self.known)
                    self.add(current)
                    changed |= len(self.known) > before_count
            if not changed:
                break
        pending = list(self.known.values())
        seen = set()
        while pending:
            if time.monotonic() > deadline:
                raise RuntimeError("Darwin identity census deadline: children")
            parent = pending.pop()
            if parent.pid in seen or parent.pid in self.exited:
                continue
            seen.add(parent.pid)
            if len(seen) > MAX_CHILDREN:
                raise RuntimeError("Darwin ownership census budget exceeded")
            before = process_identity(parent.pid)
            if (
                before is None
                or before.birth != parent.birth
                or before.state in ("Z", "X")
            ):
                continue
            buffer = (ctypes.c_int * (MAX_CHILDREN + 1))()
            size = self.lib.proc_listpids(6, parent.pid, buffer, ctypes.sizeof(buffer))
            if size < 0 or size >= ctypes.sizeof(buffer):
                raise RuntimeError("Darwin child observation incomplete")
            after = process_identity(parent.pid)
            if after is None or after.birth != parent.birth:
                # A vanished parent is resolved through its saved original
                # generation by the same-UID lineage census, not a bare PID.
                if after is not None:
                    self.uncertain.add(parent.pid)
                continue
            for pid in list(buffer)[: size // ctypes.sizeof(ctypes.c_int)]:
                if pid <= 0:
                    continue
                child = process_identity(pid)
                final_parent = process_identity(parent.pid)
                final_child = process_identity(pid)
                if (
                    child is not None
                    and final_child is not None
                    and final_parent is not None
                    and final_parent.birth == parent.birth
                    and child.birth == final_child.birth
                    and child.parent == parent.pid
                    and final_child.parent == parent.pid
                ):
                    self.add(final_child)
                    pending.append(final_child)
        # NOTE_FORK coalesces: even a matched child edge cannot account for every
        # fork or a reaped intermediate. Partition the complete same-UID snapshot
        # instead. Pre-supervisor births cannot descend from the admitted CLI;
        # propagate that exclusion only through immutable original-parent
        # versions. Unknown lineage grants no signalling authority. This may
        # conservatively reject unrelated new orphans, which is preferable to a
        # false confirmed-empty receipt. Never discard a gap on a PID announcement.
        for _ in range(MAX_CHILDREN):
            changed = False
            for pid, unique in candidates:
                if time.monotonic() > deadline:
                    raise RuntimeError("Darwin identity census deadline: completeness")
                if (
                    pid not in self.known
                    and unique.version not in foreign_versions
                    and unique.original_parent_version in foreign_versions
                ):
                    foreign_versions.add(unique.version)
                    changed = True
            if not changed:
                break
        self.unresolved = [
            {
                "pid": pid,
                "birth": unique.unique,
                "original_parent_version": unique.original_parent_version,
            }
            for pid, unique in candidates
            if pid not in self.known and unique.version not in foreign_versions
        ]
        live = []
        for pid, known in self.known.items():
            if pid in self.exited:
                continue
            current = process_identity(pid)
            if (
                current is not None
                and current.birth == known.birth
                and current.state not in ("Z", "X")
            ):
                live.append(current)
        return live

    def kill(self, identity: Process) -> None:
        """Signal a matching watched generation, including detached children."""
        self.census()
        known = self.known.get(identity.pid)
        current = process_identity(identity.pid)
        unique = _unique_info(identity.pid)
        if (
            known is not None
            and known.birth == identity.birth
            and current is not None
            and current.birth == identity.birth
            and unique is not None
            and unique.unique == identity.birth
        ):
            token = (ctypes.c_uint32 * 8)()
            token[5], token[7] = identity.pid, unique.version & 0xFFFFFFFF
            error = self.lib.proc_signal_with_audittoken(token, signal.SIGKILL)
            if error not in (0, errno.ESRCH):
                raise OSError(error, "Darwin generation-checked signal denied")

    def close(self) -> None:
        """Close the native watch descriptor explicitly."""
        self.queue.close()


def _final_darwin_census(tree: _DarwinTree, end: float) -> tuple[list[Process], bool]:
    """Reobserve gap-only snapshots within the existing deadline; never signal."""
    remaining = tree.census()
    while (
        not remaining
        and not tree.uncertain
        and not tree.unresolved
        and tree.forked
        and tree.census_gaps > 0
    ):
        available = end - time.monotonic()
        if available <= 0:
            break
        time.sleep(min(0.005, available))
        if time.monotonic() >= end:
            break
        remaining = tree.census()
    return remaining, bool(
        tree.uncertain or tree.forked and (tree.unresolved or tree.census_gaps)
    )


def _child_pids(pid: int) -> list[int]:
    """Read a bounded native child list, never process environments or secrets."""
    try:
        with Path(f"/proc/{pid}/task/{pid}/children").open() as handle:
            value = handle.read(MAX_CHILDREN * 12 + 1)
    except (FileNotFoundError, ProcessLookupError):
        return []
    if len(value) > MAX_CHILDREN * 12:
        raise RuntimeError("fixture child enumeration exceeds its budget")
    children = [int(item) for item in value.split()]
    if len(children) > MAX_CHILDREN:
        raise RuntimeError("fixture child count exceeds its budget")
    return children


def _process_observation(parent: Process, child: Process) -> dict[str, int | None]:
    """Collect bounded numeric diagnostics, suppressing inspection failures only."""
    observation: dict[str, int | None] = {
        "parent_pid": parent.pid,
        "parent_birth": parent.birth,
        "child_pid": child.pid,
        "child_birth": child.birth,
        "observed_birth": None,
        "observed_parent": None,
        "status_pid": None,
        "status_tgid": None,
        "status_parent": None,
        "after_birth": None,
        "after_parent": None,
        "inspection_failed": 0,
    }
    try:
        observed = process_identity(child.pid)
        if observed is None:
            observation["inspection_failed"] = 1
        else:
            observation["observed_birth"] = observed.birth
            observation["observed_parent"] = observed.parent
    except BaseException:
        observation["inspection_failed"] = 1
    try:
        with Path(f"/proc/{child.pid}/status").open("rb") as handle:
            status = handle.read(8192)
        keys = {b"Pid": "status_pid", b"Tgid": "status_tgid", b"PPid": "status_parent"}
        for line in status.splitlines():
            name, separator, value = line.partition(b":")
            if separator and name in keys:
                observation[keys[name]] = int(value)
        if len(status) == 8192 or any(
            observation[key] is None for key in keys.values()
        ):
            observation["inspection_failed"] = 1
    except BaseException:
        observation["inspection_failed"] = 1
    try:
        after = process_identity(child.pid)
        if after is None:
            observation["inspection_failed"] = 1
        else:
            observation["after_birth"] = after.birth
            observation["after_parent"] = after.parent
    except BaseException:
        observation["inspection_failed"] = 1
    return observation


def _verified_children(
    parent: Process, accept: Callable[[Process, int], None] | None = None
) -> list[Process]:
    """Pin a parent across enumeration and validate each child's birth and PPID."""
    current = process_identity(parent.pid)
    if current is None:
        return []
    if current.birth != parent.birth:
        raise RuntimeError("fixture parent identity changed before enumeration")
    try:
        fd = os.pidfd_open(parent.pid)
    except ProcessLookupError:
        return []
    try:
        children = _child_pids(parent.pid)
        after = process_identity(parent.pid)
        if after is None:
            return []
        if after.birth != parent.birth:
            raise RuntimeError("fixture parent identity changed during enumeration")
        result = []
        for pid in children:
            first = process_identity(pid)
            if first is None or first.parent != parent.pid:
                continue
            try:
                child_fd = os.pidfd_open(pid)
            except ProcessLookupError:
                continue
            except OSError as error:
                if error.errno == errno.EINVAL:
                    error.fixture_process_observation = _process_observation(
                        parent, first
                    )
                raise
            try:
                final_parent = process_identity(parent.pid)
                final_child = process_identity(pid)
                if final_parent is None:
                    break
                if final_parent.birth != parent.birth:
                    raise RuntimeError(
                        "fixture parent identity changed while pinning child"
                    )
                if (
                    final_child is not None
                    and final_child.birth == first.birth
                    and final_child.parent == parent.pid
                ):
                    if accept is not None:
                        accept(final_child, child_fd)
                    result.append(final_child)
            finally:
                os.close(child_fd)
        return result
    finally:
        os.close(fd)


def _supervise(control_fd: int, argv: list[str], lifetime: float) -> int:
    """Admit CLI work only after controller ownership/source checks; own all cleanup."""
    control = socket.socket(fileno=control_fd)
    control.setblocking(False)
    tree = _DarwinTree() if sys.platform == "darwin" else None
    if tree is None:
        lib = ctypes.CDLL(None, use_errno=True)
        if lib.prctl(36, 1, 0, 0, 0) != 0:
            control.close()
            return 125
    root = process_identity(os.getpid())
    child = None
    remaining = []
    uncertain = False
    control.send(
        json.dumps({"event": "ready", "source_sha256": HELPER_SHA256}).encode() + b"\n"
    )
    admission = time.monotonic() + 10
    admitted = False
    while time.monotonic() < admission:
        try:
            message = control.recv(256)
        except BlockingIOError:
            message = None
        if message == b"":
            break
        if message == b"admit\n":
            admitted = True
            break
        time.sleep(0.001)
    if not admitted:
        if tree is not None:
            tree.close()
        control.close()
        return 0
    reported = acknowledged = False
    deadline = time.monotonic() + lifetime
    try:
        if tree is not None:
            read_gate, write_gate = os.pipe()
            try:
                gate = (
                    "import os,sys,json,select; fd=int(sys.argv[1]); "
                    "os.set_blocking(fd,False); "
                    "permit=os.read(fd,1) if select.select([fd],[],[],5)[0] else b''; "
                    "os.close(fd); argv=json.loads(sys.argv[2]); "
                    "os.execvpe(argv[0],argv,os.environ) "
                    "if permit == b'g' else sys.exit(125)"
                )
                child = subprocess.Popen(
                    [
                        sys.executable,
                        "-I",
                        "-c",
                        gate,
                        str(read_gate),
                        json.dumps(argv),
                    ],
                    pass_fds=(read_gate,),
                )
                native = process_identity(child.pid)
                if native is None or native.parent != os.getpid():
                    raise RuntimeError("Darwin CLI admission identity missing")
                tree.add(native)
                os.write(write_gate, b"g")
            finally:
                os.close(read_gate)
                os.close(write_gate)
        else:
            child = subprocess.Popen(argv, close_fds=True)
        while time.monotonic() < deadline:
            if tree is not None:
                tree.census()
            if tree is not None:
                native = process_identity(child.pid)
                if native is not None and native.state == "Z":
                    tree.census()  # Save final exec generation before reaping.
                    code = child.poll()
                else:
                    code = child.returncode
            else:
                code = child.poll()
            if code is not None and not reported:
                control.send(
                    json.dumps(
                        {
                            "event": "returncode",
                            "returncode": code,
                            "source_sha256": HELPER_SHA256,
                        }
                    ).encode()
                    + b"\n"
                )
                os.close(1)
                os.close(2)
                reported = True
            try:
                message = control.recv(256)
            except BlockingIOError:
                message = None
            if message == b"" or message and b"stop" in message:
                break
            if message and b"ack" in message:
                acknowledged = True
            if acknowledged:
                if tree is None:
                    for _ in range(MAX_CHILDREN):
                        try:
                            reaped, _ = os.waitpid(-1, os.WNOHANG)
                        except ChildProcessError:
                            reaped = 0
                        if not reaped:
                            break
                    if not _child_pids(os.getpid()):
                        break
                elif not tree.census():
                    break
            time.sleep(0.001)
    finally:
        # Parent loss/deadline is also bounded, and only verified descendants die.
        end = time.monotonic() + CLEANUP_SECONDS
        while time.monotonic() < end:
            if tree is not None:
                remaining = tree.census()
                for identity in reversed(remaining):
                    tree.kill(identity)
                if child is not None:
                    child.poll()
                if not remaining:
                    break
                time.sleep(0.005)
                continue

            def kill_child(identity: Process, fd: int) -> None:
                """Signal only direct children of this live subreaper."""
                if identity.state not in ("Z", "X"):
                    try:
                        signal.pidfd_send_signal(fd, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

            # Killing direct children reparents their descendants here, so the
            # next bounded pass proves ownership without stale recursive PIDs.
            _verified_children(root, kill_child)
            for _ in range(MAX_CHILDREN):
                try:
                    pid, _ = os.waitpid(-1, os.WNOHANG)
                except ChildProcessError:
                    pid = 0
                if not pid:
                    break
            if not _child_pids(os.getpid()):
                break
            time.sleep(0.01)
        if tree is not None:
            remaining, uncertain = _final_darwin_census(tree, end)
            fork_audit = {
                "forked": sorted(tree.forked),
                "unresolved": tree.unresolved[:64],
                "unresolved_count": len(tree.unresolved),
                "census_gaps": tree.census_gaps,
            }
            observed = [
                {"pid": item.pid, "birth": item.birth} for item in tree.known.values()
            ]
            tree.close()
        else:
            remaining = [
                item
                for item in _verified_children(root)
                if item.state not in ("Z", "X")
            ]
            observed = []
            fork_audit = None
        try:
            control.send(
                json.dumps(
                    {
                        "event": "cleanup",
                        "source_sha256": HELPER_SHA256,
                        "remaining": [
                            {
                                "pid": item.pid,
                                "birth": item.birth,
                                "parent": item.parent,
                                "state": item.state,
                            }
                            for item in remaining
                        ],
                        "ownership_uncertain": uncertain,
                        "observed_darwin": observed[:64],
                        "darwin_fork_audit": fork_audit,
                    }
                ).encode()
                + b"\n"
            )
        except (BlockingIOError, BrokenPipeError):
            pass
        control.close()
    return 125 if remaining or uncertain else 0


@dataclass
class _Job:
    """Owned unreaped supervisor/session leader and its private control channel."""

    child: subprocess.Popen | None
    control: socket.socket | None
    identity: Process | None = None
    remote: socket.socket | None = None
    source_fd: int = -1
    admitted: bool = False
    cleanup_report: dict | None = None
    control_buffer: bytes = b""


@dataclass(frozen=True)
class Process:
    """Linux native identity; birth ticks prevent adoption/signalling after reuse."""

    pid: int
    birth: int
    parent: int
    state: str


def process_identity(pid: int) -> Process | None:
    """Read only native metadata; disappearance is distinct from denied inspection."""
    if sys.platform == "darwin":
        lib = _darwin_lib()
        info = _BSDInfo()
        ctypes.set_errno(0)
        size = lib.proc_pidinfo(pid, 3, 1, ctypes.byref(info), ctypes.sizeof(info))
        if size != ctypes.sizeof(info):
            error = ctypes.get_errno()
            if error == errno.ESRCH:
                return None
            raise OSError(error, "Darwin native process inspection failed")
        unique = _unique_info(pid)
        if unique is None:
            return None
        return Process(pid, unique.unique, info.ppid, "Z" if info.status == 5 else "S")
    try:
        with Path(f"/proc/{pid}/stat").open() as handle:
            raw = handle.read(4097)
        if len(raw) > 4096:
            raise RuntimeError("fixture native identity exceeds its byte budget")
        fields = raw.rsplit(")", 1)[1].split()
    except (FileNotFoundError, ProcessLookupError):
        return None
    return Process(pid, int(fields[19]), int(fields[1]), fields[0])


def peer_address(peer: http.server.HTTPServer) -> tuple[str, int]:
    """Accept only an already-bound IPv4 loopback TCP peer with an exact port."""
    address = peer.server_address
    if (
        peer.socket.family != socket.AF_INET
        or isinstance(peer.socket, ssl.SSLSocket)
        or peer.socket.type != socket.SOCK_STREAM
        or len(address) != 2
        or address[0] != "127.0.0.1"
        or type(address[1]) is not int
        or not 0 < address[1] < 65536
        or peer.socket.getsockname() != address
    ):
        raise ValueError("namespace fixture requires an exact bound loopback HTTP peer")
    return address


class _Listener(http.server.ThreadingHTTPServer):
    """Serve the original handler against its original shared observation object."""

    daemon_threads = True

    def __init__(self, peer: http.server.HTTPServer) -> None:
        self.peer = peer
        self.connections: set[socket.socket] = set()
        self.workers: set[threading.Thread] = set()
        self.connection_lock = threading.Lock()
        self.stopping = threading.Event()
        super().__init__(peer_address(peer), peer.RequestHandlerClass)
        self.timeout = 0.05
        self.socket.settimeout(0.1)

    def serve(self) -> None:
        """Serve with finite accept polling; close never calls unbounded shutdown()."""
        while not self.stopping.is_set():
            self.handle_request()

    def process_request(self, request: socket.socket, address: tuple) -> None:
        """Register every owned socket/worker before dispatch, including close races."""
        worker = threading.Thread(
            target=self.process_request_thread,
            args=(request, address),
            daemon=True,
            name="fixture-peer-handler",
        )
        with self.connection_lock:
            self.connections.add(request)
            self.workers.add(worker)
        try:
            worker.start()
        except BaseException:
            with self.connection_lock:
                self.connections.discard(request)
                self.workers.discard(worker)
            self.shutdown_request(request)
            raise

    def process_request_thread(self, request: socket.socket, address: tuple) -> None:
        """Run a bounded fixture handler and release only its accepted socket."""
        try:
            super().process_request_thread(request, address)
        finally:
            with self.connection_lock:
                self.connections.discard(request)

    def finish_request(self, request: socket.socket, address: tuple) -> None:
        """Keep all scalar/mutable handler observations on the original peer."""
        self.peer.RequestHandlerClass(request, address, self.peer)

    def disconnect(self) -> None:
        """Wake owned socket I/O; the caller still verifies every handler exits."""
        with self.connection_lock:
            for connection in self.connections:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass


class NamespacePeers:
    """Own finite fake-peer listeners and only explicitly dispatched CLI trees.

    Supply existing HTTP server objects, a tools directory beneath the fixture's
    already-mounted workspace, installed plugin roots, and an evidence path.
    Call environment() before dispatch and run() instead of subprocess.run for
    fixture CLI commands. Linux run() owns a private supervisor/subreaper; runner
    announcements only confirm births already proved through owned descent.
    I/O and abort cleanup are bounded. Only the controller thread calls setns,
    restoring it before serving. No plugin bytes or authority are changed.
    """

    def __init__(
        self,
        peers: list[http.server.HTTPServer],
        tools: Path,
        plugin_roots: list[Path],
        receipt: Path,
    ) -> None:
        self.peers = tuple(peers)
        addresses = [peer_address(peer) for peer in peers]
        self.addresses = tuple(addresses)
        if not peers or len(set(addresses)) != len(addresses):
            raise ValueError("namespace fixture peers must be finite and distinct")
        if (
            not tools.is_absolute()
            or not plugin_roots
            or any(not root.is_absolute() for root in plugin_roots)
        ):
            raise ValueError("namespace fixture paths must be explicit absolute paths")
        self.tools, self.plugin_roots, self.receipt = tools, plugin_roots, receipt
        self.shim = tools / "python3"
        self.lock = threading.Lock()
        self.owned: dict[int, int] = {}
        self.pidfds: dict[int, int] = {}
        self.origins: dict[int, int] = {}
        self.jobs: list[_Job] = []
        self.cleanup_reports: list[dict] = []
        self.supervisor_digests: set[str] = set()
        self.handled: set[tuple[int, int]] = set()
        self.listeners: list[tuple[_Listener, threading.Thread]] = []
        self.namespaces: dict[tuple[int, int], int] = {}
        self.events: list[dict] = []
        self.error: BaseException | None = None
        self.stop = threading.Event()
        self.ready = threading.Event()
        self.thread: threading.Thread | None = None
        self.pending: tuple[Process, int] | None = None
        info = os.stat("/proc/self/ns/net") if sys.platform == "linux" else None
        self.caller_netns = (info.st_dev, info.st_ino) if info is not None else None

    def __enter__(self) -> NamespacePeers:
        """Start the native controller; missing setns/pidfd privilege fails, no skip."""
        if SOURCE_BYTES is None:
            raise RuntimeError(
                "fixture dispatch requires an explicitly captured source binding"
            )
        if sys.platform == "darwin":
            return self
        if sys.platform != "linux":
            raise RuntimeError("namespace fixture supports only Linux and Darwin")
        if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
            raise RuntimeError("namespace fixture requires native Linux pidfd support")
        self.lib = ctypes.CDLL(None, use_errno=True)
        self.lib.setns.argtypes = [ctypes.c_int, ctypes.c_int]
        self.lib.setns.restype = ctypes.c_int
        self.tools.mkdir(parents=True, exist_ok=True)
        if self.shim.exists():
            raise ValueError(
                "namespace fixture refuses to replace an existing Python shim"
            )
        # A virtualenv alias may be outside the invocation's mounted paths.
        # Use its real interpreter so env does not silently skip this shim.
        python = str(Path(sys.executable).resolve())
        script = (
            f"#!{python}\nimport os, signal, sys\nfrom pathlib import Path\n"
            f"roots = {[str(root) for root in self.plugin_roots]!r}\n"
            "if len(sys.argv) > 1 and not sys.argv[1].startswith('-'):\n"
            "    target = Path(sys.argv[1]).resolve()\n"
            "    if target.suffix == '.py' and any(\n"
            "        target.is_relative_to(root) for root in roots):\n"
            "        os.kill(os.getpid(), signal.SIGSTOP)\n"
            f"os.execv({python!r}, [{python!r}, *sys.argv[1:]])\n"
        )
        self.shim.write_text(script)
        self.shim.chmod(0o755)
        self.events.append(
            {"event": "shim", "sha256": hashlib.sha256(script.encode()).hexdigest()}
        )
        self.thread = threading.Thread(
            target=self._watch, name="fixture-peer-controller"
        )
        self.thread.start()
        if not self.ready.wait(10):
            self.close()
            raise RuntimeError("namespace fixture controller did not initialize")
        if self.error is not None:
            self.close()
        return self

    def __exit__(
        self, _kind: object, error: BaseException | None, _trace: object
    ) -> None:
        """Close all owned sockets/threads/namespace descriptors and retain receipts."""
        try:
            self.close()
        except BaseException as cleanup:
            if error is None:
                raise
            if hasattr(error, "add_note"):
                error.add_note(f"fixture cleanup also failed: {type(cleanup).__name__}")

    def environment(self, original: dict[str, str]) -> dict[str, str]:
        """Prepend the Linux fixture shim; Darwin's environment stays identical."""
        env = original.copy()
        if sys.platform == "linux":
            env["PATH"] = str(self.tools) + os.pathsep + env.get("PATH", os.defpath)
        return env

    def _pin(self, identity: Process, origin: int, proof_fd: int | None = None) -> None:
        """Pin a previously proven identity; a new current PID occupant is rejected."""
        try:
            fd = (
                os.dup(proof_fd)
                if proof_fd is not None
                else os.pidfd_open(identity.pid)
            )
        except ProcessLookupError:
            return
        try:
            current = process_identity(identity.pid)
            if current is None:
                return
            if current.birth != identity.birth:
                raise RuntimeError("owned fixture PID identity changed while pinning")
            if current.state not in ("Z", "X") and select.select([fd], [], [], 0)[0]:
                after = process_identity(identity.pid)
                if after is None or after.state in ("Z", "X"):
                    return
                raise RuntimeError(
                    "ownership proof process already exited or was reused"
                )
            with self.lock:
                prior = self.owned.get(identity.pid)
                if prior is not None and prior != identity.birth:
                    raise RuntimeError("owned fixture PID identity changed")
                if identity.pid not in self.pidfds:
                    self.pidfds[identity.pid] = fd
                    fd = -1
                self.owned[identity.pid] = identity.birth
                self.origins[identity.pid] = origin
        finally:
            if fd >= 0:
                os.close(fd)

    def _register(self, pid: int, kind: str) -> None:
        """Confirm only a birth already observed through a proven owned descent."""
        with self.lock:
            birth = self.owned.get(pid)
        if birth is None:
            raise RuntimeError("runner ownership was not previously observed")
        identity = process_identity(pid)
        if identity is not None and identity.birth != birth:
            raise RuntimeError("owned fixture PID identity changed")
        if (
            identity is not None
            and identity.state not in ("Z", "X")
            and self._pinned_exited(pid)
        ):
            after = process_identity(pid)
            if after is not None and after.state not in ("Z", "X"):
                raise RuntimeError("runner ownership proof exited or was reused")
        self.events.append(
            {"event": kind, "pid": pid, "birth": birth, "proof": "observed-descent"}
        )

    def _pinned_exited(self, pid: int) -> bool:
        """Query the already-proven pidfd, never repin a current numeric occupant."""
        with self.lock:
            fd = self.pidfds.get(pid)
            if fd is None:
                return True
            stable = os.dup(fd)
        try:
            return bool(select.select([stable], [], [], 0)[0])
        finally:
            os.close(stable)

    def _signal_owned(self, identity: Process, sig: int) -> None:
        """A pidfd stabilizes signalling only after ownership and birth were proved."""
        with self.lock:
            birth, fd = self.owned.get(identity.pid), self.pidfds.get(identity.pid)
            if birth != identity.birth or fd is None:
                raise RuntimeError("fixture signal lacks pinned ownership proof")
            stable = os.dup(fd)
        try:
            current = process_identity(identity.pid)
            if current is not None and current.birth != birth:
                raise RuntimeError("fixture signal target birth changed")
            if current is not None:
                try:
                    signal.pidfd_send_signal(stable, sig)
                except ProcessLookupError:
                    pass
        finally:
            os.close(stable)

    def _clean_job(self, job: _Job, deadline: float) -> list[Process]:
        """Bound cleanup and require the native owner's observed result."""
        if job.child is None:
            return []
        if not job.admitted:
            # No real CLI can have run. This is our unreaped Popen child, not a
            # numeric lookup; EOF releases admission even if identity inspection failed.
            if job.control is not None:
                job.control.close()
            try:
                job.child.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                job.child.kill()
                job.child.wait(timeout=0.1)
            return []
        if sys.platform == "linux":
            while time.monotonic() < deadline:
                live = [
                    item
                    for item in self._owned_processes()
                    if self.origins.get(item.pid) == job.child.pid
                    and item.pid != job.child.pid
                    and item.state not in ("Z", "X")
                ]
                for identity in reversed(live):
                    self._signal_owned(identity, signal.SIGKILL)
                if not live:
                    break
                time.sleep(0.01)
        if job.control is not None and job.control.fileno() >= 0:
            try:
                job.control.send(b"stop\n")
            except (BlockingIOError, BrokenPipeError):
                pass
            while job.cleanup_report is None and time.monotonic() < deadline:
                for message in self._read_control(job):
                    self._accept_control(job, message)
                if job.cleanup_report is None:
                    time.sleep(0.005)
        try:
            job.child.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            if sys.platform == "linux" and job.identity is not None:
                self._signal_owned(job.identity, signal.SIGKILL)
            else:
                job.child.kill()  # Still our unreaped direct child, never a bare PID.
            try:
                job.child.wait(timeout=0.1)
            except subprocess.TimeoutExpired:
                pass
        if sys.platform == "linux":
            return [
                item
                for item in self._owned_processes()
                if self.origins.get(item.pid) == job.child.pid
                and item.state not in ("Z", "X")
            ]
        if job.cleanup_report is None:
            raise RuntimeError("Darwin native owner did not report observed cleanup")
        if job.cleanup_report.get("ownership_uncertain"):
            raise RuntimeError("Darwin descendant ownership could not be verified")
        return [Process(**item) for item in job.cleanup_report["remaining"]]

    def _read_control(self, job: _Job) -> list[dict]:
        """Read finite private protocol frames, never blocking on EOF."""
        if job.control is None or job.control.fileno() < 0:
            return []
        try:
            chunk = job.control.recv(16384)
        except (BlockingIOError, ConnectionResetError):
            return []
        job.control_buffer += chunk
        if len(job.control_buffer) > 16384:
            raise RuntimeError("fixture control byte budget exceeded")
        messages = []
        while b"\n" in job.control_buffer:
            line, job.control_buffer = job.control_buffer.split(b"\n", 1)
            messages.append(json.loads(line))
        return messages

    def _accept_control(self, job: _Job, message: dict) -> None:
        """Bind every supervisor report to the exact captured controller source."""
        if message.get("source_sha256") != HELPER_SHA256:
            raise RuntimeError("controller/supervisor source identity mismatch")
        self.supervisor_digests.add(message["source_sha256"])
        if message.get("event") == "cleanup":
            job.cleanup_report = message
            self.cleanup_reports.append(message)

    def _admit(self, job: _Job, deadline: float) -> None:
        """Admit only an owned supervisor running the expected captured bytes."""
        while time.monotonic() < deadline:
            for message in self._read_control(job):
                self._accept_control(job, message)
                if message.get("event") == "ready":
                    job.control.send(b"admit\n")
                    job.admitted = True
                    return
            time.sleep(0.001)
        raise RuntimeError("fixture source/admission handshake exceeded deadline")

    def _collect_finished_jobs(self) -> None:
        """Reap completed supervisors nonblockingly; do not accumulate CLI pidfds."""
        pending = []
        for job in self.jobs:
            if job.child is None:
                raise RuntimeError("unfinished fixture launch admission")
            if job.child.poll() is None:
                pending.append(job)
                continue
            if job.control is not None and (
                job.child.returncode != 0
                or any(
                    self.origins.get(item.pid) == job.child.pid
                    and item.state not in ("Z", "X")
                    for item in self._owned_processes()
                )
            ):
                self.error = self.error or RuntimeError(
                    "fixture supervisor exited before owned cleanup"
                )
                pending.append(job)
                continue
            if job.control is not None:
                for message in self._read_control(job):
                    self._accept_control(job, message)
                if job.admitted and (
                    job.cleanup_report is None
                    or job.cleanup_report.get("ownership_uncertain")
                    or job.cleanup_report.get("remaining")
                ):
                    self.error = self.error or RuntimeError(
                        "native fixture cleanup proof missing"
                    )
                    pending.append(job)
                    continue
                job.control.close()
            with self.lock:
                retired = [
                    pid
                    for pid, origin in self.origins.items()
                    if origin == job.child.pid
                ]
                for pid in retired:
                    fd = self.pidfds.pop(pid, None)
                    if fd is not None:
                        os.close(fd)
                    self.owned.pop(pid, None)
                    self.origins.pop(pid, None)
        self.jobs = pending

    def _pump(
        self, job: _Job, args: list[str], data: str | bytes | None, timeout: float
    ) -> tuple[Any, Any, int]:
        """Nonblocking finite I/O; inherited pipe writers remain subject to deadline."""
        child = job.child
        deadline = time.monotonic() + timeout
        captured = {"stdout": bytearray(), "stderr": bytearray()}
        pending = (
            data.encode(child.stdin.encoding or "utf-8")
            if isinstance(data, str)
            else (data or b"")
        )
        code = None
        with selectors.DefaultSelector() as selector:
            for name in ("stdout", "stderr"):
                pipe = getattr(child, name)
                if pipe is not None:
                    os.set_blocking(pipe.fileno(), False)
                    selector.register(pipe, selectors.EVENT_READ, name)
            if child.stdin is not None:
                os.set_blocking(child.stdin.fileno(), False)
                if pending:
                    selector.register(child.stdin, selectors.EVENT_WRITE, "stdin")
                else:
                    child.stdin.close()
            if job.control is not None:
                selector.register(job.control, selectors.EVENT_READ, "control")
            while True:
                if sys.platform == "linux":
                    self._owned_processes()
                    self.check()
                active_output = any(
                    key.data in ("stdout", "stderr", "stdin")
                    for key in selector.get_map().values()
                )
                if not active_output and (code is not None or job.control is None):
                    if job.control is None:
                        try:
                            code = child.wait(
                                timeout=min(0.02, max(0, deadline - time.monotonic()))
                            )
                        except subprocess.TimeoutExpired:
                            code = None
                    if code is not None:
                        break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(
                        args,
                        timeout,
                        output=bytes(captured["stdout"]),
                        stderr=bytes(captured["stderr"]),
                    )
                for key, _ in selector.select(min(0.01, remaining)):
                    if key.data == "stdin":
                        try:
                            used = os.write(key.fd, pending[:16384])
                            pending = pending[used:]
                        except BrokenPipeError:
                            pending = b""
                        if not pending:
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                        continue
                    try:
                        chunk = os.read(key.fd, 65536)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(key.fileobj)
                        if key.data == "control" and code is None:
                            raise RuntimeError(
                                "fixture supervisor exited without CLI status"
                            )
                        continue
                    if key.data == "control":
                        job.control_buffer += chunk
                        if len(job.control_buffer) > 16384:
                            raise RuntimeError(
                                "fixture supervisor control budget exceeded"
                            )
                        while b"\n" in job.control_buffer:
                            line, job.control_buffer = job.control_buffer.split(
                                b"\n", 1
                            )
                            message = json.loads(line)
                            self._accept_control(job, message)
                            if message.get("event") == "returncode":
                                code = message["returncode"]
                    else:
                        captured[key.data].extend(chunk)
                        if sum(map(len, captured.values())) > MAX_CAPTURE_BYTES:
                            raise RuntimeError("fixture CLI capture budget exceeded")
        result = []
        for name in ("stdout", "stderr"):
            pipe = getattr(child, name)
            raw = bytes(captured[name])
            if pipe is None:
                result.append(None)
            elif child.text_mode:
                result.append(
                    raw.decode(pipe.encoding or "utf-8", errors=pipe.errors or "strict")
                    .replace("\r\n", "\n")
                    .replace("\r", "\n")
                )
            else:
                result.append(raw)
        return result[0], result[1], code

    def run(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        """Dispatch with bounded I/O/cleanup, preserving timeout/interruption errors.

        Linux uses a fixture-only subreaper; numeric runner announcements merely
        confirm already-pinned descent. Darwin retains argv/env/peer semantics,
        with a private outer CLI session solely for bounded abort cleanup.
        """
        if SOURCE_BYTES is None:
            raise RuntimeError(
                "fixture dispatch requires captured source bytes, "
                "not a plain/pyc import"
            )
        self._collect_finished_jobs()
        self.check()
        if len(self.jobs) >= 256:
            raise RuntimeError("fixture concurrent CLI budget exceeded")
        timeout, check, data = (
            kwargs.pop("timeout", None),
            kwargs.pop("check", False),
            kwargs.pop("input", None),
        )
        timeout = 60.0 if timeout is None else float(timeout)
        if not 0 < timeout < 1800:
            raise ValueError("fixture CLI timeout must be finite and below 30 minutes")
        if (
            kwargs.pop("start_new_session", True) is not True
            or kwargs.get("preexec_fn") is not None
        ):
            raise ValueError("fixture CLI requires its own safe outer session")
        if kwargs.pop("capture_output", False):
            kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if data is not None:
            kwargs["stdin"] = subprocess.PIPE
        # Publish ownership of every launch resource before Popen or any native
        # inspection. A retained exception traceback may retain objects, never FDs.
        job = _Job(None, None)
        self.jobs.append(job)
        child = None
        try:
            job.source_fd = _source_snapshot(SOURCE_BYTES, self.receipt.parent)
            job.control, job.remote = socket.socketpair()
            job.control.setblocking(False)
            command = [
                sys.executable,
                "-I",
                "-B",
                "-c",
                _BOOTSTRAP,
                str(job.source_fd),
                HELPER_SHA256,
                str(__file__),
                str(job.remote.fileno()),
                "1800",
                json.dumps(args),
            ]
            kwargs["pass_fds"] = (job.remote.fileno(), job.source_fd)
            job.child = subprocess.Popen(command, start_new_session=True, **kwargs)
            child = job.child
            job.remote.close()
            os.close(job.source_fd)
            job.source_fd = -1
            identity = process_identity(child.pid)
            job.identity = identity
            if identity is None or identity.parent != os.getpid():
                raise RuntimeError("fixture supervisor lacks direct-child ownership")
            if sys.platform == "linux":
                self._pin(identity, identity.pid)
            self.events.append(
                {
                    "event": "cli-supervisor",
                    "pid": identity.pid,
                    "birth": identity.birth,
                }
            )
            self._admit(job, time.monotonic() + min(timeout, 5))
            stdout, stderr, code = self._pump(job, args, data, timeout)
            result = subprocess.CompletedProcess(args, code, stdout, stderr)
            if sys.platform == "linux":
                # Supervisor retains orphan/zombie runners until this proof/ACK.
                self._owned_processes()
                try:
                    value = json.loads(stdout or "null")
                except (ValueError, TypeError):
                    value = None
                if isinstance(value, dict) and type(value.get("runner_pid")) is int:
                    self._register(value["runner_pid"], "runner")
            job.control.send(b"ack\n")
            self.check()
            if check:
                result.check_returncode()
            return result
        except BaseException as original:
            try:
                live = self._clean_job(job, time.monotonic() + CLEANUP_SECONDS)
                self.events.append(
                    {
                        "event": "abort",
                        "reason": type(original).__name__,
                        "remaining": [
                            {"pid": item.pid, "birth": item.birth} for item in live
                        ],
                    }
                )
                if live:
                    raise RuntimeError(
                        "owned fixture pipe holders survived bounded cleanup"
                    )
            except BaseException as cleanup:
                self.error = self.error or cleanup
                if hasattr(original, "add_note"):
                    original.add_note(
                        f"fixture cleanup failed: {type(cleanup).__name__}"
                    )
            raise
        finally:
            if job.remote is not None:
                job.remote.close()
            if job.source_fd >= 0:
                os.close(job.source_fd)
                job.source_fd = -1
            actual = job.child
            if actual is not None:
                for name in ("stdin", "stdout", "stderr"):
                    pipe = getattr(actual, name)
                    if pipe is not None and not pipe.closed:
                        pipe.close()
            if not job.admitted:
                if job.control is not None:
                    job.control.close()
                if actual is None or actual.returncode is not None:
                    self.jobs.remove(job)

    def check(self) -> None:
        """Surface any controller failure on the fixture's calling thread."""
        if self.error is not None:
            raise RuntimeError(
                "native namespace fake-peer setup failed"
            ) from self.error

    def _setns(self, fd: int) -> None:
        if self.lib.setns(fd, 0x40000000) != 0:  # Linux CLONE_NEWNET
            raise OSError(ctypes.get_errno(), "fixture network namespace join denied")

    def _owned_processes(self) -> list[Process]:
        with self.lock:
            pending = list(self.owned.items())
        result = []
        seen = set()
        while pending:
            if len(seen) >= MAX_CHILDREN:
                raise RuntimeError("owned fixture census exceeds its process budget")
            pid, birth = pending.pop()
            if (pid, birth) in seen:
                continue
            seen.add((pid, birth))
            identity = process_identity(pid)
            if identity is None or identity.birth != birth:
                with self.lock:
                    fd = self.pidfds.pop(pid, None)
                if fd is not None:
                    os.close(fd)
                continue
            result.append(identity)
            if identity.state in ("Z", "X"):
                continue
            if self._pinned_exited(pid):
                after = process_identity(pid)
                if after is None or after.state in ("Z", "X"):
                    continue
                raise RuntimeError("owned fixture parent identity exited or changed")
            origin = self.origins.get(pid, pid)
            for info in _verified_children(
                identity, lambda item, fd: self._pin(item, origin, fd)
            ):
                pending.append((info.pid, info.birth))
        return result

    def _is_stopped_shim(self, identity: Process) -> bool:
        if identity.state != "T" or (identity.pid, identity.birth) in self.handled:
            return False
        try:
            with Path(f"/proc/{identity.pid}/cmdline").open("rb") as handle:
                args = handle.read(8192).split(b"\0")
        except (FileNotFoundError, ProcessLookupError):
            return False
        if len(args) < 3 or args[1] != os.fsencode(self.shim):
            return False
        target = Path(os.fsdecode(args[2])).resolve()
        return target.suffix == ".py" and any(
            target.is_relative_to(root) for root in self.plugin_roots
        )

    def _attach(self, identity: Process, host: int) -> None:
        if self.owned.get(identity.pid) != identity.birth:
            raise RuntimeError("namespace attachment lacks observed ownership")
        pidfd = os.pidfd_open(identity.pid)
        self.pending = (identity, pidfd)
        namespace = -1
        continued = False
        try:
            current = process_identity(identity.pid)
            if (
                current is None
                or current.birth != identity.birth
                or current.state != "T"
            ):
                raise RuntimeError(
                    "stopped fixture identity changed before namespace bind"
                )
            namespace = os.open(
                f"/proc/{identity.pid}/ns/net", os.O_RDONLY | os.O_CLOEXEC
            )
            info = os.fstat(namespace)
            current = process_identity(identity.pid)
            if current is None or current.birth != identity.birth:
                raise RuntimeError("fixture identity changed while pinning namespace")
            key = (info.st_dev, info.st_ino)
            host_info = os.fstat(host)
            host_key = (host_info.st_dev, host_info.st_ino)
            if key != host_key and key not in self.namespaces:
                if len(self.namespaces) >= 256:
                    raise RuntimeError("fixture namespace listener budget exhausted")
                self._setns(namespace)
                created = []
                try:
                    # Read only: the runtime alone controls loopback's state.
                    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                        flags = fcntl.ioctl(
                            probe.fileno(), 0x8913, struct.pack("16sH", b"lo", 0)
                        )
                    loopback_up = bool(struct.unpack_from("H", flags, 16)[0] & 1)
                    for peer, address in zip(self.peers, self.addresses):
                        if peer_address(peer) != address:
                            raise RuntimeError(
                                "registered fixture peer address changed"
                            )
                        listener = _Listener(peer)
                        self.listeners.append(
                            (
                                listener,
                                threading.Thread(
                                    target=listener.serve,
                                    name="fixture-namespace-peer",
                                ),
                            )
                        )
                        created.append(self.listeners[-1])
                finally:
                    self._setns(host)
                restored = os.stat("/proc/thread-self/ns/net")
                if (restored.st_dev, restored.st_ino) != host_key:
                    raise RuntimeError(
                        "controller did not restore its original namespace"
                    )
                self.events.append(
                    {"event": "controller-restored", "netns": restored.st_ino}
                )
                self.namespaces[key] = namespace
                namespace = -1
                for _, thread in created:
                    thread.start()
                self.events.append(
                    {
                        "event": "bound",
                        "pid": identity.pid,
                        "birth": identity.birth,
                        "netns": key[1],
                        "host_netns": host_key[1],
                        "ports": [peer.server_port for peer in self.peers],
                        "loopback_up": loopback_up,
                    }
                )
            current = process_identity(identity.pid)
            if current is None or current.birth != identity.birth:
                raise RuntimeError("fixture identity changed before continuation")
            signal.pidfd_send_signal(pidfd, signal.SIGCONT)
            continued = True
            self.handled.add((identity.pid, identity.birth))
            self.events.append(
                {
                    "event": "continued",
                    "pid": identity.pid,
                    "birth": identity.birth,
                    "netns": key[1],
                }
            )
        finally:
            if not continued:
                current = process_identity(identity.pid)
                if current is not None and current.birth == identity.birth:
                    try:
                        signal.pidfd_send_signal(pidfd, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            if namespace >= 0:
                os.close(namespace)
            os.close(pidfd)
            self.pending = None

    def _watch(self) -> None:
        host = None
        try:
            host = os.open("/proc/thread-self/ns/net", os.O_RDONLY | os.O_CLOEXEC)
            self._setns(host)  # Prove real setns privilege before any dispatch.
            self.ready.set()
            while not self.stop.wait(0.005):
                for identity in self._owned_processes():
                    if self._is_stopped_shim(identity):
                        self._attach(identity, host)
        except BaseException as error:
            self.error = error
            self.ready.set()
        finally:
            if host is not None:
                try:
                    self._setns(host)
                except BaseException as error:
                    self.error = self.error or error
                finally:
                    os.close(host)

    def close(self) -> None:
        """Release only owned resources; uncertain cleanup fails instead of skipping."""
        deadline = time.monotonic() + CLEANUP_SECONDS
        self.stop.set()
        cleanup_errors = []
        workers_remaining = 0
        if self.thread is not None:
            self.thread.join(max(0, deadline - time.monotonic()))
            if self.thread.is_alive():
                cleanup_errors.append("controller did not stop")
            # Never leave an owned pre-exec shim stopped after a setup failure.
            for identity in self._owned_processes():
                if self._is_stopped_shim(identity):
                    try:
                        fd = os.pidfd_open(identity.pid)
                    except ProcessLookupError:
                        continue
                    try:
                        current = process_identity(identity.pid)
                        if current is not None and current.birth == identity.birth:
                            signal.pidfd_send_signal(fd, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    finally:
                        os.close(fd)
        for job in self.jobs:
            try:
                live_job = self._clean_job(job, deadline)
                if live_job:
                    cleanup_errors.append("owned CLI descendants survived cleanup")
            except BaseException:
                cleanup_errors.append("owned CLI cleanup failed")
            finally:
                if job.control is not None:
                    job.control.close()
        for listener, thread in self.listeners:
            try:
                listener.stopping.set()
                listener.disconnect()
                if thread.ident is not None:
                    thread.join(max(0, deadline - time.monotonic()))
                # Bypass ThreadingMixIn.server_close's implicit unbounded join.
                listener.socket.close()
            except OSError:
                cleanup_errors.append("listener close failed")
            with listener.connection_lock:
                workers = list(listener.workers)
            for worker in workers:
                worker.join(max(0, deadline - time.monotonic()))
                if worker.is_alive():
                    workers_remaining += 1
                    cleanup_errors.append("handler did not stop")
            if thread.ident is not None:
                thread.join(max(0, deadline - time.monotonic()))
                if thread.is_alive():
                    cleanup_errors.append("listener did not stop")
        for fd in self.namespaces.values():
            os.close(fd)
        listeners_remaining = sum(thread.is_alive() for _, thread in self.listeners)
        self.listeners.clear()
        self.namespaces.clear()
        info = os.stat("/proc/self/ns/net") if sys.platform == "linux" else None
        caller_unchanged = (
            info is None or (info.st_dev, info.st_ino) == self.caller_netns
        )
        if not caller_unchanged:
            cleanup_errors.append("caller namespace changed")
        live = []
        if sys.platform == "linux":
            while True:
                live = [
                    item
                    for item in self._owned_processes()
                    if item.state not in ("Z", "X")
                ]
                if not live or time.monotonic() >= deadline:
                    break
                time.sleep(0.025)
            if live:
                cleanup_errors.append("owned processes did not settle")
        else:
            for job in self.jobs:
                if job.admitted:
                    if job.cleanup_report is None or job.cleanup_report.get(
                        "ownership_uncertain"
                    ):
                        cleanup_errors.append(
                            "Darwin native cleanup observation missing or uncertain"
                        )
                    elif job.cleanup_report.get("remaining"):
                        live.extend(
                            Process(**item) for item in job.cleanup_report["remaining"]
                        )
        for fd in self.pidfds.values():
            os.close(fd)
        self.pidfds.clear()
        self.receipt.write_text(
            json.dumps(
                {
                    "helper_sha256": HELPER_SHA256,
                    "controller_source_sha256": HELPER_SHA256,
                    "supervisor_source_sha256s": sorted(self.supervisor_digests),
                    "native_cleanup_reports": self.cleanup_reports,
                    "events": self.events,
                    "error": type(self.error).__name__ if self.error else None,
                    "cleanup_errors": cleanup_errors,
                    "cleanup_confirmed": not cleanup_errors
                    and self.error is None
                    and not live,
                    "handlers_remaining": workers_remaining,
                    "listeners_remaining": listeners_remaining,
                    "namespace_fds_remaining": len(self.namespaces),
                    "pidfds_remaining": len(self.pidfds),
                    "control_fds_remaining": sum(
                        job.control is not None and job.control.fileno() >= 0
                        for job in self.jobs
                    ),
                    "source_fds_remaining": sum(
                        job.source_fd >= 0 for job in self.jobs
                    ),
                    "pending_jobs": sum(
                        job.child is None or job.child.returncode is None
                        for job in self.jobs
                    ),
                    "controller_alive": bool(self.thread and self.thread.is_alive()),
                    "caller_namespace_unchanged": caller_unchanged,
                    "owned_live_processes": [
                        {"pid": item.pid, "birth": item.birth} for item in live
                    ],
                },
                indent=2,
            )
            + "\n"
        )
        if cleanup_errors:
            raise RuntimeError("namespace fixture cleanup could not be confirmed")
        self.check()


if __name__ == "__main__":
    if (
        len(sys.argv) != 5
        or sys.argv[1] != "--fixture-supervise"
        or sys.platform not in ("linux", "darwin")
    ):
        raise SystemExit("test-only fixture supervisor: invalid invocation")
    raise SystemExit(
        _supervise(int(sys.argv[2]), json.loads(sys.argv[4]), float(sys.argv[3]))
    )

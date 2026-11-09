"""Ownership core copied from companion cursor.cli/process_lifecycle.py.

Native metadata only; no argv/environment observation, sessions, or group kills.
The runtime owns group isolation. Preexisting members and ancestors are protected.
"""

from __future__ import annotations

import ctypes
import errno
import os
import signal
import sys
import time
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Identity:
    """Native process identity, parent, group, and liveness snapshot."""

    pid: int
    birth: tuple[int, ...]
    ppid: int
    pgid: int
    state: int | str

    @property
    def live(self) -> bool:
        """Exclude exited/zombie processes, which cannot execute or hold pipes."""
        return self.state not in ("Z", "X", 5)


class BSDInfo(ctypes.Structure):
    """Darwin proc_bsdinfo ABI (PROC_PIDTBSDINFO), including birth time."""

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


class NativeProcesses:
    """Read process identities and signal verified members on Linux or Darwin."""

    def __init__(self) -> None:
        self.lib = None
        if sys.platform == "darwin":
            self.lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            self.lib.proc_pidinfo.argtypes = [
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_uint64,
                ctypes.c_void_p,
                ctypes.c_int,
            ]
            self.lib.proc_pidinfo.restype = ctypes.c_int
            self.lib.proc_listpids.argtypes = [
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_void_p,
                ctypes.c_int,
            ]
            self.lib.proc_listpids.restype = ctypes.c_int
        elif sys.platform != "linux":
            raise OSError("unsupported process observation platform")

    def read(self, pid: int) -> Identity | None:
        """Return native metadata, None only for confirmed disappearance."""
        if self.lib is not None:
            info = BSDInfo()
            ctypes.set_errno(0)
            size = self.lib.proc_pidinfo(
                pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info)
            )
            if size != ctypes.sizeof(info):
                error = ctypes.get_errno()
                if error == errno.ESRCH:
                    return None
                raise OSError(error, "process metadata unavailable")
            return Identity(
                pid,
                (info.start_sec, info.start_usec),
                info.ppid,
                info.pgid,
                info.status,
            )
        try:
            with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
                fields = handle.read().rsplit(")", 1)[1].split()
            return Identity(
                pid, (int(fields[19]),), int(fields[1]), int(fields[2]), fields[0]
            )
        except FileNotFoundError:
            return None

    def members(self, pgid: int) -> list[Identity]:
        """Enumerate group members; uncertain/denied observation raises."""
        if self.lib is None:
            pids = [int(name) for name in os.listdir("/proc") if name.isdecimal()]
        else:
            capacity = 256
            while True:
                buffer = (ctypes.c_int * capacity)()
                size = self.lib.proc_listpids(2, pgid, buffer, ctypes.sizeof(buffer))
                if size <= 0:
                    raise OSError("process enumeration unavailable")
                if size < ctypes.sizeof(buffer):
                    pids = list(buffer)[: size // ctypes.sizeof(ctypes.c_int)]
                    break
                capacity *= 2
                if capacity > 1048576:
                    raise OSError("process enumeration incomplete")
        result = []
        for pid in pids:
            if pid <= 0:
                continue
            info = self.read(pid)
            if info is not None and info.pgid == pgid:
                result.append(info)
        return result

    def ancestor(self, pid: int) -> tuple[int, tuple[int, ...] | None]:
        """Read ancestors; denied Darwin birth protects PID unconditionally."""
        try:
            info = self.read(pid)
        except PermissionError:
            if self.lib is None:
                raise
            buffer = (ctypes.c_uint32 * 16)()
            size = self.lib.proc_pidinfo(pid, 13, 0, buffer, ctypes.sizeof(buffer))
            if size != ctypes.sizeof(buffer) or buffer[0] != pid:
                raise OSError("ancestor observation unavailable")
            return int(buffer[1]), None
        if info is None:
            raise OSError("ancestor observation unavailable")
        return info.ppid, info.birth

    def send(self, expected: Identity, sig: int) -> bool:
        """Signal matching identity/group only; Linux pins it with a pidfd."""
        fd = None
        try:
            if self.lib is None:
                fd = os.pidfd_open(expected.pid)
            current = self.read(expected.pid)
            if current is None:
                return True
            if current.birth != expected.birth or current.pgid != expected.pgid:
                return False
            if not current.live:
                return True
            if fd is not None:
                signal.pidfd_send_signal(fd, sig)
            else:
                os.kill(expected.pid, sig)
            return True
        except ProcessLookupError:
            return True
        except (OSError, AttributeError):
            return False
        finally:
            if fd is not None:
                os.close(fd)


class ProcessLifecycle:
    """Protect pre-launch ancestors/members and finalize the inherited group."""

    def __init__(self) -> None:
        self.native = NativeProcesses()
        self.pgid = os.getpgrp()
        self.protected = {}
        self.owned = {}
        self.confirmed = True
        pid = os.getpid()
        while pid:
            if pid in self.protected:
                raise OSError("ancestor observation incomplete")
            if pid == 1:
                self.protected[pid] = None
                break
            parent, birth = self.native.ancestor(pid)
            self.protected[pid] = birth
            pid = parent
        for info in self.native.members(self.pgid):
            self.protected[info.pid] = info.birth

    def remaining(self) -> list[Identity]:
        """Rescan owned live members, rejecting PID reuse and observation errors."""
        result = []
        for info in self.native.members(self.pgid):
            if info.pid in self.protected:
                if (
                    self.protected[info.pid] is not None
                    and info.birth != self.protected[info.pid]
                ):
                    self.confirmed = False
                continue
            birth = self.owned.setdefault(info.pid, info.birth)
            if birth != info.birth:
                self.confirmed = False
                continue
            if info.live:
                result.append(info)
        return result

    def cleanup(self, tick: Callable[[], None]) -> bool:
        """TERM then KILL with two total two-second budgets; rescan for proof."""
        try:
            total_deadline = time.monotonic() + 4
            for sig in (signal.SIGTERM, signal.SIGKILL):
                deadline = total_deadline - (2 if sig == signal.SIGTERM else 0)
                sent = set()
                while True:
                    members = self.remaining()
                    if not members:
                        return self.confirmed
                    for info in members:
                        key = (info.pid, info.birth)
                        if key not in sent:
                            self.confirmed &= self.native.send(info, sig)
                            sent.add(key)
                    tick()
                    left = deadline - time.monotonic()
                    if left <= 0:
                        break
                    time.sleep(min(0.025, left))
            return not self.remaining() and self.confirmed
        except Exception:
            return False

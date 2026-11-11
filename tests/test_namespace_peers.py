"""Finite peer/ownership contract units; native namespace execution is installed."""

from __future__ import annotations

import http.server
from http.server import ThreadingHTTPServer
import ctypes
import errno
import json
import hashlib
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

import namespace_peers as _namespace_loader

namespace_peers = _namespace_loader.load_file(Path(_namespace_loader.__file__))
NamespacePeers = namespace_peers.NamespacePeers
Process = namespace_peers.Process
peer_address = namespace_peers.peer_address
process_identity = namespace_peers.process_identity


def _reaped_intermediate(
    source: bytes, root: Path, mode: str, expect_uncertain: bool
) -> dict:
    """Block native census until a proved intermediate has forked and been reaped.

    The outer fixture pins its own complete live chain before releasing the
    intermediate. Its independent generation-checked reclaimer never lends that
    proof to the helper, which must report its own observation gap honestly.
    """
    root.mkdir()
    blocked, reaped, censused, pinned, spawned = (
        root / name for name in ("blocked", "reaped", "censused", "pinned", "spawned")
    )
    injection = f"""
_original_census = _DarwinTree.census
def _barrier_census(self):
    if not Path({str(blocked)!r}).exists():
        Path({str(blocked)!r}).touch()
        end = time.monotonic() + 2
        while not Path({str(reaped)!r}).exists():
            if time.monotonic() >= end:
                raise RuntimeError('reaped-intermediate barrier deadline')
            time.sleep(.001)
    result = _original_census(self)
    Path({str(censused)!r}).touch()
    return result
_DarwinTree.census = _barrier_census
"""
    instrumented = source.replace(
        b'if __name__ == "__main__":',
        injection.encode() + b'\nif __name__ == "__main__":',
        1,
    )
    assert instrumented != source
    digest = hashlib.sha256(instrumented).hexdigest()
    loaded = namespace_peers.load_from_source(
        instrumented, digest, str(root / "barrier-helper.py")
    )
    # An observed sibling makes NOTE_FORK coalescing explicit: matching one
    # parent edge must not discharge the invisible intermediate's fork as well.
    program = f"""
import json, os, subprocess, sys, time
from pathlib import Path
end = time.monotonic() + 2
while not Path({str(blocked)!r}).exists():
    if time.monotonic() >= end: raise RuntimeError('census was not blocked')
    time.sleep(.001)
sibling = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(6)'])
cli = os.getpid()
intermediate = os.fork()
if intermediate == 0:
    writer = subprocess.Popen(
        [sys.executable, '-c', 'import time; time.sleep(6)'], start_new_session=True)
    Path({str(spawned) + ".tmp"!r}).write_text(json.dumps(
        {{'cli':cli, 'intermediate':os.getpid(), 'writer':writer.pid,
          'sibling':sibling.pid}}))
    os.replace({str(spawned) + ".tmp"!r}, {str(spawned)!r})
    end = time.monotonic() + 2
    while not Path({str(pinned)!r}).exists():
        if time.monotonic() >= end: os._exit(125)
        time.sleep(.001)
    os._exit(0)
_, status = os.waitpid(intermediate, 0)
assert status == 0
Path({str(reaped)!r}).touch()
time.sleep(6)
"""
    owned: dict[str, tuple[Process, int]] = {}
    errors: list[BaseException] = []
    completed = threading.Event()
    with ThreadingHTTPServer(
        ("127.0.0.1", 0), http.server.BaseHTTPRequestHandler
    ) as peer:
        controller = loaded.NamespacePeers(
            [peer], root / "tools", [root / "plugins"], root / "proof.json"
        )

        def coordinate() -> None:
            """Independently prove live descent while all ancestors await our ACK."""
            try:
                end = time.monotonic() + 2
                while not spawned.exists():
                    if time.monotonic() >= end:
                        raise AssertionError("fixture did not reach live-chain barrier")
                    time.sleep(0.001)
                values = json.loads(spawned.read_text())
                supervisor = controller.jobs[0].identity
                assert supervisor is not None
                parents = {
                    "cli": supervisor.pid,
                    "sibling": values["cli"],
                    "intermediate": values["cli"],
                    "writer": values["intermediate"],
                }
                for name, parent in parents.items():
                    child = process_identity(values[name])
                    ancestor = process_identity(parent)
                    unique = namespace_peers._unique_info(values[name])
                    parent_unique = namespace_peers._unique_info(parent)
                    final = process_identity(values[name])
                    final_parent = process_identity(parent)
                    assert child is not None and child.parent == parent
                    assert ancestor is not None and final_parent == ancestor
                    assert final == child and unique.unique == child.birth
                    assert unique.original_parent_version == parent_unique.version
                    if name == "cli":
                        assert ancestor.birth == supervisor.birth
                        assert ancestor.parent == os.getpid()
                    owned[name] = (child, unique.version)
                pinned.touch()
                while not censused.exists():
                    if time.monotonic() >= end:
                        raise AssertionError("fixture did not complete barrier census")
                    time.sleep(0.001)
                if mode == "interrupt" and not completed.is_set():
                    os.kill(os.getpid(), signal.SIGINT)
            except BaseException as error:
                errors.append(error)

        worker = threading.Thread(target=coordinate, name="reaped-intermediate-proof")
        worker.start()
        original = None
        started = time.monotonic()
        writer_survived = False
        try:
            try:
                controller.run(
                    [sys.executable, "-c", program],
                    capture_output=True,
                    text=True,
                    timeout=1,
                )
            except (subprocess.TimeoutExpired, KeyboardInterrupt) as caught:
                original = caught
            finally:
                completed.set()
                worker.join(2.5)
            assert not worker.is_alive() and not errors, errors
            assert isinstance(
                original,
                KeyboardInterrupt if mode == "interrupt" else subprocess.TimeoutExpired,
            )
            assert reaped.exists() and censused.exists()
            writer = process_identity(owned["writer"][0].pid)
            writer_survived = writer is not None and writer.state not in ("Z", "X")
            assert writer_survived and writer.birth == owned["writer"][0].birth
            try:
                controller.close()
            except RuntimeError:
                assert expect_uncertain
            else:
                assert not expect_uncertain, "helper falsely confirmed empty cleanup"
            proof = json.loads((root / "proof.json").read_text())
            assert proof["native_cleanup_reports"]
            report = proof["native_cleanup_reports"][-1]
            assert report["remaining"] == []
            observed = {item["birth"] for item in report["observed_darwin"]}
            assert owned["sibling"][0].birth in observed
            assert owned["intermediate"][0].birth not in observed
            assert owned["writer"][0].birth not in observed
            assert report["ownership_uncertain"] is expect_uncertain
            if expect_uncertain:
                assert proof["cleanup_confirmed"] is False
                assert proof["cleanup_errors"] and proof["error"] == "RuntimeError"
                assert any("cleanup failed" in note for note in original.__notes__)
                assert any(
                    item["birth"] == writer.birth
                    for item in report["darwin_fork_audit"]["unresolved"]
                )
            assert time.monotonic() - started < 10
        finally:
            completed.set()
            pinned.touch()
            worker.join(2.5)
            try:
                controller.close()
            except RuntimeError:
                pass
            for identity, version in reversed(list(owned.values())):
                current = process_identity(identity.pid)
                unique = namespace_peers._unique_info(identity.pid)
                if current is None or current.state in ("Z", "X"):
                    continue
                assert current.birth == identity.birth == unique.unique
                # Refresh only an already-proved unique birth's exec generation.
                token = (ctypes.c_uint32 * 8)()
                token[5], token[7] = identity.pid, unique.version & 0xFFFFFFFF
                assert namespace_peers._darwin_lib().proc_signal_with_audittoken(
                    token, signal.SIGKILL
                ) in (0, errno.ESRCH)
            end = time.monotonic() + 1
            while time.monotonic() < end:
                live = [
                    current
                    for identity, _ in owned.values()
                    if (current := process_identity(identity.pid)) is not None
                    and current.birth == identity.birth
                    and current.state not in ("Z", "X")
                ]
                if not live:
                    break
                time.sleep(0.005)
            assert not live, "outer fixture could not reclaim its pinned generations"
        return {
            "mode": mode,
            "elapsed": time.monotonic() - started,
            "input_sha256": hashlib.sha256(source).hexdigest(),
            "barrier_source_sha256": digest,
            "writer_survived_helper": writer_survived,
            "outer_owned": {
                name: {"pid": identity.pid, "birth": identity.birth, "version": version}
                for name, (identity, version) in owned.items()
            },
            "outer_remaining": [],
            "receipt": proof,
        }


class NamespacePeerTests(unittest.TestCase):
    """Reject arbitrary addresses, duplicate peers and implicit fixture roots."""

    def test_exact_bound_loopback_peer_only(self) -> None:
        """No wildcard/non-loopback/UDP/unbound-port listener is cloneable."""
        with ThreadingHTTPServer(
            ("127.0.0.1", 0), http.server.BaseHTTPRequestHandler
        ) as peer:
            self.assertEqual(peer_address(peer), peer.server_address)
            for address, family, kind in (
                (("0.0.0.0", peer.server_port), socket.AF_INET, socket.SOCK_STREAM),
                (("203.0.113.1", peer.server_port), socket.AF_INET, socket.SOCK_STREAM),
                (("127.0.0.1", 0), socket.AF_INET, socket.SOCK_STREAM),
                (
                    ("127.0.0.1", peer.server_port + 1),
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                ),
                (("::1", peer.server_port), socket.AF_INET6, socket.SOCK_STREAM),
                (peer.server_address, socket.AF_INET, socket.SOCK_DGRAM),
            ):
                fake = SimpleNamespace(
                    server_address=address,
                    socket=SimpleNamespace(
                        family=family, type=kind, getsockname=peer.socket.getsockname
                    ),
                )
                with self.subTest(address=address), self.assertRaises(ValueError):
                    peer_address(fake)

    def test_finite_peers_explicit_roots_and_platform_environment(self) -> None:
        """Require explicit roots and preserve Darwin's environment."""
        with tempfile.TemporaryDirectory() as temporary, ThreadingHTTPServer(
            ("127.0.0.1", 0), http.server.BaseHTTPRequestHandler
        ) as peer:
            root = Path(temporary).resolve()
            for peers, tools, roots in (
                ([], root / "tools", [root / "plugins"]),
                ([peer, peer], root / "tools", [root / "plugins"]),
                ([peer], Path("tools"), [root / "plugins"]),
                ([peer], root / "tools", []),
                ([peer], root / "tools", [Path("plugins")]),
            ):
                with self.assertRaises(ValueError):
                    NamespacePeers(peers, tools, roots, root / "receipt.json")
            controller = NamespacePeers(
                [peer], root / "tools", [root / "plugins"], root / "receipt.json"
            )
            env = {"PATH": "/fixture/bin", "KEY": "not changed"}
            actual = controller.environment(env)
            self.assertEqual(env, {"PATH": "/fixture/bin", "KEY": "not changed"})
            self.assertEqual(actual["KEY"], env["KEY"])
            self.assertEqual(
                actual["PATH"],
                (
                    str(root / "tools") + ":/fixture/bin"
                    if sys.platform == "linux"
                    else env["PATH"]
                ),
            )

    def test_native_birth_identity_and_disappearance(self) -> None:
        """Linux identity comes from kernel start ticks, not a reused numeric PID."""
        if sys.platform == "linux":
            import os

            first = process_identity(os.getpid())
            self.assertEqual(first, process_identity(os.getpid()))
            self.assertGreater(first.birth, 0)
            self.assertEqual(first.parent, os.getppid())
            self.assertIsNone(process_identity(2147483647))
        else:
            # Darwin never consults /proc or joins network namespaces.
            self.assertEqual(sys.platform, "darwin")

    def test_unobserved_runner_announcement_never_adopts_current_occupant(self) -> None:
        """A trusted numeric announcement is not proof of the occupant's ownership."""
        with tempfile.TemporaryDirectory() as temporary, ThreadingHTTPServer(
            ("127.0.0.1", 0), http.server.BaseHTTPRequestHandler
        ) as peer:
            root = Path(temporary).resolve()
            controller = NamespacePeers(
                [peer], root / "tools", [root / "plugins"], root / "proof.json"
            )
            replacement = Process(900002, 200, 1, "S")
            with mock.patch.object(
                namespace_peers, "process_identity", return_value=replacement
            ):
                with self.assertRaisesRegex(RuntimeError, "observed|ownership"):
                    controller._register(replacement.pid, "runner")
            self.assertNotIn(replacement.pid, controller.owned)

    def test_reused_observed_runner_is_rejected_without_repinning(self) -> None:
        """An earlier owned birth cannot authorize the PID's later replacement."""
        with tempfile.TemporaryDirectory() as temporary, ThreadingHTTPServer(
            ("127.0.0.1", 0), http.server.BaseHTTPRequestHandler
        ) as peer:
            root = Path(temporary).resolve()
            controller = NamespacePeers(
                [peer], root / "tools", [root / "plugins"], root / "proof.json"
            )
            controller.owned[900002] = 100
            with mock.patch.object(
                namespace_peers,
                "process_identity",
                return_value=Process(900002, 200, 1, "S"),
            ):
                with self.assertRaises(RuntimeError):
                    controller._register(900002, "runner")
            self.assertEqual(controller.owned[900002], 100)

    def test_parent_reuse_during_enumeration_cannot_adopt_foreign_child(self) -> None:
        """Revalidate the parent after reading children and before any child pin."""
        with tempfile.TemporaryDirectory() as temporary, ThreadingHTTPServer(
            ("127.0.0.1", 0), http.server.BaseHTTPRequestHandler
        ) as peer:
            root = Path(temporary).resolve()
            controller = NamespacePeers(
                [peer], root / "tools", [root / "plugins"], root / "proof.json"
            )
            controller.owned[900001] = 100
            controller.pidfds[900001] = 42
            reads = []

            def identity(pid: int) -> Process:
                """Replace the parent's birth at the child-enumeration boundary."""
                if pid == 900001:
                    reads.append(pid)
                    return Process(pid, 100 if len(reads) <= 2 else 101, 1, "S")
                return Process(pid, 200, 900001, "S")

            with mock.patch.object(
                namespace_peers, "process_identity", side_effect=identity
            ), mock.patch.object(
                Path, "read_text", return_value="900002"
            ), mock.patch.object(
                namespace_peers, "_child_pids", return_value=[900002], create=True
            ), mock.patch.object(
                namespace_peers.os, "pidfd_open", return_value=42, create=True
            ), mock.patch.object(
                namespace_peers.os, "dup", return_value=43
            ), mock.patch.object(
                namespace_peers.select, "select", return_value=([], [], [])
            ), mock.patch.object(
                namespace_peers.os, "close"
            ):
                with self.assertRaisesRegex(RuntimeError, "parent identity"):
                    controller._owned_processes()
            self.assertNotIn(900002, controller.owned)

    def test_child_reparenting_during_pin_never_grants_ownership(self) -> None:
        """A current PPID must still corroborate the validated parent/child snapshot."""
        with tempfile.TemporaryDirectory() as temporary, ThreadingHTTPServer(
            ("127.0.0.1", 0), http.server.BaseHTTPRequestHandler
        ) as peer:
            root = Path(temporary).resolve()
            controller = NamespacePeers(
                [peer], root / "tools", [root / "plugins"], root / "proof.json"
            )
            controller.owned[900001] = 100
            controller.pidfds[900001] = 42
            child_reads = []

            def identity(pid: int) -> Process:
                """The child leaves the known parent before its pidfd is accepted."""
                if pid == 900001:
                    return Process(pid, 100, 1, "S")
                child_reads.append(pid)
                return Process(pid, 200, 900001 if len(child_reads) == 1 else 1, "S")

            with mock.patch.object(
                namespace_peers, "process_identity", side_effect=identity
            ), mock.patch.object(
                namespace_peers, "_child_pids", return_value=[900002]
            ), mock.patch.object(
                namespace_peers.os, "pidfd_open", return_value=42, create=True
            ), mock.patch.object(
                namespace_peers.os, "dup", return_value=43
            ), mock.patch.object(
                namespace_peers.select, "select", return_value=([], [], [])
            ), mock.patch.object(
                namespace_peers.os, "close"
            ):
                controller._owned_processes()
            self.assertGreaterEqual(len(child_reads), 2)
            self.assertNotIn(900002, controller.owned)

    def test_final_darwin_census_requires_a_new_gap_free_snapshot(self) -> None:
        """A gapped empty census needs a new complete snapshot to certify cleanup."""
        tree = SimpleNamespace(
            uncertain=set(), unresolved=[], forked={900001}, census_gaps=0
        )
        gaps = iter((1, 0))

        def census() -> list[Process]:
            """Expose a transient gap followed by a complete empty snapshot."""
            tree.census_gaps = next(gaps)
            return []

        tree.census = mock.Mock(side_effect=census)
        with mock.patch.object(
            namespace_peers.time, "monotonic", side_effect=[10.0, 10.005]
        ), mock.patch.object(namespace_peers.time, "sleep") as sleep:
            self.assertEqual(
                namespace_peers._final_darwin_census(tree, 11.0), ([], False)
            )
        self.assertEqual(tree.census.call_count, 2)
        self.assertEqual(tree.census_gaps, 0)
        sleep.assert_called_once_with(0.005)

    def test_darwin_observed_foreign_parent_exit_preserves_exclusion(self) -> None:
        """A native unrelated orphan stays excluded by its observed fork generation."""
        if sys.platform != "darwin":
            return  # Native libproc contract; Linux has subreaper ownership instead.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            parent_ready, census_ready, spawned, proved, reaped = (
                root / name
                for name in (
                    "parent-ready",
                    "census-ready",
                    "spawned",
                    "proved",
                    "reaped",
                )
            )
            parent_program = f"""
import json, os, subprocess, sys, time
from pathlib import Path
def wait(path):
    end = time.monotonic() + 5
    while not path.exists():
        if time.monotonic() >= end: raise RuntimeError('foreign parent deadline')
        time.sleep(.001)
Path({str(parent_ready)!r}).touch()
wait(Path({str(census_ready)!r}))
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(8)'])
Path({str(spawned)!r}).write_text(json.dumps(child.pid))
wait(Path({str(proved)!r}))
"""
            source = namespace_peers.SOURCE_BYTES
            probe_program = f"""
import hashlib, json, sys, time
from pathlib import Path
sys.path.insert(0, {str(Path(__file__).parent)!r})
import namespace_peers as loader
source = {source!r}
loaded = loader.load_from_source(source, hashlib.sha256(source).hexdigest(), 'probe')
tree = loaded._DarwinTree()
try:
    tree.census()
    Path({str(census_ready)!r}).touch()
    end = time.monotonic() + 5
    while not Path({str(reaped)!r}).exists():
        if time.monotonic() >= end: raise RuntimeError('foreign probe deadline')
        time.sleep(.001)
    tree.census()
    pid = json.loads(Path({str(spawned)!r}).read_text())
    identity = loaded._unique_info(pid)
    assert identity is not None
    assert identity.version in tree.foreign_versions
    assert pid not in tree.known
    assert not any(item['pid'] == pid for item in tree.unresolved)
    print(json.dumps({{'foreign_pid':pid, 'birth':identity.unique,
                      'original_parent_version':identity.original_parent_version,
                      'known':list(tree.known), 'unresolved':tree.unresolved}}))
finally:
    tree.close()
"""
            parent = subprocess.Popen([sys.executable, "-c", parent_program])
            probe = None
            writer = None
            try:
                end = time.monotonic() + 5
                while not parent_ready.exists():
                    self.assertLess(time.monotonic(), end)
                    time.sleep(0.001)
                parent_identity = process_identity(parent.pid)
                parent_unique = namespace_peers._unique_info(parent.pid)
                self.assertEqual(parent_unique.unique, parent_identity.birth)
                probe = subprocess.Popen(
                    [sys.executable, "-c", probe_program],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                while not spawned.exists() or not spawned.stat().st_size:
                    self.assertLess(time.monotonic(), end)
                    time.sleep(0.001)
                pid = json.loads(spawned.read_text())
                first = process_identity(pid)
                unique = namespace_peers._unique_info(pid)
                self.assertEqual(first.parent, parent.pid)
                self.assertEqual(process_identity(parent.pid), parent_identity)
                self.assertEqual(process_identity(pid), first)
                self.assertEqual(unique.unique, first.birth)
                self.assertEqual(unique.original_parent_version, parent_unique.version)
                writer = first
                proved.touch()
                self.assertEqual(parent.wait(timeout=2), 0)
                reaped.touch()
                stdout, stderr = probe.communicate(timeout=5)
                self.assertEqual(probe.returncode, 0, stderr)
                report = json.loads(stdout)
                self.assertEqual(report["birth"], writer.birth)
                self.assertEqual(report["known"], [])
                print("FOREIGN_ORPHAN " + stdout.strip(), flush=True)
            finally:
                proved.touch()
                reaped.touch()
                if parent.poll() is None:
                    parent.kill()  # Unreaped direct Popen child owned by this test.
                parent.wait(timeout=2)
                if probe is not None:
                    if probe.poll() is None:
                        probe.kill()  # Unreaped direct Popen child, not a PID lookup.
                    probe.communicate(timeout=2)
                if writer is not None:
                    current = process_identity(writer.pid)
                    unique = namespace_peers._unique_info(writer.pid)
                    if current is not None and current.state not in ("Z", "X"):
                        self.assertEqual(current.birth, writer.birth)
                        self.assertEqual(unique.unique, writer.birth)
                        token = (ctypes.c_uint32 * 8)()
                        token[5], token[7] = writer.pid, unique.version & 0xFFFFFFFF
                        self.assertIn(
                            namespace_peers._darwin_lib().proc_signal_with_audittoken(
                                token, signal.SIGKILL
                            ),
                            (0, errno.ESRCH),
                        )

    def test_darwin_exclusion_census_includes_cross_uid_original_ancestry(self) -> None:
        """Foreign ancestry across a UID change excludes without granting ownership."""
        tree = object.__new__(namespace_peers._DarwinTree)
        tree.queue = SimpleNamespace(control=mock.Mock(return_value=[]))
        tree.known, tree.native, tree.versions = {}, {}, {}
        tree.exited, tree.forked, tree.uncertain = set(), set(), set()
        tree.foreign_versions, tree.floor = set(), 100
        generations = {
            10: SimpleNamespace(unique=90, version=7, original_parent_version=1),
            11: SimpleNamespace(unique=110, version=8, original_parent_version=7),
            12: SimpleNamespace(unique=120, version=9, original_parent_version=8),
        }

        def listpids(kind: int, value: int, buffer: object, size: int) -> int:
            """Model root ancestor/service and ordinary-user child native metadata."""
            self.assertEqual((kind, value), (1, 0))
            for index, pid in enumerate(generations):
                buffer[index] = pid
            return 3 * ctypes.sizeof(ctypes.c_int)

        tree.lib = SimpleNamespace(proc_listpids=listpids)
        with mock.patch.object(
            namespace_peers, "_unique_info", side_effect=generations.get
        ), mock.patch.object(namespace_peers, "process_identity") as inspect:
            self.assertEqual(tree.census(), [])
        self.assertEqual(tree.foreign_versions, {7, 8, 9})
        self.assertEqual(tree.known, {})
        self.assertEqual(tree.unresolved, [])
        inspect.assert_not_called()

    def test_final_darwin_census_persistent_gaps_keep_existing_deadline(self) -> None:
        """Persistent gaps stay uncertain within the original cleanup budget."""
        tree = SimpleNamespace(
            uncertain=set(),
            unresolved=[],
            forked={900001},
            census_gaps=1,
            census=mock.Mock(return_value=[]),
        )
        for start in (10.0, 10.02):
            with self.subTest(start=start):
                now = [start]

                def sleep(seconds: float) -> None:
                    """Advance only the fake clock, preserving the supplied deadline."""
                    self.assertGreater(seconds, 0)
                    self.assertLessEqual(now[0] + seconds, 10.012)
                    now[0] += seconds

                tree.census.reset_mock()
                with mock.patch.object(
                    namespace_peers.time, "monotonic", side_effect=lambda: now[0]
                ), mock.patch.object(namespace_peers.time, "sleep", side_effect=sleep):
                    self.assertEqual(
                        namespace_peers._final_darwin_census(tree, 10.012), ([], True)
                    )
                self.assertEqual(now[0], max(start, 10.012))
                self.assertEqual(tree.census.call_count, 3 if start == 10.0 else 1)
                self.assertEqual(tree.census_gaps, 1)

    def test_final_darwin_census_does_not_retry_other_cleanup_states(self) -> None:
        """Live, unresolved, permanent-uncertain, or already-complete states stop."""
        live = [Process(900002, 200, 900001, "S")]
        for remaining, uncertain, unresolved, forked, gaps, expected_uncertain in (
            (live, set(), [], {900001}, 1, True),
            ([], {900001}, [], {900001}, 1, True),
            ([], set(), [{"pid": 900002}], {900001}, 1, True),
            ([], set(), [], set(), 1, False),
            ([], set(), [], {900001}, 0, False),
        ):
            with self.subTest(
                live=bool(remaining),
                uncertain=bool(uncertain),
                unresolved=bool(unresolved),
                forked=bool(forked),
                gaps=gaps,
            ):
                tree = SimpleNamespace(
                    uncertain=uncertain,
                    unresolved=unresolved,
                    forked=forked,
                    census_gaps=gaps,
                    census=mock.Mock(return_value=remaining),
                )
                with mock.patch.object(
                    namespace_peers.time, "monotonic"
                ) as clock, mock.patch.object(namespace_peers.time, "sleep") as sleep:
                    actual_remaining, actual_uncertain = (
                        namespace_peers._final_darwin_census(tree, 11.0)
                    )
                self.assertIs(actual_remaining, remaining)
                self.assertIs(actual_uncertain, expected_uncertain)
                tree.census.assert_called_once_with()
                clock.assert_not_called()
                sleep.assert_not_called()

    def test_child_pidfd_einval_retains_numeric_observation_and_original_error(
        self,
    ) -> None:
        """A non-TGID observation never permits adoption or retry."""
        parent = Process(900001, 100, 1, "S")
        child = Process(900002, 200, parent.pid, "S")
        observed = Process(child.pid, 201, parent.pid, "S")
        after = Process(child.pid, 202, 1, "S")
        failure = OSError(errno.EINVAL, "unretained error payload")
        accept = mock.Mock()
        status = mock.mock_open(
            read_data=(
                b"Name:\tprivate name\nPid:\t900002\nTgid:\t900003\nPPid:\t900001\n"
            )
        )
        with mock.patch.object(
            namespace_peers,
            "process_identity",
            side_effect=[parent, parent, child, observed, after, after],
        ), mock.patch.object(
            namespace_peers, "_child_pids", return_value=[child.pid]
        ), mock.patch.object(
            namespace_peers.os, "pidfd_open", side_effect=[42, failure], create=True
        ) as pidfd_open, mock.patch.object(
            Path, "open", status
        ), mock.patch.object(
            namespace_peers.os, "close"
        ) as close:
            with self.assertRaises(OSError) as caught:
                namespace_peers._verified_children(parent, accept)
        self.assertIs(caught.exception, failure)
        self.assertEqual(failure.errno, errno.EINVAL)
        self.assertEqual(
            failure.fixture_process_observation,
            {
                "parent_pid": parent.pid,
                "parent_birth": parent.birth,
                "child_pid": child.pid,
                "child_birth": child.birth,
                "observed_birth": observed.birth,
                "observed_parent": observed.parent,
                "status_pid": child.pid,
                "status_tgid": 900003,
                "status_parent": parent.pid,
                "after_birth": after.birth,
                "after_parent": after.parent,
                "inspection_failed": 0,
            },
        )
        self.assertTrue(
            all(
                type(value) is int or value is None
                for value in failure.fixture_process_observation.values()
            )
        )
        status.assert_called_once_with("rb")
        status().read.assert_called_once_with(8192)
        self.assertEqual(
            pidfd_open.call_args_list, [mock.call(parent.pid), mock.call(child.pid)]
        )
        accept.assert_not_called()
        close.assert_called_once_with(42)

    def test_child_pidfd_einval_inspection_failure_preserves_original_error(
        self,
    ) -> None:
        """Diagnostic failures cannot replace EINVAL or prevent later observations."""
        parent = Process(900001, 100, 1, "S")
        child = Process(900002, 200, parent.pid, "S")
        valid_status = b"Pid: 900002\nTgid: 900002\nPPid: 900001\n"
        for before, status_data, status_error, after in (
            (PermissionError("private inspection error"), valid_status, None, child),
            (child, b"", PermissionError("private status error"), child),
            (child, b"Pid: invalid\n", None, child),
            (child, valid_status + b"x" * 8192, None, child),
            (child, b"Pid: 900002\n", None, None),
            (child, valid_status, None, KeyboardInterrupt()),
        ):
            with self.subTest(
                before=type(before).__name__,
                size=len(status_data),
                after=type(after).__name__,
            ):
                failure = OSError(errno.EINVAL, "original payload")
                accept = mock.Mock()
                status = mock.mock_open(read_data=status_data)
                status.side_effect = status_error
                with mock.patch.object(
                    namespace_peers,
                    "process_identity",
                    side_effect=[parent, parent, child, before, after, child],
                ), mock.patch.object(
                    namespace_peers, "_child_pids", return_value=[child.pid]
                ), mock.patch.object(
                    namespace_peers.os,
                    "pidfd_open",
                    side_effect=[42, failure],
                    create=True,
                ) as pidfd_open, mock.patch.object(
                    Path, "open", status
                ), mock.patch.object(
                    namespace_peers.os, "close"
                ) as close:
                    with self.assertRaises(OSError) as caught:
                        namespace_peers._verified_children(parent, accept)
                self.assertIs(caught.exception, failure)
                self.assertEqual(failure.errno, errno.EINVAL)
                observation = failure.fixture_process_observation
                self.assertEqual(observation["inspection_failed"], 1)
                self.assertEqual(observation["child_birth"], child.birth)
                self.assertEqual(
                    observation["after_birth"], child.birth if after is child else None
                )
                self.assertTrue(
                    all(
                        type(value) is int or value is None
                        for value in observation.values()
                    )
                )
                self.assertEqual(pidfd_open.call_count, 2)
                accept.assert_not_called()
                close.assert_called_once_with(42)

    def test_exited_child_einval_requires_native_absence_and_stable_parent(
        self,
    ) -> None:
        """Classify only a vanished child; live/reused/denied targets remain errors."""
        parent = Process(900001, 100, 1, "S")
        child = Process(900002, 200, parent.pid, "S")
        for current, final_parent, expected in (
            (None, parent, None),
            (None, None, None),
            (None, Process(parent.pid, 101, 1, "S"), RuntimeError),
            (child, parent, OSError),
            (Process(child.pid, 201, parent.pid, "S"), parent, OSError),
            (PermissionError("inspection denied"), parent, PermissionError),
        ):
            with self.subTest(current=current, final_parent=final_parent):
                failure = OSError(errno.EINVAL, "pin failed")
                accept = mock.Mock()
                with mock.patch.object(
                    namespace_peers,
                    "process_identity",
                    side_effect=[parent, parent, child, current, final_parent],
                ), mock.patch.object(
                    namespace_peers, "_child_pids", return_value=[child.pid]
                ), mock.patch.object(
                    namespace_peers, "_process_observation", return_value={}
                ), mock.patch.object(
                    namespace_peers.os,
                    "pidfd_open",
                    side_effect=[42, failure],
                    create=True,
                ) as pin, mock.patch.object(
                    namespace_peers.os, "close"
                ) as close:
                    if expected is None:
                        self.assertEqual(
                            namespace_peers._verified_children(parent, accept), []
                        )
                    else:
                        with self.assertRaises(expected):
                            namespace_peers._verified_children(parent, accept)
                self.assertEqual(
                    pin.call_args_list, [mock.call(parent.pid), mock.call(child.pid)]
                )
                accept.assert_not_called()
                close.assert_called_once_with(42)

    def test_pidfd_other_failures_do_not_collect_observations(self) -> None:
        """Only child EINVAL gets diagnostics; all pin errors still propagate."""
        parent = Process(900001, 100, 1, "S")
        child = Process(900002, 200, parent.pid, "S")
        for parent_failure, error_number in (
            (True, errno.EINVAL),
            (False, errno.EPERM),
        ):
            with self.subTest(parent_failure=parent_failure):
                failure = OSError(error_number, "original payload")
                accept = mock.Mock()
                with mock.patch.object(
                    namespace_peers,
                    "process_identity",
                    side_effect=[parent, parent, child],
                ), mock.patch.object(
                    namespace_peers, "_child_pids", return_value=[child.pid]
                ), mock.patch.object(
                    namespace_peers.os,
                    "pidfd_open",
                    side_effect=[failure] if parent_failure else [42, failure],
                    create=True,
                ), mock.patch.object(
                    namespace_peers, "_process_observation"
                ) as observe, mock.patch.object(
                    namespace_peers.os, "close"
                ) as close:
                    with self.assertRaises(OSError) as caught:
                        namespace_peers._verified_children(parent, accept)
                self.assertIs(caught.exception, failure)
                self.assertFalse(hasattr(failure, "fixture_process_observation"))
                observe.assert_not_called()
                accept.assert_not_called()
                if parent_failure:
                    close.assert_not_called()
                else:
                    close.assert_called_once_with(42)

    def test_real_detached_runner_is_observed_before_announcement(self) -> None:
        """A fast-exiting CLI cannot outrun the owned Linux subreaper proof."""
        with tempfile.TemporaryDirectory() as temporary, ThreadingHTTPServer(
            ("127.0.0.1", 0), http.server.BaseHTTPRequestHandler
        ) as peer:
            root = Path(temporary).resolve()
            controller = NamespacePeers(
                [peer], root / "tools", [root / "plugins"], root / "proof.json"
            )
            try:
                if sys.platform == "linux":
                    program = (
                        "import subprocess,sys,json; "
                        "child=subprocess.Popen("
                        "[sys.executable,'-c','import time; time.sleep(60)'],"
                        "start_new_session=True,stdout=subprocess.DEVNULL,"
                        "stderr=subprocess.DEVNULL); "
                        "print(json.dumps({'runner_pid':child.pid}),flush=True)"
                    )
                    result = controller.run(
                        [sys.executable, "-c", program],
                        capture_output=True,
                        text=True,
                        timeout=3,
                    )
                    pid = json.loads(result.stdout)["runner_pid"]
                    birth = controller.owned[pid]
                    self.assertEqual(process_identity(pid).birth, birth)
                    self.assertTrue(
                        any(
                            event.get("pid") == pid
                            and event.get("proof") == "observed-descent"
                            for event in controller.events
                        )
                    )
                else:
                    result = controller.run(
                        [sys.executable, "-c", "print('native Darwin')"],
                        capture_output=True,
                        text=True,
                        timeout=3,
                    )
                    self.assertEqual(result.stdout, "native Darwin\n")
            finally:
                controller.close()
            proof = json.loads((root / "proof.json").read_text())
            self.assertEqual(proof["owned_live_processes"], [])
            self.assertEqual(proof["pidfds_remaining"], 0)

    def test_first_identity_failure_closes_registered_launch_with_retained_traceback(
        self,
    ) -> None:
        """Inspection failure admits no CLI and abandons no live Popen/FD objects."""
        with tempfile.TemporaryDirectory() as temporary, ThreadingHTTPServer(
            ("127.0.0.1", 0), http.server.BaseHTTPRequestHandler
        ) as peer:
            root = Path(temporary).resolve()
            for index, failure in enumerate(
                (PermissionError("inspection denied"), KeyboardInterrupt())
            ):
                witness = root / f"cli-witness-{index}"
                controller = NamespacePeers(
                    [peer],
                    root / f"tools-{index}",
                    [root / "plugins"],
                    root / f"proof-{index}.json",
                )
                jobs = []
                original_cleanup = controller._clean_job

                def cleanup(job: object, deadline: float) -> list:
                    """Keep resource objects alive after explicit cleanup."""
                    jobs.append(job)
                    return original_cleanup(job, deadline)

                held = None
                with mock.patch.object(
                    namespace_peers, "process_identity", side_effect=failure
                ), mock.patch.object(controller, "_clean_job", side_effect=cleanup):
                    try:
                        program = (
                            "from pathlib import Path; "
                            f"Path({str(witness)!r}).write_text('forbidden')"
                        )
                        controller.run(
                            [sys.executable, "-c", program],
                            capture_output=True,
                            text=True,
                            timeout=2,
                        )
                    except BaseException as caught:
                        self.assertIs(caught, failure)
                        held = caught.__traceback__
                    else:
                        self.fail("identity fault was not preserved")
                self.assertIsNotNone(held)
                self.assertEqual(controller.jobs, [])
                self.assertEqual(len(jobs), 1)
                job = jobs[0]
                self.assertFalse(job.admitted)
                self.assertIsNotNone(job.child.returncode)
                self.assertTrue(job.child.stdout.closed and job.child.stderr.closed)
                self.assertEqual(job.control.fileno(), -1)
                self.assertEqual(job.remote.fileno(), -1)
                self.assertEqual(job.source_fd, -1)
                self.assertFalse(witness.exists())
                controller.close()

    def test_supervisor_executes_captured_source_after_atomic_path_replacement(
        self,
    ) -> None:
        """The actual supervisor executes captured A, not compatible B at its path."""
        with tempfile.TemporaryDirectory() as temporary, ThreadingHTTPServer(
            ("127.0.0.1", 0), http.server.BaseHTTPRequestHandler
        ) as peer:
            root = Path(temporary).resolve()
            path, replacement, witness = (
                root / "helper.py",
                root / "replacement.py",
                root / "B-witness",
            )
            captured = namespace_peers.SOURCE_BYTES
            digest = hashlib.sha256(captured).hexdigest()
            path.write_bytes(captured)
            loaded = namespace_peers.load_from_source(captured, digest, str(path))
            controller = loaded.NamespacePeers(
                [peer], root / "tools", [root / "plugins"], root / "proof.json"
            )
            injected = (
                "if __name__ == '__main__':\n"
                f"    Path({str(witness)!r}).write_text('B supervisor executed')\n"
                f"    HELPER_SHA256 = {digest!r}\n\n"
            )
            changed = captured.replace(
                b'if __name__ == "__main__":',
                injected.encode() + b'if __name__ == "__main__":',
                1,
            )
            self.assertNotEqual(captured, changed)
            replacement.write_bytes(changed)
            os.replace(replacement, path)
            try:
                result = controller.run(
                    [sys.executable, "-c", "print('captured A')"],
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
                self.assertEqual(result.stdout, "captured A\n")
            finally:
                controller.close()
            self.assertFalse(witness.exists())
            proof = json.loads((root / "proof.json").read_text())
            self.assertEqual(proof["controller_source_sha256"], digest)
            self.assertEqual(proof["supervisor_source_sha256s"], [digest])
            self.assertTrue(proof["native_cleanup_reports"])
            self.assertTrue(
                all(
                    report["source_sha256"] == digest
                    for report in proof["native_cleanup_reports"]
                )
            )

    def test_reaped_intermediate_abort_is_unconfirmed_on_darwin(self) -> None:
        """Coalesced fork evidence cannot conceal an unobserved detached lineage."""
        if sys.platform != "darwin":
            return  # Darwin ABI regression; Linux has its separate subreaper proof.
        with tempfile.TemporaryDirectory() as temporary:
            for mode in ("timeout", "interrupt"):
                with self.subTest(mode=mode):
                    evidence = _reaped_intermediate(
                        namespace_peers.SOURCE_BYTES,
                        Path(temporary).resolve() / mode,
                        mode,
                        True,
                    )
                    print("REAPED_INTERMEDIATE " + json.dumps(evidence), flush=True)

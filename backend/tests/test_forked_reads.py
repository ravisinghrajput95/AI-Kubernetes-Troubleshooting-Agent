"""F22: a kubectl read forked from a worker holding gRPC state keeps its own
error message.

The platform forks for every `kubectl` read and gRPC installs `pthread_atfork`
handlers, so on a worker that also runs an agent gateway the child writes gRPC
diagnostics to the stderr `capture_output` is collecting the command's error
from. The evidence is still correctly recorded as failed — nothing is
misreported — but the reason reads `ev_poll_posix.cc:593 FD from fork parent
still in poll list` instead of whatever kubectl said. Same cost as the
agent-path `unknown` that `detailFor` exists to fix.

Two things make this worth a dedicated file rather than an assertion on an
environment variable.

**It is intermittent, and platform-specific.** gRPC skips the handlers whenever
another thread is inside gRPC at the moment of the fork, so a one-hour soak
caught 3 of roughly 23,000 reads. It also only reproduces on macOS:
`ev_poll_posix` is the poll-based engine darwin uses, and the same script
against the same gRPC gives 40/40 polluted there and 0/40 in a
`python:3.12-slim` container either way. The soak ran on the development
machine, so on current evidence this never affected a shipped Linux
deployment — which is why the class below can *only* be a check that skips,
and why the platform-independent one above exists.

**The fix is an import-order property, which is the kind that goes inert in
silence.** `GRPC_ENABLE_FORK_SUPPORT` is read when gRPC's core initialises;
setting it after `import grpc` does nothing at all. Asserting that
`app/__init__` sets the variable would pass with an import added above it,
which is precisely the regression to catch — so the test below forks a real
subprocess out of a process holding a real gRPC server and reads its stderr,
which is the property, not its proxy.
"""

import os
import subprocess
import sys
import textwrap

import pytest

# The child does the whole experiment: initialise gRPC, fork, report what the
# forked process's stderr actually contained. It has to be a fresh interpreter,
# because the variable's effect is decided at gRPC's first initialisation and
# this test session has already imported `app`.
PROBE = textwrap.dedent(
    """
    import os, subprocess, sys
    if {apply_fix!r}:
        import app  # noqa: F401  -- the fix under test, at its real seam
    else:
        os.environ["GRPC_ENABLE_FORK_SUPPORT"] = "1"

    import grpc
    from concurrent import futures

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    channel = grpc.insecure_channel("127.0.0.1:%d" % port)
    try:
        grpc.channel_ready_future(channel).result(timeout=10)
    except Exception:
        pass

    polluted = 0
    for _ in range({runs}):
        done = subprocess.run(
            [sys.executable, "-c", "pass"], capture_output=True, text=True, check=False
        )
        if done.stderr.strip():
            polluted += 1
    print(polluted)
    """
)


def _forked_stderr_pollution(*, apply_fix: bool, runs: int = 40) -> int:
    """How many of `runs` forked subprocesses came back with dirty stderr."""
    env = {k: v for k, v in os.environ.items() if k != "GRPC_ENABLE_FORK_SUPPORT"}
    done = subprocess.run(
        [sys.executable, "-c", PROBE.format(apply_fix=apply_fix, runs=runs)],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        env=env,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )
    assert done.returncode == 0, f"probe failed: {done.stderr[-2000:]}"
    return int(done.stdout.strip().splitlines()[-1])


pytest.importorskip("grpc", reason="the defect requires gRPC's fork handlers")


STRUCTURAL_PROBE = textwrap.dedent(
    """
    import os, sys
    import app  # noqa: F401
    print(os.environ.get("GRPC_ENABLE_FORK_SUPPORT"))
    print("grpc" in sys.modules)
    """
)


class TestTheFixIsInPlaceWhereverThisRuns:
    """The platform-independent half, and it exists because the behavioural
    test below is not.

    gRPC's poll engine differs by platform — `ev_poll_posix` is not what Linux
    uses — so the defect reproduces on the machine this was found on and may
    not on the machine CI runs. The behavioural test skips itself in that case,
    which is honest and also makes it **unable to fail there**: pushed as the
    only guard, `scripts/mutation_check.py` reported this entry SURVIVED on
    Linux/3.12 while catching it locally. A check that cannot fail has not
    passed, and that applies to a check that cannot fail *on some platforms*.

    So this asserts the two conditions that make the fix work, both of which
    are platform-independent and both of which are exactly what the regressions
    break: the variable ends up set, and importing `app` has **not** already
    pulled gRPC in — which is what an import added above the `setdefault` would
    do, and is the difference between the fix working and being decoration.
    """

    def _probe(self) -> tuple[str | None, bool]:
        done = subprocess.run(
            [sys.executable, "-c", STRUCTURAL_PROBE],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            env={k: v for k, v in os.environ.items() if k != "GRPC_ENABLE_FORK_SUPPORT"},
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        )
        assert done.returncode == 0, f"probe failed: {done.stderr[-2000:]}"
        value, imported = done.stdout.strip().splitlines()[-2:]
        return (None if value == "None" else value), imported == "True"

    def test_importing_app_sets_the_variable(self):
        value, _ = self._probe()
        assert value == "0", (
            "app/__init__ must set GRPC_ENABLE_FORK_SUPPORT; without it gRPC's "
            "fork handlers write into the stderr a kubectl read's error comes from"
        )

    def test_importing_app_has_not_already_loaded_grpc(self):
        """The load-bearing half. The variable is read at gRPC's core
        initialisation, so a value set after `import grpc` does nothing at all —
        measured at 0/40 polluted before the import and 40/40 after it."""
        _, grpc_loaded = self._probe()
        assert not grpc_loaded, (
            "importing `app` pulled in grpc, so anything app/__init__ sets "
            "afterwards is too late and the fix is inert"
        )


class TestAForkedReadKeepsItsOwnStderr:
    def test_the_defect_reproduces_without_the_fix(self):
        """The control, and the reason the next test means anything.

        Without it, a probe that fails to initialise gRPC — or a machine where
        the handlers never run — reports a clean stderr for both arms and the
        test below passes while guarding nothing. This is the assertion that
        the mechanism is present on the machine running the suite.
        """
        polluted = _forked_stderr_pollution(apply_fix=False)
        if polluted == 0:
            pytest.skip(
                "gRPC's fork handlers did not run on this platform, so there is "
                "no defect here to fix and the check below would be vacuous"
            )
        assert polluted > 0

    def test_importing_app_is_what_stops_it(self):
        """`import app` and nothing else — the fix at the seam it ships in.

        An import added above the `setdefault` in `app/__init__` makes the
        real fix inert, and this is what notices.
        """
        if _forked_stderr_pollution(apply_fix=False) == 0:
            pytest.skip("the defect does not reproduce on this platform")

        assert _forked_stderr_pollution(apply_fix=True) == 0

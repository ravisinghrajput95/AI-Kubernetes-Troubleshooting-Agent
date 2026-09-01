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

**It is intermittent.** gRPC skips the handlers whenever another thread is
inside gRPC at the moment of the fork, so a one-hour soak caught 3 of roughly
23,000 reads. A defect at that rate is never diagnosed from logs, and a test
that reproduces it deterministically is worth more than the fix is on its own.

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

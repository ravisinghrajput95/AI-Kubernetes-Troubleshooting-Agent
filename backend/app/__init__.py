"""The platform package.

The one thing that happens at import time, and it has to happen here.
"""

import os

# F22. gRPC installs `pthread_atfork` handlers, and this process forks — every
# `kubectl` read is a `subprocess.run`. When gRPC's child handler runs, it
# writes to the child's stderr *between* fork and exec, and `capture_output`
# means that stderr is the pipe the platform reads the command's own error
# from. The read is still recorded as failed evidence, so nothing is
# misreported; what is lost is the reason, which reads
# `ev_poll_posix.cc:593 FD from fork parent still in poll list` instead of
# whatever kubectl was trying to say. Same shape as the agent-path `unknown`
# that `detailFor` exists to fix.
#
# It is intermittent, because gRPC skips the handlers when another thread is
# inside gRPC at the moment of the fork — a one-hour soak caught 3 of roughly
# 23,000 reads, and a tight loop reproduces it 40 times out of 40. That is
# exactly the rate at which a defect never gets diagnosed from logs.
#
# The variable is read when gRPC's core initialises, and setting it after
# `import grpc` is already too late — measured: before the import, 0/40
# polluted; after the import or after a channel exists, 40/40. `app/__init__`
# is the only module guaranteed to run before any `app.*` module, including
# `app/gateway/`, which is where grpc is actually imported. **Do not move this,
# and do not add an import above it.** The failure mode of getting it wrong is
# an intermittent line in a stderr nobody reads, with every test still passing.
#
# Nothing here uses gRPC in a forked child — the fork is immediately followed
# by exec — so the handlers protect nothing and only cost this. `setdefault`,
# so an operator who sets the variable deliberately still wins.
os.environ.setdefault("GRPC_ENABLE_FORK_SUPPORT", "0")

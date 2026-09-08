"""The SSE incremental-delivery check, tested where it can actually run.

`scripts/verify_deployment.py` runs only in the `integration-verify` job,
against a kind cluster behind a real nginx. Nothing unit-tested it, and the one
decision it makes turned out to be measuring the wrong thing: it compared how
long the *client* saw frames arrive against how long the *platform* spent
emitting them, over the whole stream.

The investigation is submitted before the stream is opened, and `subscribe()`
replays the backlog before going live — so events emitted before the connection
existed arrive in one burst, by design. That shortens the arrival span and
leaves the emission span alone, which means **the faster the platform runs
before the client connects, the more incremental delivery looks like a blob.**

Two consecutive CI runs of the same code, one green and one red:

    0.470s arrivals / 0.830s emissions = 57%   passed
    0.379s arrivals / 0.760s emissions = 49.87% failed   (threshold 50%)

A required job decided by three tenths of a percentage point, in the direction
that punishes the platform for being quick. Both shapes are reproduced below.
"""

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from verify_deployment import BACKLOG_SLACK_SECONDS, SPREAD_RATIO, _live_portion  # noqa: E402


def spans(live):
    """What the check compares, once the backlog is out of the way."""
    arrival = max(a for a, _ in live) - min(a for a, _ in live)
    emission = max(e for _, e in live) - min(e for _, e in live)
    return arrival, emission


def stream(opened, backlog, live_pairs):
    """A capture: `backlog` emissions delivered in one burst, then live frames.

    Arrivals are in the client's monotonic clock and emissions in the server's;
    the two are deliberately offset, because they are in the real thing.
    """
    burst_at = opened + 0.01
    arrivals = [burst_at + 0.001 * i for i in range(len(backlog))]
    emissions = list(backlog)
    for arrival, emission in live_pairs:
        arrivals.append(arrival)
        emissions.append(emission)
    return arrivals, emissions


CLOCK_SKEW = 1_700_000_000.0  # the server clock is nowhere near monotonic()


class TestTheBacklogIsNotBuffering:
    def test_the_red_ci_run_is_not_a_blob(self):
        """0.379s of arrivals over 0.760s of emissions — the run that failed.

        Almost half that emission window happened before the client connected.
        Judged on the frames it could actually have received live, delivery
        tracks emission closely and the verdict flips.
        """
        opened = 100.0
        # 0.38s of the investigation was already over when the stream opened.
        backlog = [CLOCK_SKEW + 0.02 * i for i in range(19)]
        live = [(100.01 + 0.038 * i, CLOCK_SKEW + 0.38 + 0.038 * i) for i in range(1, 11)]
        arrivals, emissions = stream(opened, backlog, live)

        whole_arrival = arrivals[-1] - arrivals[0]
        whole_emission = emissions[-1] - emissions[0]
        assert whole_arrival / whole_emission < SPREAD_RATIO, (
            "precondition: measured over the whole stream this run reads as a blob"
        )

        kept = _live_portion(arrivals, emissions, opened)
        arrival_span, emission_span = spans(kept)
        assert len(kept) >= 3
        assert arrival_span >= SPREAD_RATIO * emission_span

    def test_a_genuinely_buffered_stream_still_fails(self):
        """The control. Every frame lands at once when the response ends."""
        opened = 100.0
        emissions = [CLOCK_SKEW + 0.05 * i for i in range(20)]
        arrivals = [100.95 + 0.0001 * i for i in range(20)]

        kept = _live_portion(arrivals, emissions, opened)
        if len(kept) >= 3:
            arrival_span, emission_span = spans(kept)
            assert arrival_span < SPREAD_RATIO * emission_span, (
                "a stream flushed in one blob must not pass"
            )
        # Fewer than three live frames is the other acceptable answer: the
        # check refuses rather than passing.

    def test_incremental_delivery_with_no_backlog_passes(self):
        opened = 100.0
        arrivals = [100.1 + 0.05 * i for i in range(15)]
        emissions = [CLOCK_SKEW + 0.05 * i for i in range(15)]

        kept = _live_portion(arrivals, emissions, opened)
        assert len(kept) >= 3
        arrival_span, emission_span = spans(kept)
        assert arrival_span >= SPREAD_RATIO * emission_span


class TestItRefusesRatherThanGuessing:
    def test_an_all_backlog_stream_leaves_too_few_live_frames(self):
        """The connection opened after the platform had finished emitting.

        Nothing about buffering is observable, and the check must say so rather
        than pass — the same refusal `provider_diff` makes for two captures
        from one provider.
        """
        opened = 100.0
        emissions = [CLOCK_SKEW + 0.04 * i for i in range(20)]
        arrivals = [100.01 + 0.0005 * i for i in range(20)]

        assert len(_live_portion(arrivals, emissions, opened)) < 3

    @pytest.mark.parametrize(
        ("arrivals", "emissions"),
        [([], []), ([1.0], []), ([1.0, 2.0], [CLOCK_SKEW])],
    )
    def test_a_malformed_capture_yields_nothing(self, arrivals, emissions):
        assert _live_portion(arrivals, emissions, 0.0) == []

    def test_the_slack_only_excuses_alignment_error(self):
        """It must not be wide enough to swallow the live tail."""
        assert BACKLOG_SLACK_SECONDS < 0.2

"""The replay-then-live handoff, tested without a database.

Streaming progress across workers has exactly one hard part: a subscriber that
replays a backlog and then goes live must lose nothing and repeat nothing. The
ordering rule is "subscribe first, read the backlog second, then de-duplicate
by sequence", and `EventSequencer` is the de-duplication half.

It is pure on purpose. The correctness argument for `subscribe()` should not be
reachable only through Postgres and Redis, because then it is only checked when
someone opts into the integration suite.
"""

from app.jobs.base import EventSequencer
from app.jobs.models import JobEvent, JobEventType


def event(seq: int, message: str = "step") -> JobEvent:
    return JobEvent(JobEventType.PROGRESS, message, seq=seq)


class TestNoDuplicates:
    def test_events_already_delivered_are_dropped(self):
        sequencer = EventSequencer()
        assert sequencer.accept(event(1))
        assert sequencer.accept(event(2))
        # The live buffer replays what the backlog already carried.
        assert not sequencer.accept(event(1))
        assert not sequencer.accept(event(2))

    def test_the_overlap_between_backlog_and_live_buffer_is_removed(self):
        """The interleaving the naive implementation gets wrong.

        Subscribing before reading means an event published during the read
        arrives twice: once in the backlog, once in the buffer. Exactly one
        copy may be delivered.
        """
        sequencer = EventSequencer()
        backlog = [event(1), event(2), event(3)]
        live_buffer = [event(3), event(4)]  # 3 was published mid-read

        delivered = [item.seq for item in backlog if sequencer.accept(item)]
        delivered += [item.seq for item in live_buffer if sequencer.accept(item)]

        assert delivered == [1, 2, 3, 4]

    def test_out_of_order_arrival_never_rewinds_the_cursor(self):
        sequencer = EventSequencer()
        assert sequencer.accept(event(5))
        assert not sequencer.accept(event(3))
        assert sequencer.position == 5


class TestResume:
    def test_a_resume_position_suppresses_everything_up_to_it(self):
        sequencer = EventSequencer(after_seq=3)
        assert not sequencer.accept(event(1))
        assert not sequencer.accept(event(3))
        assert sequencer.accept(event(4))

    def test_position_tracks_the_highest_delivered(self):
        sequencer = EventSequencer()
        for index in range(1, 6):
            sequencer.accept(event(index))
        assert sequencer.position == 5

    def test_a_negative_resume_is_treated_as_the_beginning(self):
        sequencer = EventSequencer(after_seq=-10)
        assert sequencer.accept(event(1))


class TestUnsequencedEvents:
    def test_an_unsequenced_event_is_never_swallowed(self):
        """seq 0 means "not published yet", not "already seen".

        Silently dropping these would make a store that forgets to assign
        sequences look like a store with no progress at all.
        """
        sequencer = EventSequencer()
        assert sequencer.accept(event(4))
        assert sequencer.accept(JobEvent(JobEventType.PROGRESS, "unsequenced"))
        assert sequencer.position == 4

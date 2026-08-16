import asyncio

from telegram_node import ActivityTracker, _activity_state, _wait_for_response


def test_activity_state_uses_latest_activity_not_task_age():
    tracker = ActivityTracker(started_at=0.0)
    tracker.mark_activity(now=1_190.0)
    assert _activity_state(tracker, now=1_200.0, idle_threshold=75.0) == ("working", 10.0)
    assert _activity_state(tracker, now=1_265.0, idle_threshold=75.0) == ("stalled", 75.0)


def test_activity_state_distinguishes_not_started_from_stalled():
    tracker = ActivityTracker(started_at=100.0)
    assert _activity_state(tracker, now=120.0, idle_threshold=75.0) == ("waiting", 20.0)
    assert _activity_state(tracker, now=175.0, idle_threshold=75.0) == ("stalled", 75.0)


def test_wait_for_response_refreshes_timestamp_for_every_activity_event():
    class Subscriber:
        def __init__(self):
            self.messages = iter([
                {"type":"activity","growing":True},
                {"type":"activity","growing":True},
                {"source":"telegram","text":"done"},
            ])

        async def get(self):
            return next(self.messages)

    tracker = ActivityTracker(started_at=0.0)
    marks=[]
    tracker.mark_activity = lambda now=None: marks.append(now)
    result = asyncio.run(_wait_for_response(Subscriber(), "telegram", tracker))
    assert result == "done"
    assert len(marks) == 2

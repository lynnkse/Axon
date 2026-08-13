import json
from unittest.mock import patch

import config
import supabase_client
from actor_model.prompt_blocks import (
    ActorBlockError, BEGIN_UPDATE, END_UPDATE, active_actor_rows,
    parse_actor_updates, render_actor_inputs, strip_actor_blocks,
)


def row(actor_id="rog:a", disposition="ready_again", history=None):
    return {"actor_id":actor_id,"actor_type":"project","revision":2,
            "disposition":disposition,"state":{"work":"x","history":history or []}}


def update(actor_id="rog:a", status="running", state=None, summary="progress", error_reason=None):
    payload={"actor_id":actor_id,"status":status,"state":state or {"step":2},
             "summary":summary,"error_reason":error_reason}
    return f"{BEGIN_UPDATE}\n{json.dumps(payload)}\n{END_UPDATE}"


def test_active_rows_exclude_finished_and_error():
    rows=[row("running"),row("finished","completed"),row("error","blocked"),row("dormant","dormant")]
    assert [r["actor_id"] for r in active_actor_rows(rows)] == ["dormant","running"]


def test_actor_slots_cap_and_prioritize_lower_nice():
    rows = [
        {**row("normal-b"), "nice": 0},
        {**row("lowest"), "nice": -10},
        {**row("excluded"), "nice": 12},
        {**row("normal-a"), "nice": 0},
        {**row("high"), "nice": -5},
    ]
    selected = active_actor_rows(rows, max_slots=4)
    assert [actor["actor_id"] for actor in selected] == [
        "lowest", "high", "normal-a", "normal-b"]


def test_input_history_is_bounded_to_eight():
    text=render_actor_inputs([row(history=[{"n":n} for n in range(12)])])
    payload=json.loads(text.split("<<<AXON_ACTOR_INPUT>>>\n",1)[1].split("\n<<<END",1)[0])
    assert [entry["n"] for entry in payload["recent_history"]] == list(range(4,12))
    assert "history" not in payload["state"]


def test_duplicate_input_actor_is_rejected():
    try: render_actor_inputs([row(),row()])
    except ActorBlockError as exc: assert "duplicate actor input" in str(exc)
    else: raise AssertionError("duplicate actor input was accepted")


def test_strict_valid_parse_and_strip():
    raw="Visible reply\n"+update()
    parsed=parse_actor_updates(raw,{"rog:a"})
    assert parsed[0].actor_id == "rog:a" and parsed[0].status == "running"
    assert strip_actor_blocks(raw) == "Visible reply"


def test_missing_actor_is_rejected():
    try: parse_actor_updates(update("rog:a"),{"rog:a","rog:b"})
    except ActorBlockError as exc: assert "missing actor updates" in str(exc)
    else: raise AssertionError("missing actor was accepted")


def test_invalid_json_and_unbalanced_delimiters_are_rejected():
    for raw in (f"{BEGIN_UPDATE}\nnot json\n{END_UPDATE}", f"{BEGIN_UPDATE}\n{{}}"):
        try: parse_actor_updates(raw,{"rog:a"})
        except ActorBlockError: pass
        else: raise AssertionError("malformed actor output was accepted")


def test_error_requires_reason():
    try: parse_actor_updates(update(status="error"),{"rog:a"})
    except ActorBlockError as exc: assert "requires error_reason" in str(exc)
    else: raise AssertionError("reasonless error was accepted")


class _Response:
    def __init__(self, body): self.body=body
    def __enter__(self): return self
    def __exit__(self,*args): return False
    def read(self): return json.dumps(self.body).encode()


def test_persistence_is_revision_checked_and_appends_bounded_history():
    actor=row(history=[{"n":n} for n in range(60)])
    parsed=parse_actor_updates(update(status="finished"),{"rog:a"})[0]
    captured={}
    def open_(request,timeout=0):
        captured["url"]=request.full_url
        captured["payload"]=json.loads(request.data)
        return _Response([{"actor_id":"rog:a","revision":3}])
    with patch.object(config,"SUPABASE_URL","https://example.supabase.co"), \
         patch.object(config,"SUPABASE_ANON_KEY","key"), \
         patch("urllib.request.urlopen",open_):
        assert supabase_client.save_prompt_actor_update(actor,parsed)
    assert "revision=eq.2" in captured["url"]
    assert captured["payload"]["revision"] == 3
    assert captured["payload"]["disposition"] == "completed"
    assert len(captured["payload"]["state"]["history"]) == 50


def test_persistence_zero_rows_is_visible_failure():
    parsed=parse_actor_updates(update(),{"rog:a"})[0]
    with patch.object(config,"SUPABASE_URL","https://example.supabase.co"), \
         patch.object(config,"SUPABASE_ANON_KEY","key"), \
         patch("urllib.request.urlopen",lambda request,timeout=0:_Response([])):
        assert not supabase_client.save_prompt_actor_update(row(),parsed)

import json
from datetime import datetime, timezone
from unittest.mock import patch

import config
import supabase_client
from actor_model.prompt_blocks import (
    ActorBlockError, BEGIN_UPDATE, END_UPDATE, active_actor_rows,
    parse_actor_updates, prompt_actor_rows, render_actor_inputs, strip_actor_blocks,
)


def row(actor_id="fitness-food-coach", disposition="ready_again", history=None):
    return {"actor_id":actor_id,"actor_type":"fitness-food-coach","revision":2,
            "disposition":disposition,"nice":10,
            "state":{"role":"static coach instructions","work":"x","history":history or []}}


def update(actor_id="fitness-food-coach", status="running", state=None, summary="progress", error_reason=None):
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


def test_dirty_gate_skips_clean_low_priority_but_includes_dirty_new_and_dormant():
    now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
    clean = {**row("clean"), "last_advanced_at":"2026-08-14T11:00:00Z"}
    dirty = {**row("dirty"), "dirty":True, "last_advanced_at":"2026-08-14T11:00:00Z"}
    new = row("new")
    dormant = {**row("dormant"), "last_advanced_at":"2026-08-12T11:00:00Z"}
    calls=[]
    selected=prompt_actor_rows([clean,dirty,new,dormant],lambda actor:calls.append(actor["actor_id"]) or False,now=now)
    assert [actor["actor_id"] for actor in selected] == ["dirty","dormant","new"]
    assert calls == ["clean"]


def test_code_hash_replaces_repeated_static_role_with_compact_reference():
    sent={}
    first=render_actor_inputs([row()],sent,current_turn=1)
    second=render_actor_inputs([row()],sent,current_turn=2)
    first_payload=json.loads(first.split("<<<AXON_ACTOR_INPUT>>>\n",1)[1].split("\n<<<END",1)[0])
    second_payload=json.loads(second.split("<<<AXON_ACTOR_INPUT>>>\n",1)[1].split("\n<<<END",1)[0])
    assert first_payload["code"] == {"role":"static coach instructions"}
    assert "role" not in first_payload["state"]
    assert second_payload["code_ref"]["role_hash"] == first_payload["role_hash"]
    assert "code" not in second_payload


def test_duplicate_input_actor_is_rejected():
    try: render_actor_inputs([row(),row()])
    except ActorBlockError as exc: assert "duplicate actor input" in str(exc)
    else: raise AssertionError("duplicate actor input was accepted")


def test_strict_valid_parse_and_strip():
    raw="Visible reply\n"+update()
    parsed=parse_actor_updates(raw,{"fitness-food-coach"})
    assert parsed[0].actor_id == "fitness-food-coach" and parsed[0].status == "running"
    assert strip_actor_blocks(raw) == "Visible reply"


def test_missing_actor_is_rejected():
    try: parse_actor_updates(update("fitness-food-coach"),{"fitness-food-coach","project-coach"})
    except ActorBlockError as exc: assert "missing actor updates" in str(exc)
    else: raise AssertionError("missing actor was accepted")


def test_invalid_json_and_unbalanced_delimiters_are_rejected():
    for raw in (f"{BEGIN_UPDATE}\nnot json\n{END_UPDATE}", f"{BEGIN_UPDATE}\n{{}}"):
        try: parse_actor_updates(raw,{"fitness-food-coach"})
        except ActorBlockError: pass
        else: raise AssertionError("malformed actor output was accepted")


def test_error_requires_reason():
    try: parse_actor_updates(update(status="error"),{"fitness-food-coach"})
    except ActorBlockError as exc: assert "requires error_reason" in str(exc)
    else: raise AssertionError("reasonless error was accepted")


class _Response:
    def __init__(self, body): self.body=body
    def __enter__(self): return self
    def __exit__(self,*args): return False
    def read(self): return json.dumps(self.body).encode()


def test_prompt_actor_fetch_is_global_across_instances():
    captured={}
    rows=[
        {"actor_id":"rog:legacy","instance":"rog"},
        {"actor_id":"aevadim-09:legacy","instance":"aevadim-09"},
        {"actor_id":"global-coach","instance":"rog"},
    ]
    def open_(request,timeout=0):
        captured["url"]=request.full_url
        return _Response(rows)
    with patch.object(config,"SUPABASE_URL","https://example.supabase.co"), \
         patch.object(config,"SUPABASE_ANON_KEY","key"), \
         patch("urllib.request.urlopen",open_):
        assert supabase_client.fetch_prompt_actor_states() == rows
    assert "actor_state?order=actor_id.asc" in captured["url"]
    assert "instance=" not in captured["url"]


def test_fitness_actor_relevance_probes_food_and_fitness_timestamps():
    actor={**row(),"last_advanced_at":"2026-08-14T10:00:00+00:00"}
    paths=[]
    with patch.object(supabase_client,"_prompt_relevance_exists",
                      lambda path: paths.append(path) or False):
        assert supabase_client.prompt_actor_relevance_changed(actor) is False
    assert paths == [
        "food_entries?select=id&created_at=gt.2026-08-14T10:00:00%2B00:00&limit=1",
        "fitness_log?select=date&updated_at=gt.2026-08-14T10:00:00%2B00:00&limit=1",
    ]


def test_anton_state_actor_relevance_probes_behavior_food_and_fitness_timestamps():
    actor = {
        **row("anton-state-tracker"),
        "actor_type": "anton-state-tracker",
        "last_advanced_at": "2026-08-14T10:00:00+00:00",
    }
    paths = []
    with patch.object(supabase_client, "_prompt_relevance_exists",
                      lambda path: paths.append(path) or False):
        assert supabase_client.prompt_actor_relevance_changed(actor) is False
    assert paths == [
        "compulsive_behavior_tracking?select=id&created_at=gt.2026-08-14T10:00:00%2B00:00&limit=1",
        "food_entries?select=id&created_at=gt.2026-08-14T10:00:00%2B00:00&limit=1",
        "fitness_log?select=date&updated_at=gt.2026-08-14T10:00:00%2B00:00&limit=1",
    ]


def test_persistence_is_revision_checked_and_appends_bounded_history():
    actor=row(history=[{"n":n} for n in range(60)])
    parsed=parse_actor_updates(update(status="finished"),{"fitness-food-coach"})[0]
    captured={}
    def open_(request,timeout=0):
        captured["url"]=request.full_url
        captured["payload"]=json.loads(request.data)
        return _Response([{"actor_id":"fitness-food-coach","revision":3}])
    with patch.object(config,"SUPABASE_URL","https://example.supabase.co"), \
         patch.object(config,"SUPABASE_ANON_KEY","key"), \
         patch("urllib.request.urlopen",open_):
        assert supabase_client.save_prompt_actor_update(actor,parsed)
    assert "revision=eq.2" in captured["url"]
    assert captured["payload"]["revision"] == 3
    assert captured["payload"]["disposition"] == "completed"
    assert captured["payload"]["state"]["role"] == "static coach instructions"
    assert len(captured["payload"]["state"]["history"]) == 50
    latest = captured["payload"]["state"]["history"][-1]
    assert latest["revision"] == 3
    assert latest["status"] == "finished"
    assert latest["summary"] == "progress"
    assert "at" in latest
    assert "state" not in latest
    assert "error_reason" not in latest


def test_persistence_zero_rows_is_visible_failure():
    parsed=parse_actor_updates(update(),{"fitness-food-coach"})[0]
    with patch.object(config,"SUPABASE_URL","https://example.supabase.co"), \
         patch.object(config,"SUPABASE_ANON_KEY","key"), \
         patch("urllib.request.urlopen",lambda request,timeout=0:_Response([])):
        assert not supabase_client.save_prompt_actor_update(row(),parsed)

from actor_model.shadow import actor_is_authoritative, compare_numeric, dual_run_legacy_wins
import config
import session_manager


def test_shadow_runs_both_paths_and_legacy_wins():
    calls=[]
    def legacy(): calls.append("legacy"); return {"valence":.25}
    def actor(): calls.append("actor"); return {"valence":.75}
    compared=[]
    result=dual_run_legacy_wins(legacy,actor,lambda left,right: compared.append((left,right)))
    assert calls == ["legacy","actor"]
    assert compared == [({"valence":.25},{"valence":.75})]
    assert result == {"valence":.25}


def test_authority_requires_runtime_and_shadow_off():
    assert actor_is_authoritative(True,False) is True
    assert actor_is_authoritative(True,True) is False
    assert actor_is_authoritative(False,False) is False


def test_shadow_mismatch_is_not_silently_dropped():
    assert compare_numeric("test",{"valence":0.0},{"valence":0.01},("valence",),1e-4)


def test_session_shadow_appends_actor_events_but_keeps_legacy_live_result():
    old_runtime,old_shadow=config.ACTOR_RUNTIME_ENABLED,config.ACTOR_SHADOW_MODE
    old_append=session_manager.supabase_client.append_actor_event
    try:
        config.ACTOR_RUNTIME_ENABLED=True; config.ACTOR_SHADOW_MODE=True
        appended=[]; session_manager.supabase_client.append_actor_event=appended.append
        manager=session_manager.SessionManagerNode()
        manager._alive_valence=0.; manager._alive_valence_sigma=.2
        manager._save_full_alive_state=lambda: None
        manager._publish_clean_response=lambda item,text: None
        compared=[]; original=manager._compare_shadow_response
        def comparison(*args): compared.append(True); return original(*args)
        manager._compare_shadow_response=comparison
        manager._publish_response(session_manager.QueueItem("hello","telegram","anton"),"[VALENCE: +0.10]")
        expected,_=manager._kalman_update(0.,.2,.10)
        assert appended and compared
        assert manager._alive_valence == expected
    finally:
        config.ACTOR_RUNTIME_ENABLED=old_runtime; config.ACTOR_SHADOW_MODE=old_shadow
        session_manager.supabase_client.append_actor_event=old_append

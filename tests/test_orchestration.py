"""Real SQLite state-machine checks, independent of model/voice credentials."""

from concurrent.futures import ThreadPoolExecutor
import pytest

from audio_learning.orchestration import InvalidCommand, OrchestrationConflict, Orchestrator, wants_resume
from audio_learning.schemas import Course, Segment
from audio_learning.storage import Store, now


@pytest.fixture
def lesson(tmp_path):
    store = Store(tmp_path)
    course = Course(id="lesson", title="平方误差", source_name="lecture.txt", created_at=now(), provider="demo", segments=[
        Segment(id="unit-a", title="平方", source_excerpt="误差先平方。", source_locator="第1行", script=["先计算误差。", "再把误差平方。"]),
        Segment(id="unit-b", title="平均", source_excerpt="再取平均。", source_locator="第2行", script=["最后取平均。"]),
    ])
    store.add_course(course)
    return Orchestrator(store), course, tmp_path


def act(orchestrator, state, command, **extra):
    return orchestrator.command(state["course_id"], {"type": command, "revision": state["revision"],
                                                  "connection_id": state["connection_id"], **extra})["state"]


def claimed(orchestrator, course):
    return act(orchestrator, orchestrator.get(course.id), "claim")


def test_delivery_alone_does_not_advance_and_refresh_repeats_only_unheard_sentence(lesson):
    engine, course, _ = lesson
    state = act(engine, claimed(engine, course), "start")
    first = state["utterance"]
    assert engine.get(course.id)["sentence_index"] == 0
    state = act(engine, state, "heard", utterance_id=first["id"])
    second = state["utterance"]
    assert state["sentence_index"] == 1
    state = act(engine, state, "claim")
    assert state["utterance"] is None
    state = act(engine, state, "start")
    assert state["utterance"]["text"] == second["text"]
    assert state["utterance"]["id"] != second["id"]
    assert state["sentence_index"] == 1


def test_multiturn_discussion_persists_anchor_and_restores_exact_sentence(lesson):
    engine, course, directory = lesson
    state = act(engine, claimed(engine, course), "start")
    state = act(engine, state, "heard", utterance_id=state["utterance"]["id"])
    state = act(engine, state, "interrupt")
    anchor = {"segment_index": 0, "sentence_index": 1}
    assert state["activity"] == "listening"
    assert state["resume_anchor"] == anchor
    for question in ("为什么平方？", "假如误差很小呢？", "这与概率有什么联系？"):
        state = act(engine, state, "turn", text=question)
        assert state["utterance"]["kind"] == "discussion"
        assert "误差先平方" in state["utterance"]["prompt"]
        state = act(engine, state, "heard", utterance_id=state["utterance"]["id"], transcript="补充解释：平方使负误差变正。")
        assert state["resume_anchor"] == anchor
        assert (state["segment_index"], state["sentence_index"]) == (0, 1)
    engine = Orchestrator(Store(directory))
    state = act(engine, engine.get(course.id), "claim")
    state = act(engine, state, "start")
    assert state["activity"] == "discussing"
    assert len(state["history"]) == 6
    state = act(engine, state, "turn", text="继续课程。")
    assert state["activity"] == "resuming"
    assert state["resume_anchor"] is None
    assert state["utterance"]["text"] == "再把误差平方。"
    assert len(state["history"]) == 7


def test_old_connection_cannot_save_even_with_current_revision(lesson):
    engine, course, _ = lesson
    old = claimed(engine, course)
    state = act(engine, old, "claim")
    with pytest.raises(OrchestrationConflict):
        engine.command(course.id, {"type": "start", "revision": state["revision"], "connection_id": old["connection_id"]})
    assert engine.get(course.id) == state


def test_late_heard_after_interrupt_and_quick_restart_cannot_skip(lesson):
    engine, course, _ = lesson
    state = act(engine, claimed(engine, course), "start")
    old = state["utterance"]
    state = act(engine, state, "interrupt")
    for _ in range(3):
        state = act(engine, state, "interrupt")
    state = act(engine, state, "resume")
    with pytest.raises(OrchestrationConflict):
        act(engine, state, "heard", utterance_id=old["id"])
    assert engine.get(course.id)["sentence_index"] == 0
    assert engine.get(course.id)["utterance"]["id"] == state["utterance"]["id"]


def test_double_heard_race_advances_once(lesson):
    engine, course, _ = lesson
    state = act(engine, claimed(engine, course), "start")

    def acknowledge():
        try:
            return act(engine, state, "heard", utterance_id=state["utterance"]["id"])
        except OrchestrationConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: acknowledge(), range(2)))
    assert sum(result is not None for result in results) == 1
    assert engine.get(course.id)["sentence_index"] == 1


def test_restart_invalidates_transport_without_advancing_pending_sentence(lesson):
    engine, course, directory = lesson
    old = act(engine, claimed(engine, course), "start")
    recovered = Orchestrator(Store(directory))
    state = recovered.get(course.id)
    assert state["connection_id"] is None and state["utterance"] is None
    assert state["activity"] == "paused" and state["sentence_index"] == 0
    assert state["revision"] > old["revision"]
    with pytest.raises(OrchestrationConflict):
        act(recovered, old, "heard", utterance_id=old["utterance"]["id"])


def test_completion_requires_every_sentence_and_is_not_mastery(lesson):
    engine, course, _ = lesson
    state = act(engine, claimed(engine, course), "start")
    texts = []
    while state["utterance"]:
        texts.append(state["utterance"]["text"])
        state = act(engine, state, "heard", utterance_id=state["utterance"]["id"])
    assert texts == ["先计算误差。", "再把误差平方。", "最后取平均。"]
    assert state["completed"] and state["activity"] == "completed"
    assert engine.store.get_session(course.id).status == "completed"
    assert "mastery" not in state
    state = act(engine, state, "start")
    assert state["utterance"] is None


def test_failure_pause_preserves_anchor_and_discussion_history(lesson):
    engine, course, _ = lesson
    state = act(engine, claimed(engine, course), "start")
    state = act(engine, state, "interrupt")
    state = act(engine, state, "turn", text="负误差怎么办？")
    pending = state["utterance"]
    state = act(engine, state, "disconnect")
    assert state["history"][-1]["role"] == "user"
    assert not any(turn["role"] == "assistant" for turn in state["history"])
    assert state["resume_anchor"] == {"segment_index": 0, "sentence_index": 0}
    state = act(engine, state, "claim")
    state = act(engine, state, "start")
    with pytest.raises(OrchestrationConflict):
        act(engine, state, "heard", utterance_id=pending["id"], transcript="旧回答")


def test_changed_course_content_never_reuses_old_cursor(lesson):
    engine, course, _ = lesson
    engine.get(course.id)
    course.segments[0].script[0] = "内容已改变。"
    with engine.store.connection(write=True) as db:
        db.execute("UPDATE courses SET body=? WHERE id=?", (course.model_dump_json(), course.id))
    with pytest.raises(InvalidCommand, match="版本"):
        engine.get(course.id)


def test_explicit_resume_only(lesson):
    assert wants_resume("请继续课程！")
    assert wants_resume("resume the lesson")
    for text in ("继续课程之前，我想问为什么平方？", "我说继续课程是什么意思？", "不能继续课程", "what does resume lesson mean?"):
        assert not wants_resume(text)


def test_metrics_are_measured_values_and_invalid_input_is_atomic(lesson):
    engine, course, _ = lesson
    state = claimed(engine, course)
    state = act(engine, state, "metric", metric={"name": "interruption_stop_ms", "value": 125.5})
    event = engine.events(course.id)[-1]
    assert event["body"]["metric"]["value"] == 125.5
    assert event["body"]["metric"]["source"] == "browser_observation"
    with pytest.raises(InvalidCommand):
        act(engine, state, "metric", metric={"name": "interruption_stop_ms", "value": float("nan")})
    assert engine.get(course.id) == state
    with pytest.raises(InvalidCommand):
        act(engine, state, "turn", text="尚未打断")
    assert engine.get(course.id) == state

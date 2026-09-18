import json
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from containment.admission import replay_policy, resolve
from containment.backend import FakeBackend
from containment.cli import main
from containment.models import Outcome, Scenario, State
from containment.replay import ReplayScript
from containment.replay_journal import BudgetExceeded
from containment.store import Store
from containment.supervised_lifecycle import supervised_controller

EXAMPLES = Path(__file__).parents[1] / "examples"


@pytest.fixture
def replay_inputs():
    return (
        Scenario.model_validate_json((EXAMPLES / "replay-scenario.json").read_text()),
        ReplayScript.model_validate_json((EXAMPLES / "replay-script.json").read_text()),
    )


@contextmanager
def system(path):
    store = Store(path / "trials.sqlite3")
    try:
        with supervised_controller(store, FakeBackend(path / "resources"), path) as controller:
            yield controller
    finally:
        store.close()


def test_replay_executes_only_bounded_pure_tools_and_seals(tmp_path, replay_inputs):
    scenario, script = replay_inputs
    with system(tmp_path) as c:
        trial = c.run(scenario, replay_policy(), script)
        report = c.store.summary(trial)
        assert report["state"] == State.COMPLETE and report["outcome"] == Outcome.SIMULATED
        replay = report["replay"]
        assert replay["state"] == "complete"
        assert replay["model_calls"] == 3 and replay["tool_calls"] == 2
        assert replay["script_digest"] == script.digest()
        assert replay["output_bytes"] == sum(len(a["result"].encode()) for a in replay["actions"])
        expected_tokens = sum(len(x.encode()) for x in script.responses)
        expected_tokens += (
            len(script.prompt.encode())
            + len(script.fixtures["greeting"].encode())
            + len("Hello from replay")
        )
        assert replay["tokens"] == expected_tokens
        assert replay["model_cost_microusd"] == 0
        assert [a["result"] for a in replay["actions"] if a["kind"] == "tool"] == [
            script.fixtures["greeting"],
            "Hello from replay",
        ]
        anchor = c.store.anchor(trial)
        assert anchor.complete
        evidence = c.collector.verify(trial, anchor)
        assert any(script.digest() in r["event"]["text"] for r in evidence.receipts)
        assert c.backend.verify_cleanup(trial)


@pytest.mark.parametrize(
    "field,value", [("model_calls", 0), ("model_calls", 1), ("tool_calls", 0), ("model_tokens", 1)]
)
def test_budget_exhaustion_revokes_and_does_not_overspend(tmp_path, replay_inputs, field, value):
    scenario, script = replay_inputs
    scenario = scenario.model_copy(
        update={"budget": scenario.budget.model_copy(update={field: value})}
    )
    with system(tmp_path) as c:
        trial = c.run(scenario, replay_policy(), script)
        report = c.store.summary(trial)
        assert report["outcome"] == Outcome.ERROR
        assert c.watchdog.status(trial)["reason"] == "budget_exhausted"
        counter = "tokens" if field == "model_tokens" else field
        assert report["replay"][counter] <= value
        assert report["replay"]["state"] == "failed"


@pytest.mark.parametrize(
    "raw",
    [
        "{",
        "[]",
        '{"kind":"finish","text":"done","extra":true}',
        '{"kind":"tool","name":"shell","arguments":{"text":"touch /tmp/forbidden"}}',
        '{"kind":"tool","name":"lookup","arguments":{"key":"../../etc/passwd"}}',
        '{"kind":"tool","name":"echo","arguments":{"text":"ok","extra":"bad"}}',
    ],
)
def test_malformed_or_unapproved_action_stops_without_executing_host_tools(
    tmp_path, replay_inputs, raw
):
    scenario, script = replay_inputs
    script = script.model_copy(update={"responses": (raw,)})
    with system(tmp_path) as c:
        trial = c.run(scenario, replay_policy(), script)
        report = c.store.summary(trial)
        assert report["outcome"] == Outcome.ERROR
        assert report["replay"]["model_calls"] == 1
        assert c.watchdog.status(trial)["state"] == "stopped"
        assert not c.store.anchor(trial).complete
        assert "touch /tmp" not in report["error"]


@pytest.mark.parametrize("mode", ["response", "tool", "total", "finish"])
def test_utf8_output_limits(tmp_path, replay_inputs, mode):
    scenario, script = replay_inputs
    if mode == "response":
        script = script.model_copy(update={"max_response_bytes": 1})
    elif mode == "total":
        script = script.model_copy(update={"total_output_bytes": 1})
    elif mode == "tool":
        script = script.model_copy(update={"fixtures": {"greeting": "éé"}, "max_output_bytes": 3})
    else:
        script = script.model_copy(
            update={"responses": ('{"kind":"finish","text":"éé"}',), "max_output_bytes": 3}
        )
    with system(tmp_path) as c:
        trial = c.run(scenario, replay_policy(), script)
        report = c.store.summary(trial)
        assert report["outcome"] == Outcome.ERROR
        assert report["replay"]["output_bytes"] <= script.total_output_bytes
        assert report["replay"]["state"] == "failed"


def test_replay_requires_explicit_finish(tmp_path, replay_inputs):
    scenario, script = replay_inputs
    script = script.model_copy(update={"responses": script.responses[:1]})
    with system(tmp_path) as c:
        trial = c.run(scenario, replay_policy(), script)
        assert c.store.get(trial)["outcome"] == Outcome.ERROR


@pytest.mark.parametrize("phase", ["model", "tool"])
def test_crash_retains_reservation_and_recovery_never_replays(
    tmp_path, replay_inputs, monkeypatch, phase
):
    scenario, script = replay_inputs
    import containment.replay as replay_module

    calls = []
    with system(tmp_path) as c:
        with monkeypatch.context() as patch:
            if phase == "model":

                def crash(*_args, **_kwargs):
                    raise KeyboardInterrupt

                patch.setattr(c.store.replay, "settle", crash)
            else:
                original = replay_module.execute_tool

                def crash(call, config):
                    calls.append(original(call, config))
                    raise KeyboardInterrupt

                patch.setattr(replay_module, "execute_tool", crash)
            with pytest.raises(KeyboardInterrupt):
                c.run(scenario, replay_policy(), script)
            trial = UUID(c.store.records()[0]["id"])
            before = c.store.replay.summary(trial)
            assert before["actions"][-1]["state"] == "reserved"
    with system(tmp_path) as c:
        monkeypatch.setattr(
            replay_module, "execute_tool", lambda *_: pytest.fail("Action replayed")
        )
        assert c.reconcile() == [trial]
        after = c.store.replay.summary(trial)
        assert after["state"] == "interrupted"
        assert after["actions"][-1]["state"] == "uncertain"
        assert (
            after["tokens"] == before["tokens"] and after["output_bytes"] == before["output_bytes"]
        )
        assert c.store.get(trial)["state"] == State.COMPLETE
        assert not c.store.anchor(trial).complete
        with pytest.raises(RuntimeError, match="closed"):
            c.store.replay.reserve(trial, "tool", output_bytes=1)
    assert len(calls) == (1 if phase == "tool" else 0)


def test_evidence_failure_blocks_tool_call(tmp_path, replay_inputs, monkeypatch):
    scenario, script = replay_inputs
    with system(tmp_path) as c:
        original = c.supervisor.ingest

        def fault(trial, token, raw):
            if "tool_reserved" in raw.decode():
                c.collector.db.execute(
                    "UPDATE collections SET fault = 1 WHERE id = ?", (str(trial),)
                )
            return original(trial, token, raw)

        monkeypatch.setattr(c.supervisor, "ingest", fault)
        monkeypatch.setattr(
            "containment.replay.execute_tool",
            lambda *_: pytest.fail("Tool ran after evidence fault"),
        )
        trial = c.run(scenario, replay_policy(), script)
        assert c.watchdog.status(trial)["reason"] == "evidence_loss"
        assert c.store.replay.summary(trial)["actions"][-1]["state"] == "uncertain"


def test_reservation_scope_replay_and_settlement_guards(tmp_path, replay_inputs):
    scenario, script = replay_inputs
    with system(tmp_path) as c:
        trial = uuid4()
        c.store.create(trial, resolve(scenario, replay_policy()))
        journal = c.store.replay
        journal.register(trial, script.digest(), scenario.budget, 100)
        with pytest.raises(KeyError):
            journal.reserve(uuid4(), "model", tokens=1, output_bytes=1)
        action = journal.reserve(trial, "model", tokens=10, output_bytes=10)
        with pytest.raises(RuntimeError, match="Uncertain"):
            journal.reserve(trial, "model", tokens=1, output_bytes=1)
        with pytest.raises(BudgetExceeded):
            journal.settle(trial, action, "elevenbytes!", tokens=1)
        assert journal.summary(trial)["tokens"] == 10
        journal.settle(trial, action, "ok", tokens=2)
        with pytest.raises(RuntimeError):
            journal.settle(trial, action, "ok", tokens=2)
        assert journal.summary(trial)["tokens"] == 2


def test_concurrent_reservations_cannot_overspend(tmp_path, replay_inputs):
    scenario, script = replay_inputs
    trial = uuid4()
    path = tmp_path / "trials.sqlite3"
    store = Store(path)
    store.create(trial, resolve(scenario, replay_policy()))
    budget = scenario.budget.model_copy(update={"tool_calls": 1})
    store.replay.register(trial, script.digest(), budget, 10)
    store.close()

    def reserve():
        connection = Store(path)
        try:
            action = connection.replay.reserve(trial, "tool", output_bytes=1)
            connection.replay.settle(trial, action, "x")
            return True
        except (BudgetExceeded, RuntimeError):
            return False
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: reserve(), range(2))) == [False, True]


def test_cli_replay_and_validation(tmp_path, capsys):
    scenario, script = str(EXAMPLES / "replay-scenario.json"), str(EXAMPLES / "replay-script.json")
    assert main(["validate", scenario, "--replay"]) == 0
    capsys.readouterr()
    assert main(["--state-dir", str(tmp_path / "state"), "replay", scenario, script]) == 0
    assert json.loads(capsys.readouterr().out)[0]["replay"]["state"] == "complete"
    bad = tmp_path / "oversized.json"
    bad.write_bytes(b" " * 1_048_577)
    assert main(["--state-dir", str(tmp_path / "unused"), "replay", scenario, str(bad)]) == 2
    assert not (tmp_path / "unused").exists()


def test_expired_model_response_cannot_trigger_tool(tmp_path, replay_inputs, monkeypatch):
    scenario, script = replay_inputs
    with system(tmp_path) as c:
        original = c.store.replay.settle

        def slow(*args, **kwargs):
            original(*args, **kwargs)
            c.watchdog.clock = lambda: time.monotonic() + 10

        monkeypatch.setattr(c.store.replay, "settle", slow)
        monkeypatch.setattr(
            "containment.replay.execute_tool", lambda *_: pytest.fail("Tool after expiry")
        )
        trial = c.run(scenario, replay_policy(), script)
        assert c.watchdog.status(trial)["reason"] == "lease_expired"
        assert c.store.replay.summary(trial)["tool_calls"] == 0


def test_known_tool_still_requires_explicit_permission(tmp_path, replay_inputs):
    scenario, script = replay_inputs
    script = script.model_copy(update={"allowed_tools": frozenset({"lookup"})})
    with system(tmp_path) as c:
        trial = c.run(scenario, replay_policy(), script)
        replay = c.store.replay.summary(trial)
        assert replay["model_calls"] == 2 and replay["tool_calls"] == 1
        assert replay["state"] == "failed"


def test_reservation_write_failure_prevents_action(tmp_path, replay_inputs, monkeypatch):
    scenario, script = replay_inputs
    with system(tmp_path) as c:
        with c.store.db:
            c.store.db.execute("""CREATE TRIGGER fail_action BEFORE INSERT ON replay_actions
                                  BEGIN SELECT RAISE(ABORT, 'unavailable'); END""")
        monkeypatch.setattr(
            "containment.replay.execute_tool", lambda *_: pytest.fail("Unreserved tool")
        )
        trial = c.run(scenario, replay_policy(), script)
        replay = c.store.replay.summary(trial)
        assert replay["model_calls"] == 0 and replay["actions"] == []
        assert replay["state"] == "failed"


def test_crash_after_committed_tool_result_does_not_reexecute(tmp_path, replay_inputs, monkeypatch):
    scenario, script = replay_inputs
    with system(tmp_path) as c:
        settle = c.store.replay.settle

        def committed(trial, sequence, *args, **kwargs):
            settle(trial, sequence, *args, **kwargs)
            if sequence == 2:
                raise KeyboardInterrupt

        with monkeypatch.context() as patch:
            patch.setattr(c.store.replay, "settle", committed)
            with pytest.raises(KeyboardInterrupt):
                c.run(scenario, replay_policy(), script)
    with system(tmp_path) as c:
        monkeypatch.setattr(
            "containment.replay.execute_tool", lambda *_: pytest.fail("Repeated tool")
        )
        trial = UUID(c.store.records()[0]["id"])
        c.reconcile()
        replay = c.store.replay.summary(trial)
        assert replay["state"] == "interrupted"
        assert replay["actions"][-1]["state"] == "done"
        assert replay["actions"][-1]["result"] == script.fixtures["greeting"]

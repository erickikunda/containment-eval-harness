import asyncio
import json
import time
from pathlib import Path

import pytest
from model_stub import model_server
from test_replay import system

from containment.admission import development_policy, local_model_policy, replay_policy, resolve
from containment.cli import main
from containment.local_model import LocalModelScript, prompt_for
from containment.model_transport import _supervised_post
from containment.models import Scenario
from containment.replay import ReplayScript
from containment.replay_journal import BudgetExceeded
from containment.store import Store

EXAMPLES = Path(__file__).parents[1] / "examples"


def inputs(port):
    scenario = Scenario.model_validate_json((EXAMPLES / "local-model-scenario.json").read_text())
    script = LocalModelScript.model_validate_json(
        (EXAMPLES / "local-model-script.json").read_text()
    )
    return scenario, script.model_copy(update={"port": port})


def test_live_transport_tools_tokens_evidence_and_reproducible_config(tmp_path):
    with model_server() as (port, calls), system(tmp_path) as c:
        scenario, script = inputs(port)
        trial = c.run(scenario, local_model_policy(), script)
        report = c.store.summary(trial)
        run = report["replay"]
        assert report["outcome"] == "simulation_only" and run["state"] == "complete"
        assert run["accounting"] == "server_reported_tokens"
        assert run["metadata"]["configuration"]["port"] == port
        assert run["metadata"]["server_termination_confirmed"] is False
        assert run["model_calls"] == 3 and run["tool_calls"] == 2
        assert [x[0] for x in calls] == ["/tokenize", "/completion"] * 3
        completions = [payload for path, payload in calls if path == "/completion"]
        assert all(p["n_predict"] == 256 and p["stream"] is False for p in completions)
        assert run["tokens"] == sum(len(p["prompt"]) for p in completions) + sum(
            len(a["result"].encode()) for a in run["actions"] if a["kind"] == "model"
        )
        assert run["actions"][-1]["result"].endswith('Hello from a synthetic fixture"}')
        assert c.store.anchor(trial).complete
        assert c.collector.verify(trial, c.store.anchor(trial))


@pytest.mark.parametrize(
    "field,value",
    [
        ("tokens_predicted", None),
        ("tokens_predicted", True),
        ("tokens_predicted", 257),
        ("tokens_evaluated", 0),
        ("tokens_evaluated", True),
        ("truncated", True),
        ("stop", False),
        ("content", "a" * 2049),
        ("content", []),
    ],
)
def test_bad_usage_or_response_preserves_reservation_without_tools(tmp_path, field, value):
    def corrupt(path, reply):
        if path == "/completion":
            reply[field] = value
        return reply

    with model_server(corrupt) as (port, calls), system(tmp_path) as c:
        scenario, script = inputs(port)
        trial = c.run(scenario, local_model_policy(), script)
        report = c.store.summary(trial)
        assert report["outcome"] == "error"
        assert report["replay"]["tool_calls"] == 0
        assert report["replay"]["tokens"] == 2304
        assert report["replay"]["actions"][0]["state"] == "uncertain"
        assert len(calls) == 2
        assert not c.store.anchor(trial).complete


@pytest.mark.parametrize("tokens", [[], [True], [-1], [2147483648], [1] * 2049, "wrong"])
def test_tokenization_fails_before_generation(tmp_path, tokens):
    with (
        model_server(lambda path, reply: {"tokens": tokens}) as (port, calls),
        system(tmp_path) as c,
    ):
        scenario, script = inputs(port)
        trial = c.run(scenario, local_model_policy(), script)
        assert c.store.summary(trial)["outcome"] == "error"
        assert len(calls) == 1


@pytest.mark.parametrize("phase", ["/tokenize", "/completion"])
def test_timeout_retains_charge_and_recovery_never_retries(tmp_path, phase):
    def stall(path, reply):
        if path == phase:
            time.sleep(0.4)
        return reply

    with model_server(stall) as (port, calls), system(tmp_path) as c:
        scenario, script = inputs(port)
        script = script.model_copy(update={"request_timeout_seconds": 0.15})
        start = time.monotonic()
        trial = c.run(scenario, local_model_policy(), script)
        assert time.monotonic() - start < 0.4
        run = c.store.summary(trial)["replay"]
        assert run["state"] == "failed" and run["actions"][0]["state"] == "uncertain"
        assert run["tokens"] == 2304 and run["tool_calls"] == 0
        count = len(calls)
        assert c.reconcile() == []
        assert len(calls) == count


def test_deadline_while_waiting_cancels_before_tools(tmp_path):
    def stall(path, reply):
        time.sleep(1.5)
        return reply

    with model_server(stall) as (port, calls), system(tmp_path) as c:
        scenario, script = inputs(port)
        scenario = scenario.model_copy(
            update={"budget": scenario.budget.model_copy(update={"wall_seconds": 1})}
        )
        trial = c.run(scenario, local_model_policy(), script)
        assert c.store.summary(trial)["outcome"] == "error"
        assert c.store.summary(trial)["replay"]["tool_calls"] == 0
        assert c.watchdog.status(trial)["state"] == "stopped"
        assert len(calls) == 1


def test_budget_rejected_before_contacting_model(tmp_path):
    with model_server() as (port, calls), system(tmp_path) as c:
        scenario, script = inputs(port)
        scenario = scenario.model_copy(
            update={"budget": scenario.budget.model_copy(update={"model_tokens": 2303})}
        )
        trial = c.run(scenario, local_model_policy(), script)
        assert c.store.summary(trial)["outcome"] == "error"
        assert calls == []


def test_evidence_fault_during_wait_cancels_transport(tmp_path, monkeypatch):
    def stall(path, reply):
        time.sleep(0.4)
        return reply

    with model_server(stall) as (port, calls), system(tmp_path) as c:
        scenario, script = inputs(port)
        original = c.collector.health

        def health(trial):
            if calls:
                raise OSError("Injected evidence outage while request is in flight")
            return original(trial)

        monkeypatch.setattr(c.collector, "health", health)
        trial = c.run(scenario, local_model_policy(), script)
        report = c.store.summary(trial)
        assert report["outcome"] == "error"
        assert report["replay"]["actions"][0]["state"] == "uncertain"
        assert report["replay"]["tool_calls"] == 0
        assert c.watchdog.status(trial)["reason"] == "evidence_loss"
        assert len(calls) == 1


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        '{"kind":"tool","name":"shell","arguments":{"text":"ignored"}}',
        '{"kind":"tool","name":"lookup","arguments":{"key":"../../etc/passwd"}}',
        '{"kind":"finish","text":"' + "a" * 257 + '"}',
    ],
)
def test_model_output_cannot_expand_tool_authority(tmp_path, content):
    def replace(path, reply):
        if path == "/completion":
            reply["content"] = content
        return reply

    with model_server(replace) as (port, calls), system(tmp_path) as c:
        scenario, script = inputs(port)
        trial = c.run(scenario, local_model_policy(), script)
        report = c.store.summary(trial)
        assert report["outcome"] == "error"
        assert report["replay"]["model_calls"] == 1
        assert not any(
            a["kind"] == "tool" and a["state"] == "done" for a in report["replay"]["actions"]
        )
        assert len(calls) == 2


def test_stream_drip_cannot_extend_total_request_deadline():
    async def check():
        disconnected = asyncio.Event()

        async def handle(reader, writer):
            try:
                await reader.read(65536)
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\n{")
                await writer.drain()
                # This server never completes its body. Cancellation must close the peer socket.
                assert await reader.read(1) == b""
            finally:
                writer.close()
                await writer.wait_closed()
                disconnected.set()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        async with server:
            port = server.sockets[0].getsockname()[1]
            with pytest.raises(TimeoutError):
                await _supervised_post(port, "/tokenize", {}, 0.15, lambda: None)
            await asyncio.wait_for(disconnected.wait(), timeout=1)

    asyncio.run(check())


def test_local_admission_and_runner_pairing_fail_closed(tmp_path):
    scenario, script = inputs(8080)
    for policy in (development_policy(), replay_policy()):
        with pytest.raises(ValueError):
            resolve(scenario, policy)
    with system(tmp_path) as c, pytest.raises(ValueError):
        c.run(scenario, local_model_policy())
    assert resolve(scenario, local_model_policy()).admission == "static_only"
    with pytest.raises(ValueError):
        resolve(scenario.model_copy(update={"inference": "private_service"}), local_model_policy())


def test_prompt_limit_does_not_truncate():
    _, script = inputs(8080)
    with pytest.raises(BudgetExceeded):
        prompt_for(script, [{"result": "x" * 32768}])


def test_old_replay_journal_migration_preserves_results_and_byte_accounting(tmp_path):
    scenario = Scenario.model_validate_json((EXAMPLES / "replay-scenario.json").read_text())
    script = ReplayScript.model_validate_json((EXAMPLES / "replay-script.json").read_text())
    with system(tmp_path) as c:
        trial = c.run(scenario, replay_policy(), script)
        before = c.store.replay.summary(trial)
        # Restore the previous slice's schema with a completed replay already persisted.
        c.store.db.execute("ALTER TABLE replay_runs DROP COLUMN metadata_json")
        c.store.db.commit()
    store = Store(tmp_path / "trials.sqlite3")
    try:
        assert store.replay.summary(trial) == before
        assert before["accounting"] == "utf8_byte_units_not_provider_tokens"
    finally:
        store.close()


def test_cli_local_model(tmp_path, capsys):
    with model_server() as (port, calls):
        _, script = inputs(port)
        path = tmp_path / "script.json"
        path.write_text(script.model_dump_json())
        assert (
            main(
                [
                    "--state-dir",
                    str(tmp_path / "state"),
                    "local-model",
                    str(EXAMPLES / "local-model-scenario.json"),
                    str(path),
                ]
            )
            == 0
        )
        assert json.loads(capsys.readouterr().out)[0]["replay"]["model_calls"] == 3


@pytest.mark.parametrize(
    "response",
    [
        b"HTTP/1.1 302 Found\r\nLocation: http://example.com\r\nContent-Length: 2\r\n\r\n{}",
        b"HTTP/1.1 200 OK\r\nContent-Length: 131073\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Length: 2\r\n\r\n{}",
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Encoding: gzip\r\n\r\n{}",
        b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\n{}",
        b"HTTP/1.1 200 OK\r\nX-Long: " + b"a" * 8192 + b"\r\n\r\n",
    ],
)
def test_transport_rejects_unsafe_or_unbounded_responses(response):
    async def check():
        async def handle(reader, writer):
            await reader.read(65536)
            writer.write(response)
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        async with server:
            port = server.sockets[0].getsockname()[1]
            with pytest.raises(
                (ValueError, asyncio.IncompleteReadError, asyncio.LimitOverrunError)
            ):
                await _supervised_post(port, "/tokenize", {}, 1, lambda: None)

    asyncio.run(check())

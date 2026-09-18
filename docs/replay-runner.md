# Bounded replay runner

The `replay` command exercises an agent-style response/tool loop using prewritten responses and
two pure tools. It runs inside the supervised fake lifecycle, with evidence health checks, watchdog
leases, budget reservations, evidence sealing, and recovery. It does not call a model, execute host
commands, access arbitrary files, or contact a network. It is not an autonomous agent or a real
containment evaluation.

## Run it

From the repository root after installing the locked development dependencies:

```sh
uv run containment validate examples/replay-scenario.json --replay
uv run containment --state-dir .harness/replay-demo replay examples/replay-scenario.json examples/replay-script.json
uv run containment --state-dir .harness/replay-demo list
uv run containment --state-dir .harness/replay-demo reconcile
```

The existing `simulate` command and its zero model/tool-call development policy remain available.
`replay` uses a separate local-only policy: at most 32 scripted model responses, 32 tool calls,
262144 replay accounting units, and zero model cost. Scenarios must still pass normal fake/replay
admission. Their declared budgets can be lower than those ceilings. `validate --replay` selects
this policy for static validation; it does not execute or validate response semantics.

The scenario and script envelope are validated before CLI trial state is created. Script files are
limited to 1 MiB; a script has at most 32 responses, 16 fixture values, and schema bounds on all
strings and outputs.
Each invocation starts a new trial after reconciling prior unfinished work. It is not a retry or
resume of a prior trial.

For the offline Docker workflow, build and run:

```sh
sh scripts/check-docker-simulation.sh
```

This now exercises normal and interrupted simulations plus normal and interrupted replay trials.
It verifies four evidence seals from fresh containers and checks that an interrupted tool action
remains uncertain with its output reservation retained. The [Docker guide](../deployment/docker-local.md)
also provides a persistent-state helper; with that helper defined:

```sh
harness_local replay /examples/replay-scenario.json /examples/replay-script.json
```

## Scripted responses and tools

The script contains a prompt, response strings, an explicit `allowed_tools` set, synthetic fixture
values, and output limits. Each response string is interpreted as untrusted JSON only after the
model-call reservation commits. Examples:

```json
{"kind":"tool","name":"echo","arguments":{"text":"hello"}}
```

```json
{"kind":"tool","name":"lookup","arguments":{"key":"greeting"}}
```

```json
{"kind":"finish","text":"Replay completed"}
```

`echo` returns its text argument. `lookup` returns the value for an exact logical key in the
supplied fixture dictionary. These are the only implemented tools, and each still requires
permission in `allowed_tools`. A lookup key is not a filesystem path. Unknown tools, missing or
extra arguments, unavailable fixture keys, malformed messages, and extra message fields stop the
trial. Arbitrary code, shell commands, URLs, plugin tools, and dynamic tool registration are not
supported. The loop requires an explicit finish response; running out of responses is an error.

The script is deterministic: later responses do not change based on tool output. The initial
prompt and then the most recent tool result serve as input for accounting; there is no growing
conversation history, tokenizer, model sampler, or inference transport. This tests runner control
flow before those adapters exist.

## Budget accounting

`trials.sqlite3` gains additive `replay_runs` and `replay_actions` tables. Each replay is bound to
its trial and a digest of the script, including permissions and fixtures. The script digest is
also emitted into the sealed evidence stream. Keep the original script and scenario for
reproduction; the digest alone cannot reconstruct them.

Reservations use SQLite `BEGIN IMMEDIATE` transactions:

| Resource | Reserved before action | Settled after known result |
| --- | --- | --- |
| Model calls | One call | Never refunded |
| Tool calls | One call | Never refunded |
| Replay token units | Input UTF-8 bytes plus maximum response bytes | Input bytes plus actual response bytes |
| Model response output | Maximum response bytes | Actual response UTF-8 bytes |
| Tool output | Maximum tool-output bytes | Actual result UTF-8 bytes |

**Replay units are not real provider tokens.** The scenario's `model_tokens` field supplies their
budget in this mode; reports label accounting as `utf8_byte_units_not_provider_tokens`. Monetary
cost is exactly zero because no model service is called. This does not implement provider pricing
or real tokenizer accounting.

`max_response_bytes` is at most 8192, `max_output_bytes` at most 4096, and cumulative
`total_output_bytes` at most 65536. The cumulative counter includes model response strings and tool
results. Final text must also fit the per-output bound; it is already included in its model
response and is not charged twice. Unicode limits count UTF-8 bytes, not characters.

Remaining budget must cover the worst-case reservation, even if the eventual response would be
smaller. Unused capacity is released only when a bounded result is successfully committed. An
oversized or unsettled result keeps its reservation charged. A malformed but bounded response
can already have a settled model-call charge before semantic validation rejects it. An unknown or
disallowed tool is rejected before tool dispatch/reservation; an allowed tool's invalid arguments
can leave a conservative uncertain reservation without an actual effect.

These counters bound replay accounting and retained result data, not total SQLite disk usage or
real CPU/memory consumption. A real inference adapter needs bounded transport, timeouts, tokenizer
and cost accounting, and independently enforced resources.

## Evidence, stop behavior, and recovery

Before each response and tool dispatch, the runner checks evidence health and renews its lease.
Reservation and completion metadata go through the evidence supervisor. Model/tool completion
events include output sizes and hashes. Bounded response/result text is retained in the replay
journal, and the final text is recorded as a guest note. Treat stored text as untrusted data;
it never becomes a command. The observer role here is synthetic controller instrumentation, not
an independent witness of an escape.

Budget exhaustion revokes the lease with `budget_exhausted`; invalid responses/actions revoke it
with `health_failure`. Earlier evidence or clock failures retain their original stop reason.
The normal lifecycle confirms termination, seals evidence, and cleans up the fake marker. A
failed replay normally has incomplete evidence. Stop or finalization uncertainty can quarantine
it instead. Replay `state: complete` means the scripted loop reached finish; inspect the overall
trial state, outcome, and evidence seal as well.

Each action moves from `reserved` to `done` after settlement. If the process dies before settlement,
recovery changes it to `uncertain`, retaining its full charge and a null result. An uncertain
record means “we cannot establish completion,” not “the action definitely happened.” A crash after
settlement retains the known result as `done`. Neither case replays the action: reconciliation
stops the trial, marks an active replay interrupted, and preserves the evidence that exists.

An unfinished reservation blocks further dispatch on that trial. There is no automatic refund,
retry, resume, or reset command. Storage failures propagate; a failed reservation commit must not
dispatch a tool. The same process still owns the collector and watchdog, so a blocked process or
future blocking adapter cannot rely on this loop for independent real-time termination. Current
tools are bounded, pure in-process functions. Real execution remains disabled.

## Reports and exit status

Trial summaries include a `replay` object (null for non-replay trials) with script digest, limits,
consumed/reserved counters, run state, and ordered action records. Counters retain charges for
uncertain actions; they are not just totals of completed work.

Exit `0` means the command returned no error outcome or quarantine. Exit `1` indicates a returned
error/quarantine, while input/setup errors return `2`. `reconcile` still returns only records it
processed and does not release existing quarantine. Evidence completeness must be read separately.

## Tests and next steps

```sh
uv run pytest tests/test_replay.py -q
```

Coverage includes valid tools, permission denial, malformed/oversized responses, UTF-8 limits,
budget exhaustion, concurrent reservations, duplicate settlement, wrong-trial scope, evidence
loss, lease expiry, storage failure, and crashes before/after tool settlement. Docker additionally
uses a real SIGKILL after a pure tool executes but before its result is committed.

Next adapters can connect a local model or private inference service to the same reservation and
evidence concepts. They require explicit protocol, identity, timeout, accounting, and deployment
work. Do not replace the pure tools with host execution to approximate that future work.

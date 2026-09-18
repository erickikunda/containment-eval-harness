# Local model adapter and Docker rehearsal

`containment local-model` connects the supervised lab runner to a local llama.cpp server.
It can run a model-driven loop with the same two pure tools as replay: `echo` and lookup in
explicit synthetic fixtures. It accepts only fake deployment, simulation boundary, validation
track, local inference, and disconnected connectivity. Outcomes remain `simulation_only` because
the containment backend is still a resource marker. No shell, filesystem, network, or process tool
is exposed to the model.

The adapter uses Python's standard library; no new Python dependency or model download is required.
A model runtime and its weights must be provisioned separately. The automated rehearsal uses a
test server and **does not demonstrate real inference or model quality**.

## Run the automated Docker rehearsal

From the repository root, with Docker running and uv available:

```sh
uv sync --locked --extra dev
sh scripts/check-docker-simulation.sh
```

The connected build prepares the installed package image. All test containers then run with
`--network none`, UID 10001, a read-only root filesystem, no capabilities, and resource limits.
Only loopback is available; no port is published. The new test server runs inside that network
namespace and exposes the two expected HTTP endpoints. It returns predetermined responses based
on the tool history and uses byte values as fake token IDs; it loads no weights.

In addition to the existing four simulation/replay trials, the model-adapter rehearsal checks:

1. A complete tokenize/generate/lookup/generate/echo/generate/finish sequence through the CLI.
2. A generation timeout with a retained uncertain reservation and no tool dispatch.
3. SIGKILL of the controller process after the server receives a generation request, followed by
   reconciliation from a fresh container without a model server.
4. Three retained seals, incomplete evidence on failures, cleanup, and idempotent reconciliation.

Reports are saved under the printed `.harness/model-lab.*` directory. The dedicated temporary
volume is removed on success and retained on failure. After building the current image, the model
checks alone can be run with `sh scripts/check-docker-model.sh`. CI runs both sets of checks.

## Connect an actual local runtime

Use a dedicated llama.cpp server with a prepositioned model, bound to `127.0.0.1:8080` in the same
network namespace as the controller. Keep its model fixed for the entire trial; disable unrelated
server features/tools and do not use a shared multi-model router. The adapter cannot authenticate
the process listening on loopback. Run the lab only where that namespace is trusted.

For an already installed `llama-server`, a starting configuration is:

```sh
llama-server --model /absolute/path/to/model.gguf --host 127.0.0.1 --port 8080 \
  --ctx-size 4096 --parallel 1
```

This starts a long-running server separately from the harness. Hardware limits, model loading,
and stopping that server are operator responsibilities. The harness never launches a command
chosen by configuration or by model output. In Docker, use a separately provisioned, pinned model
image and read-only weights; place both processes in a network namespace with no external
interfaces (for example, the controller joining an offline server container's namespace).
No model image is bundled, downloaded, or tested by this PR. Model-specific image commands depend
on the selected runtime image and hardware.

Copy `examples/local-model-script.json` to a working configuration and replace `model_identity`
and `runtime_identity` with the model file SHA-256 and runtime build/image digest. These are
operator-supplied provenance labels, not server attestations. Set `port` if necessary. Then run:

```sh
uv run containment validate examples/local-model-scenario.json --local-model
uv run containment --state-dir .harness-model local-model \
  examples/local-model-scenario.json /absolute/path/to/local-model-script.json
uv run containment --state-dir .harness-model list
```

The example requests lookup and echo of a synthetic greeting, then a final answer. Models can
refuse, return invalid JSON, finish early, or exhaust their budget. A successful `finish` means
the runner completed; it does not grade whether the model followed every task instruction.
The prompt uses a plain completion scaffold, not a model-specific chat template. Select a
completion-capable model and evaluate its behavior before relying on the example as a demonstration.

Exit codes follow replay: 0 for a completed lab run; 1 for a persisted error/quarantine outcome;
2 for input/setup failures. Keep the trial report and inspect the `replay` field (the existing
journal name retained for compatibility). Its metadata distinguishes `local_llama_cpp` from
scripted replay. On interruption, use `reconcile` with the same state directory; it never resumes
or retries model/tool actions. Reconciliation confirms marker cleanup, not model-server shutdown.

## Request and accounting contract

The adapter uses llama.cpp's native `/tokenize` and `/completion` endpoints, not its chat or tool
execution APIs. It creates a prompt from the task, allowed tool descriptions/fixture keys, and
prior bounded responses/results. The complete context must fit 32 KiB; it is never silently
truncated. Fixture values enter the context only through an authorized lookup.

Before contacting the server, the runner atomically reserves one model call,
`max_input_tokens + max_new_tokens`, and `max_response_bytes` in SQLite. Tokenization occurs inside
that reservation. Token IDs must be nonnegative integers and fit the input allowance; otherwise
generation is never requested. The exact returned IDs are sent as the completion prompt.
The request disables prompt caching and streaming, requests one completion with a finite
`n_predict`, and uses fixed temperature/seed settings plus JSON-object grammar. Reproducibility
still depends on the runtime, model, and hardware.

Completion settlement requires a bounded text result, matching `tokens_evaluated`, bounded
`tokens_predicted`, and no reported context truncation. The journal records input plus generated
tokens as **server-reported tokens**, distinct from replay's byte units. It does not independently
validate the tokenizer or meter server computation. The upstream API notes that `n_predict` can
slightly overshoot for partial multibyte characters; this adapter rejects such an over-limit
response and retains the reservation. This cannot undo computation already performed by a server.

Unused reserved capacity is released only after a valid bounded result is committed. A timeout,
disconnect, invalid usage report, or crash leaves the full reservation charged and uncertain.
There are no automatic retries. A bounded but semantically invalid model message may already have
settled usage; it still stops the loop before unauthorized tool execution. Tool and cumulative
output budgets retain the replay rules.

The supported profile has no metered provider billing. `model_cost_microusd: 0` means no provider
cost integration, not zero hardware or electricity cost. Paid/private services need a separate
adapter with authenticated scope, pricing, and provider-specific usage semantics.

## Transport, evidence, and cancellation limits

Only numeric IPv4 loopback and the configured port are used. There is no DNS, proxy environment
support, URL override, redirect following, authentication header, or retry. Request JSON is capped
at 256 KiB, response headers at 8 KiB, and response bodies at 128 KiB. HTTP responses must have a
single valid Content-Length; chunked/compressed bodies and non-200 status fail closed. This is a
deliberately narrow HTTP profile, not a general HTTP client.

Each endpoint request has an absolute timeout, including headers and body. The runner checks
evidence health and renews its lease approximately every 100 ms while awaiting I/O. The trial's
hard deadline still bounds the entire run. A failed check cancels the task and closes the local
socket before any subsequent tool dispatch. These checks run in the controller process; a stalled
or killed controller does not provide an independent watchdog for the model server.

`server_termination_confirmed` and `server_identity_verified` remain false. A closed connection,
response flag, or cleaned fake marker is not proof that server work stopped. An interrupted trial
must never be treated as safe server reuse without the operator inspecting/stopping the dedicated
runtime. Independent termination and authenticated private-service inference remain future work.

The full bounded configuration, its digest, prompt hashes, response/result records, and usage are
retained locally. Operator-supplied runtime/model labels are explicitly unverified. Treat stored
model text as untrusted data. Seals remain local lab evidence, not externally protected production
evidence. Transport/tool-loop tests do not establish AWS containment.

Protocol reference: [llama.cpp server API](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md).
Compatibility with the selected pinned runtime and real weights still requires a live acceptance
run; the test server is only a wire-contract fixture.

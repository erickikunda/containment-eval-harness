# Local Ollama adapter and Docker rehearsal

`containment local-model` connects the supervised lab runner to an already running local Ollama
server. The model can choose between two pure tools: `echo` and lookup in explicit synthetic
fixtures. Only fake deployment, simulation boundary, validation track, local inference, and
disconnected connectivity are admitted. Outcomes remain `simulation_only`: the containment
backend is still a resource marker, even when inference is real. No shell, filesystem, network,
or process tool is exposed to the model.

The adapter uses Python's standard library. It never installs, pulls, creates, or deletes a model,
and does not start or restart Ollama. Private/cloud inference is outside this slice.

## Use your installed Ollama model

Ollama must listen on `127.0.0.1:11434` in the same network namespace as the controller. Inspect
what is installed and the running version:

```sh
ollama list
curl http://127.0.0.1:11434/api/tags
curl http://127.0.0.1:11434/api/version
```

Copy `examples/local-model-script.json` to a working file. Set `model` to the full installed name
including its tag, `model_digest` to its 64-character digest from the inventory, and
`runtime_identity` to the runtime version/build label. The example names
`LiquidAI/lfm2.5-1.2b-instruct:latest`; another installed completion model can be selected by changing
both the name and digest. No automatic download occurs if it is absent.

```sh
uv sync --locked --extra dev
uv run containment validate examples/local-model-scenario.json --local-model
uv run containment --state-dir .harness-model local-model \
  examples/local-model-scenario.json examples/local-model-script.json
uv run containment --state-dir .harness-model list
```

The example asks for a greeting lookup, an echo of its value, and a final answer. Before each
inference request, the adapter checks the installed model's reported digest and rejects remote
model metadata, cloud-named models, and non-GGUF inventory entries. This is a consistency check
against a trusted local server, not an authenticated attestation or protection against a concurrent
model replacement. Keep the selected model fixed during a trial.

Use an operator-controlled local runtime with cloud features disabled (`OLLAMA_NO_CLOUD=1`) for
the offline deployment, plus actual network isolation. Do not restart an existing shared Ollama
service merely to run this example. Localhost restricts the harness connection; it does not
independently prevent the Ollama process from reaching the internet. See
[Ollama's local-only configuration](https://docs.ollama.com/faq#how-do-i-disable-ollama-cloud-features).

The request uses Ollama's stored prompt template around the harness context. A JSON
schema describes the permitted actions and fixture keys, and the runner independently revalidates
all output. Models can still finish early, repeat actions, refuse, or exhaust their budget. A
completed run is not a task-quality verdict or a containment finding.

Exit codes: 0 for completed lab execution, 1 for a persisted error/quarantine, 2 for input/setup
failure. The existing report field `replay` is retained for journal compatibility; its metadata
identifies `local_ollama` and the complete bounded configuration. `runtime_identity` remains an
operator-supplied label. `server_identity_verified` and `server_termination_confirmed` remain false.

## Automated offline Docker rehearsal

With Docker running and build connectivity, run from the repository root:

```sh
sh scripts/check-docker-simulation.sh
```

The connected build prepares the installed package. Test containers then run with `--network none`,
UID 10001, read-only root filesystems, no capabilities, and resource limits. No ports are published.
In addition to four simulation/replay trials, three model-adapter trials exercise:

1. Ollama inventory/generation requests through the CLI, two tool results, and final output.
2. A generation timeout that retains its charge without dispatching a tool.
3. SIGKILL after the server receives a generation request, followed by recovery in a fresh
   container without a server, no retry, and verification of all retained seals.

This test uses a small **synthetic Ollama API fixture**, not model weights. It assigns synthetic
usage counts and validates the wire path, accounting, and recovery. The default offline Docker
namespace cannot reach Ollama running on the host. Run the real-model CLI example on the host;
for a future offline container deployment, provision Ollama and its preloaded weights in the
same offline network namespace as the controller. No host-network or external gateway access is
added by this change.

Reports are saved under `.harness/model-lab.*`. Temporary state volumes are removed on success
and retained on failure. After building the current image, use `sh scripts/check-docker-model.sh`
to run just the model checks. CI uses the same fixture; no model downloads or live inference occur
in CI.

## Budgets and usage

The runner builds context from the task, tool descriptions/fixture keys, and prior bounded
responses/results. It enforces a 32 KiB prompt limit and a conservative admission rule:
`UTF-8 prompt bytes + 64 <= max_input_tokens`. This leaves some slack for tokenizer/template overhead and avoids
sending large contexts. It is **not exact tokenization** and cannot prove that a particular Ollama
runtime never truncates a prompt. The adapter does not silently truncate its own context.

Before either inventory or generation I/O, SQLite reserves one model call,
`max_input_tokens + max_new_tokens`, and `max_response_bytes`. The finite `num_predict` and
`num_ctx` options bound requested generation/context sizes. Streaming and thinking are disabled;
temperature and seed are fixed. The adapter sends only `/api/tags` and `/api/generate` requests.

Usage arrives after generation. A response settles only when `prompt_eval_count` and `eval_count`
are positive integers within their individual allowances, the model name matches, generation
reports a normal stop, and the text fits its byte limit. Reports label this **server-reported
tokens**, not independently measured usage. The adapter rejects hidden thinking, incomplete
responses, length-limit stops, and invalid usage. It cannot undo work already performed by a
misbehaving server. See the [Ollama generation API](https://docs.ollama.com/api/generate).

Unused reservation capacity is released only on valid committed results. Timeouts, disconnects,
invalid usage, and crashes retain the full reservation as uncertain. There are no automatic retries.
A bounded but semantically invalid model message may have settled usage before the runner rejects
its action. Tool and cumulative output budgets retain the replay rules. Provider billing is not
integrated; `model_cost_microusd: 0` does not mean hardware/electricity costs are zero.

## Transport and stop behavior

Only numeric IPv4 loopback and the configured port are used. No DNS, proxy settings, redirects,
authentication headers, or URL overrides are supported. Requests are capped at 256 KiB, response
headers at 8 KiB, and decoded bodies at 128 KiB. Content-Length and bounded HTTP chunked framing
are supported; ambiguous lengths, compression, chunk extensions/trailers, and non-200 status fail
closed. Chunked transfer framing is different from Ollama token streaming, which remains disabled.

Each HTTP request has an absolute timeout, including the complete body. While waiting, the
controller checks evidence health and renews its lease approximately every 100 ms. A deadline or
health failure cancels I/O and closes the local socket before any later tool dispatch. Recovery
never resumes inference or tools.

Closing a socket does not prove the server stopped generating. Reconciliation confirms fake
marker cleanup only. The controller/collector/watchdog still share a process and compromise domain;
independent server termination and authenticated private-service inference remain future work.
Preserve failed trial evidence and inspect the dedicated runtime before reusing it after an
ambiguous interruption. Local seals do not establish AWS containment.

## Observed local acceptance run

A development run with Ollama 0.33.2 and the example's installed
`LiquidAI/lfm2.5-1.2b-instruct:latest` model completed three generations and two lookups, then returned
`Hello from a synthetic fixture`. The server reported 686 total tokens, and the retained evidence
seal verified. The model repeated lookup instead of performing the requested echo: transport,
accounting, tools, and finalization worked, but this was not a full task-following success. The
Docker fixture separately exercises lookup followed by echo. This observation is not a guarantee
of future model behavior or AWS containment.

# Universal experimental core: architecture contract

Status: canonical working contract. It remains authoritative until the user
explicitly changes it.

## Requirement in one sentence

Build one domain-neutral, model-driven and host-controlled experimental core;
batch evaluation and live interaction are two adapters over that core.

The requirement is **not** to build a full general-purpose agent. The core must
be universal; concrete capabilities remain explicit, bounded plugins selected
by an experiment profile.

## Meaning of “universal”

The core is universal when:

- it can run with zero tools or any trusted `ToolProvider`;
- it does not contain names or branches for a domain, tool, scenario, provider
  brand, analyzer, attack, or evaluator;
- batch and live modes use the same model/tool loop and control boundaries;
- a new scenario using installed components is added as inert data;
- a new physical tool, analyzer, policy, attack, evaluator, or provider is
  added as a separate trusted adapter without editing the model/tool loop;
- provider selection changes wiring, not engine behavior;
- the model may answer directly or propose a tool; the host never substitutes
  a hidden scripted choice.

Universal does **not** mean:

- a ready-made coding, browser, shell, repository, or internet agent;
- an unlimited catalog of tools;
- guaranteed answers to every question;
- support for a new provider without an adapter;
- a detector that recognizes every prompt injection;
- deterministic model behavior;
- permission for scenario files to execute arbitrary code.

## Target architecture

```text
                         trusted composition
                  profile + installed component catalog
                                      |
                                      v
                     +-------------------------------+
                     | universal AgentSession/Engine |
                     | messages, turns, budgets,     |
                     | tool protocol, cancellation   |
                     +-------------------------------+
                        |          |          |
                        v          v          v
                 ModelProvider  ToolProvider  BoundaryPipeline
                                             Analyzer -> Policy
                        |          |          |
                        +----------+----------+
                                   |
                         events + private evidence
                              /             \
                             v               v
                   redacted EventSink    mode adapter
                                        /            \
                                       v              v
                                batch evaluator    live REPL/report
```

Dependency direction:

```text
core contracts <- core engine <- batch/live adapters <- CLI/Compose
       ^                 ^
       |                 |
trusted component adapters and composition roots
```

Core modules must not import batch, live, `repo_rag`, PR review, scenario
fixtures, Docker configuration, or concrete provider/tool/control adapters.

## Shared contracts

### `ModelProvider`

- Returns a validated model descriptor and capabilities.
- Accepts a normalized completion request plus a host-owned invocation context
  containing the run ID, model-request ID and turn counters.
- Returns a normalized assistant final turn or tool proposals.
- Provider URLs, authentication and transport details stay in the adapter.

### `ToolProvider`

- Publishes bounded, immutable `ToolSpec` objects.
- Executes exactly one validated tool name and JSON argument object.
- Owns its lifecycle and reports whether an execution started, completed,
  failed, or has an unknown completion state after cancellation.
- A shared router composes multiple providers without branching on tool names.

### `Analyzer`

- Observes one private boundary envelope.
- Returns stable, content-free findings.
- Does not execute tools, mutate global state, or set a verdict.

### `ControlPolicy`

- Receives the boundary envelope plus analyzer findings.
- Returns `allow`, `block`, `replace`, or `abort` with a stable reason code.
- Enforcement mode belongs to policy, not detector implementation. The same
  analyzer must produce the same findings in observe and enforce profiles.

### `AttackTransform`

- Used only by batch experiments to mutate data at a declared boundary.
- Records attempted, prepared and delivered provenance.
- Does not contain tool-name special cases in core.

### `Evaluator`

- Runs after batch execution using private evidence and a private oracle.
- Cannot change engine events or `pipeline_ok`.
- Returns independent task, security, tool-selection and runtime assertions.
- Live mode does not require or fabricate an evaluator result.

### `EventSink` and private evidence

- Public events are ordered, bounded and redacted.
- Model-controlled call IDs are replaced with host-generated correlation IDs.
- Every model request/response pair has a host-generated `model_request_id`.
  Gateway transports carry the run and request IDs in bounded internal headers,
  echo them to the client and include them in both request and response logs.
- Raw prompts, answers, tool arguments/results, or credentials are not written
  to the public event stream.
- Exact payload evidence stays in memory or in an explicitly enabled private
  owner-only sink; it is never silently mixed with stdout/stderr.

### Trusted component catalog

- Profiles/manifests reference versioned logical component IDs.
- Runtime resolves IDs only from a host-installed allowlisted catalog.
- Config cannot contain Python import paths, executable commands, arbitrary
  provider URLs, or credentials.
- Unknown IDs, versions, duplicate tools or invalid config fail during preflight
  before the first model request.

## Common boundary lifecycle

The engine uses the same boundaries in batch and live modes:

```text
input
  -> model_request
  -> model_output
       -> final_output
       or
       -> tool_call
          -> host validation/policy
          -> tool execution
          -> tool_result
          -> model_request ...
```

Each boundary has a private payload, provenance/taints, run/session ID, user and
model turn, optional tool/call identity, findings, policy decision and public
summary.

Required events include input receipt, model request/response, every tool
proposal, policy decision, execution start and terminal state, result delivery
or replacement, final output, cancellation/failure and final report.

## Non-negotiable execution invariants

1. Tool execution occurs only because the current model response contained a
   valid proposal.
2. If the tool catalog is non-empty, the model request uses
   `tool_choice: "auto"`; if it is empty, no tool fields are sent.
3. Core never uses `required` or a specific forced tool choice.
4. The host owns model/provider selection, tool catalog, allowlist, schemas,
   execution, analyzers, policy, budgets, cancellation, trace and verdict.
5. Unknown tools, malformed JSON, schema-invalid arguments, duplicate call IDs,
   repeats beyond policy and exhausted budgets are stopped before side effects.
6. Tool results cross the analyzer/policy boundary before returning to the
   model. Final output crosses it before reaching the user/evaluator.
7. Parallel tool execution is disabled in the first implementation so event
   and side-effect ordering is deterministic.
8. Analyzer/control failures are fail-closed and auditable.
9. `pipeline_ok`, task success and security success are separate facts.
10. A model-written `PASS`, `BLOCK` or success claim has no authority.
11. External model output is nondeterministic; the harness reproduces the exact
    configuration, component versions, inputs, budgets and evidence, not the
    exact prose.
12. No default mode or core path requires a repository mount.

## What can be reused from the current code

- Model/tool protocol parsing, bounded loop and budgets from
  `runners/autonomous.py`.
- `ToolSpec`, `ToolProvider`, `ToolRegistry`, JSON/schema validation and result
  limits from `tools/core.py`.
- MCP as one optional tool-provider adapter from `tools/mcp.py`.
- Exact model validation and HF/LM Studio gateway adapters.
- A local gateway liveness probe for recurring container healthchecks. Exact
  model identity and capabilities are still checked by model discovery before
  an experiment runs; Docker healthchecks do not poll the upstream provider.
- Bounded/redacted Trace v2 and private tool-call evidence.
- Declarative subset assertions as one built-in evaluator.
- Live structured history, aggregate budgets, repeat guard, cancellation,
  bounded input and stdout/stderr separation.

What must be decoupled:

- `lookup_fact`, `ORCHID`, `search_repo` and single-MCP routing from batch
  orchestration;
- injection tracking from particular tool names;
- analyzer/control contracts from the `live` namespace;
- workspace registry and repository system prompt from live runtime;
- scenario-specific evaluation from engine execution;
- private helper imports between `live/runtime.py` and `autonomous_lab.py`.

## Architecture gate for every implementation change

Reject or redesign a change if any answer below is “no”:

- Can core run with an empty tool catalog?
- When tools exist, can the model still answer without using them?
- Is every execution tied to a valid model proposal and host decision?
- Are analyzer findings separate from enforcement decisions?
- Do batch and live call the same engine and boundary pipeline?
- Can the component be replaced without branching in the engine on its name?
- Is a repository merely an optional adapter rather than a core concept?
- Is scenario/profile data inert and strictly validated?
- Is verdict authority outside the model?
- Does public audit avoid raw payloads and credentials?
- Can provider/tool/analyzer/evaluator extension happen without editing the
  agent loop?

Implementation of live mode cannot start until Plan 1 proves this architecture
with two unrelated tool/control/evaluator combinations using the same engine.

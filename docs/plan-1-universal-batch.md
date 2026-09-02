# Plan 1: universal batch/security-evaluation harness

Status: implemented; the verification record is in
`plan-1-implementation.md`. This remains the canonical contract for the first
mode and depends on `universal-core-contract.md`.

## Goal

Produce a reproducible finite experiment:

```text
public scenario + trusted component profile
  -> model-driven universal engine
  -> redacted event trace + private evidence
  -> independent scenario evaluator
  -> deterministic structured report
```

This is an evaluation harness, not a live assistant. A scenario may provide
tools, attacks, analyzers, policies and an oracle, but none of those concepts is
hardcoded in the engine.

## Current starting point

Keep the valid foundation in `78011c3`:

- a real optional-tool loop;
- `tool_choice: auto`;
- strict tool parsing, schemas and budgets;
- host execution and independent evaluation;
- HF and LM Studio through a pinned gateway;
- redacted trace and private evidence.

Do not preserve as architecture:

- hardcoded `lookup_fact`/`ORCHID`;
- special handling of `search_repo`;
- “every other tool belongs to one MCP endpoint” routing;
- prompts that explicitly tell the model to use or avoid a named tool;
- analyzer/enforcement logic tied to declared injection IDs;
- an evaluator schema presented as the only possible oracle type;
- mandatory `repo-rag`, repository volumes or repository mounts.

## Target experiment format

A strict public manifest compiles into an immutable `RunPlan` before contacting
the model. It selects only trusted logical IDs:

```text
schema/version
experiment ID
public task/messages provider
required model capabilities
tool-provider instances and visible allowlist
attack transforms
analyzers
control policy profile
budgets
private evaluator/oracle reference
```

The manifest contains no `tool_choice`; core derives `auto` from a non-empty
catalog. It contains no import paths, commands, credentials or arbitrary URLs.

The private oracle may require or forbid a tool, but that expectation is never
placed in the model-visible task, tool description or system prompt.

## Implementation phases

### Phase 1 — Characterize and extract the engine

- Preserve current protocol/budget tests as characterization tests.
- Introduce shared core contracts and `AgentSession.run_turn()`.
- Move response parsing, history validation, turn budgets, tool proposal
  handling and cancellation into the domain-neutral engine.
- Replace raw `dict -> dict` model callback with `ModelProvider`.
- Add an architecture test forbidding imports from `repo_rag`, PR review,
  batch/live adapters and fixture modules into core.

Exit criteria:

- scripted model tests cover direct final answer and sequential tool cycles;
- no tool call can execute without a parsed proposal;
- existing provider adapters work through the new model contract.

### Phase 2 — Unify boundary analyzers and policy

- Move analyzer/control types out of `live` into shared core contracts.
- Add one ordered boundary pipeline for input, model request/output, tool call,
  tool result and final output.
- Separate detector findings from policy decisions.
- Support `observe`, `block`, bounded safe replacement and abort.
- Keep schema, allowlist, budgets and repeat guards as mandatory core controls.
- Generalize taint/provenance and injection delivery tracking without checks for
  a particular tool or scenario ID.

Exit criteria:

- the same analyzer findings appear in observe and enforce tests;
- only policy changes delivery/execution behavior;
- blocked tool calls have no side effect and blocked results never return raw to
  the model.

### Phase 3 — Trusted component catalog and scenario preflight

- Implement an explicit, host-owned catalog for model, tool, analyzer, policy,
  attack and evaluator adapters.
- Add strict versioned scenario format and compilation to `RunPlan`.
- Resolve and validate all component IDs, tool schemas, allowlists, capability
  requirements, budgets and private references before the first model call.
- Move shared multi-provider tool routing out of `autonomous_lab.py`.
- Keep MCP as one adapter; it is not the default for every unknown tool.
- Retain Scenario v2 only as a compatibility loader during migration.

Exit criteria:

- unknown/duplicate/incompatible component configuration fails preflight;
- a scenario using installed components is added without Python changes;
- adding a new adapter does not change engine code.

### Phase 4 — Separate execution evidence from evaluation

- Engine returns immutable private run evidence and emits public events.
- Batch evaluator receives evidence plus a private oracle only after execution.
- Define independent assertion groups: runtime, task, security and tool
  selection.
- Keep exact/contains/subset checks as one built-in evaluator plugin.
- Ensure an evaluator cannot rewrite engine `pipeline_ok` or earlier events.
- Record exact model ID, model capabilities, sampling parameters, component
  IDs/versions, schemas, scenario digest and budgets in the run manifest.

Exit criteria:

- guessing a hidden answer without required result evidence does not pass;
- model prose saying `PASS` or `BLOCK` changes no assertion;
- oracle markers occur in no model request, tool delivery or public trace.

### Phase 5 — Rebuild neutral reference experiments

Replace the current prompted plumbing demos with experiments that do not reveal
the expected tool behavior:

- `direct-answer`: an unrelated tool is available, but the answer is already in
  the task;
- `external-fact`: the answer exists only behind a fixture provider, while the
  task simply asks the question and never names the tool;
- `tool-choice`: multiple tools are visible and only one can supply the fact;
- `poisoned-result`: a useful tool returns valid data plus an injected
  instruction;
- `unsafe-arguments`: injected content attempts to move a private canary into a
  tool call;
- `second-domain`: a different tool/analyzer/evaluator combination proving that
  the engine contains no first-domain assumptions.

Fixture values come from private scenario configuration, not constants in the
engine or orchestration. Required, optional and forbidden tools exist only in
the private evaluator configuration.

Exit criteria:

- no selection experiment prompt says “use”, “do not use” or names the expected
  tool;
- both direct-answer and tool-needed paths are observable with the same engine;
- two unrelated component combinations require no core changes.

### Phase 6 — Batch orchestration and artifacts

- Provide one-run and matrix execution:

  ```text
  scenarios x model profiles x defense profiles x repeats
  ```

- Isolate failures so one run does not corrupt the remaining matrix.
- Produce per-run immutable metadata, public trace and evaluation report.
- Keep raw private evidence in memory by default; optional persistence requires
  an explicit path, bounded format and owner-only permissions.
- Aggregate infrastructure, task, security and tool-selection outcomes
  separately.

Exit criteria:

- repeated runs retain their own model/profile/component identities;
- operational failure is distinguishable from model task/security failure;
- public artifacts remain bounded and redacted.

### Phase 7 — Providers, Compose, CI and documentation

- Keep HF and LM Studio as model-provider configurations of the same gateway
  adapter.
- Require tool capability only when the resolved tool catalog is non-empty.
- Remove default batch dependency on `repo-rag` and repository mounts.
- Add deterministic CI using scripted model/providers for all engine claims.
- Add opt-in real HF/LM Studio smoke tests; record real model choice as evidence,
  not a deterministic CI oracle.
- Update docs to state precisely what is framework behavior and what is observed
  model behavior.

Exit criteria:

- the same normalized run works with fake, HF and LM Studio provider adapters;
- default agent container receives no provider credential or unrelated mount;
- full tests, Ruff and resolved Compose isolation checks pass.

## Plan 1 acceptance checklist

Plan 1 is complete only when all items pass:

1. Empty catalog produces a direct model request without tool fields.
2. Non-empty catalog produces exact schemas with `tool_choice: "auto"`.
3. Model may finish without using advertised tools.
4. Valid model-selected tool proposals execute and their results return to the
   next model turn.
5. Malformed, unknown, schema-invalid, repeated, blocked or over-budget calls
   cannot execute.
6. Tool results and final output cross the shared analyzer/policy pipeline.
7. Every proposal, decision, execution state, result delivery, finding and stop
   has a correlated public event.
8. New scenarios over installed components require only inert scenario files.
9. New tool/analyzer/policy/evaluator adapters require no engine modification.
10. Two unrelated component combinations pass the architecture test.
11. Evaluator, not model text, computes task/security/tool-selection results.
12. `pipeline_ok`, task success and security success remain independent.
13. Private evidence/oracle data never leaks into public/model-visible channels.
14. HF and LM Studio differ only in provider wiring and exact model profile.
15. Default batch mode has no repository-specific service, mount, prompt or
    tool.

## Branch and migration rule

- Preserve `feature/autonomous-security-lab` and `feature/live-agent-mode` as
  historical spikes until the replacement is accepted.
- Create the corrected Plan 1 branch from the reusable autonomous foundation,
  not from the repo-coupled live branch.
- Implement phases as reviewable commits with their exit criteria.
- Do not begin Plan 2 until the two-unrelated-components acceptance test passes.

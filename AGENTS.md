# Architecture guard for autonomous and live modes

Before planning or changing the autonomous batch harness, agent runner, tools,
controls, evaluation, or live mode, read all three documents:

- `docs/universal-core-contract.md`
- `docs/plan-1-universal-batch.md`
- `docs/plan-2-live-adapter.md`

These documents are the source of truth until the user explicitly changes the
scope. The central requirement is: the experimental core is universal; the
agent is not required to be a full general-purpose agent.

Non-negotiable guards:

- Core code must not know about repositories, linters, `lookup_fact`, concrete
  scenarios, provider brands, or a particular attack.
- Batch and live modes must use the same model-driven engine and the same
  boundary-control pipeline.
- The model may answer without tools. When tools are present, use
  `tool_choice: auto`; never force a tool in core.
- A tool executes only after a valid model proposal and host policy approval.
- Tools, analyzers, policies, attacks, evaluators, and providers are injected
  through trusted contracts/registries; they are not hardcoded in the engine.
- The host owns execution, stopping, budgets, audit, and verdicts.
- Live mode must not require a repository or workspace. Repository tooling may
  exist only as an optional adapter/profile.
- A scenario/profile is inert data and must not load arbitrary Python, URLs,
  commands, or credentials.
- Do not broaden the work into a coding/browser/shell agent unless the user
  explicitly changes the scope.

If an implementation conflicts with these guards, stop and correct the design
before adding more features.

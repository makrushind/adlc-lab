# Task 4 report — opt-in PR-review Compose UX, CI, and README

## Summary

Added the opt-in `pr-review` Compose overlay, a local-user workflow in the
README, and CI coverage for preparation plus review-mode `repo-rag` health.
The CI never starts `hf-gateway` or `agent` in that review-specific step, so it
cannot make a live model/gateway call there.

## Changed files

- `scenarios/pr-review.compose.yaml` (new): exactly two required-variable,
  read-only bind mounts on `reset`; only `ADLC_PR_REVIEW_MODE: "1"` on
  `repo-rag`.
- `README.md`: host diff creation, exact three Compose commands, result shape,
  billing/privacy/MVP limits, snapshot persistence, and review cleanup.
- `.github/workflows/ci.yml`: temporary absolute fixture paths exported through
  `GITHUB_ENV`, overlay model/config validation, snapshot preparation,
  review-mode RAG health, and normalized isolation assertions.

## Checks

`git diff --check` exited 0 with no output.

YAML parse using the workspace virtual environment:

```text
YAML OK: compose.yaml
YAML OK: scenarios/pr-review.compose.yaml
YAML OK: .github/workflows/ci.yml
```

Static overlay contract and CI fixture shell syntax checks:

```text
overlay contract OK
CI fixture shell syntax OK
```

Full Python suite:

```text
.....................................................                                       [100%]
53 passed, 53 subtests passed in 0.89s
```

Docker runtime verification is unavailable in this workspace. The exact local
check output was:

```text
Docker CLI unavailable: docker not found
```

`docker-compose` is present on `PATH`, but the required `docker` CLI (and thus
`docker compose`) is absent; no Compose config, preparation, or health command
was claimed to have run locally.

## CI reasoning

The first CI step creates a temporary absolute checkout/diff fixture and makes
both paths available to every later review-overlay Compose invocation through
`GITHUB_ENV`. CI validates the overlay config, builds the existing images,
runs `reset prepare-review` against that fixture, then runs only
`up -d --wait repo-rag`. It does not start `hf-gateway` or invoke `agent`, so
the health path only uses the review-mode repository service and its safe MCP
health contract. The isolation check requires the two exact read-only reset
binds and the sole RAG environment flag, removes only those permitted changes,
and compares the remainder to the base model. Existing fixed-scenario
normalization and final Compose cleanup are retained.

## Self-review and concerns

Reviewed the final diff for scope and found only the approved overlay, README,
and CI changes plus this required report; base Compose, source, tests, fixed
scenarios, and runtime/dependency files remain untouched. The remaining
limitation is local Docker CLI absence; CI is the runtime executor for Compose
configuration, preparation, and review-mode health verification.

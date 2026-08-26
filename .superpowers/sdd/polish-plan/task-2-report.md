# Task 2 Report: Portable native builds and CI tests

## Changes

- Removed the global `TARGETPLATFORM` argument and `FROM --platform` override from `Dockerfile`; the pinned Python 3.13.13 Bookworm digest and the `repo-rag`, `hf-gateway`, and `agent-runtime` targets are unchanged.
- Added `tests/` to `.dockerignore`, keeping public tests out of every production image build context.
- Changed CI to use plain `docker compose build` and added an in-image public-test step:
  `docker compose run --rm --no-deps -T --entrypoint python -v "$PWD/tests:/tests:ro" agent -m unittest discover -s /tests -v`.
- Left the existing always-run `docker compose down -v --remove-orphans` cleanup intact.

## Files changed

- `Dockerfile`
- `.dockerignore`
- `.github/workflows/ci.yml`

## Validation

| Command / check | Result |
| --- | --- |
| `git diff --check` | Passed (no whitespace errors). |
| Inspected staged diff | Confirmed the base digest and all three production targets remain, and no Compose/runtime/scenario/source/test/requirements files changed. |
| `docker compose config --quiet` plus five overlay forms | Deferred: `docker` executable is absent (`command not found`, exit 127). |
| `docker compose build` | Deferred: `docker` executable is absent (exit 127). |
| In-image public-test command above | Deferred: `docker` executable is absent (exit 127). |
| `docker compose down -v --remove-orphans` | Deferred: `docker` executable is absent (exit 127); no project resources could have been created locally. |

The host interpreter is Python 3.9.6 and does not include the image dependency `httpx`; a direct host unittest run therefore cannot substitute for the required in-image Python 3.13 test gate.

## Self-review

- Plain `docker compose build` is now the sole CI build command; no architecture build argument or platform override remains in the changed build path.
- The public-test mount is read-only, is supplied only at `docker compose run` time, and `--entrypoint python` overrides the service entrypoint before `-m unittest discover` runs.
- Tests are excluded from the Docker context, so they cannot be copied into production images through the context.
- The CI cleanup condition remains `if: always()`.
- No source-grep test was added.

## Concern / controller gate

Run the deferred Docker commands on an amd64 or arm64 host with a working Docker Engine before accepting the task. That is the remaining evidence for native three-image build success, resolved base/overlay Compose configuration, in-image test execution, isolation validation, and cleanup.

# Task 1 report — Runtime protocol, taxonomy, types, and focused tests

## Scope completed

- Added side-effect-free strict JSON, first-turn tool call, final assistant, and MCP search response validation in `agent_protocol.py`.
- Preserved the public `run_agent()` signature and production Compose behavior: exactly two LLM posts, one MCP call, no retry/fallback, pinned `openai/gpt-oss-20b:groq` request bodies, and the ten-event success sequence.
- Centralized upstream HTTP classification and canonical local response statuses in `errors.py`; the gateway, transport, agent, and readiness wrapper now use them.
- Replaced the agent's tuple turn result with `FirstToolTurn`, moved repo-RAG result shapes into shared TypedDicts, and introduced the `HealthSession` protocol.
- Added 11 public `unittest` behavior tests for taxonomy, the exact runtime loop/event order, malformed peer documents, gateway readiness wrapping, scenarios, and trace redaction/stages.

## RED evidence

Focused command before production changes:

```text
$ PYTHONPATH=src python3 -m unittest tests.test_errors -v
ImportError: cannot import name 'classify_upstream_status' from 'aiweekend_target.errors'
...
FAILED (errors=1)
```

This is a genuine RED for the missing central taxonomy API. The initially added agent-loop test also had no injectable `post_llm`/`mcp_search` seam in the original `_run`; the host interpreter could not import the original runtime because `httpx`/`mcp` were not installed. A temporary isolated environment was then used for GREEN verification.

## GREEN verification

```text
$ TASK_VENV=/private/tmp/adlc-task1-venv PYTHONPATH=src "$TASK_VENV/bin/python" -m unittest discover -s tests -v
Ran 11 tests in 0.033s
OK

$ TASK_VENV=/private/tmp/adlc-task1-venv PYTHONPATH=src "$TASK_VENV/bin/python" -c '<in-memory compile all src and tests>'
compiled 24 Python sources in memory

$ git diff --check
(no output; passed)
```

## Files changed

- `src/aiweekend_target/agent_protocol.py` (new)
- `src/aiweekend_target/errors.py`
- `src/aiweekend_target/agent.py`
- `src/aiweekend_target/__main__.py`
- `src/aiweekend_target/gateway/app.py`
- `src/aiweekend_target/gateway/transport.py`
- `src/aiweekend_target/repo_rag/types.py` (new)
- `src/aiweekend_target/repo_rag/search.py`
- `src/aiweekend_target/repo_rag/server.py`
- `tests/test_agent.py` (new)
- `tests/test_errors.py` (new)
- `tests/test_gateway.py` (new)
- `tests/test_scenarios.py` (new)
- `tests/test_trace.py` (new)

## Self-review

- The wire event schema remains `schema: 1`; individual events have no `stage`, and only `lab_result.stages` records progression.
- The success-path test asserts the exact ten event types, two LLM requests, one MCP request, first named tool choice, and second `none` tool choice.
- Agent, MCP server, and gateway retain independent input/result validation at their respective trust boundaries.
- Dead local parsing and duplicate status symbols were removed only after replacement by the new common implementations; public `run_agent` and command forms remain unchanged.

## Concerns

- Docker is unavailable in this environment, so Compose execution was not possible here. Unit and source-compilation verification ran in a temporary environment using the declared runtime dependency versions.
- `requirements.lock` currently rejects the available macOS ARM `cffi==2.1.1` wheel hash. The temporary environment therefore used `requirements.in`; no dependency or lockfile was changed.

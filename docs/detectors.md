# Граница детектора

Внешний детектор использует стабильные адреса `ADLC_MCP_URL` и
`ADLC_LLM_URL`. Они являются единственными агентскими границами: агент не
получает секрет, маршрут провайдера или прямой egress. Детектор может быть
прокси, наблюдателем или обоими, но его реализация в лабораторию не включена.

Сохраните следующий generic Compose override-блок как `detector.compose.yaml`
и применяйте вместе с базовым Compose-файлом. Он направляет **оба** вызова агента на detector, а
самому detector передаёт исходные внутренние upstream-адреса. Дополнительная
зависимость гарантирует запуск detector до agent; порт на хост не публикуется.

```yaml
services:
  detector:
    image: example/detector:stable
    environment:
      DETECTOR_MCP_UPSTREAM: http://repo-rag:8000/mcp
      DETECTOR_LLM_UPSTREAM: http://hf-gateway:8080/v1/chat/completions
    networks: [internal]
  agent:
    environment:
      ADLC_MCP_URL: http://detector:9000/mcp
      ADLC_LLM_URL: http://detector:9000/v1/chat/completions
    depends_on:
      detector:
        condition: service_started
```

Например: `docker compose -f compose.yaml -f detector.compose.yaml run --rm
agent`. Detector остаётся во внутренней сети и не получает `hf_token`.

## Контракт JSONL

Каждая строка — один нормализованный JSON-объект `schema: 1` с обязательными
`type`, `scenario` и `canaries`. `scenario` — один из пяти фиксированных
сценариев, а `canaries` содержит только известные значения в фиксированном
порядке. Необработанные provider/MCP тела не передаются.

Успешный двухшаговый запуск содержит ровно десять событий в таком порядке:

1. `prompt`
2. `llm_request`
3. `tool_call`
4. `rag`
5. `mcp_request`
6. `mcp_result`
7. `llm_request`
8. `llm_response`
9. `agent`
10. `lab_result`

События границ используют `status`; стадия `stage` появляется для `prompt`,
`rag`, `mcp_result`, `llm_response` и `agent`. `llm_request` и `tool_call`
содержат номер `turn`, фиксированные `model`/`tool`; `tool_call`, `rag` и
`mcp_request` имеют redacted `query_preview` не длиннее 160 символов.
`mcp_result` содержит `result_count` не больше 20 и до 10 redacted `paths`,
каждый не длиннее 128 символов. В `agent` поле `text_preview` redacted и не
длиннее 512 символов. `prompt` содержит только `prompt_chars`, а не исходный
prompt. В preview credential-подобные значения и canary заменяются на
`[REDACTED]`/`[CANARY]`; сами фиксированные canary могут появляться только как
метаданные `canaries` на разрешённых границах.

Финальный `lab_result` содержит `schema`, `type`, `ok`, `scenario`, `stages`
и `canaries`; при успехе `stages` ровно равен
`["prompt", "rag", "mcp", "llm", "agent"]`. При ошибке поток заканчивается
`agent_error` (`status: "failed"`, стабильный `code`, без `stage`) и затем
`lab_result` с `ok: false` и только пройденными `stages`. Точная формулировка
ответа модели не является контрактом.

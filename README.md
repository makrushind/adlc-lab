# ADLC Lab

Учебная Compose-лаборатория показывает двухшаговый фиксированный цикл Python-агента: первый запрос к модели выбирает `search_repo`, затем агент делает один MCP/RAG-вызов, а второй запрос к модели формирует ответ. В stdout идут нормализованные подробные JSONL-события границ `prompt → RAG → MCP → LLM → agent`; точный текст live-модели непредсказуем и не гарантируется.

Сценарии намеренно уязвимы и используют только учебные canary-значения; защитные меры остаются задачей отдельного проекта.

## Подготовка и baseline

Нужны Docker Compose v2, Linux-контейнеры, минимум 2 CPU, 4 GiB RAM, 10 GiB места и Hugging Face credits. Один раз создайте секрет: ввод скрыт, файл `.lab/secrets/hf_token` монтируется только в `hf-gateway`.

```bash
docker compose config --quiet
docker compose build
docker compose run --rm setup-token
```

Для baseline и любой отдельной атаки сначала выполните одинаковую подготовку здоровых сервисов:

```bash
docker compose run --rm reset
docker compose up -d --wait repo-rag hf-gateway
docker compose run --rm agent
```

## Три фиксированные атаки

Каждая команда ниже самостоятельно сбрасывает сценарий; перед ней выполните один раз точную подготовку здоровых сервисов выше.

```bash
docker compose -f compose.yaml -f scenarios/rag-poisoning.compose.yaml run --rm reset
docker compose -f compose.yaml -f scenarios/rag-poisoning.compose.yaml up -d --wait repo-rag hf-gateway
docker compose -f compose.yaml -f scenarios/rag-poisoning.compose.yaml run --rm agent

docker compose -f compose.yaml -f scenarios/mcp-poisoning.compose.yaml run --rm reset
docker compose -f compose.yaml -f scenarios/mcp-poisoning.compose.yaml up -d --wait repo-rag hf-gateway
docker compose -f compose.yaml -f scenarios/mcp-poisoning.compose.yaml run --rm agent

docker compose -f compose.yaml -f scenarios/llm-injection.compose.yaml run --rm reset
docker compose -f compose.yaml -f scenarios/llm-injection.compose.yaml up -d --wait repo-rag hf-gateway
docker compose -f compose.yaml -f scenarios/llm-injection.compose.yaml run --rm agent
```

## Свой payload и очистка

Измените `scenarios/custom/payload.txt`, затем используйте тот же подготовленный набор здоровых сервисов:

```bash
docker compose -f compose.yaml -f scenarios/custom.compose.yaml run --rm reset
docker compose -f compose.yaml -f scenarios/custom.compose.yaml up -d --wait repo-rag hf-gateway
docker compose -f compose.yaml -f scenarios/custom.compose.yaml run --rm agent

# Обычная очистка: секрет на хосте сохраняется.
docker compose run --rm reset
docker compose down -v --remove-orphans
# Секрет сохраняется; его удаление — отдельное действие ниже.

# Явное отдельное удаление учётных данных.
docker compose run --rm setup-token delete
```

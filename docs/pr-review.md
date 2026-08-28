# Local PR review: справочник

`pr-review` — opt-in локальный режим для Python PR. Он не заменяет fixed
attack lab: принимает только подготовленные локальные данные и использует
отдельный JSONL-контракт.

## Входы и подготовка

На хосте checkout-ите PR head и создайте обычный text unified diff относительно
target branch:

```bash
git -C /absolute/path/to/repo diff --no-ext-diff --unified=3 origin/main...HEAD > /tmp/pr-review.diff
export PR_REVIEW_REPO="/absolute/path/to/repo"
export PR_REVIEW_DIFF="/tmp/pr-review.diff"
```

Обе переменные должны содержать абсолютные пути. `origin/main` — только пример:
выберите base ref, соответствующий целевой ветке PR, и убедитесь, что он есть
локально. Checkout обязан представлять новую сторону diff. Подготовка сверяет
изменённые пути с checkout, отклоняет небезопасные/неподдерживаемые или
несогласованные diff до обращения к внешней модели.

Выполните подготовку:

```bash
docker compose -f compose.yaml -f scenarios/pr-review.compose.yaml run --rm reset prepare-review
docker compose -f compose.yaml -f scenarios/pr-review.compose.yaml up -d --wait repo-rag hf-gateway
```

`prepare-review` заменяет прежний runtime-снимок в именованных volumes и
печатает объект с `ok`, `prepared`, `copied_files`, `copied_bytes` и
`changed_files`. В нём находятся отфильтрованный checkout, нормализованный
`pr.diff`, SQLite RAG index и baseline marker. `up --wait` подтверждает
готовность `repo-rag` и `hf-gateway`.

Через Hugging Face gateway внешней модели будут переданы raw либо bounded diff
context, retrieved RAG fragments и lint diagnostics. Не запускайте review,
если этот код или данные нельзя передавать этому провайдеру.

```bash
docker compose -f compose.yaml -f scenarios/pr-review.compose.yaml run --rm agent pr-review
```

## События и результат

Успешный review печатает ровно девять JSONL-событий в этом порядке:

1. `llm_request` — turn 1;
2. `llm_response` — поисковый query;
3. `mcp_request` — `search_repo`;
4. `mcp_result` — RAG results;
5. `mcp_request` — обязательный `lint_pr`;
6. `mcp_result` — lint diagnostics;
7. `llm_request` — turn 2 без tools;
8. `llm_response` — `report_preview`;
9. `pr_review_result`.

Успех делает ровно два billable live-вызова закреплённой
`openai/gpt-oss-20b:groq` через Hugging Face gateway/Router. Автоматических
повторов и fallback нет; ошибка может остановить процесс до первого, после
первого или после второго вызова.

`verdict` вычисляется детерминированно из diagnostics, а не из текста модели:

```json
{"schema":1,"type":"pr_review_result","ok":true,"verdict":"pass","diagnostics":[],"report_preview":"..."}
```

Любая high-severity диагностика даёт `block`:

```json
{"schema":1,"type":"pr_review_result","ok":true,"verdict":"block","diagnostics":[{"path":"app.py","line":12,"column":9,"rule":"ADLC001","severity":"high","message":"Avoid eval() on untrusted input"}],"report_preview":"..."}
```

При обработанной ошибке поток заканчивается `pr_review_error` следующей формы:

```json
{"schema":1,"type":"pr_review_error","ok":false,"code":"POLICY","stage":"diff"}
```

`stage` — `diff`, `llm`, `mcp` или `review`; `code` — нормализованная категория
ошибки. Ранее напечатанные `llm_*`/`mcp_*` события сохраняются; preflight/diff
ошибки до внешних границ печатают только эту единственную строку. Preview
проходит redaction и ограничен 512 символами.

## Доверительные границы и данные

- Host checkout и diff монтируются read-only только в офлайн `reset`.
  `repo-rag`, `agent` и `hf-gateway` не имеют прямого доступа к этим путям.
- `prepare-review` кладёт снимок в именованные volumes. Он сохраняется до
  явного cleanup, даже после завершения review.
- Gateway — единственный сервис с Hugging Face token и external egress. Через
  него внешнему провайдеру уходят raw или bounded diff context, RAG-фрагменты и
  lint diagnostics. Redaction защищает локальный trace, но не отменяет передачу
  кода, уже отправленного внешней модели.
- GitHub PR URL, GitHub API, Git checkout base/head внутри контейнеров и
  публикация review comments не поддерживаются.

Первый LLM user-message содержит raw diff, если его сериализованный размер не
превышает 32 KiB. Иначе используется детерминированный bounded digest. Сумма
сериализованных messages второго вызова ограничена 96 KiB.

## Границы формата и размера

Подготовка принимает только непустой UTF-8 text unified diff размером до
512 KiB; binary diff и небезопасные пути отклоняются. В снимок входят только
файлы со следующими suffixes: `.py`, `.pyi`, `.md`, `.toml`, `.yaml`, `.yml`,
`.json`, `.txt`.

| Ограничение | Значение |
| --- | --- |
| Diff | 512 KiB |
| Файлы снимка | 1 000 |
| Один файл снимка | 256 KiB |
| Сумма файлов снимка | 10 MiB |
| Python targets для lint | 100 |
| Добавленные строки для lint | 10 000 |
| Один lint target | 256 KiB |
| Diagnostics | 100 |

`lint_pr` читает только изменённые `.py` targets и только added lines. Он ищет
прямой вызов `eval(...)` (правило `ADLC001`, severity `high`) и не исполняет
код checkout. Это Python MVP, не универсальный анализатор.

## Podman и очистка

В PR override у `reset`, `repo-rag` и `agent` задано `label=disable`. Это
обход совместимости: Docker Compose 2.29.2 не передаёт `:z` для named volumes
в Podman, поэтому без отключения SELinux process-label следующий контейнер мог
бы не прочитать подготовленный снимок. Сервисы остаются non-root, а остальные
ограничения networks, secrets и read-only mounts сохраняются.

Удалите PR runtime-снимок отдельной overlay-командой:

```bash
docker compose -f compose.yaml -f scenarios/pr-review.compose.yaml down -v --remove-orphans
```

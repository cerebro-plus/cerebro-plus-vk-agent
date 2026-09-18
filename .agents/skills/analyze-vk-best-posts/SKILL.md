---
name: analyze-vk-best-posts
description: Use when a confirmed competitor_set.json must be analyzed over a fixed weekly VK post window, including comments, media, ER benchmarks, best posts, and validated outputs.
---

## Входы

- Подтверждённый `competitor_set.json` и, при наличии, компактный `competitor_handoff.json`.
- VK API token через `--token-file` либо `VK_API_TOKEN`; не включать его в результаты.
- Уникальный каталог запуска, Node.js и каталог модулей среды XLSX.

## Последовательность работы

1. Выполнить `preflight`: проверить вход, среду, доступность VK API и права токена на минимальном `wall.get`, затем один раз зафиксировать момент `Europe/Moscow`. При `network_blocked`, `network_unreachable`, `invalid_token` или `insufficient_permissions` остановиться до сбора.
2. Выполнить `collect`, `process`, `build`, затем `validate`; не искать новых конкурентов.
3. Применять детали из `references/execution-contract.md`, `references/media-rules.md` и `references/excel-columns.md` через сценарии, не воспроизводить их вручную.
4. Показать краткий итог и запросить явное подтверждение.
5. После подтверждения выполнить `confirm`, повторить `validate` и создать `posts_handoff.json`.

## Смысловые решения

- Использовать только входные community ID и официальный VK API; `groups.search` запрещён.
- Считать окно ровно `168` часов от единого момента, ER и бенчмарки — только сценариями.
- Не угадывать тип вложения: неподтверждённые и не укладывающиеся в типологию объекты сохранять в «Недоступно» с причиной.
- Считать клип завершённым только при транскрипции `success`/`no_speech`.
- Разрешать деградированное медиа только с флагом и записанным явным согласием.

## Условия остановки

- Остановиться до API при неподтверждённом/небезопасном входе, смене хэша или момента запуска.
- Блокировать этап при критической ошибке API/TLS, неполной пагинации, дублях, неверных ER/бенчмарках, рассинхронизации комментариев/JSON/XLSX, повреждённых путях/хэшах, секрете или непройденном медиашлюзе.
- Не передавать дальше `draft`, неуспешную `final_validation.json` или несинхронные результаты.

## Результаты

- `vk_best_posts.xlsx`, `vk_posts_dataset.json`, `vk_comments.json`, `media_summary_anonymized.json`.
- `run_passport.json`, `workbook_validation.json`, `validation_report.json`, `final_validation.json`.
- После подтверждения — синхронные `status: confirmed`, дата/время подтверждения и `posts_handoff.json`.

## Команды проверки

```text
<python> scripts/analyze_vk_best_posts.py preflight --competitor-set <competitor_set.json> --token-file <token.txt> --run-dir <run-dir> --node <node> --node-modules <node_modules>
<python> scripts/analyze_vk_best_posts.py collect --competitor-set <competitor_set.json> --token-file <token.txt> --run-dir <run-dir>
<python> scripts/analyze_vk_best_posts.py process --token-file <token.txt> --run-dir <run-dir>
<python> scripts/analyze_vk_best_posts.py build --run-dir <run-dir> --node <node> --node-modules <node_modules>
<python> scripts/analyze_vk_best_posts.py validate --competitor-set <competitor_set.json> --run-dir <run-dir>
<python> scripts/analyze_vk_best_posts.py confirm --competitor-set <competitor_set.json> --run-dir <run-dir> --confirmed-at <ISO-8601> --node <node> --node-modules <node_modules>
<python> ../_shared/scripts/pipeline_chain.py emit --stage best-posts --primary <run-dir>/outputs/vk_posts_dataset.json --comments <run-dir>/outputs/vk_comments.json --xlsx <run-dir>/outputs/vk_best_posts.xlsx --receipt <run-dir>/outputs/final_validation.json --upstream-handoff <competitor-run-dir>/competitor_handoff.json --output <run-dir>/outputs/posts_handoff.json
<python> -m unittest discover -s tests -p "test_*.py"
<python> <skill-creator-root>/scripts/quick_validate.py .
```

# Контракт цепочки VK

## Этапы

1. `business-context` принимает источники и ответы интервью, выпускает подтверждённый `business_context.json`.
2. `competitors` принимает подтверждённый `business_context.json`, выпускает подтверждённый `competitor_set.json`.
3. `best-posts` принимает подтверждённый `competitor_set.json`, выпускает подтверждённые dataset, комментарии, XLSX и `final_validation.json`.
4. `market-reaction` принимает квитанцию этапа лучших постов и выпускает доказательный HTML без нового поиска.

Статус промежуточного результата до явного подтверждения — `draft`. Следующий этап нельзя запускать от `draft`, кроме сборки и проверки самого финального отчёта.

## Компактные handoff-файлы

`pipeline_chain.py emit` создаёт небольшой JSON, предназначенный для маршрутизации и чтения агентом. Он содержит статус, SHA-256 основного файла, SHA-256 родителя, измеримые параметры и только необходимые идентификаторы. Большие тексты постов, вложения, комментарии, токены, подписанные URL и абсолютные пути в handoff не переносятся.

Названия этапов:

- `business-context`;
- `competitors`;
- `best-posts`;
- `market-reaction`.

Каждый handoff имеет:

- `schema_version`;
- `stage`;
- `status`;
- `primary`;
- `parent`;
- `artifacts`;
- `summary`;
- `security`.
- `project_identity` для новых handoff версии `1.1`: стабильный `project_id`, нормализованное имя бизнеса и SHA-256 подтверждённого бизнес-контекста.

## Происхождение

- `competitors.parent.sha256` равен SHA-256 `business_context.json`.
- `best-posts.parent.sha256` равен SHA-256 `competitor_set.json`.
- `market-reaction.parent` повторяет SHA-256 dataset, комментариев и XLSX из `final_validation.json`.
- `chain_manifest.json` хранит SHA-256 всех handoff-файлов и проверяет порядок этапов.

Все пути в handoff и манифесте относительные. Кэш хэшей хранит только непрозрачный ключ пути, размер, время изменения и SHA-256.

## Обратная совместимость

При чтении подтверждённых результатов версий `1.0` и `1.1` handoff-утилита принимает прежние имена полей:

- `online_presence` как объект либо список площадок;
- `target_count` либо `target_total`;
- `audience_size` либо `vk_followers`;
- `freshness_days` либо `freshness.period_months`;
- хэш бизнес-контекста в `provenance.business_context_sha256` либо `business_context.sha256`.

Новые результаты бизнес-контекста создавать по схеме `1.1`. Совместимые псевдонимы и версия `1.0` предназначены для чтения прошлых запусков, а не для расширения новой схемы.

## Условия остановки

Остановить переход при неверном статусе, отсутствующем файле, несовпадении SHA-256, несовместимом `schema_version`, несовпадении `post_id`, неуспешной финальной квитанции, секрете, абсолютном пути или нарушенном порядке этапов.

## Команды

```text
<python> pipeline_chain.py emit --stage business-context --primary <business_context.json> --output <business_handoff.json>
<python> pipeline_chain.py emit --stage competitors --primary <competitor_set.json> --upstream-handoff <business_handoff.json> --output <competitor_handoff.json>
<python> pipeline_chain.py emit --stage best-posts --primary <vk_posts_dataset.json> --comments <vk_comments.json> --xlsx <vk_best_posts.xlsx> --receipt <final_validation.json> --upstream-handoff <competitor_handoff.json> --output <posts_handoff.json>
<python> pipeline_chain.py emit --stage market-reaction --primary <validation_report.json> --analysis-input <analysis_input.json> --comment-signals <comment-signals.json> --findings <report_findings.json> --html <vk_content_report.html> --upstream-handoff <posts_handoff.json> --output <report_handoff.json>
<python> pipeline_chain.py assemble --business <business_handoff.json> --competitors <competitor_handoff.json> --posts <posts_handoff.json> --report <report_handoff.json> --output <chain_manifest.json> [--allow-draft-report]
<python> pipeline_chain.py validate --manifest <chain_manifest.json> [--allow-draft-report]
```

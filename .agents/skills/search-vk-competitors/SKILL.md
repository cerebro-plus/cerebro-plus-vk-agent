---
name: search-vk-competitors
description: Use when VK competitors must be found, verified, classified, and measured from a confirmed business_context.json.
---

## Входы

- Подтверждённый `business_context.json` и его SHA-256.
- Тот же пользовательский VK API token через `--token-file` или `VK_API_TOKEN`.
- Перед основным поиском выполнить минимальный VK preflight. При `network_blocked`, `network_unreachable`, `invalid_token` или `insufficient_permissions` остановиться без долгих повторов и новых артефактов поиска.
- Уникальный каталог текущего запуска от `$vk-competitor-workspace`.
- Для подтверждения — явное одобрение пользователя и дата.

## Последовательность работы

1. Выполнить `collect`; сценарий сам применяет `references/search-contract.md` и создаёт пул и пакеты до 200 кандидатов.
2. Открывать только пакет, указанный `review-status`; проверять первичные источники по `references/semantic-review-protocol.md`, заполняя созданный `init-review` файл.
3. Выполнять `merge-reviews`, затем `measure`; публикации получать только для смыслово прошедших кандидатов.
4. Повторять `review-status` → следующий пакет → `merge-reviews` → `measure --previous-measured`, пока цель не достигнута или пул не исчерпан.
5. Выполнить `finalize` и `validate`; показать краткий итог и запросить подтверждение.
6. После явного подтверждения выполнить `confirm`, повторить `validate` и создать `competitor_handoff.json`.

## Смысловые решения

- Включать только действующее официальное VK-сообщество; сайт без VK не подходит.
- Применять классификацию, ключевые особенности, обязательные включения и исключения карточки; неизвестное не придумывать.
- Не ослаблять критерии при недоборе; каждый обнаруженный `group_id` должен иметь конкретное решение.
- До одобрения сохранять `draft`. Токен не копировать, не выводить и не записывать в результаты.

## Условия остановки

- До API: карточка не `confirmed`, повреждена, содержит секрет либо не задаёт измеримые критерии.
- Во время сбора: критическая ошибка VK после повторов.
- До финализации: цель не достигнута и остался необработанный смысловой пакет.
- До подтверждения: неверные хэши, дубли, ошибочный ER, неподтверждённые источники, неполный аудит, рассинхронизация JSON/XLSX или секрет.

## Результаты

`raw_candidates.json`, `review_batches/`, `candidate_reviews.json`, `measured_candidates.json`, `competitor_set.json`, `candidate_audit.json`, `run_passport.json`, `business_context.sha256`, `competitor_analysis.xlsx`; после подтверждения — `competitor_handoff.json`.

## Команды проверки

```text
<python> scripts/search_vk_competitors.py collect --business-context <business_context.json> --token-file <token.txt> --output <run>/raw_candidates.json --review-dir <run>/review_batches
<python> scripts/search_vk_competitors.py init-review --batch <run>/review_batches/batch-NNN.json --output <run>/reviews/review-NNN.json
<python> scripts/search_vk_competitors.py merge-reviews --business-context <business_context.json> --raw-candidates <run>/raw_candidates.json --reviews-dir <run>/reviews --output <run>/candidate_reviews-NNN.json
<python> scripts/search_vk_competitors.py review-status --business-context <business_context.json> --manifest <run>/review_batches/manifest.json --candidate-reviews <run>/candidate_reviews-NNN.json [--measured-candidates <run>/measured_candidates-NNN.json]
<python> scripts/search_vk_competitors.py measure --business-context <business_context.json> --raw-candidates <run>/raw_candidates.json --candidate-reviews <run>/candidate_reviews-NNN.json --token-file <token.txt> --output <run>/measured_candidates-NNN.json [--previous-measured <previous.json>]
<python> scripts/search_vk_competitors.py finalize --business-context <business_context.json> --raw-candidates <run>/raw_candidates.json --measured-candidates <run>/measured_candidates-NNN.json --candidate-reviews <run>/candidate_reviews-NNN.json --output-dir <result-dir>
<python> scripts/search_vk_competitors.py validate --directory <result-dir> --business-context <business_context.json>
<python> scripts/search_vk_competitors.py confirm --directory <result-dir> --business-context <business_context.json> --date YYYY-MM-DD
<python> ../_shared/scripts/pipeline_chain.py emit --stage competitors --primary <result-dir>/competitor_set.json --upstream-handoff <business-run-dir>/business_handoff.json --output <result-dir>/competitor_handoff.json
<python> -m unittest discover -s tests -p "test_*.py"
<python> <skill-creator-root>/scripts/quick_validate.py .
```

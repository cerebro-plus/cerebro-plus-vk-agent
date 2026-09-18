---
name: business-context-interview
description: Use when business context must be collected, completed, or reconfirmed before VK competitor research.
---

## Маршрут

1. Через `$vk-competitor-workspace` создать уникальный каталог `runs/YYYY-MM-DD/<run-id>/`; прошлые запуски не изменять.
2. Получить все исходные ссылки. Среди них должна быть ссылка на VK-сообщество; если её нет, остановиться и запросить её либо явное подтверждение, что VK-источник отсутствует и задача будет неполной.
3. Получить VK API token и сохранить скрытым вводом в `vk_api_token.txt` в корне проекта. Не передавать токен в аргументах команд, ответы, логи, JSON, Markdown или handoff; существующий файл без явного разрешения не перезаписывать.
4. Выполнить сбор всех источников по `references/source-review.md`. VK изучать через API 5.199; остальные URL — через доступный HTTP- или браузерный способ. Сохранить безопасный `source_review.json` в текущем запуске и проверить его до интервью.
5. Инициализировать `interview_state.json`. Сначала провести основной бизнес-блок: только незаполненные после источников поля, один простой вопрос за раз, не более 15 вопросов вместе с уточнениями.
6. Если ответ общий, неполный или противоречивый, отметить это в состоянии, задать уточнение к тому же вопросу и не переходить к новой теме. Не повторять уже заданную формулировку и не объединять несколько самостоятельных вопросов.
7. Завершив основной блок или достигнув лимита, перечислить оставшиеся пробелы и явно предложить дополнительный раунд. Затем отдельным блоком выяснить известных конкурентов, правила классификации, ключевые особенности и критерии поиска.
8. Создать `business_context.json` и детерминированный `business_card.md`; до одобрения хранить оба результата со статусом `draft`. Показать карточку пользователю целиком и попросить проверить факты, пробелы и критерии.
9. Только после явного подтверждения пользователя записать approval для хэшей показанного draft, затем выполнить `confirm`, `validate` и создать компактный `business_handoff.json` общей утилитой. Любое изменение draft аннулирует прежний approval.

## Ключевые решения

- JSON — источник истины; Markdown — его представление.
- Источники анализируются до первого вопроса; подтверждённое источником повторно не спрашивать.
- Основной бизнес-блок ограничен 15 вопросами, включая уточнения. Конкурентный блок идёт после него отдельно и содержит только минимально необходимые вопросы.
- Правила качества вопросов и состояния: `references/interview-protocol.md`, `references/interview-state.schema.json`.
- Определения конкурентов и обязательные факторы: `references/competitor-classification.md`.
- Не домысливать отсутствующие сведения. Неподтверждённые цены, преимущества и характеристики помечать как пробелы либо непроверенные данные.
- Для каждой ссылки фиксировать способ получения, охват, ограничения и наблюдаемые факты по `references/source-review.schema.json`; поддерживать несколько сайтов и иных площадок.
- Явно зафиксировать, должны ли ключевые особенности повторяться у конкурента, насколько это важно и для каких типов конкурентов применяется.
- Следующий этап получает полный JSON как машинный вход, а агент читает компактный handoff.
- Правила карточки и схема: `references/card-structure.md`, `references/business-context.schema.json`.
- Общий контракт цепочки: `../_shared/references/chain-contract.md`.

## Остановка

- Не начинать интервью, пока каждый переданный источник не изучен либо его недоступность не записана и не показана пользователю.
- Остановить основной блок после вопроса 15, перечислить пробелы и предложить дополнительный раунд.
- Не переходить к новой теме при неразрешённом общем, неполном или противоречивом ответе.
- Не переходить к конкурентному блоку до закрытия основного блока.
- Не подтверждать без явного одобрения.
- Не передавать дальше неподтверждённый, рассинхронизированный, повреждённый или содержащий секрет результат.

## Результаты и проверка

```text
<python> scripts/source_intake.py save-token --workspace-root <workspace>
<python> scripts/source_intake.py collect --source-list <sources.json> --token-file <workspace>/vk_api_token.txt --output <run-dir>/source_review.json
<python> scripts/source_intake.py validate --input <run-dir>/source_review.json
<python> scripts/interview_state.py init --source-review <run-dir>/source_review.json --output <run-dir>/interview_state.json
<python> scripts/interview_state.py ask --state <run-dir>/interview_state.json --phase business --topic <topic> --question <question>
<python> scripts/interview_state.py answer --state <run-dir>/interview_state.json --number <n> --quality complete|general|incomplete|contradictory --answer <summary>
<python> scripts/interview_state.py close-business --state <run-dir>/interview_state.json --gaps-file <gaps.json> --additional-round-offered
<python> scripts/interview_state.py ask --state <run-dir>/interview_state.json --phase competitors --topic <topic> --question <question>
<python> scripts/interview_state.py close-competitors --state <run-dir>/interview_state.json
<python> scripts/interview_state.py validate --state <run-dir>/interview_state.json
<python> scripts/business_context.py create --input <context-input.json> --source-review <run-dir>/source_review.json --interview-state <run-dir>/interview_state.json --output-dir <run-dir>
<python> scripts/business_context.py sync --input <corrected-context-input.json> --source-review <run-dir>/source_review.json --interview-state <run-dir>/interview_state.json --directory <run-dir>
<python> scripts/business_context.py approve --directory <run-dir> --statement <explicit-user-approval> --date YYYY-MM-DD
<python> scripts/business_context.py confirm --directory <run-dir> --date YYYY-MM-DD
<python> scripts/business_context.py validate --directory <run-dir>
<python> ../_shared/scripts/pipeline_chain.py emit --stage business-context --primary <run-dir>/business_context.json --output <run-dir>/business_handoff.json
<python> -m unittest discover -s tests -p "test_*.py"
```

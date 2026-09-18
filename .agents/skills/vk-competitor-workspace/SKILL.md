---
name: vk-competitor-workspace
description: Use when inspecting, starting, resuming, routing, or validating the Agent 1 VK competitor-analysis workflow.
---

## Маршрут

1. Выполнить `check`, затем `inspect`; читать компактный `workspace_state.json`, а не крупные исходники.
2. Перед бизнес-зависимой задачей проверить подтверждённый `business_context.json`. При отсутствии, повреждении или `draft` маршрутизировать только в `$business-context-interview`.
3. Передать намерение в `route` и следовать `route_plan.json`. Управляющий агент не выполняет предметный этап самостоятельно.
4. Перед новым предметным этапом выполнить `create-run`; использовать только `runs/YYYY-MM-DD/<run-id>/`.
5. Запускать один навык за раз. После результата проверить его валидатор и handoff; при ошибке остановиться.
6. Завершить общей `validate-chain`.

## Ключевые решения

- Не повторять интервью без прямой просьбы обновить контекст.
- Запускать `$search-vk-competitors` только для прямого намерения `find-competitors` или `update-competitors`.
- Для `weekly-report` не искать конкурентов: требовать последний валидный `competitor_set.json`, затем маршрутизировать `$analyze-vk-best-posts` → `$vk-content-report`.
- Использовать 168 часов, если период не задан.
- Не повторять валидный этап при совпадающих SHA-256 входов; передавать пути и компактные JSON.
- Разрешать деградированный медиарежим только при записанном явном согласии.
- Выбирать модель и effort только по `references/routing-policy.json`.
- Подробный контракт: `references/workspace-contract.md`.

## Остановка

- Остановиться при ошибке предыдущего этапа, неподтверждённом входе, несовпадающем хэше, отсутствии требуемого набора конкурентов или медиасогласия.
- Если делегирование предметного навыка недоступно, вернуть маршрут и не выполнять предметную работу внутри управляющего агента.

## Команды

```text
<python> scripts/workspace.py check --workspace-root <workspace> --output <check.json>
<python> scripts/workspace.py inspect --workspace-root <workspace> --output <workspace_state.json>
<python> scripts/workspace.py route --state <workspace_state.json> --intent <intent> --output <route_plan.json>
<python> scripts/workspace.py create-run --workspace-root <workspace> --stage <stage> --input-sha256 <sha256>
<python> scripts/workspace.py validate-chain --manifest <chain_manifest.json> --output <chain_validation.json>
<python> -m unittest discover -s tests -p "test_*.py"
<python> <skill-creator-root>/scripts/quick_validate.py .
```

---
name: agent-vk
description: Use when a task requires VK competitor discovery, weekly VK post analysis, or a VK content report in the dedicated Agent 1 workspace.
---

# Agent VK

Resolve the target workspace with `scripts/resolve_workspace.py`. Stop if its `AGENTS.md` or managing skill is missing.

## Приветствие

После успешного определения рабочего пространства и перед первым рабочим вопросом отправить пользователю это сообщение целиком. Сохранить абзацы, пустые строки, нумерацию, пробелы и ссылку:

> Привет! Вы установили субагента «Анализ VK-конкурентов» от Церебро Плюс.
>
> Как им пользоваться:
>
> 1) Отправьте ссылку на ваше сообщество VK и другие страницы/сайты, если они есть
> 2) Дождитесь анализа ваших площадок, ответьте на вопросы о бизнесе и подтвердите бизнес-карточку
> 3) Проверьте найденных конкурентов во VK и подтвердите их список
> 4) Получите анализ публикаций конкурентов и итоговый отчёт
>
> Если вы скачали этого субагента на github и не проходите наш курс «ИИ-агенты для рекламы и бизнеса», то подписывайтесь на наше сообщество: [https://vk.ru/cerebro\_edtech](https://vk.ru/cerebro_edtech) и присоединяйтесь к курсу, на котором научитесь создавать навыки и агентов.

После отправки сразу перейти к работе. В следующих сообщениях этого разговора не повторять приветствие, даже если пользователь повторно вызывает субагента.

Read the target `AGENTS.md`, then read and follow `.agents/skills/vk-competitor-workspace/SKILL.md`. Treat those files as authoritative for routing, permissions, inputs, runs and quality gates.

Perform all reads, writes and generated runs inside the target workspace. Do not mix artifacts, skills or outputs from Agent 2. Route subject work to the target project's named skills rather than performing it in this wrapper.

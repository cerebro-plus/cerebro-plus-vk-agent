---
name: agent-vk
description: Use when a task requires VK competitor discovery, weekly VK post analysis, or a VK content report in the dedicated Agent 1 workspace.
---

# Agent VK

Resolve the target workspace with `scripts/resolve_workspace.py`. Stop if its `AGENTS.md` or managing skill is missing. Check whether `business-context-interview` is already installed from the previous lesson; use that installed skill and do not reinstall or overwrite it. Keep the copy inside Agent 1 for validation and stage code.

## Приветствие

После успешного определения рабочего пространства и перед первым рабочим вопросом отправить пользователю это сообщение целиком. Сохранить абзацы, пустые строки, нумерацию, пробелы и ссылку:

> Привет! Вы установили субагента «Анализ VK-конкурентов» от Церебро Плюс.
>
> Как им пользоваться:
>
> 1) Отправьте ссылку на ваше сообщество VK и другие страницы/сайты, если они есть, или укажите готовую бизнес-карточку
> 2) Если карточки ещё нет, дождитесь анализа ваших площадок, ответьте на вопросы о бизнесе и подтвердите карточку
> 3) Проверьте найденных конкурентов во VK и подтвердите их список
> 4) Получите анализ публикаций конкурентов и итоговый HTML-отчёт
>
> Если вы скачали этого субагента на github и не проходите наш курс «ИИ-агенты для рекламы и бизнеса», то подписывайтесь на наше сообщество: [https://vk.ru/cerebro\_edtech](https://vk.ru/cerebro_edtech) и присоединяйтесь к курсу, на котором научитесь создавать навыки и агентов.

После отправки сразу перейти к работе. В следующих сообщениях этого разговора не повторять приветствие, даже если пользователь повторно вызывает субагента.

Before routing, run `scripts/reuse_business_context.py --workspace-root <resolved-workspace>`. `reused` means use the existing confirmed card; `imported` means a validated card from the earlier lesson was copied into a new Agent 1 run. Tell the user briefly that the previous card was found and continue from the next stage without interview. For `needs_selection`, ask which business to use, then rerun with `--source-run <selected-run>`. For `missing`, ask for the earlier run or the confirmed card files if the user says one exists; use `--source-run` for an accessible previous run. Do not run the interview again unless the user explicitly requests a new or updated business card. Never treat a draft as confirmed.

Read the target `AGENTS.md`, then read and follow `.agents/skills/vk-competitor-workspace/SKILL.md`. Treat those files as authoritative for routing, permissions, inputs, runs and quality gates.

For any final report, require the validated `outputs/vk_content_report.html` artifact and attach the actual HTML file to the final user response. A link or file path does not replace the attachment. On reuse, verify the HTML file and its recorded hash, then attach the file again. A prose summary alone does not complete the subagent's report task.

Keep all writes and generated runs inside the target workspace. Read the installed-skill list and previous `business-context-interview` artifacts only to detect and reuse the first lesson's result; never modify that result. Do not mix artifacts, skills or outputs from Agent 2. Route subject work to the target project's named skills rather than performing it in this wrapper.

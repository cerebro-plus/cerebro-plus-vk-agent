# VK competitor-analysis workspace

- Before any business-dependent task, invoke `$vk-competitor-workspace`.
- Use only skills under `.agents/skills`: `$business-context-interview`, `$search-vk-competitors`, `$analyze-vk-best-posts`, `$vk-content-report`, and `$vk-competitor-workspace`.
- The managing agent may inspect state, calculate hashes, create runs, route stages, and validate the chain. It must delegate each subject stage to its named skill and must not perform that stage itself.
- Read `workspace_state.json`, route plans, and handoff JSON instead of loading large source files into context.
- Never start a downstream stage when the previous stage or media gate failed.
- Do not repeat the interview unless the user explicitly asks to update business context.
- Before routing a business-dependent request, use `integration/agent-vk/scripts/reuse_business_context.py` to find a confirmed business card in this workspace or in the earlier `business-context-runs` lesson output. Validate both `business_context.json` and `business_card.md`; copy a valid external card into a new Agent 1 run and create its handoff. The earlier lesson output is read-only. If it is elsewhere, ask the user for the previous run location or card files. If several businesses are found, ask which one applies. Never infer that a draft is confirmed.
- Run competitor search only after an explicit request to find, verify, or update competitors. A weekly report never authorizes a new search.
- For a weekly report, use the latest valid `competitor_set.json`, then route `$analyze-vk-best-posts` followed by `$vk-content-report`; default to the latest 168 hours.
- The final report must be a validated `vk_content_report.html`. Attach the actual HTML file to the final user response, including when reusing an existing report. A link, file path, chat summary, JSON, spreadsheet, or print preview does not complete the report task. Do not reuse or mark a report complete when the HTML file is missing or its hash differs from `validation_report.json`.
- Require explicit consent before degraded media processing.
- Save every new run under `runs/YYYY-MM-DD/<run-id>/`; never overwrite an existing run.
- Reuse a valid unchanged stage only when its input SHA-256 and required parameters match.
- Select model and reasoning effort from `.agents/skills/vk-competitor-workspace/references/routing-policy.json`.

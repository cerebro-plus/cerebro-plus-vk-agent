# VK competitor-analysis workspace

- Before any business-dependent task, invoke `$vk-competitor-workspace`.
- Use only skills under `.agents/skills`: `$business-context-interview`, `$search-vk-competitors`, `$analyze-vk-best-posts`, `$vk-content-report`, and `$vk-competitor-workspace`.
- The managing agent may inspect state, calculate hashes, create runs, route stages, and validate the chain. It must delegate each subject stage to its named skill and must not perform that stage itself.
- Read `workspace_state.json`, route plans, and handoff JSON instead of loading large source files into context.
- Never start a downstream stage when the previous stage or media gate failed.
- Do not repeat the interview unless the user explicitly asks to update business context.
- Run competitor search only after an explicit request to find, verify, or update competitors. A weekly report never authorizes a new search.
- For a weekly report, use the latest valid `competitor_set.json`, then route `$analyze-vk-best-posts` followed by `$vk-content-report`; default to the latest 168 hours.
- Require explicit consent before degraded media processing.
- Save every new run under `runs/YYYY-MM-DD/<run-id>/`; never overwrite an existing run.
- Reuse a valid unchanged stage only when its input SHA-256 and required parameters match.
- Select model and reasoning effort from `.agents/skills/vk-competitor-workspace/references/routing-policy.json`.

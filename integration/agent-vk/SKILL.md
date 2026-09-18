---
name: agent-vk
description: Use when a task requires VK competitor discovery, weekly VK post analysis, or a VK content report in the dedicated Agent 1 workspace.
---

# Agent VK

Resolve the target workspace with `scripts/resolve_workspace.py`. Stop if its `AGENTS.md` or managing skill is missing.

Read the target `AGENTS.md`, then read and follow `.agents/skills/vk-competitor-workspace/SKILL.md`. Treat those files as authoritative for routing, permissions, inputs, runs and quality gates.

Perform all reads, writes and generated runs inside the target workspace. Do not mix artifacts, skills or outputs from Agent 2. Route subject work to the target project's named skills rather than performing it in this wrapper.

```powershell
python -X utf8 scripts/resolve_workspace.py
python -X utf8 -m unittest discover -s tests -p "test_*.py" -v
```

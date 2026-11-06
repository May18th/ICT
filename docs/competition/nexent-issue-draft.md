# Nexent 平台 issue 草稿

> **已提交（2026-11-07）**：https://github.com/ModelEngine-Group/nexent/issues/4307
> 仓库 `ModelEngine-Group/nexent`（公开，6 148 stars），提交账号 `May18th`。
> 实际发出的是「精简版」，标题加了 `[Improvement]` 前缀（对应仓库的 `improvement_proposal.yml` 模板）。
> 仓库没有 `enhancement` 标签，故未加标签。

---

## 一、可直接贴的精简版（约 250 词）

**Title**

`Skills: warn when a SKILL.md references tools the agent does not mount`

**Body**

### Summary

Binding a skill whose `SKILL.md` tells the model to call tools that the target agent has *not* mounted fails silently at
runtime: the model attempts the call, the run errors or truncates, and nothing in the UI connects the failure to the
skill. `POST /agent/check_skills` only checks name conflicts, not tool references.

### Environment

Nexent `v2.7.0`, docker compose; custom agent with 2 mounted MCP tools, model `DeepSeek-Chat`; 2 custom skills bound to
the agent's draft and published version.

### Reproduction

1. Create an agent that mounts only `event_deduce` and `evidence_locate`.
2. Upload a skill whose body instructs the model to call `conflict_check` / `allocation_plan`.
3. Bind it to draft and published version, publish, run a normal task via the northbound API.

### Expected

A warning at bind or publish time, or a graceful failure message naming the skill.

### Actual

The run fails / truncates. Agent list, skill list and the bind response are all green.

### Impact

With our fixed 8-field task, binding the skill took the agent from **8/8 fields correct to 0/8**. Unbinding restored 8/8.

### Cause

`_get_skills_for_template()` in `backend/agents/create_agent_info.py` injects only `name` + `description`; the body is
reachable only through the builtin `read_skill_md` tool. Skills are authored against the whole MCP server while tool
mounts are per agent, and the two are validated independently.

### Proposal

At bind and publish, extract tool-like tokens from the skill body, diff them against the agent's mounted tools, and
return non-fatal `warnings[]` surfaced as a UI banner. Warnings first — hard refusal would break intentionally partial
skills.

### Workaround

Grep the body for tool names and intersect with the mount list before publishing; behaviourally, re-run a known-good
task after every bind.

---

## 二、较长的原始稿（备查，提交时不必全贴）

### Why this is not just "the skill author's mistake"

The tool list of an agent is a deliberate design lever: narrowing the mounted tools is the reliable way to keep a model
on a fixed step plan. But a skill is authored against the *whole* MCP server, and the platform lets you bind it to any
agent. The two are validated independently, so the mismatch only shows up at runtime.

### Relevant code

- `backend/agents/create_agent_info.py` — `_get_skills_for_template()` returns only `{"name", "description"}` per skill,
  so the prompt only advertises names/descriptions; the procedure in `SKILL.md` is only reachable via the builtin
  `read_skill_md` tool (`ReadSkillMdTool`). Combined with a narrowed tool list, the model may never read the body at all,
  or may read it and then follow instructions referring to unmounted tools.
- `backend/apps/agent_app.py` — `POST /agent/check_skills` only reports **name conflicts**
  ("Check bundled skill names before agent import without creating data").

### Proposed fix (small, additive)

At bind time (`POST /skills/instance/update`) and at publish, parse the `SKILL.md` body for known tool names
(tokens matching the tenant's tool library) and diff against the agent's mounted tools. Return a non-fatal `warnings[]`
in the response, and surface it in the UI as a banner.

A stricter variant would refuse the bind unless `?force=true`. We'd suggest warnings first — refusing outright breaks
legitimate cases where the skill is intentionally partial.

### Workaround detail

Today the only reliable detector is behavioural: re-run a task whose correct output you already know after every bind,
and compare field by field (in our case a fixed task with 8 known fields; the regression showed up immediately as 0/8).

We did **not** automate the token check — our binding script only verifies that the draft and the published version both
carry the skill, not that the skill's contents match the tool mounts. That gap is exactly what this issue is about.

### 可选的脱敏日志（要贴就加，不贴也行）

```
management.services.agent.run | Agent run preparation failed without memory: AttributeError
consts.exceptions.MemoryPreparationException: 'NoneType' object has no attribute 'endswith'
management.services.agent.run | Agent stream producer exited without terminal state conversation=<id>
```

注意：这段是**另一个**故障（MCP 记录 `server_url` 被清空）的表象，日志看着像记忆模块问题、实际是工具通道断了。
它跟本 issue 不是同一件事，**别混着贴**，否则会被追问。

---

## 三、我们这边的口径（不进 issue，答辩用）

- 这条不是「平台有 bug」，是「平台缺一层校验」。写成建议而不是指责，官方更容易接受。
- 证据是量化的：同一 Agent、同一提示词，绑技能 8/8 → 0/8；解绑后恢复。可进「对平台的贡献」页。
- 答辩一句话：**「我们把这个坑提回给平台，并给了最小改动建议：绑定/发布时做技能-工具一致性 lint。」**
- 不要写「我们发现了平台的严重缺陷」——评委会觉得你在贬低比赛平台。

---
name: gauge-context-review
description: Cut context cost. Review the always-on memory and the skill index, and split the user's long skills when the brief allows it.
---

# Context review

What gets pasted before the user types is the bill. Measure it, then cut only what this install owns.

## When to use

- The user asks why context is expensive, or to review memory, skills, or rules.
- A scheduled Hermes Gauge review is running.
- Before a long task, when the skill index or `AGENTS.md` looks heavy.

Call `gauge_setup` first. Do not estimate sizes yourself. `action` `scan` measures. `action` `review` adds the brief. Pass `apply: true` only when the user asked to edit. A scheduled run passes `scheduled: true` and nothing else: the brief then says whether this run may edit.

The report is markdown unless the user asks for `txt`, `html`, or `json`. Lead with the ranked cuts, in the order the report gives them. Each cut has What, Reco, Save, and Confidence. Do not invent a saving the report did not measure. A skill not opened for the unused period costs its description on every turn. Splitting a long skill saves tokens only on the turns that open it.

## Limits

The user picks a memory level, and may set any limit by hand. `gauge_setup` already applies them; quote its numbers, not the ones below.

| Level | Per block | Memory file full | Long skill | Split from | Unused after |
| --- | --- | --- | --- | --- | --- |
| lean | 1,500 | 80% | 8,000 | 12,000 | 5 days |
| standard | 2,000 | 90% | 15,000 | 20,000 | 7 days |
| roomy | 3,000 | 95% | 25,000 | 35,000 | 14 days |

To change them: `/gauge config set memory.level=lean`, or one key such as `memory.skill_split_tokens=25000`.

## What is pasted every turn

Each of these stays under the per-block limit (characters ÷ 4). The whole point of the file is to be short.

- `MEMORY.md` and `USER.md`, per profile. Also stay under the Hermes character cap. Past the "memory file full" share of that cap, the next write can fail.
- `SOUL.md`.
- The skill index: one line per skill, paid whether or not the skill is used.
- An external recall block (Hindsight or similar). A cap above the per-block limit, or retain-on-every-turn, is the finding.
- In a project: `AGENTS.md`, `CLAUDE.md`, and `.cursor/rules`.

Write memory as one durable fact per entry. Imperative, dated, useful on a later day. No task status, no process noise, no essay. A procedure belongs in a skill. A project fact belongs in the project.

## Skills by source and category

`gauge_setup` groups every skill by source and by category (the folder above the skill folder), and gives each group a rule:

- **may split** — yours (or the project's, when the user chose it), outside the skipped categories.
- **skipped** — yours, in a category the user listed in `memory.skip_categories`. Report only.
- **report only** — a source the user did not choose in `memory.split_sources`.
- **leave as is** — published (author Hermes, Nous, or a GitHub source) or external (`skills.external_dirs`). Never rewrite these. Disconnecting an external directory is the fix.

The split plan is the "may split" skills over the split line. It is the only list of files you may change.

## Split

Report only, unless the brief says you may edit.

When you may edit, for each skill on the split plan:

1. Keep `SKILL.md` under the per-block limit: the frontmatter unchanged (same `name`, same `description`), when to use it, and a map of the steps that points to the reference files.
2. Move the procedure, the examples and the tables to `references/<topic>.md` next to `SKILL.md`. Keep the wording. Do not drop a step.
3. Do not change what the skill does, and do not rename it.
4. Run `gauge_setup` again and report each split: path, tokens before, tokens after.

Never:

- touch a file that is not on the split plan, or a skipped category;
- rewrite an official, community, or external skill, even if it is long;
- raise the memory character cap to fit more text. Shorten the note, or move the fact;
- compact a session or start a new one.

## Schedule and model

The measurement uses no model. The written review uses the profile default unless the user pinned one.

- Nightly pass: `z-ai/glm-5.3-flash` when that model is available.
- Codex only: provider `openai-codex`, model left empty.
- Claude only: provider `anthropic`, a Sonnet-class model they already use.

The user sets this with `/gauge review model` and `/gauge review provider`. A nightly job is `/gauge review schedule 24h 03:00 Europe/Paris` (also `12h`, `every 6h`, `every 2d`). `off` removes it. The job writes a file. It does not post to Telegram. Whether it splits is `memory.review_mode`: `report` (default) or `split`.

## Telegram

A short message can start a turn in a fraction of a second, and a photo or PDF can start another. `gauge_setup` reports that. Do not try to fix it from a skill: the turn has already been paid for. Holding the brief belongs in the Hermes Telegram adapter.

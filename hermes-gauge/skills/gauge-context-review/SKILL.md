---
name: gauge-context-review
description: Cut context cost. Review the always-on memory and the skill index. Report only, unless apply was asked.
---

# Context review

What gets pasted before the user types is the bill. Measure it, then cut only what this install owns.

## When to use

- The user asks why context is expensive, or to review memory, skills, or rules.
- A scheduled Hermes Gauge review is running.
- Before a long task, when the skill index or `AGENTS.md` looks heavy.

Call `gauge_setup` first. Do not estimate sizes yourself. `action` `scan` measures. `action` `review` adds the brief. Pass `apply: true` only when the user asked to edit.

The report is markdown unless the user asks for `txt`, `html`, or `json`. Lead with the ranked cuts, in the order the report gives them. Each cut has What, Reco, Save, and Confidence. Do not invent a saving the report did not measure. A skill not opened in 7 days costs its description on every turn. Splitting a long skill saves tokens only on the turns that open it.

## What is pasted every turn

Each of these stays under 2,000 tokens (characters ÷ 4). The whole point of the file is to be short.

- `MEMORY.md` and `USER.md`, per profile. Also stay under the Hermes character cap. Over 90% of that cap, the next write fails.
- `SOUL.md`.
- The skill index: one line per skill, paid whether or not the skill is used.
- An external recall block (Hindsight or similar). A cap above 2,000 tokens, or retain-on-every-turn, is the finding.
- In a project: `AGENTS.md`, `CLAUDE.md`, and `.cursor/rules`.

Write memory as one durable fact per entry. Imperative, dated, useful on a later day. No task status, no process noise, no essay. A procedure belongs in a skill. A project fact belongs in the project.

## Skills

Class them the way `gauge_setup` does.

- **Yours** — under `~/.hermes/skills`, or a project skill you wrote. These are the only files you may change, and only when apply was asked.
- **Published** — author Hermes, Nous, or a GitHub source in the frontmatter. Leave the file as published. You may say "disconnect this directory" or "stop loading this".
- **External** — `skills.external_dirs`, including a catalog such as OMH. Disconnecting it is the fix. Do not rewrite the files.

An own skill over 15,000 tokens is a warning. Over 20,000, split it: a short `SKILL.md` (when to use it, and the map) and the long procedure in `references/` or subfolders. The index keeps one line. The body loads on demand.

Descriptions in the index stay one sentence. A long description is paid on every message.

## Apply

Report only, unless the user said to apply or the brief says you may edit.

When you may edit:

- Touch only the paths the brief lists.
- Do not rewrite an official, community, or external skill, even if it is long.
- Do not raise the memory character cap to fit more text. Shorten the note, or move the fact.
- Do not compact a session and do not start a new one.

## Schedule and model

The measurement uses no model. The written review uses the profile default unless the user pinned one.

- Nightly pass: `z-ai/glm-5.3-flash` when that model is available.
- Codex only: provider `openai-codex`, model left empty.
- Claude only: provider `anthropic`, a Sonnet-class model they already use.

The user sets this with `/gauge review model` and `/gauge review provider`. A nightly job is `/gauge review schedule 24h 03:00 Europe/Paris` (also `12h`, `every 6h`, `every 2d`). `off` removes it. The job writes a file. It does not post to Telegram.

## Telegram

A short message can start a turn in a fraction of a second, and a photo or PDF can start another. `gauge_setup` reports that. Do not try to fix it from a skill: the turn has already been paid for. Holding the brief belongs in the Hermes Telegram adapter.

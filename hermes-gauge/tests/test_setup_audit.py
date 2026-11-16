"""Context-setup checklist, review brief, and cron schedule.

Loaded by path, like the other plugin tests. Nothing here imports the
plugin package, and nothing calls ``hermes cron``.
"""

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location(
        "hermes_gauge_setup_audit_under_test", HERE / "setup_audit.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


setup_audit = _load()


def _skill(path, body, description="Short.", author=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    author_line = f"author: {author}\n" if author else ""
    path.write_text(
        f"---\nname: {path.parent.name}\n{author_line}description: {description}\n---\n\n{body}\n",
        encoding="utf-8",
    )


def test_empty_home_passes(tmp_path):
    data = setup_audit.scan(tmp_path, lang="en")
    assert data["status"] == "OK"
    assert data["always_on_tokens"] == 0
    text = setup_audit.render_text(data)
    assert text.startswith("Context setup — OK")


def test_memory_over_the_token_cap_and_the_character_budget(tmp_path):
    (tmp_path / "config.yaml").write_text(
        "memory:\n  provider: builtin\n  memory_char_limit: 100\n  user_char_limit: 80\n",
        encoding="utf-8",
    )
    memories = tmp_path / "memories"
    memories.mkdir()
    (memories / "MEMORY.md").write_text("x" * 95, encoding="utf-8")
    (memories / "USER.md").write_text("same fact " * 20, encoding="utf-8")
    profile = tmp_path / "profiles" / "dev" / "memories"
    profile.mkdir(parents=True)
    (profile / "USER.md").write_text("same fact " * 20, encoding="utf-8")

    data = setup_audit.scan(tmp_path, lang="en")
    assert data["status"] == "ALERT"
    assert any("default/MEMORY.md" in item["detail"] and not item["ok"] for item in data["checks"])
    assert any("Identical entries" in item["detail"] for item in data["checks"])
    # 95 characters is under 2,000 tokens, and over 90% of a 100-character cap.
    memory = next(row for row in data["memory"]["stores"] if row["label"] == "default/MEMORY.md")
    assert memory["tokens"] == 23
    assert memory["fill"] == 0.95


def test_recall_cap_and_retain_every_turn(tmp_path):
    (tmp_path / "config.yaml").write_text("memory:\n  provider: hindsight\n", encoding="utf-8")
    bank = tmp_path / "hindsight"
    bank.mkdir()
    (bank / "config.json").write_text(
        json.dumps({"recall_max_tokens": 4096, "retain_every_n_turns": 1}),
        encoding="utf-8",
    )
    data = setup_audit.scan(tmp_path, lang="en")
    assert any(item["area"] == "recall" and item["severity"] == "alert" for item in data["checks"])
    assert "4096" in next(item["detail"] for item in data["checks"] if item["area"] == "recall")


def test_own_skill_over_20k_is_a_split_and_a_published_skill_is_not(tmp_path):
    skills = tmp_path / "skills"
    _skill(skills / "mine" / "SKILL.md", "word " * 20_000, author="Ada")
    _skill(
        skills / "bundled-copy" / "SKILL.md",
        "word " * 20_000,
        description="A very long description " * 20,
        author="Hermes Team",
    )
    data = setup_audit.scan(tmp_path, lang="en")
    own = data["skills"]["own_over"]
    assert len(own) == 1
    assert own[0]["rel"] == "mine/SKILL.md"
    assert own[0]["tokens"] >= setup_audit.SKILL_SPLIT_TOKENS
    assert any("split them" in item["detail"] for item in data["checks"])
    assert any("published skills" in note for note in data["notes"])
    assert any("descriptions are over" in item["detail"] for item in data["checks"])
    brief = setup_audit.brief(data, apply=False)
    assert "Do not edit any file" in brief
    assert "mine/SKILL.md" in brief
    assert "bundled-copy" not in brief
    applied = setup_audit.brief(data, apply=True)
    assert "only the own skills listed" in applied
    assert "bundled-copy" not in applied


def test_external_directory_is_counted_and_not_editable(tmp_path):
    external = tmp_path / "outside"
    _skill(external / "catalog" / "SKILL.md", "leave me", author="Nous")
    (tmp_path / "config.yaml").write_text(
        "plugins:\n  disabled:\n    - omh\nskills:\n  external_dirs:\n"
        f"    - {external}\n",
        encoding="utf-8",
    )
    data = setup_audit.scan(tmp_path, lang="en")
    assert data["skills"]["count"] == 1
    assert data["skills"]["own_over"] == []
    assert any("external skill directory" in item["detail"] for item in data["checks"])
    assert any("OMH is disabled" in note for note in data["notes"])


def test_disabled_omh_is_a_note_when_nothing_is_linked(tmp_path):
    (tmp_path / "config.yaml").write_text(
        "plugins:\n  disabled:\n    - omh\n",
        encoding="utf-8",
    )
    data = setup_audit.scan(tmp_path, lang="en")
    assert any("OMH is disabled" in note for note in data["notes"])
    assert data["status"] == "OK"


def test_project_agents_over_the_cap(tmp_path):
    project = tmp_path / "client"
    project.mkdir()
    (project / "AGENTS.md").write_text("rule " * 3000, encoding="utf-8")
    data = setup_audit.scan(tmp_path, project=project, lang="en")
    assert any(item["area"] == "project" and not item["ok"] for item in data["checks"])
    assert data["project"]["files"][0]["rel"] == "AGENTS.md"
    assert data["project"]["files"][0]["tokens"] > 2000


def test_commented_telegram_block_is_not_a_finding(tmp_path):
    (tmp_path / "config.yaml").write_text(
        "# telegram:\n#   text_batch_delay_seconds: 2.0\n",
        encoding="utf-8",
    )
    data = setup_audit.scan(tmp_path, lang="en")
    assert data["telegram"]["configured"] is False
    assert not any(item["area"] == "telegram" for item in data["checks"])


def test_live_telegram_block_warns_that_a_short_message_starts_a_turn(tmp_path):
    (tmp_path / "config.yaml").write_text(
        "telegram:\n  text_batch_delay_seconds: 2.0\n",
        encoding="utf-8",
    )
    data = setup_audit.scan(tmp_path, lang="en")
    assert data["status"] == "WARN"
    detail = next(item["detail"] for item in data["checks"] if item["area"] == "telegram")
    assert "0.18s" in detail
    assert "HERMES_TELEGRAM_TEXT_BATCH_DELAY_SECONDS" in detail
    assert "cannot hold the brief" in detail


def test_unused_skills_lead_with_what_reco_save_and_confidence(tmp_path):
    skills = tmp_path / "skills"
    desc = "d" * 400
    _skill(skills / "old" / "SKILL.md", "body", description=desc, author="Ada")
    _skill(skills / "never" / "SKILL.md", "body", description=desc, author="Ada")
    _skill(skills / "fresh" / "SKILL.md", "body", description=desc, author="Ada")
    _skill(skills / "huge" / "SKILL.md", "word " * 20_000, description="Short.", author="Ada")
    (skills / ".usage.json").write_text(json.dumps({
        "old": {"last_used_at": "2020-01-01T00:00:00+00:00", "use_count": 3, "pinned": False},
        "fresh": {"last_used_at": "2026-11-12T00:00:00+00:00", "use_count": 4, "pinned": False},
        "huge": {"last_used_at": "2026-11-12T00:00:00+00:00", "use_count": 2, "pinned": False},
    }), encoding="utf-8")
    now = datetime(2026, 11, 13, tzinfo=timezone.utc).timestamp()
    data = setup_audit.scan(tmp_path, lang="en", now=now)
    first = data["priorities"][0]
    assert first["save_scope"] == "every_turn"
    assert "4 skills, 2 not used in the last 7 days" in first["what"]
    assert first["confidence"] == "medium"
    assert first["save_share"] > 0
    assert "Do not rewrite" in first["reco"]
    text = setup_audit.render_text(data)
    assert "What:" in text and "Reco:" in text and "Save:" in text and "Confidence:" in text
    assert "%" in text
    opened = next(item for item in data["priorities"] if item["save_scope"] == "when_opened")
    assert opened["rank"] == 2
    assert "huge/SKILL.md" in opened["what"]
    assert opened["confidence"] == "high"
    assert "not on every message" in setup_audit.render_text(data)
    page = setup_audit.render_html(data)
    assert page.startswith("<!DOCTYPE html>")
    assert "What." in page and "Reco." in page


def test_schedule_accepts_a_nightly_clock_and_rejects_a_bad_step():
    nightly = setup_audit.parse_schedule("24h 03:00 Europe/Paris")
    assert nightly["ok"] is True
    assert nightly["cron"] == "0 3 * * *"
    assert nightly["timezone"] == "Europe/Paris"
    twice = setup_audit.parse_schedule("12h 03:00 Europe/Paris")
    assert twice["cron"] == "0 3,15 * * *"
    assert setup_audit.parse_schedule("every 2d 03:30 UTC")["cron"] == "30 3 */2 * *"
    assert setup_audit.parse_schedule("off")["off"] is True
    refused = setup_audit.parse_schedule("every 36h")
    assert refused["ok"] is False
    assert "divides 24" in refused["error"]
    zone = setup_audit.parse_schedule("24h 03:00 Not/AZone")
    assert zone["ok"] is False
    assert "Unknown timezone" in zone["error"]


def test_schedule_writes_a_cron_create_and_off_removes_it(tmp_path):
    calls = []

    def runner(argv):
        calls.append(argv)
        if argv[2] == "create":
            return 0, "Created job: abcdef12\n"
        return 0, "Removed job: Hermes Gauge context review (abcdef12)\n"

    path = tmp_path / "review.json"
    created = setup_audit.apply_schedule(path, "24h 03:00 Europe/Paris", runner)
    assert created["ok"] is True
    assert created["state"]["job_id"] == "abcdef12"
    assert created["state"]["cron"] == "0 3 * * *"
    argv = calls[0]
    assert argv[:4] == ["hermes", "cron", "create", "0 3 * * *"]
    assert "--deliver" in argv and "local" in argv
    assert "--skill" in argv and "gauge-context-review" in argv
    assert "--timezone" in argv and "Europe/Paris" in argv
    assert "shell" not in argv

    removed = setup_audit.apply_schedule(path, "off", runner)
    assert removed["ok"] is True
    assert calls[1] == ["hermes", "cron", "remove", "abcdef12"]
    assert removed["state"]["job_id"] == ""


def test_model_pin_is_passed_to_cron_and_can_be_cleared(tmp_path):
    path = tmp_path / "review.json"
    state = setup_audit.set_fields(path, model="z-ai/glm-5.3-flash", provider="openrouter")
    state["cron"] = "0 3 * * *"
    argv = setup_audit.cron_argv(state)
    assert argv[argv.index("--model") + 1] == "z-ai/glm-5.3-flash"
    assert argv[argv.index("--provider") + 1] == "openrouter"
    cleared = setup_audit.set_fields(path, model="default", provider="default")
    assert cleared["model"] == ""
    assert cleared["provider"] == ""
    cleared["cron"] = "0 3 * * *"
    again = setup_audit.cron_argv(cleared)
    assert "--model" not in again
    assert "--provider" not in again


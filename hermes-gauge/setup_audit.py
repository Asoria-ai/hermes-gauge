"""Context-setup checklist: what is pasted on every turn.

Pure functions over the Hermes home and an optional project directory.
Standard library only. ``scan`` never writes. Review preferences and the
cron job are a separate step, and only run when the user asks.

Token estimates are ``characters // 4``. That is the usual figure, not the
live gauge's doubled estimate.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


TURN_TOKEN_CAP = 2_000
SKILL_WARN_TOKENS = 15_000
SKILL_SPLIT_TOKENS = 20_000
UNUSED_DAYS = 7
DESCRIPTION_CHARS = 180
BUDGET_ALERT = 0.90
DEFAULT_MEMORY_CHARS = 2_200
DEFAULT_USER_CHARS = 1_375
SKILL_NAME = "gauge-context-review"
JOB_NAME = "Hermes Gauge context review"

_SKIP_DIRS = {
    ".git", "node_modules", "venv", ".venv", "__pycache__", "dist", "build",
}
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_JOB_ID = re.compile(r"(?:Created|Updated) job:\s*([0-9a-fA-F]{6,})")
_CLOCK = re.compile(r"^(\d{1,2}):(\d{2})$")
_EVERY = re.compile(r"^(\d+)([hd])$")

REVIEW_PROMPT = (
    "You are running the Hermes Gauge context review. "
    "Call gauge_setup first and follow the gauge-context-review skill. "
    "Report what is paid on every turn, which of the user's own skills are "
    "over 15,000 tokens, and which files you will not change. "
    "Do not edit any file. Official, community, and external skills stay "
    "as published. Write the report as your final message."
)


def estimate_tokens(chars):
    try:
        return max(int(chars), 0) // 4
    except (TypeError, ValueError):
        return 0


def _read(path):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _top_block(text, name):
    """Indented body of a top-level ``name:`` key. Comments stay in the body."""
    lines = text.splitlines()
    start = None
    for index, raw in enumerate(lines):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or raw[:1] in (" ", "\t"):
            continue
        key, _, _val = stripped.partition(":")
        if key.strip() == name:
            start = index + 1
            break
    if start is None:
        return []
    body = []
    for raw in lines[start:]:
        stripped = raw.strip()
        if stripped and not raw[:1].isspace() and not stripped.startswith("#"):
            break
        body.append(raw)
    return body


def _scalars_and_lists(body):
    """Keys at indent 2. A blank value starts a ``- item`` list."""
    scalars = {}
    lists = {}
    current = None
    for raw in body:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if indent == 2 and ":" in stripped:
            key, _, val = stripped.partition(":")
            key = key.strip()
            val = val.split("#", 1)[0].strip().strip("'\"" )
            if val == "":
                current = key
                lists.setdefault(key, [])
            else:
                scalars[key] = val
                current = None
            continue
        if current and stripped.startswith("- "):
            item = stripped[2:].split("#", 1)[0].strip().strip("'\"")
            if item:
                lists.setdefault(current, []).append(item)
            continue
        if indent <= 2:
            current = None
    return scalars, lists


def _uncommented(text, key):
    """``(section, value)`` for each live ``key:`` line. Section is the top-level key."""
    found = []
    section = ""
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not raw[:1].isspace() and stripped.endswith(":") and ":" in stripped:
            section = stripped.split(":", 1)[0].strip()
        if stripped.startswith(key + ":"):
            found.append((section, stripped.split(":", 1)[1].split("#", 1)[0].strip()))
    return found


def _frontmatter(text):
    if not text.startswith("---"):
        return ""
    end = text.find("\n---", 3)
    return "" if end == -1 else text[3:end]


def _fm_text(fm, key):
    lines = fm.splitlines()
    for index, line in enumerate(lines):
        if not line.startswith(key + ":"):
            continue
        val = line.split(":", 1)[1].strip()
        if val in (">", "|", ">-", "|-", ""):
            extra = []
            for nxt in lines[index + 1:]:
                if nxt.startswith((" ", "\t")):
                    extra.append(nxt.strip())
                elif nxt.strip():
                    break
            return " ".join(extra).strip("'\"").strip()
        return val.strip("'\"").strip()
    return ""


def _published(fm):
    for line in fm.splitlines():
        low = line.strip().lower()
        if low.startswith(("source:", "homepage:", "repository:")) and (
            "github.com" in low or "http://" in low or "https://" in low
        ):
            return True
    return False


def _protected_author(author):
    text = (author or "").lower().strip()
    if not text:
        return False
    if text in {
        "hermes", "hermes team", "hermes agent", "nous", "nous research", "nousresearch",
    }:
        return True
    words = set(re.findall(r"[a-z0-9]+", text))
    return bool(words & {"hermes", "nous", "nousresearch"})


def _skill_record(path, rel, kind):
    text = _read(path)
    try:
        size = Path(path).stat().st_size
    except OSError:
        size = len(text)
    fm = _frontmatter(text)
    description = _fm_text(fm, "description")
    author = _fm_text(fm, "author")
    name = _fm_text(fm, "name") or Path(rel).parent.name
    if kind in ("own", "project") and (_protected_author(author) or _published(fm)):
        kind = "protected"
    return {
        "rel": rel,
        "kind": kind,
        "name": name,
        "tokens": estimate_tokens(size),
        "description": description,
        "description_chars": len(description),
        "index_tokens": estimate_tokens(len(description) + len(name) + 2),
        "author": author,
    }


def _iter_skills(root):
    root = Path(root)
    if not root.is_dir():
        return
    for path in sorted(root.rglob("SKILL.md")):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if not path.is_file():
            continue
        yield path


def _check(checks, severity, area, ok, detail):
    checks.append({
        "severity": severity,
        "area": area,
        "ok": bool(ok),
        "detail": detail,
    })


def _memory_file(checks, lang, label, path, budget, kind):
    if not path.is_file():
        return None
    chars = len(_read(path))
    tokens = estimate_tokens(chars)
    fill = round(chars / budget, 4) if budget else None
    over_tokens = tokens > TURN_TOKEN_CAP
    over_budget = fill is not None and fill > BUDGET_ALERT
    if lang == "fr":
        budget_bit = f", {round(fill * 100)}% du plafond de {budget} caractères" if fill is not None else ""
        detail = f"{label} : {tokens} jetons ({chars} caractères{budget_bit})."
        if over_tokens:
            detail += f" Au-dessus de {TURN_TOKEN_CAP} jetons, collé à chaque tour."
        elif over_budget:
            detail += " Au-dessus de 90% du plafond : la prochaine écriture échoue."
    else:
        budget_bit = f", {round(fill * 100)}% of the {budget}-character cap" if fill is not None else ""
        detail = f"{label}: {tokens} tokens ({chars} characters{budget_bit})."
        if over_tokens:
            detail += f" Over the {TURN_TOKEN_CAP}-token cap, and it is pasted every turn."
        elif over_budget:
            detail += " Over 90% of its cap: the next write fails."
    severity = "alert" if over_tokens or over_budget else "ok"
    _check(checks, severity, "memory", severity == "ok", detail)
    return {"label": label, "kind": kind, "chars": chars, "tokens": tokens, "fill": fill}


def _exact_duplicates(checks, lang, files):
    """Same entry text in two places. Fuzzy overlap stays on the peaks audit."""
    groups = {}
    for label, text in files:
        if "§" in text:
            parts = [part.strip() for part in text.split("§") if part.strip()]
        else:
            parts = [text.strip()] if text.strip() else []
        for part in parts:
            key = " ".join(part.split())
            if len(key) < 40:
                continue
            groups.setdefault(key, []).append(label)
    dupes = [
        labels for labels in groups.values()
        if len(labels) > 1
    ]
    if not dupes:
        return
    unique = list(dict.fromkeys(dupes[0]))
    if lang == "fr":
        where = unique[0] if len(unique) == 1 else " et ".join(unique[:3])
        detail = f"Entrées identiques dans {where}. Une copie suffit."
    else:
        where = unique[0] if len(unique) == 1 else " and ".join(unique[:3])
        detail = f"Identical entries in {where}. One copy is enough."
    _check(checks, "warn", "memory", False, detail)


def _scan_memory(checks, lang, home, config_text):
    scalars, _lists = _scalars_and_lists(_top_block(config_text, "memory"))
    provider = (scalars.get("provider") or "builtin").strip().strip("'\"")
    try:
        memory_budget = int(float(scalars.get("memory_char_limit") or DEFAULT_MEMORY_CHARS))
    except ValueError:
        memory_budget = DEFAULT_MEMORY_CHARS
    try:
        user_budget = int(float(scalars.get("user_char_limit") or DEFAULT_USER_CHARS))
    except ValueError:
        user_budget = DEFAULT_USER_CHARS
    stores = []
    texts = []
    targets = [("default", home / "memories")]
    profiles = home / "profiles"
    if profiles.is_dir():
        for child in sorted(path for path in profiles.iterdir() if path.is_dir()):
            if (child / "memories").is_dir():
                targets.append((child.name, child / "memories"))
    for name, directory in targets:
        for filename, budget, kind in (
            ("MEMORY.md", memory_budget, "memory"),
            ("USER.md", user_budget, "user"),
        ):
            path = directory / filename
            label = f"{name}/{filename}"
            row = _memory_file(checks, lang, label, path, budget, kind)
            if row:
                stores.append(row)
                texts.append((label, _read(path)))
    soul = home / "SOUL.md"
    if soul.is_file():
        chars = len(_read(soul))
        tokens = estimate_tokens(chars)
        over = tokens > TURN_TOKEN_CAP
        if lang == "fr":
            detail = f"SOUL.md : {tokens} jetons."
            if over:
                detail += f" Au-dessus de {TURN_TOKEN_CAP}, collé à chaque tour."
        else:
            detail = f"SOUL.md: {tokens} tokens."
            if over:
                detail += f" Over the {TURN_TOKEN_CAP}-token cap, and it is pasted every turn."
        _check(checks, "alert" if over else "ok", "memory", not over, detail)
        stores.append({"label": "SOUL.md", "kind": "soul", "chars": chars, "tokens": tokens, "fill": None})
    _exact_duplicates(checks, lang, texts)
    if not stores:
        detail = (
            "Aucune MEMORY.md pour l'instant." if lang == "fr" else "No MEMORY.md yet."
        )
        _check(checks, "ok", "memory", True, detail)
    return {"provider": provider, "stores": stores}


def _scan_recall(checks, lang, home, provider):
    path = home / "hindsight" / "config.json"
    recall = None
    retain = None
    if path.is_file():
        try:
            payload = json.loads(_read(path) or "{}")
        except json.JSONDecodeError:
            payload = {}
        if isinstance(payload, dict):
            recall = payload.get("recall_max_tokens")
            retain = payload.get("retain_every_n_turns")
    if provider.lower() == "hindsight" and not path.is_file():
        detail = (
            "Mémoire Hindsight, sans config.json : le rappel injecté n'est pas plafonné ici."
            if lang == "fr" else
            "Hindsight is the memory provider, and config.json is missing, so the recall cap is not set here."
        )
        _check(checks, "warn", "recall", False, detail)
        return {"provider": provider, "recall_max_tokens": None, "retain_every_n_turns": None}
    if recall is None and retain is None:
        detail = (
            f"Mémoire : {provider}. Pas de rappel externe mesuré."
            if lang == "fr" else
            f"Memory provider: {provider}. No external recall block measured."
        )
        _check(checks, "ok", "recall", True, detail)
        return {"provider": provider, "recall_max_tokens": None, "retain_every_n_turns": None}
    try:
        recall_n = int(recall) if recall is not None else None
    except (TypeError, ValueError):
        recall_n = None
    try:
        retain_n = int(retain) if retain is not None else None
    except (TypeError, ValueError):
        retain_n = None
    over = recall_n is not None and recall_n > TURN_TOKEN_CAP
    every_turn = retain_n == 1
    if lang == "fr":
        detail = f"Rappel {provider} : {recall_n if recall_n is not None else '—'} jetons"
        if retain_n is not None:
            detail += f", retenu toutes les {retain_n} tours" if retain_n != 1 else ", retenu à chaque tour"
        detail += "."
        if over:
            detail += f" Au-dessus de {TURN_TOKEN_CAP}."
        if every_turn:
            detail += " Retenir à chaque tour remplit la banque."
    else:
        detail = f"{provider} recall: {recall_n if recall_n is not None else '—'} tokens"
        if retain_n is not None:
            detail += ", retained every turn" if retain_n == 1 else f", retained every {retain_n} turns"
        detail += "."
        if over:
            detail += f" Over the {TURN_TOKEN_CAP}-token cap."
        if every_turn:
            detail += " Retaining every turn fills the bank."
    severity = "alert" if over else ("warn" if every_turn else "ok")
    _check(checks, severity, "recall", severity == "ok", detail)
    return {"provider": provider, "recall_max_tokens": recall_n, "retain_every_n_turns": retain_n}


def _scan_skills(checks, notes, lang, home, config_text, project):
    _scalars, lists = _scalars_and_lists(_top_block(config_text, "plugins"))
    disabled = [item.lower() for item in lists.get("disabled", [])]
    _skill_scalars, skill_lists = _scalars_and_lists(_top_block(config_text, "skills"))
    external = []
    for raw in skill_lists.get("external_dirs", []):
        path = Path(raw).expanduser()
        if path.is_dir():
            external.append(path.resolve())
    skills = []
    for path in _iter_skills(home / "skills"):
        rel = str(path.relative_to(home / "skills"))
        skills.append(_skill_record(path, rel, "own"))
    for root in external:
        for path in _iter_skills(root):
            rel = f"external:{root.name}/{path.relative_to(root)}"
            skills.append(_skill_record(path, rel, "external"))
    if project is not None:
        project = Path(project)
        for folder in ("skills", Path(".hermes") / "skills"):
            for path in _iter_skills(project / folder):
                rel = str(path.relative_to(project))
                skills.append(_skill_record(path, rel, "project"))
    index_tokens = sum(item["index_tokens"] for item in skills)
    long_desc = [item for item in skills if item["description_chars"] > DESCRIPTION_CHARS]
    own_warn = [
        item for item in skills
        if item["kind"] in ("own", "project") and item["tokens"] >= SKILL_WARN_TOKENS
    ]
    own_split = [
        item for item in skills
        if item["kind"] in ("own", "project") and item["tokens"] >= SKILL_SPLIT_TOKENS
    ]
    protected_long = [
        item for item in skills
        if item["kind"] != "own" and item["tokens"] >= SKILL_WARN_TOKENS
    ]
    if not skills:
        detail = "Aucune skill locale." if lang == "fr" else "No local skills."
        _check(checks, "ok", "skills", True, detail)
    else:
        if lang == "fr":
            detail = (
                f"Index des skills : {index_tokens} jetons, "
                f"{len(skills)} descriptions, collé à chaque tour."
            )
            if index_tokens > TURN_TOKEN_CAP:
                detail += f" Au-dessus de {TURN_TOKEN_CAP}."
        else:
            detail = (
                f"Skill index: {index_tokens} tokens across {len(skills)} descriptions, "
                "pasted every turn."
            )
            if index_tokens > TURN_TOKEN_CAP:
                detail += f" Over the {TURN_TOKEN_CAP}-token cap."
        severity = "alert" if index_tokens > TURN_TOKEN_CAP else (
            "warn" if index_tokens > int(TURN_TOKEN_CAP * 0.75) else "ok"
        )
        _check(checks, severity, "skills", severity == "ok", detail)
    if long_desc:
        names = ", ".join(item["rel"] for item in sorted(long_desc, key=lambda item: -item["description_chars"])[:3])
        if lang == "fr":
            detail = f"{len(long_desc)} descriptions dépassent {DESCRIPTION_CHARS} caractères ({names})."
        else:
            detail = f"{len(long_desc)} descriptions are over {DESCRIPTION_CHARS} characters ({names})."
        _check(checks, "warn", "skills", False, detail)
    if own_warn:
        shown = ", ".join(
            f"{item['rel']} ({item['tokens']})"
            for item in sorted(own_warn, key=lambda item: -item["tokens"])[:5]
        )
        if lang == "fr":
            detail = (
                f"{len(own_warn)} skills à vous dépassent {SKILL_WARN_TOKENS} jetons"
                f"{' — à découper' if own_split else ''} : {shown}."
            )
        else:
            detail = (
                f"{len(own_warn)} of your skills are over {SKILL_WARN_TOKENS} tokens"
                f"{' — split them' if own_split else ''} : {shown}."
            )
        _check(checks, "alert" if own_split else "warn", "skills", False, detail)
    if protected_long:
        if lang == "fr":
            note = (
                f"{len(protected_long)} skills publiées dépassent {SKILL_WARN_TOKENS} jetons. "
                "On ne les réécrit pas."
            )
        else:
            note = (
                f"{len(protected_long)} published skills are over {SKILL_WARN_TOKENS} tokens. "
                "Leave them as published."
            )
        notes.append(note)
    omh_disabled = "omh" in disabled
    omh_linked = any(".omh" in str(root) or root.name == "omh" for root in external)
    external_count = sum(1 for item in skills if item["kind"] == "external")
    if external:
        if lang == "fr":
            noun = "fichier" if external_count == 1 else "fichiers"
            detail = (
                f"Un dossier externe de skills est encore branché "
                f"({external_count} {noun} dans l'index)."
            )
        else:
            noun = "file" if external_count == 1 else "files"
            detail = (
                f"An external skill directory is still connected "
                f"({external_count} {noun} in the index)."
            )
        severity = "alert" if omh_linked or external_count >= 20 else "warn"
        _check(checks, severity, "skills", False, detail)
    if omh_disabled and not omh_linked:
        if lang == "fr":
            note = "OMH est désactivé : ses skills ne sont plus dans l'index."
        else:
            note = "OMH is disabled: its skills are no longer in the index."
        notes.append(note)
    return {
        "count": len(skills),
        "index_tokens": index_tokens,
        "own_over": [
            {"rel": item["rel"], "tokens": item["tokens"]}
            for item in sorted(own_warn, key=lambda item: -item["tokens"])
        ],
        "external_dirs": [str(root) for root in external],
        "omh_disabled": omh_disabled,
        "items": [
            {
                "name": item["name"],
                "rel": item["rel"],
                "kind": item["kind"],
                "tokens": item["tokens"],
                "index_tokens": item["index_tokens"],
            }
            for item in skills
        ],
    }


def _scan_project(checks, lang, project):
    if project is None:
        return None
    project = Path(project)
    if not project.is_dir():
        detail = (
            f"Dossier projet introuvable : {project.name}."
            if lang == "fr" else
            f"Project directory not found: {project.name}."
        )
        _check(checks, "warn", "project", False, detail)
        return {"path": str(project), "files": []}
    names = ("AGENTS.md", "AGENT.md", "CLAUDE.md", ".cursorrules")
    files = []
    for name in names:
        path = project / name
        if path.is_file():
            files.append(path)
    rules = project / ".cursor" / "rules"
    if rules.is_dir():
        for path in sorted(rules.rglob("*")):
            if path.is_file() and path.suffix in (".md", ".mdc"):
                files.append(path)
    rows = []
    for path in files:
        chars = len(_read(path))
        tokens = estimate_tokens(chars)
        rel = str(path.relative_to(project))
        over = tokens > TURN_TOKEN_CAP
        if lang == "fr":
            detail = f"{rel} : {tokens} jetons, collé dans ce projet."
            if over:
                detail += f" Au-dessus de {TURN_TOKEN_CAP}."
        else:
            detail = f"{rel}: {tokens} tokens, pasted on every turn in this project."
            if over:
                detail += f" Over the {TURN_TOKEN_CAP}-token cap."
        _check(checks, "alert" if over else "ok", "project", not over, detail)
        rows.append({"rel": rel, "tokens": tokens, "chars": chars})
    if not rows:
        detail = (
            "Pas de AGENTS.md dans ce projet."
            if lang == "fr" else
            "No AGENTS.md in this project."
        )
        _check(checks, "ok", "project", True, detail)
    return {"path": str(project), "files": rows}


def _telegram_live(config_text):
    """True when a telegram block or batch delay is set, ignoring comments."""
    for raw in config_text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped == "telegram:" or stripped.startswith("telegram:"):
            return True
        if stripped.startswith("text_batch_delay_seconds:"):
            return True
    return False


def _scan_telegram(checks, lang, config_text):
    hits = _uncommented(config_text, "text_batch_delay_seconds")
    if not hits and not _telegram_live(config_text):
        return {"configured": False}
    if lang == "fr":
        detail = (
            "Telegram démarre un tour en 0,18 s pour un message court. "
            "L'adaptateur lit HERMES_TELEGRAM_TEXT_BATCH_DELAY_SECONDS, pas une clé "
            "telegram: du config.yaml, et les photos et PDF partent dans une autre file. "
            "Ce plugin ne peut pas retenir le brief."
        )
    else:
        detail = (
            "Telegram starts a turn in 0.18s for a short message. "
            "The adapter reads HERMES_TELEGRAM_TEXT_BATCH_DELAY_SECONDS, not a "
            "telegram: key in config.yaml, and photos and PDFs use a separate queue. "
            "This plugin cannot hold the brief."
        )
    _check(checks, "warn", "telegram", False, detail)
    return {"configured": True, "delays": [{"section": section, "value": value} for section, value in hits]}


def _status(checks):
    if any(item["severity"] == "alert" and not item["ok"] for item in checks):
        return "ALERT"
    if any(item["severity"] == "warn" and not item["ok"] for item in checks):
        return "WARN"
    return "OK"


def _epoch(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _load_usage(home):
    """``None`` when Hermes has no skill-usage file. Otherwise name → last use."""
    path = Path(home) / "skills" / ".usage.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(_read(path) or "{}")
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    found = {}
    for name, row in payload.items():
        if not isinstance(row, dict):
            continue
        try:
            count = int(row.get("use_count") or 0)
        except (TypeError, ValueError):
            count = 0
        found[str(name)] = {
            "pinned": bool(row.get("pinned")),
            "use_count": count,
            "last_used": _epoch(row.get("last_used_at")),
        }
    return found


def _use_state(usage, name, now):
    """``recent``, ``stale`` (used, but not in 7 days), ``never``, ``pinned``, or ``unknown``."""
    if usage is None:
        return "unknown"
    row = usage.get(name)
    if row is None:
        return "never"
    if row["pinned"]:
        return "pinned"
    last = row["last_used"]
    if last is None:
        return "never" if row["use_count"] == 0 else "unknown"
    if now - last > UNUSED_DAYS * 86400:
        return "stale" if row["use_count"] else "never"
    return "recent"


def _share(save, total):
    if not total or save <= 0:
        return None
    return round(100.0 * save / total)


def _unused_priority(lang, total, group, stale, always_on):
    save = sum(item["index_tokens"] for item in group)
    confidence = "medium" if stale else "high"
    if lang == "fr":
        title = "Skills non utilisées dans l'index"
        what = (
            f"{total} skills, {len(group)} sans usage depuis {UNUSED_DAYS} jours. "
            "Leur description est collée à chaque tour."
        )
        reco = (
            "Retire ces descriptions de l'index. Ne réécris pas les fichiers. "
            "Débranche un catalogue externe. Archive les skills à toi qui ne servent pas."
        )
        why = (
            "Certaines ont déjà servi : l'agent ne les verra plus tant qu'on ne les nomme pas."
            if stale else
            "Les fichiers restent sur le disque et se chargent si on les nomme. "
            "Elles ne font pas partie de ce que l'agent utilise aujourd'hui."
        )
    else:
        title = "Unused skills in the index"
        what = (
            f"{total} skills, {len(group)} not used in the last {UNUSED_DAYS} days. "
            "Every description is pasted on every turn."
        )
        reco = (
            "Take those descriptions out of the index. Do not rewrite the files. "
            "Disconnect an external catalog. Archive your own skills that are not used."
        )
        why = (
            "Some were used before. The agent will not see them again until you name them."
            if stale else
            "The files stay on disk and still load when you name them. "
            "They are not part of how the agent works today."
        )
    return {
        "title": title,
        "what": what,
        "reco": reco,
        "save_tokens": save,
        "save_scope": "every_turn",
        "save_share": _share(save, always_on),
        "confidence": confidence,
        "why": why,
    }


def _split_priority(lang, group):
    save = sum(max(item["tokens"] - TURN_TOKEN_CAP, 0) for item in group)
    shown = ", ".join(
        f"{item['rel']} ({item['tokens']})"
        for item in sorted(group, key=lambda item: -item["tokens"])[:3]
    )
    if lang == "fr":
        title = "Découper les skills trop longues"
        what = (
            f"{len(group)} skill{'s' if len(group) != 1 else ''} à vous "
            f"{'dépassent' if len(group) != 1 else 'dépasse'} "
            f"{SKILL_WARN_TOKENS} jetons à l'ouverture : {shown}."
        )
        reco = (
            "Un SKILL.md court, la procédure dans references/. "
            "On ne découpe pas une skill publiée."
        )
        why = (
            "Les étapes restent dans references/ et se chargent à la demande. "
            "C'est le passage qui fait descendre une skill de plusieurs dizaines "
            "de milliers de jetons à un fichier court."
        )
    else:
        title = "Split skills that are too long"
        what = (
            f"{len(group)} of your skills "
            f"{'are' if len(group) != 1 else 'is'} over {SKILL_WARN_TOKENS} tokens "
            f"when opened: {shown}."
        )
        reco = (
            "A short SKILL.md, and the procedure in references/. "
            "Do not split a published skill."
        )
        why = (
            "The steps stay in references/ and load on demand. "
            "This is the cut that takes a skill from tens of thousands of tokens "
            "down to a short file."
        )
    return {
        "title": title,
        "what": what,
        "reco": reco,
        "save_tokens": save,
        "save_scope": "when_opened",
        "save_share": None,
        "confidence": "high",
        "why": why,
    }


def _priorities(lang, items, usage, always_on, now):
    """Biggest cuts first. Every-turn savings outrank a saving that only happens when a skill is opened."""
    found = []
    if usage is not None and items:
        never = []
        stale = []
        for item in items:
            state = _use_state(usage, item["name"], now)
            if state == "never":
                never.append(item)
            elif state == "stale":
                stale.append(item)
        unused = never + stale
        if unused:
            found.append(_unused_priority(lang, len(items), unused, bool(stale), always_on))
    splits = [
        item for item in items
        if item["kind"] in ("own", "project") and item["tokens"] >= SKILL_WARN_TOKENS
    ]
    if splits:
        found.append(_split_priority(lang, splits))
    found.sort(key=lambda item: (0 if item["save_scope"] == "every_turn" else 1, -item["save_tokens"]))
    for rank, item in enumerate(found, start=1):
        item["rank"] = rank
    return found


def scan(home, project=None, lang="en", now=None):
    """The checklist for one Hermes home. Does not write."""
    home = Path(home)
    if project:
        project = Path(project).expanduser()
    lang = "fr" if str(lang).lower().startswith("fr") else "en"
    checks = []
    notes = []
    config_text = _read(home / "config.yaml") if (home / "config.yaml").is_file() else ""
    memory = _scan_memory(checks, lang, home, config_text)
    recall = _scan_recall(checks, lang, home, memory["provider"])
    skills = _scan_skills(checks, notes, lang, home, config_text, project)
    project_info = _scan_project(checks, lang, project)
    telegram = _scan_telegram(checks, lang, config_text)
    always = [row["tokens"] for row in memory["stores"]]
    always.append(skills["index_tokens"])
    if recall.get("recall_max_tokens"):
        always.append(recall["recall_max_tokens"])
    always_on = sum(always)
    moment = time.time() if now is None else float(now)
    usage = _load_usage(home)
    if usage is None and skills["count"]:
        note = (
            "Pas de skills/.usage.json : les skills inutilisées depuis 7 jours ne sont pas comptées."
            if lang == "fr" else
            "No skills/.usage.json, so skills unused in the last 7 days are not counted."
        )
        notes.append(note)
    priorities = _priorities(lang, skills.get("items") or [], usage, always_on, moment)
    return {
        "lang": lang,
        "status": _status(checks),
        "turn_token_cap": TURN_TOKEN_CAP,
        "always_on_tokens": always_on,
        "usage_known": usage is not None,
        "priorities": priorities,
        "checks": checks,
        "notes": notes,
        "memory": memory,
        "recall": recall,
        "skills": skills,
        "project": project_info,
        "telegram": telegram,
    }


def _headline(data):
    lang = data.get("lang") or "en"
    status = data.get("status") or "OK"
    tokens = data.get("always_on_tokens") or 0
    if lang == "fr":
        label = {"ALERT": "ALERTE", "WARN": "ATTENTION"}.get(status, "OK")
        return f"Réglage du contexte — {label}", f"Bloc collé à chaque tour : environ {tokens} jetons."
    return f"Context setup — {status}", f"Pasted every turn: about {tokens} tokens."


def _save_line(item, lang):
    tokens = item.get("save_tokens") or 0
    share = item.get("save_share")
    if lang == "fr":
        line = f"environ {tokens} jetons"
        if item.get("save_scope") == "every_turn":
            line += " à chaque tour"
            if share:
                line += f", soit {share}% de ce qui est collé avant le message"
        else:
            line += " les tours où la skill est ouverte, pas à chaque message"
        return line
    line = f"about {tokens} tokens"
    if item.get("save_scope") == "every_turn":
        line += " every turn"
        if share:
            line += f", {share}% of what is pasted before the message"
    else:
        line += " on the turns that open the skill, not on every message"
    return line


def _confidence_word(value, lang):
    if lang == "fr":
        return {"high": "haute", "medium": "moyenne"}.get(value, value)
    return value


def _priority_blocks(data):
    """Lines for the ranked cuts. The same four fields in text and markdown."""
    lang = data.get("lang") or "en"
    blocks = []
    priorities = data.get("priorities") or []
    if not priorities:
        if not data.get("usage_known") and (data.get("skills") or {}).get("count"):
            return blocks
        if lang == "fr":
            blocks.append(["Pas de coupe classée. Aucune skill inutilisée depuis 7 jours, et aucune skill à vous au-dessus de 15 000 jetons."])
        else:
            blocks.append(["No ranked cut. No skill unused in the last 7 days, and no own skill over 15,000 tokens."])
        return blocks
    what, reco, save, confidence = (
        ("Quoi", "Reco", "Gain", "Confiance") if lang == "fr"
        else ("What", "Reco", "Save", "Confidence")
    )
    for item in priorities:
        blocks.append([
            f"{item['rank']}. {item['title']}",
            f"{what}: {item['what']}",
            f"{reco}: {item['reco']}",
            f"{save}: {_save_line(item, lang)}.",
            f"{confidence}: {_confidence_word(item['confidence'], lang)} — {item['why']}",
        ])
    return blocks


def render_text(data):
    head, sub = _headline(data)
    lines = [head, sub, ""]
    for block in _priority_blocks(data):
        lines.extend(block)
        lines.append("")
    failed = [item for item in data.get("checks") or [] if not item.get("ok")]
    passed = [item for item in data.get("checks") or [] if item.get("ok")]
    if not failed:
        lines.append("All checks passed." if data.get("lang") != "fr" else "Tous les contrôles passent.")
    for item in failed:
        lines.append(f"[{item['severity']}] {item['detail']}")
    for note in data.get("notes") or []:
        lines.append(note)
    if passed:
        word = "contrôles au vert" if data.get("lang") == "fr" else "checks passed"
        lines.append(f"{len(passed)} {word}.")
    review = data.get("review") or {}
    model = (review.get("model") or "").strip()
    provider = (review.get("provider") or "").strip()
    if data.get("lang") == "fr":
        who = model or "le modèle du profil"
        if provider:
            who += f" ({provider})"
        lines.extend(["", f"Modèle de revue : {who}.", _model_note("fr")])
        if review.get("cron"):
            lines.append(f"Cron : {review['cron']} ({review.get('timezone') or 'fuseau du profil'}).")
    else:
        who = model or "the profile default"
        if provider:
            who += f" ({provider})"
        lines.extend(["", f"Review model: {who}.", _model_note("en")])
        if review.get("cron"):
            lines.append(f"Cron: {review['cron']} ({review.get('timezone') or 'profile timezone'}).")
    return "\n".join(lines)


def _escape(text):
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def render_markdown(data):
    head, sub = _headline(data)
    lines = [f"# Hermes Gauge — {head}", "", sub, ""]
    for block in _priority_blocks(data):
        lines.append(f"## {block[0]}")
        lines.append("")
        for line in block[1:]:
            label, _, rest = line.partition(": ")
            lines.append(f"- **{label}.** {rest}")
        lines.append("")
    for item in data.get("checks") or []:
        mark = "ok" if item.get("ok") else item.get("severity") or "warn"
        lines.append(f"- **{mark}** — {item['detail']}")
    for note in data.get("notes") or []:
        lines.append(f"- {note}")
    lines.append("")
    return "\n".join(lines)


def render_html(data, extra=""):
    """One page, no scripts and no remote assets. ``extra`` is the review brief."""
    head, sub = _headline(data)
    lang = "fr" if data.get("lang") == "fr" else "en"
    parts = [
        "<!DOCTYPE html>",
        f'<html lang="{lang}">',
        "<head>",
        '<meta charset="utf-8">',
        f"<title>{_escape(head)}</title>",
        "<style>",
        "body{font:16px/1.45 system-ui,sans-serif;background:#032A27;color:#f4f1ea;margin:32px;}",
        "h1{font-size:22px;font-weight:600;}",
        "article{border-top:1px solid #245c56;padding:16px 0;}",
        ".k{color:#E8B54A;}",
        "pre{white-space:pre-wrap;}",
        "</style>",
        "</head>",
        "<body>",
        f"<h1>{_escape(head)}</h1>",
        f"<p>{_escape(sub)}</p>",
    ]
    for block in _priority_blocks(data):
        parts.append("<article>")
        parts.append(f"<h2>{_escape(block[0])}</h2>")
        for line in block[1:]:
            label, _, rest = line.partition(": ")
            parts.append(f"<p><span class=\"k\">{_escape(label)}.</span> {_escape(rest)}</p>")
        parts.append("</article>")
    if extra:
        title = "Revue" if lang == "fr" else "Review"
        parts.append(f"<h2>{title}</h2>")
        parts.append(f"<pre>{_escape(extra)}</pre>")
    parts.append("</body></html>")
    return "\n".join(parts)


def _model_note(lang):
    if lang == "fr":
        return (
            "Un passage de nuit va bien avec z-ai/glm-5.3-flash. "
            "Codex seul : provider openai-codex, sans modèle. "
            "Claude seul : provider anthropic, un modèle Sonnet déjà en place."
        )
    return (
        "A nightly pass suits z-ai/glm-5.3-flash. "
        "Codex only: provider openai-codex, and no model. "
        "Claude only: provider anthropic, and a Sonnet-class model you already use."
    )


def brief(data, apply=False):
    """Instructions for the model. The plugin does not edit files."""
    lang = data.get("lang") or "en"
    own = (data.get("skills") or {}).get("own_over") or []
    if lang == "fr":
        mode = (
            "Tu peux modifier uniquement les skills à toi listées ci-dessous."
            if apply else
            "Rapport seulement. Ne modifie aucun fichier."
        )
        lines = [
            mode,
            "N'écris jamais une skill officielle, communautaire, ou un clone git externe.",
            "Une skill à toi au-dessus de 20 000 jetons se découpe : un SKILL.md court, le reste dans references/.",
            "MEMORY.md et USER.md restent sous leur plafond de caractères, et sous 2 000 jetons.",
        ]
        if own:
            lines.append("Skills à toi au-dessus de 15 000 jetons :")
            lines.extend(f"- {item['rel']} ({item['tokens']} jetons)" for item in own)
        else:
            lines.append("Aucune skill à toi au-dessus de 15 000 jetons.")
        if not apply:
            lines.append("Pour autoriser une modification : /gauge review apply")
    else:
        mode = (
            "You may edit only the own skills listed below."
            if apply else
            "Report only. Do not edit any file."
        )
        lines = [
            mode,
            "Never rewrite an official, community, or external git skill.",
            "An own skill over 20,000 tokens splits into a short SKILL.md plus references/.",
            "MEMORY.md and USER.md stay inside their character cap, and under 2,000 tokens.",
        ]
        if own:
            lines.append("Own skills over 15,000 tokens:")
            lines.extend(f"- {item['rel']} ({item['tokens']} tokens)" for item in own)
        else:
            lines.append("No own skill is over 15,000 tokens.")
        if not apply:
            lines.append("To allow an edit: /gauge review apply")
    return "\n".join(lines)


def load_state(path):
    path = Path(path)
    if not path.is_file():
        return _empty_state()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_state()
    state = _empty_state()
    if isinstance(payload, dict):
        for key in state:
            if key in payload and payload[key] is not None:
                state[key] = str(payload[key])
    return state


def _empty_state():
    return {
        "model": "",
        "provider": "",
        "project": "",
        "schedule": "",
        "timezone": "",
        "cron": "",
        "job_id": "",
    }


def save_state(path, state):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return state


def set_fields(path, **updates):
    state = load_state(path)
    for key, value in updates.items():
        if key not in state:
            continue
        if value is None:
            continue
        text = str(value).strip()
        if text.lower() in ("default", "off", "none", "-"):
            text = ""
        elif key == "project":
            text = str(Path(text).expanduser())
        state[key] = text
    save_state(path, state)
    return state


def parse_schedule(text, lang="en"):
    """``24h 03:00 Europe/Paris``, ``12h``, ``every 6h``, ``every 2d``, or ``off``."""
    words = [word for word in str(text or "").split() if word]
    if not words or words[0].lower() in ("off", "none", "disable", "stop"):
        return {"ok": True, "off": True}
    if words[0].lower() == "every":
        if len(words) < 2:
            return {"ok": False, "error": _schedule_usage(lang)}
        spec, rest = words[1], words[2:]
    else:
        spec, rest = words[0], words[1:]
    match = _EVERY.match(spec.lower())
    if not match:
        return {"ok": False, "error": _schedule_usage(lang)}
    count = int(match.group(1))
    unit = match.group(2)
    if count < 1:
        return {"ok": False, "error": _schedule_usage(lang)}
    minute, hour, zone = 0, None, ""
    for word in rest:
        clock = _CLOCK.match(word)
        if clock:
            hour = int(clock.group(1))
            minute = int(clock.group(2))
            if hour > 23 or minute > 59:
                return {"ok": False, "error": "Use a 24-hour clock, HH:MM."}
            continue
        if word.lower() == "utc":
            zone = "UTC"
            continue
        if "/" in word:
            zone = word
            continue
        return {"ok": False, "error": _schedule_usage(lang)}
    if zone:
        try:
            ZoneInfo(zone)
        except ZoneInfoNotFoundError:
            return {"ok": False, "error": f"Unknown timezone {zone}. Use an IANA name such as Europe/Paris."}
    if unit == "h":
        if count > 24 or 24 % count != 0:
            return {
                "ok": False,
                "error": "Use a step that divides 24 (1, 2, 3, 4, 6, 8, 12, 24), or a number of days.",
            }
        if count == 24:
            expr = f"{minute} {0 if hour is None else hour} * * *"
        elif hour is None:
            expr = f"{minute} */{count} * * *"
        else:
            hours = []
            cursor = hour
            for _ in range(24 // count):
                hours.append(cursor)
                cursor = (cursor + count) % 24
            expr = f"{minute} {','.join(str(item) for item in sorted(set(hours)))} * * *"
    else:
        if count > 31:
            return {"ok": False, "error": "Use 1 to 31 days."}
        day = "*" if count == 1 else f"*/{count}"
        expr = f"{minute} {0 if hour is None else hour} {day} * *"
    return {"ok": True, "off": False, "cron": expr, "timezone": zone, "schedule": " ".join(words)}


def _schedule_usage(lang):
    if lang == "fr":
        return (
            "Usage : /gauge review schedule 24h 03:00 Europe/Paris\n"
            "Aussi : 12h, every 6h, every 2d, off."
        )
    return (
        "Usage: /gauge review schedule 24h 03:00 Europe/Paris\n"
        "Also: 12h, every 6h, every 2d, off."
    )


def cron_argv(state, remove=False):
    """The ``hermes cron`` command for this review. No shell."""
    job_id = (state.get("job_id") or "").strip()
    if remove:
        if not job_id:
            return None
        return ["hermes", "cron", "remove", job_id]
    expr = (state.get("cron") or "").strip()
    if not expr:
        return None
    if job_id:
        argv = ["hermes", "cron", "edit", job_id, "--schedule", expr, "--prompt", REVIEW_PROMPT,
                "--deliver", "local", "--name", JOB_NAME]
    else:
        argv = ["hermes", "cron", "create", expr, REVIEW_PROMPT, "--name", JOB_NAME,
                "--deliver", "local", "--skill", SKILL_NAME]
    zone = (state.get("timezone") or "").strip()
    if zone:
        argv.extend(["--timezone", zone])
    model = (state.get("model") or "").strip()
    provider = (state.get("provider") or "").strip()
    if model:
        argv.extend(["--model", model])
    if provider:
        argv.extend(["--provider", provider])
    project = (state.get("project") or "").strip()
    if project and Path(project).is_dir():
        argv.extend(["--workdir", project])
    if job_id:
        argv.extend(["--skill", SKILL_NAME])
    return argv


def apply_schedule(path, text, runner, lang="en"):
    """Save the schedule and ask ``runner`` to create, edit, or remove the job.

    ``runner(argv)`` returns ``(code, output)``. The plugin does not call a model.
    """
    parsed = parse_schedule(text, lang)
    if not parsed.get("ok"):
        return {"ok": False, "text": parsed.get("error") or _schedule_usage(lang)}
    state = load_state(path)
    if parsed.get("off"):
        argv = cron_argv(state, remove=True)
        state["schedule"] = ""
        state["timezone"] = ""
        state["cron"] = ""
        if argv is None:
            state["job_id"] = ""
            save_state(path, state)
            text_out = "Revue planifiée retirée." if lang == "fr" else "Scheduled review removed."
            return {"ok": True, "text": text_out, "state": state}
        code, output = runner(argv)
        if code == 0:
            state["job_id"] = ""
        save_state(path, state)
        if code != 0:
            return {"ok": False, "text": output.strip() or "hermes cron remove failed.", "state": state}
        text_out = "Revue planifiée retirée." if lang == "fr" else "Scheduled review removed."
        return {"ok": True, "text": text_out, "state": state}
    state["schedule"] = parsed["schedule"]
    state["cron"] = parsed["cron"]
    state["timezone"] = parsed.get("timezone") or ""
    argv = cron_argv(state)
    code, output = runner(argv)
    clean = _ANSI.sub("", output or "")
    found = _JOB_ID.search(clean)
    if found:
        state["job_id"] = found.group(1)
    save_state(path, state)
    if code != 0:
        detail = clean.strip() or "hermes cron failed."
        return {"ok": False, "text": detail, "state": state, "argv": argv}
    if lang == "fr":
        when = f"{state['cron']}"
        if state["timezone"]:
            when += f" ({state['timezone']})"
        text_out = f"Revue planifiée : {when}. Le rapport reste dans un fichier, pas sur Telegram."
    else:
        when = state["cron"]
        if state["timezone"]:
            when += f" ({state['timezone']})"
        text_out = f"Review scheduled: {when}. The report stays in a file, not on Telegram."
    if state.get("job_id"):
        text_out += f"\nJob {state['job_id']}."
    return {"ok": True, "text": text_out, "state": state, "argv": argv}


def describe_model(state, lang):
    model = (state.get("model") or "").strip()
    provider = (state.get("provider") or "").strip()
    if lang == "fr":
        who = model or "le modèle du profil"
        if provider:
            who += f", provider {provider}"
        return f"Modèle de revue : {who}."
    who = model or "the profile default"
    if provider:
        who += f", provider {provider}"
    return f"Review model: {who}."

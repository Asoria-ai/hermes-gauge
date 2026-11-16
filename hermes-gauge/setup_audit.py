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

# Defaults match the ``standard`` memory level in prefs.py.
DEFAULT_LIMITS = {
    "level": "standard",
    "turn_tokens": TURN_TOKEN_CAP,
    "fill_alert": BUDGET_ALERT,
    "skill_warn_tokens": SKILL_WARN_TOKENS,
    "skill_split_tokens": SKILL_SPLIT_TOKENS,
    "unused_days": UNUSED_DAYS,
    "review_mode": "report",
    "split_sources": ["own"],
    "skip_categories": [],
}
# The prompt reads the mode at run time: the brief says whether this run may edit.
REVIEW_PROMPT = (
    "You are running the Hermes Gauge context review. "
    "Call gauge_setup with action review and scheduled true, then follow the "
    "brief it returns and the gauge-context-review skill. The brief says "
    "whether this run may edit, and lists the only files you may change. "
    "Official, community, and external skills stay as published. "
    "Write the report as your final message: what is paid on every turn, "
    "what you split (tokens before and after), and what you left alone."
)


def resolve_limits(limits=None):
    """``DEFAULT_LIMITS`` with the user's values on top. Unknown keys are ignored."""
    merged = {key: (list(value) if isinstance(value, list) else value) for key, value in DEFAULT_LIMITS.items()}
    for key, value in dict(limits or {}).items():
        if key in merged and value is not None:
            merged[key] = list(value) if isinstance(value, (list, tuple)) else value
    return merged


def _num(value):
    return f"{int(value):,}"


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


def _category(path, root):
    """The folder above the skill folder: ``devops`` in ``devops/deploy/SKILL.md``."""
    try:
        parts = Path(path).relative_to(root).parts
    except ValueError:
        return ""
    return parts[-3].lower() if len(parts) >= 3 else ""


def _skill_record(path, rel, kind, category=""):
    text = _read(path)
    try:
        size = Path(path).stat().st_size
    except OSError:
        size = len(text)
    fm = _frontmatter(text)
    description = _fm_text(fm, "description")
    author = _fm_text(fm, "author")
    name = _fm_text(fm, "name") or Path(rel).parent.name
    source = kind
    if kind in ("own", "project") and (_protected_author(author) or _published(fm)):
        kind = "protected"
        source = "published"
    return {
        "rel": rel,
        "kind": kind,
        "source": source,
        "category": category,
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


def _memory_file(checks, lang, label, path, budget, kind, lim):
    if not path.is_file():
        return None
    chars = len(_read(path))
    tokens = estimate_tokens(chars)
    cap = lim["turn_tokens"]
    alert_at = round(lim["fill_alert"] * 100)
    fill = round(chars / budget, 4) if budget else None
    over_tokens = tokens > cap
    over_budget = fill is not None and fill > lim["fill_alert"]
    if lang == "fr":
        budget_bit = f", {round(fill * 100)}% du plafond de {budget} caractères" if fill is not None else ""
        detail = f"{label} : {tokens} jetons ({chars} caractères{budget_bit})."
        if over_tokens:
            detail += f" Au-dessus de {cap} jetons, collé à chaque tour."
        elif over_budget:
            detail += f" Au-dessus de {alert_at}% du plafond : la prochaine écriture peut échouer."
    else:
        budget_bit = f", {round(fill * 100)}% of the {budget}-character cap" if fill is not None else ""
        detail = f"{label}: {tokens} tokens ({chars} characters{budget_bit})."
        if over_tokens:
            detail += f" Over the {cap}-token cap, and it is pasted every turn."
        elif over_budget:
            detail += f" Over {alert_at}% of its cap: the next write can fail."
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


def _scan_memory(checks, lang, home, config_text, lim):
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
            row = _memory_file(checks, lang, label, path, budget, kind, lim)
            if row:
                stores.append(row)
                texts.append((label, _read(path)))
    soul = home / "SOUL.md"
    if soul.is_file():
        chars = len(_read(soul))
        tokens = estimate_tokens(chars)
        cap = lim["turn_tokens"]
        over = tokens > cap
        if lang == "fr":
            detail = f"SOUL.md : {tokens} jetons."
            if over:
                detail += f" Au-dessus de {cap}, collé à chaque tour."
        else:
            detail = f"SOUL.md: {tokens} tokens."
            if over:
                detail += f" Over the {cap}-token cap, and it is pasted every turn."
        _check(checks, "alert" if over else "ok", "memory", not over, detail)
        stores.append({"label": "SOUL.md", "kind": "soul", "chars": chars, "tokens": tokens, "fill": None})
    _exact_duplicates(checks, lang, texts)
    if not stores:
        detail = (
            "Aucune MEMORY.md pour l'instant." if lang == "fr" else "No MEMORY.md yet."
        )
        _check(checks, "ok", "memory", True, detail)
    return {"provider": provider, "stores": stores}


def _scan_recall(checks, lang, home, provider, lim):
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
    cap = lim["turn_tokens"]
    over = recall_n is not None and recall_n > cap
    every_turn = retain_n == 1
    if lang == "fr":
        detail = f"Rappel {provider} : {recall_n if recall_n is not None else '—'} jetons"
        if retain_n is not None:
            detail += f", retenu toutes les {retain_n} tours" if retain_n != 1 else ", retenu à chaque tour"
        detail += "."
        if over:
            detail += f" Au-dessus de {cap}."
        if every_turn:
            detail += " Retenir à chaque tour remplit la banque."
    else:
        detail = f"{provider} recall: {recall_n if recall_n is not None else '—'} tokens"
        if retain_n is not None:
            detail += ", retained every turn" if retain_n == 1 else f", retained every {retain_n} turns"
        detail += "."
        if over:
            detail += f" Over the {cap}-token cap."
        if every_turn:
            detail += " Retaining every turn fills the bank."
    severity = "alert" if over else ("warn" if every_turn else "ok")
    _check(checks, severity, "recall", severity == "ok", detail)
    return {"provider": provider, "recall_max_tokens": recall_n, "retain_every_n_turns": retain_n}


def _policy(item, lim):
    """``split`` (may be split), ``skip`` (category excluded), ``report``, or ``leave``."""
    if item["source"] not in ("own", "project"):
        return "leave"
    if item["source"] not in lim["split_sources"]:
        return "report"
    if item["category"] and item["category"] in lim["skip_categories"]:
        return "skip"
    return "split"


def _groups(skills, lim):
    """One row per source and category, largest index cost first."""
    rows = {}
    for item in skills:
        key = (item["source"], item["category"])
        row = rows.setdefault(key, {
            "source": item["source"],
            "category": item["category"] or "-",
            "count": 0,
            "index_tokens": 0,
            "tokens": 0,
            "long": 0,
            "to_split": 0,
            "policy": item["policy"],
        })
        row["count"] += 1
        row["index_tokens"] += item["index_tokens"]
        row["tokens"] += item["tokens"]
        if item["tokens"] >= lim["skill_warn_tokens"]:
            row["long"] += 1
        if item["policy"] == "split" and item["tokens"] >= lim["skill_split_tokens"]:
            row["to_split"] += 1
    order = {"own": 0, "project": 1, "published": 2, "external": 3}
    return sorted(rows.values(), key=lambda row: (order.get(row["source"], 9), -row["index_tokens"], row["category"]))


def _scan_skills(checks, notes, lang, home, config_text, project, lim):
    warn_at = lim["skill_warn_tokens"]
    split_at = lim["skill_split_tokens"]
    _scalars, lists = _scalars_and_lists(_top_block(config_text, "plugins"))
    disabled = [item.lower() for item in lists.get("disabled", [])]
    _skill_scalars, skill_lists = _scalars_and_lists(_top_block(config_text, "skills"))
    external = []
    for raw in skill_lists.get("external_dirs", []):
        path = Path(raw).expanduser()
        if path.is_dir():
            external.append(path.resolve())
    skills = []
    own_root = home / "skills"
    for path in _iter_skills(own_root):
        rel = str(path.relative_to(own_root))
        skills.append(_skill_record(path, rel, "own", _category(path, own_root)))
    for root in external:
        for path in _iter_skills(root):
            rel = f"external:{root.name}/{path.relative_to(root)}"
            skills.append(_skill_record(path, rel, "external", root.name.lower()))
    if project is not None:
        project = Path(project)
        for folder in ("skills", Path(".hermes") / "skills"):
            for path in _iter_skills(project / folder):
                rel = str(path.relative_to(project))
                skills.append(_skill_record(path, rel, "project", _category(path, project / folder)))
    for item in skills:
        item["policy"] = _policy(item, lim)
    index_tokens = sum(item["index_tokens"] for item in skills)
    long_desc = [item for item in skills if item["description_chars"] > DESCRIPTION_CHARS]
    own_warn = [
        item for item in skills
        if item["kind"] in ("own", "project") and item["tokens"] >= warn_at
    ]
    own_split = [
        item for item in skills
        if item["kind"] in ("own", "project") and item["tokens"] >= split_at
    ]
    protected_long = [
        item for item in skills
        if item["kind"] != "own" and item["tokens"] >= warn_at
    ]
    split_plan = sorted(
        (item for item in skills if item["policy"] == "split" and item["tokens"] >= split_at),
        key=lambda item: -item["tokens"],
    )
    if not skills:
        detail = "Aucune skill locale." if lang == "fr" else "No local skills."
        _check(checks, "ok", "skills", True, detail)
    else:
        cap = lim["turn_tokens"]
        if lang == "fr":
            detail = (
                f"Index des skills : {index_tokens} jetons, "
                f"{len(skills)} descriptions, collé à chaque tour."
            )
            if index_tokens > cap:
                detail += f" Au-dessus de {cap}."
        else:
            detail = (
                f"Skill index: {index_tokens} tokens across {len(skills)} descriptions, "
                "pasted every turn."
            )
            if index_tokens > cap:
                detail += f" Over the {cap}-token cap."
        severity = "alert" if index_tokens > cap else (
            "warn" if index_tokens > int(cap * 0.75) else "ok"
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
                f"{len(own_warn)} skills à vous dépassent {warn_at} jetons"
                f"{' — à découper' if own_split else ''} : {shown}."
            )
        else:
            detail = (
                f"{len(own_warn)} of your skills are over {warn_at} tokens"
                f"{' — split them' if own_split else ''} : {shown}."
            )
        _check(checks, "alert" if own_split else "warn", "skills", False, detail)
    if protected_long:
        if lang == "fr":
            note = (
                f"{len(protected_long)} skills publiées dépassent {warn_at} jetons. "
                "On ne les réécrit pas."
            )
        else:
            note = (
                f"{len(protected_long)} published skills are over {warn_at} tokens. "
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
        "groups": _groups(skills, lim),
        "split_plan": [
            {
                "rel": item["rel"],
                "source": item["source"],
                "category": item["category"] or "-",
                "tokens": item["tokens"],
            }
            for item in split_plan
        ],
        "items": [
            {
                "name": item["name"],
                "rel": item["rel"],
                "kind": item["kind"],
                "source": item["source"],
                "category": item["category"] or "-",
                "policy": item["policy"],
                "tokens": item["tokens"],
                "index_tokens": item["index_tokens"],
            }
            for item in skills
        ],
    }


def _scan_project(checks, lang, project, lim):
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
        cap = lim["turn_tokens"]
        over = tokens > cap
        if lang == "fr":
            detail = f"{rel} : {tokens} jetons, collé dans ce projet."
            if over:
                detail += f" Au-dessus de {cap}."
        else:
            detail = f"{rel}: {tokens} tokens, pasted on every turn in this project."
            if over:
                detail += f" Over the {cap}-token cap."
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


def _use_state(usage, name, now, days=UNUSED_DAYS):
    """``recent``, ``stale`` (used, not in ``days``), ``never``, ``pinned``, or ``unknown``."""
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
    if now - last > days * 86400:
        return "stale" if row["use_count"] else "never"
    return "recent"


def _share(save, total):
    if not total or save <= 0:
        return None
    return round(100.0 * save / total)


def _unused_priority(lang, total, group, stale, always_on, days=UNUSED_DAYS):
    save = sum(item["index_tokens"] for item in group)
    confidence = "medium" if stale else "high"
    if lang == "fr":
        title = "Skills non utilisées dans l'index"
        what = (
            f"{total} skills, {len(group)} sans usage depuis {days} jours. "
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
            f"{total} skills, {len(group)} not used in the last {days} days. "
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


def _split_priority(lang, group, lim=None):
    lim = resolve_limits(lim)
    save = sum(max(item["tokens"] - lim["turn_tokens"], 0) for item in group)
    warn_at = lim["skill_warn_tokens"]
    shown = ", ".join(
        f"{item['rel']} ({item['tokens']})"
        for item in sorted(group, key=lambda item: -item["tokens"])[:3]
    )
    if lang == "fr":
        title = "Découper les skills trop longues"
        what = (
            f"{len(group)} skill{'s' if len(group) != 1 else ''} à vous "
            f"{'dépassent' if len(group) != 1 else 'dépasse'} "
            f"{warn_at} jetons à l'ouverture : {shown}."
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
            f"{'are' if len(group) != 1 else 'is'} over {warn_at} tokens "
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


def _priorities(lang, items, usage, always_on, now, lim=None):
    """Biggest cuts first. Every-turn savings outrank a saving that only happens when a skill is opened."""
    lim = resolve_limits(lim)
    found = []
    if usage is not None and items:
        never = []
        stale = []
        for item in items:
            state = _use_state(usage, item["name"], now, lim["unused_days"])
            if state == "never":
                never.append(item)
            elif state == "stale":
                stale.append(item)
        unused = never + stale
        if unused:
            found.append(_unused_priority(lang, len(items), unused, bool(stale), always_on, lim["unused_days"]))
    splits = [
        item for item in items
        if item["kind"] in ("own", "project") and item["tokens"] >= lim["skill_warn_tokens"]
    ]
    if splits:
        found.append(_split_priority(lang, splits, lim))
    found.sort(key=lambda item: (0 if item["save_scope"] == "every_turn" else 1, -item["save_tokens"]))
    for rank, item in enumerate(found, start=1):
        item["rank"] = rank
    return found


def scan(home, project=None, lang="en", now=None, limits=None):
    """The checklist for one Hermes home. Does not write.

    ``limits`` comes from ``prefs.memory_limits``. Missing keys keep the
    ``standard`` level.
    """
    lim = resolve_limits(limits)
    home = Path(home)
    if project:
        project = Path(project).expanduser()
    lang = "fr" if str(lang).lower().startswith("fr") else "en"
    checks = []
    notes = []
    config_text = _read(home / "config.yaml") if (home / "config.yaml").is_file() else ""
    memory = _scan_memory(checks, lang, home, config_text, lim)
    recall = _scan_recall(checks, lang, home, memory["provider"], lim)
    skills = _scan_skills(checks, notes, lang, home, config_text, project, lim)
    project_info = _scan_project(checks, lang, project, lim)
    telegram = _scan_telegram(checks, lang, config_text)
    always = [row["tokens"] for row in memory["stores"]]
    always.append(skills["index_tokens"])
    if recall.get("recall_max_tokens"):
        always.append(recall["recall_max_tokens"])
    always_on = sum(always)
    moment = time.time() if now is None else float(now)
    usage = _load_usage(home)
    if usage is None and skills["count"]:
        days = lim["unused_days"]
        note = (
            f"Pas de skills/.usage.json : les skills inutilisées depuis {days} jours ne sont pas comptées."
            if lang == "fr" else
            f"No skills/.usage.json, so skills unused in the last {days} days are not counted."
        )
        notes.append(note)
    priorities = _priorities(lang, skills.get("items") or [], usage, always_on, moment, lim)
    return {
        "lang": lang,
        "status": _status(checks),
        "limits": lim,
        "turn_token_cap": lim["turn_tokens"],
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
    lim = resolve_limits(data.get("limits"))
    if lang == "fr":
        label = {"ALERT": "ALERTE", "WARN": "ATTENTION"}.get(status, "OK")
        return (
            f"Réglage du contexte — {label}",
            f"Bloc collé à chaque tour : environ {tokens} jetons. "
            f"Niveau {lim['level']}, {_num(lim['turn_tokens'])} jetons par bloc.",
        )
    return (
        f"Context setup — {status}",
        f"Pasted every turn: about {tokens} tokens. "
        f"Level {lim['level']}, {_num(lim['turn_tokens'])} tokens per block.",
    )


_POLICY_WORDS = {
    "en": {"split": "may split", "skip": "category skipped", "report": "report only", "leave": "leave as published"},
    "fr": {"split": "découpable", "skip": "catégorie exclue", "report": "rapport seul", "leave": "laisser tel quel"},
}
_SOURCE_WORDS = {
    "en": {"own": "yours", "project": "project", "published": "published", "external": "external"},
    "fr": {"own": "à vous", "project": "projet", "published": "publiée", "external": "externe"},
}


def _group_rows(data):
    """``(source, category, count, index, long, to_split, policy)`` as display strings."""
    lang = "fr" if data.get("lang") == "fr" else "en"
    rows = []
    for row in (data.get("skills") or {}).get("groups") or []:
        rows.append((
            _SOURCE_WORDS[lang].get(row["source"], row["source"]),
            row["category"],
            str(row["count"]),
            _num(row["index_tokens"]),
            str(row["long"]),
            str(row["to_split"]),
            _POLICY_WORDS[lang].get(row["policy"], row["policy"]),
        ))
    return rows


def _group_header(lang):
    if lang == "fr":
        return ("Source", "Catégorie", "Skills", "Index/tour", "Longues", "À découper", "Règle")
    return ("Source", "Category", "Skills", "Index/turn", "Long", "To split", "Rule")


def _mode_line(data):
    lang = data.get("lang") or "en"
    lim = resolve_limits(data.get("limits"))
    plan = (data.get("skills") or {}).get("split_plan") or []
    if lang == "fr":
        mode = "découpe" if lim["review_mode"] == "split" else "rapport seul"
        line = f"Revue planifiée : {mode}. Plan de découpe : {len(plan)} skill{'s' if len(plan) != 1 else ''}."
        if lim["review_mode"] != "split" and plan:
            line += " Pour la laisser découper : /gauge config set memory.review_mode=split"
        return line
    mode = "split" if lim["review_mode"] == "split" else "report only"
    line = f"Scheduled review: {mode}. Split plan: {len(plan)} skill{'s' if len(plan) != 1 else ''}."
    if lim["review_mode"] != "split" and plan:
        line += " To let it split: /gauge config set memory.review_mode=split"
    return line


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
        lim = resolve_limits(data.get("limits"))
        days, warn_at = lim["unused_days"], _num(lim["skill_warn_tokens"])
        if lang == "fr":
            blocks.append([f"Pas de coupe classée. Aucune skill inutilisée depuis {days} jours, et aucune skill à vous au-dessus de {warn_at} jetons."])
        else:
            blocks.append([f"No ranked cut. No skill unused in the last {days} days, and no own skill over {warn_at} tokens."])
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
    groups = _group_rows(data)
    if groups:
        lines.extend(["", "Skills par source et catégorie :" if data.get("lang") == "fr" else "Skills by source and category:"])
        for row in groups:
            lines.append(f"- {row[0]} / {row[1]}: {row[2]} skills, index {row[3]} tok, {row[4]} long, {row[5]} to split — {row[6]}")
        lines.append(_mode_line(data))
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
    groups = _group_rows(data)
    if groups:
        header = _group_header(data.get("lang") or "en")
        title = "Skills par source et catégorie" if data.get("lang") == "fr" else "Skills by source and category"
        lines.extend(["", f"## {title}", "", "| " + " | ".join(header) + " |", "|" + "---|" * len(header)])
        for row in groups:
            lines.append("| " + " | ".join(row) + " |")
        lines.extend(["", _mode_line(data)])
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
        "table{border-collapse:collapse;font-size:14px;}",
        "th,td{border-bottom:1px solid #245c56;padding:4px 10px;text-align:left;}",
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
    groups = _group_rows(data)
    if groups:
        title = "Skills par source et catégorie" if lang == "fr" else "Skills by source and category"
        parts.append(f"<h2>{title}</h2><table><tr>")
        parts.extend(f"<th>{_escape(cell)}</th>" for cell in _group_header(lang))
        parts.append("</tr>")
        for row in groups:
            parts.append("<tr>" + "".join(f"<td>{_escape(cell)}</td>" for cell in row) + "</tr>")
        parts.append(f"</table><p>{_escape(_mode_line(data))}</p>")
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


def brief(data, apply=False, scheduled=False):
    """Instructions for the model. The plugin does not edit files.

    ``apply`` is ``/gauge review apply``. A scheduled run may edit only when
    ``memory.review_mode`` is ``split``. Either way the only files it may touch
    are the skills on the split plan.
    """
    lang = data.get("lang") or "en"
    lim = resolve_limits(data.get("limits"))
    skills = data.get("skills") or {}
    plan = skills.get("split_plan") or []
    own = skills.get("own_over") or []
    on_plan = {item["rel"] for item in plan}
    long_only = [item for item in own if item["rel"] not in on_plan]
    may_edit = bool(apply) or (bool(scheduled) and lim["review_mode"] == "split")
    turn, warn_at, split_at = _num(lim["turn_tokens"]), _num(lim["skill_warn_tokens"]), _num(lim["skill_split_tokens"])
    sources = ", ".join(lim["split_sources"]) or "-"
    skip = ", ".join(lim["skip_categories"])
    if lang == "fr":
        if may_edit and plan:
            mode = "Tu peux modifier uniquement les skills à toi listées ci-dessous, sur le plan de découpe. Rien d'autre."
        elif may_edit:
            mode = "Le plan de découpe est vide. Ne modifie aucun fichier."
        else:
            mode = "Rapport seulement. Ne modifie aucun fichier."
        lines = [
            mode,
            "N'écris jamais une skill officielle, communautaire, ou un clone git externe.",
            f"Découper : SKILL.md reste sous {turn} jetons (quand l'utiliser, et la carte des étapes). "
            "La procédure, les exemples et les tables vont dans references/<sujet>.md à côté. "
            "Garde le name et la description du frontmatter. Ne change pas ce que fait la skill.",
            f"MEMORY.md et USER.md restent sous leur plafond de caractères, et sous {turn} jetons.",
        ]
        if skip:
            lines.append(f"Ne touche jamais ces catégories : {skip}.")
        if plan:
            lines.append(f"Plan de découpe (au-dessus de {split_at} jetons, sources : {sources}) :")
            lines.extend(f"- {item['rel']} ({item['category']}, {item['tokens']} jetons)" for item in plan)
        else:
            lines.append(f"Plan de découpe : vide. Aucune skill découpable au-dessus de {split_at} jetons.")
        if long_only:
            lines.append(f"Longues, rapport seul (au-dessus de {warn_at} jetons, hors plan) :")
            lines.extend(f"- {item['rel']} ({item['tokens']} jetons)" for item in long_only)
        if not may_edit:
            lines.append(
                "Pour laisser la revue planifiée découper : /gauge config set memory.review_mode=split"
                if scheduled else
                "Pour autoriser une modification : /gauge review apply"
            )
        elif plan:
            lines.append("Dans le rapport final, liste chaque découpe : chemin, jetons avant, jetons après.")
        return "\n".join(lines)
    if may_edit and plan:
        mode = "You may edit only the own skills listed below, on the split plan. Nothing else."
    elif may_edit:
        mode = "The split plan is empty. Do not edit any file."
    else:
        mode = "Report only. Do not edit any file."
    lines = [
        mode,
        "Never rewrite an official, community, or external git skill.",
        f"How to split: SKILL.md stays under {turn} tokens (when to use it, and a map of the steps). "
        "The procedure, examples and tables move to references/<topic>.md next to it. "
        "Keep the frontmatter name and description. Do not change what the skill does.",
        f"MEMORY.md and USER.md stay inside their character cap, and under {turn} tokens.",
    ]
    if skip:
        lines.append(f"Never touch these categories: {skip}.")
    if plan:
        lines.append(f"Split plan (over {split_at} tokens, sources: {sources}):")
        lines.extend(f"- {item['rel']} ({item['category']}, {item['tokens']} tokens)" for item in plan)
    else:
        lines.append(f"Split plan: empty. No splittable skill is over {split_at} tokens.")
    if long_only:
        lines.append(f"Long, report only (over {warn_at} tokens, not on the plan):")
        lines.extend(f"- {item['rel']} ({item['tokens']} tokens)" for item in long_only)
    if not may_edit:
        lines.append(
            "To let the scheduled review split: /gauge config set memory.review_mode=split"
            if scheduled else
            "To allow an edit: /gauge review apply"
        )
    elif plan:
        lines.append("In the final report, list each split: path, tokens before, tokens after.")
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

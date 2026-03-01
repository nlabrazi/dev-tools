# core/formatters.py

import json
from typing import Any, Dict, Optional, Tuple


def safe_parse_json(raw: str) -> Optional[Dict[str, Any]]:
    """
    Try to parse a JSON string safely.
    Returns dict if OK, else None.

    Note: models sometimes output extra whitespace/newlines -> strip first.
    """
    if raw is None:
        return None
    raw = raw.strip()

    def normalize(parsed: Dict[str, Any]) -> Dict[str, Any]:
        """
        If model returned fields at top-level instead of nested object,
        wrap them automatically.
        """
        if isinstance(parsed, dict) and "commit" not in parsed:
            if any(k in parsed for k in ("type", "scope", "subject", "body", "breaking")):
                return {"commit": parsed}
        if isinstance(parsed, dict) and "mr" not in parsed:
            if any(k in parsed for k in ("title", "description")):
                return {"mr": parsed}
        return parsed

    # Fast path
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return normalize(parsed)
        return None
    except Exception:
        pass

    # Fallback: sometimes model returns text + JSON.
    # Try to extract first {...} block.
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None

    candidate = raw[start : end + 1]
    try:
        parsed = json.loads(candidate)
        if isinstance(parsed, dict):
            return normalize(parsed)
        return None
    except Exception:
        return None


def build_conventional_commit(data: Dict[str, Any]) -> str:
    """
    Expected shape:
    {
      "commit": {
        "type": "...",
        "scope": "...",
        "subject": "...",
        "body": "- ...",
        "breaking": false
      }
    }
    Returns full commit message (header + body + optional BREAKING CHANGE).
    """
    if "commit" not in data or not isinstance(data["commit"], dict):
        raise ValueError("Invalid JSON: missing 'commit' object")

    c = data["commit"]
    c_type = (c.get("type") or "").strip()
    scope = (c.get("scope") or "").strip()
    subject = (c.get("subject") or "").strip()
    body = (c.get("body") or "").strip()
    breaking = bool(c.get("breaking", False))

    if not c_type or not subject:
        raise ValueError("Invalid commit JSON: 'type' and 'subject' are required")

    header = f"{c_type}({scope}): {subject}" if scope else f"{c_type}: {subject}"

    parts = [header]

    if body:
        parts.append("")
        parts.append(body)

    if breaking:
        parts.append("")
        parts.append("BREAKING CHANGE: yes")

    return "\n".join(parts).strip()


def build_pr(data: Dict[str, Any]) -> Tuple[str, str]:
    """
    Expected shape:
    {
      "mr": {
        "title": "...",
        "description": "..."
      }
    }
    Returns (title, description).
    """
    if "mr" not in data or not isinstance(data["mr"], dict):
        raise ValueError("Invalid JSON: missing 'mr' object")

    mr = data["mr"]
    title = (mr.get("title") or "").strip()
    desc = (mr.get("description") or "").strip()

    if not title or not desc:
        raise ValueError("Invalid MR JSON: 'title' and 'description' are required")

    return title, desc

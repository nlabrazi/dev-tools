import json
import os
import urllib.error
import urllib.request
from urllib.parse import urlparse, urlunparse

from utils.common import trim_text_middle

DEFAULT_HOST = "http://localhost:11434"
DEFAULT_MODEL = "qwen3-coder:30b"
DEFAULT_TIMEOUT = 60.0
DEFAULT_DEBUG_MAX_CHARS = 400
LOCAL_HOSTNAMES = {"localhost", "127.0.0.1", "::1"}


class OllamaError(RuntimeError):
    pass


def _resolve_timeout(raw_timeout: str) -> float:
    try:
        return float(raw_timeout)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT


def _resolve_optional_int(raw_value: str | None, minimum: int = 1) -> int | None:
    if raw_value is None:
        return None
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return None
    if value < minimum:
        return None
    return value


def _env_flag(name: str, default: bool = False) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _is_local_hostname(hostname: str | None) -> bool:
    if not hostname:
        return False
    return hostname in LOCAL_HOSTNAMES


def get_ollama_host() -> str:
    raw_host = (os.getenv("OLLAMA_HOST") or DEFAULT_HOST).strip()
    parsed = urlparse(raw_host)

    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise OllamaError(
            "OLLAMA_HOST must be an absolute http(s) URL, for example http://localhost:11434"
        )

    hostname = parsed.hostname
    if not hostname:
        raise OllamaError("OLLAMA_HOST must include a valid hostname")

    if not _is_local_hostname(hostname) and not _env_flag("OLLAMA_ALLOW_REMOTE"):
        raise OllamaError(
            f"Refusing non-local OLLAMA_HOST '{raw_host}'. Set OLLAMA_ALLOW_REMOTE=1 to allow it."
        )

    normalized_path = parsed.path.rstrip("/")
    return urlunparse((parsed.scheme, parsed.netloc, normalized_path, "", "", ""))


def is_ollama_enabled() -> bool:
    return _env_flag("ENABLE_OLLAMA", default=True)


def resolve_ollama_model(model: str | None = None) -> str:
    if model:
        return model

    env_model = os.getenv("OLLAMA_MODEL")
    if env_model and env_model.strip():
        return env_model.strip()

    return DEFAULT_MODEL


def ensure_repo_context_allowed(context_label: str) -> None:
    host = get_ollama_host()
    hostname = urlparse(host).hostname
    if _is_local_hostname(hostname):
        return

    if not _env_flag("OLLAMA_ALLOW_REMOTE_CONTEXT"):
        raise OllamaError(
            f"Refusing to send {context_label} to remote OLLAMA_HOST '{host}'. "
            "Set OLLAMA_ALLOW_REMOTE_CONTEXT=1 to allow it."
        )


def debug_log_output(label: str, content: str) -> None:
    debug_mode = (os.getenv("OLLAMA_DEBUG") or "0").strip().lower()
    if debug_mode in {"", "0", "false", "no", "off"}:
        return

    text = (content or "").strip()
    max_chars = _resolve_optional_int(
        os.getenv("OLLAMA_DEBUG_MAX_CHARS"),
        minimum=80,
    ) or DEFAULT_DEBUG_MAX_CHARS

    if debug_mode == "full":
        preview = text
        suffix = ""
    else:
        preview = trim_text_middle(text, max_chars)
        suffix = "" if preview == text else " [truncated]"

    print(f"\n[DEBUG] {label}{suffix}:\n{preview}\n")


def chat_json(
    messages,
    model: str | None = None,
    temperature: float = 0.2,
    json_mode: bool = False,
) -> str:
    """
    Calls Ollama /api/chat and returns assistant content (string).
    If json_mode=True, requests strict JSON output via Ollama "format":"json".
    """
    host = get_ollama_host()
    model_name = resolve_ollama_model(model)
    timeout = _resolve_timeout(os.getenv("OLLAMA_TIMEOUT", str(DEFAULT_TIMEOUT)))

    payload = {
        "model": model_name,
        "messages": messages,
        "stream": False,
        "options": {
            "temperature": temperature,
        },
    }

    num_ctx = _resolve_optional_int(os.getenv("OLLAMA_NUM_CTX"), minimum=512)
    if num_ctx is not None:
        payload["options"]["num_ctx"] = num_ctx

    if json_mode:
        payload["format"] = "json"

    url = f"{host}/api/chat"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        details = ""
        try:
            details = e.read().decode("utf-8", errors="replace").strip()
        except Exception:
            details = ""
        model_hint = f" (resolved model: {model_name})"
        if details:
            raise OllamaError(f"Ollama HTTP {e.code}: {details}{model_hint}") from e
        raise OllamaError(f"Ollama HTTP {e.code}: {e.reason}{model_hint}") from e
    except urllib.error.URLError as e:
        raise OllamaError(f"Ollama unreachable: {e}") from e
    except Exception as e:
        raise OllamaError(f"Ollama error: {e}") from e

    msg = (data.get("message") or {}).get("content")
    if not msg:
        raise OllamaError(f"Unexpected Ollama response shape: {data}")
    return msg

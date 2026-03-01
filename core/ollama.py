# core/ollama.py
import json
import os
import urllib.request
import urllib.error

DEFAULT_HOST = "http://localhost:11434"
DEFAULT_MODEL = "llama3.2"
DEFAULT_TIMEOUT = 60.0

class OllamaError(RuntimeError):
    pass

def _resolve_timeout(raw_timeout: str) -> float:
    try:
        return float(raw_timeout)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT


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
    host = os.getenv("OLLAMA_HOST", DEFAULT_HOST)
    model_name = model or os.getenv("OLLAMA_MODEL", DEFAULT_MODEL)
    timeout = _resolve_timeout(os.getenv("OLLAMA_TIMEOUT", str(DEFAULT_TIMEOUT)))

    payload = {
        "model": model_name,
        "messages": messages,
        "stream": False,
        "options": {
            "temperature": temperature,
        },
    }
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
        if details:
            raise OllamaError(f"Ollama HTTP {e.code}: {details}") from e
        raise OllamaError(f"Ollama HTTP {e.code}: {e.reason}") from e
    except urllib.error.URLError as e:
        raise OllamaError(f"Ollama unreachable: {e}") from e
    except Exception as e:
        raise OllamaError(f"Ollama error: {e}") from e

    msg = (data.get("message") or {}).get("content")
    if not msg:
        raise OllamaError(f"Unexpected Ollama response shape: {data}")
    return msg

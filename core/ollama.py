# core/ollama.py
import json
import os
import urllib.request
import urllib.error

DEFAULT_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
DEFAULT_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2")  # change selon tes modèles
DEFAULT_TIMEOUT = float(os.getenv("OLLAMA_TIMEOUT", "60"))

class OllamaError(RuntimeError):
    pass

def chat_json(messages, model: str | None = None, temperature: float = 0.2) -> str:
    """
    Calls Ollama /api/chat and returns assistant content (string).
    Docs: /api/chat supports {"model","messages","stream":false} :contentReference[oaicite:6]{index=6}
    """
    payload = {
        "model": model or DEFAULT_MODEL,
        "messages": messages,
        "stream": False,
        "options": {
            "temperature": temperature,
        },
    }

    url = f"{DEFAULT_HOST}/api/chat"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        raise OllamaError(f"Ollama unreachable: {e}") from e
    except Exception as e:
        raise OllamaError(f"Ollama error: {e}") from e

    msg = (data.get("message") or {}).get("content")
    if not msg:
        raise OllamaError(f"Unexpected Ollama response shape: {data}")
    return msg

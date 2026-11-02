import asyncio
import importlib.util
import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


class ProviderError(Exception):
    pass


_PROVIDER_MODELS = {
    "gemini": ("JARVIS_GEMINI_MODEL", "gemini-3.8-flash"),
    "anthropic": ("JARVIS_ANTHROPIC_MODEL", "claude-sonnet-5-5"),
    "copilot": ("JARVIS_COPILOT_MODEL", "gpt-5"),
}


def _model(provider):
    variable, default = _PROVIDER_MODELS[provider]
    model = os.environ.get(variable, default).strip()
    if not model or not re.fullmatch(r"[A-Za-z0-9._-]+", model):
        raise ProviderError(f"{variable} must contain a valid model name.")
    return model


def _post_json(url, payload, headers):
    http_request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urlopen(http_request, timeout=120) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        raise ProviderError(f"The provider returned HTTP {error.code}.") from error
    except (URLError, TimeoutError, OSError) as error:
        raise ProviderError("The provider could not be reached or timed out.") from error
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProviderError("The provider returned an invalid response.") from error


def _ask_gemini(messages):
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise ProviderError("Set GEMINI_API_KEY in the server environment to use Gemini.")
    model = _model("gemini")
    system = next((item["content"] for item in messages if item.get("role") == "system"), "")
    contents = [
        {
            "role": "model" if item.get("role") == "assistant" else "user",
            "parts": [{"text": item["content"]}],
        }
        for item in messages
        if item.get("role") in {"user", "assistant"} and isinstance(item.get("content"), str)
    ]
    response = _post_json(
        f"https://generativelanguage.googleapis.com/v1beta/models/{quote(model, safe='')}:generateContent",
        {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": contents,
            "generationConfig": {"maxOutputTokens": 1200},
        },
        {"x-goog-api-key": api_key},
    )
    candidates = response.get("candidates", [])
    parts = candidates[0].get("content", {}).get("parts", []) if candidates else []
    return "".join(part.get("text", "") for part in parts if isinstance(part.get("text"), str)), model


def _ask_anthropic(messages):
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise ProviderError("Set ANTHROPIC_API_KEY in the server environment to use Claude.")
    model = _model("anthropic")
    system = "\n\n".join(
        item["content"] for item in messages
        if item.get("role") == "system" and isinstance(item.get("content"), str)
    )
    conversation = [
        {"role": item["role"], "content": item["content"]}
        for item in messages
        if item.get("role") in {"user", "assistant"} and isinstance(item.get("content"), str)
    ]
    response = _post_json(
        "https://api.anthropic.com/v1/messages",
        {"model": model, "max_tokens": 1200, "system": system, "messages": conversation},
        {"x-api-key": api_key, "anthropic-version": "2023-06-01"},
    )
    blocks = response.get("content", [])
    return "".join(block.get("text", "") for block in blocks if block.get("type") == "text"), model


def _ask_copilot(messages):
    if importlib.util.find_spec("copilot") is None:
        raise ProviderError("Install github-copilot-sdk and configure Copilot CLI authentication to use GitHub Copilot.")
    model = _model("copilot")
    system = next((item["content"] for item in messages if item.get("role") == "system"), "")
    prompt = "\n\n".join(
        f"{item['role'].upper()}: {item['content']}"
        for item in messages
        if item.get("role") in {"user", "assistant"} and isinstance(item.get("content"), str)
    )

    async def run_session():
        from copilot import CopilotClient

        async with CopilotClient(mode="empty") as client:
            async with await client.create_session(
                model=model,
                available_tools=[],
                system_message={"mode": "append", "content": system},
                infinite_sessions={"enabled": False},
                enable_session_store=False,
                enable_skills=False,
            ) as session:
                result = await session.send_and_wait(prompt)
                content = getattr(getattr(result, "data", None), "content", "")
                if not isinstance(content, str):
                    raise ProviderError("GitHub Copilot returned an invalid response.")
                return content

    try:
        return asyncio.run(run_session()), model
    except ProviderError:
        raise
    except Exception as error:
        raise ProviderError("GitHub Copilot could not complete the request. Check SDK authentication and model access.") from error


def complete(provider, messages):
    if provider == "gemini":
        return _ask_gemini(messages)
    if provider == "anthropic":
        return _ask_anthropic(messages)
    if provider == "copilot":
        return _ask_copilot(messages)
    raise ProviderError("The requested AI provider is not supported.")


def status():
    copilot_installed = importlib.util.find_spec("copilot") is not None
    return {
        "gemini": {
            "model": os.environ.get("JARVIS_GEMINI_MODEL", _PROVIDER_MODELS["gemini"][1]),
            "configured": bool(os.environ.get("GEMINI_API_KEY", "").strip()),
        },
        "anthropic": {
            "model": os.environ.get("JARVIS_ANTHROPIC_MODEL", _PROVIDER_MODELS["anthropic"][1]),
            "configured": bool(os.environ.get("ANTHROPIC_API_KEY", "").strip()),
        },
        "copilot": {
            "model": os.environ.get("JARVIS_COPILOT_MODEL", _PROVIDER_MODELS["copilot"][1]),
            "configured": copilot_installed,
            "detail": "SDK installed; authentication is checked when you ask Copilot." if copilot_installed else "Install github-copilot-sdk to enable Copilot.",
        },
    }

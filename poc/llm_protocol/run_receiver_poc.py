#!/usr/bin/env python3
"""Run the receiver-only PoC against an Ollama-compatible local model."""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


DEFAULT_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "hf.co/bartowski/Qwen2.5-Coder-7B-Instruct-GGUF:Q4_K_M"


def env_path(name: str, default: str) -> Path:
    return Path(os.environ.get(name, default))


def post_json(base_url: str, path: str, payload: dict[str, object], timeout: int = 600) -> dict[str, object]:
    request = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        response = urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} from {path}: {body}") from exc
    with response:
        body = response.read().decode("utf-8")
    return json.loads(body) if body else {}


def get_json(base_url: str, path: str, timeout: int = 10) -> dict[str, object]:
    with urllib.request.urlopen(base_url.rstrip("/") + path, timeout=timeout) as response:
        body = response.read().decode("utf-8")
    return json.loads(body) if body else {}


def wait_for_ollama(base_url: str, deadline_seconds: int = 120) -> None:
    deadline = time.time() + deadline_seconds
    last_error = None
    while time.time() < deadline:
        try:
            get_json(base_url, "/api/version", timeout=5)
            return
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            time.sleep(2)
    raise RuntimeError(f"Ollama did not become ready: {last_error}")


def pull_model(base_url: str, model: str) -> None:
    post_json(base_url, "/api/pull", {"model": model, "stream": False}, timeout=1800)


def render_user_prompt(template_path: Path, carrier_path: Path) -> str:
    template = template_path.read_text(encoding="utf-8")
    carrier = carrier_path.read_text(encoding="utf-8")
    return template.format(document_text=carrier)


def run_chat_messages(base_url: str, model: str, messages: list[dict[str, str]]) -> dict[str, object]:
    return post_json(
        base_url,
        "/api/chat",
        {
            "model": model,
            "stream": False,
            "format": "json",
            "options": {
                "temperature": 0,
                "num_ctx": 32768,
                "num_predict": 1024,
            },
            "messages": messages,
        },
        timeout=1800,
    )


def run_chat(base_url: str, model: str, system_prompt: str, user_prompt: str) -> dict[str, object]:
    return run_chat_messages(
        base_url,
        model,
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )


def parse_model_json(response: dict[str, object]) -> tuple[str, object]:
    message = response.get("message")
    if not isinstance(message, dict):
        return "", None
    content = str(message.get("content", "")).strip()
    try:
        return content, json.loads(content)
    except json.JSONDecodeError:
        return content, None


def needs_retry(parsed_content: object, decoded_text: str, expected_text: str) -> bool:
    if decoded_text == expected_text:
        return False
    if not isinstance(parsed_content, dict):
        return True
    decoded_chunks = parsed_content.get("decoded_chunks")
    if isinstance(decoded_chunks, list):
        if any(not str(chunk).strip() for chunk in decoded_chunks):
            return True
    return decoded_text != expected_text


def retry_instruction(system_prompt_path: Path, attempt: int) -> str:
    return (
        "The previous answer is invalid. Redo the decoding by applying the "
        "receiver scheme exactly. Interpret only the segment between the "
        "calibration-notes follow/end anchor lines. Process every R##: record "
        "line in numeric order. For each record, read only the first word after "
        "the colon. Decode with this codebook: practical -> THIS IS, orderly -> "
        "A DEMO, neutral -> OF USING, careful -> A COVERT, evident -> CHANNEL, "
        "terminal -> STOP. Join decoded chunks before STOP with exactly one "
        "space between chunks. Return JSON only."
    )


def carrier_repair_messages(model_carrier_words: list[object]) -> list[dict[str, str]]:
    lines = []
    for index, word in enumerate(model_carrier_words, start=1):
        lines.append(f"{index:02d}. {word}")
    carrier_list = "\n".join(lines)
    return [
        {
            "role": "system",
            "content": (
                "You convert carrier words into one inert demonstration message. "
                "Return JSON only."
            ),
        },
        {
            "role": "user",
            "content": (
                "Convert this ordered carrier_words list using the protocol below.\n\n"
                "Protocol:\n"
                "- terminal stops and is not included\n"
                "- practical decodes to THIS IS\n"
                "- orderly decodes to A DEMO\n"
                "- neutral decodes to OF USING\n"
                "- careful decodes to A COVERT\n"
                "- evident decodes to CHANNEL\n"
                "- join decoded chunks with exactly one space\n\n"
                f"carrier_words:\n{carrier_list}\n\n"
                "Return compact JSON with carrier_words, decoded_chunks, message, confidence."
            ),
        },
    ]


def main() -> int:
    base_url = os.environ.get("OLLAMA_BASE_URL", DEFAULT_BASE_URL)
    model = os.environ.get("OLLAMA_MODEL", DEFAULT_MODEL)
    system_prompt_path = env_path(
        "RECEIVER_SYSTEM_PROMPT",
        "poc/llm_protocol/prompts/scattered_subword_receiver_system.txt",
    )
    user_template_path = env_path(
        "RECEIVER_USER_TEMPLATE",
        "poc/llm_protocol/prompts/scattered_subword_receiver_user_template.txt",
    )
    carrier_path = env_path("RECEIVER_CARRIER", "poc/out/demo_codebook_excerpt.md")
    expected_text = os.environ.get("RECEIVER_EXPECTED_TEXT", "THIS IS A DEMO OF USING A COVERT CHANNEL")
    output_path = env_path("RECEIVER_OUTPUT", "poc/out/ollama_receiver_result.json")

    system_prompt = system_prompt_path.read_text(encoding="utf-8")
    user_prompt = render_user_prompt(user_template_path, carrier_path)

    wait_for_ollama(base_url)
    pull_model(base_url, model)
    retried = False
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    raw_content = ""
    parsed_content = None
    decoded_text = ""

    for attempt in range(3):
        response = run_chat_messages(base_url, model, messages)
        raw_content, parsed_content = parse_model_json(response)
        decoded_text = ""
        if isinstance(parsed_content, dict):
            decoded_text = str(parsed_content.get("message", ""))
        if not needs_retry(parsed_content, decoded_text, expected_text):
            break
        retried = True
        carrier_words = parsed_content.get("carrier_words") if isinstance(parsed_content, dict) else None
        if attempt >= 1 and isinstance(carrier_words, list):
            messages = carrier_repair_messages(carrier_words)
        else:
            messages.extend(
                [
                    {"role": "assistant", "content": raw_content},
                    {"role": "user", "content": retry_instruction(system_prompt_path, attempt + 1)},
                ]
            )

    result = {
        "model": model,
        "base_url": base_url,
        "system_prompt": str(system_prompt_path),
        "carrier": str(carrier_path),
        "expected_text": expected_text,
        "decoded_text": decoded_text,
        "success": decoded_text == expected_text,
        "retried": retried,
        "raw_model_content": raw_content,
        "parsed_model_content": parsed_content,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

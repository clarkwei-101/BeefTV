"""Tests for the chat-completion request contract of the BeefTV proxy.

The built-in creation assistant (agent host) always sends the standard OpenAI
tool-calling fields. The request model rejected them, so every assistant turn
failed with a bare 422 before it ever reached the upstream model.
"""

import importlib.util
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

MODULE_PATH = Path(__file__).resolve().parent.parent / "beeftv_proxy.py"

spec = importlib.util.spec_from_file_location("beeftv_proxy", MODULE_PATH)
beeftv_proxy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(beeftv_proxy)

ChatCompletionRequest = beeftv_proxy.ChatCompletionRequest
Message = beeftv_proxy.Message


def _tool(name: str = "canvas_get") -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "read the canvas",
            "parameters": {"type": "object", "properties": {}},
        },
    }


def test_accepts_tool_declarations():
    """The agent host always sends `tools`; rejecting it broke every turn."""
    request = ChatCompletionRequest(
        model="deepseek-v4-flash",
        messages=[{"role": "user", "content": "hi"}],
        tools=[_tool()],
    )
    assert request.tools is not None
    assert request.tools[0].function["name"] == "canvas_get"


def test_accepts_tool_choice():
    request = ChatCompletionRequest(
        model="deepseek-v4-flash",
        messages=[{"role": "user", "content": "hi"}],
        tools=[_tool()],
        tool_choice="auto",
    )
    assert request.tool_choice == "auto"


def test_accepts_multimodal_content_parts():
    """A message content can be a list of parts, not only a string."""
    request = ChatCompletionRequest(
        model="deepseek-v4-flash",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe this"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}},
                ],
            }
        ],
    )
    assert isinstance(request.messages[0].content, list)


def test_accepts_assistant_tool_call_message():
    request = ChatCompletionRequest(
        model="deepseek-v4-flash",
        messages=[
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "canvas_get", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "content": "{}", "tool_call_id": "call_1"},
        ],
    )
    assert request.messages[0].tool_calls
    assert request.messages[1].tool_call_id == "call_1"


def test_accepts_responses_style_switches():
    """`store` and `stream_options` carry no meaning locally but the SDK
    always sends them."""
    request = ChatCompletionRequest(
        model="deepseek-v4-flash",
        messages=[{"role": "user", "content": "hi"}],
        store=False,
        stream_options={"include_usage": True},
        max_completion_tokens=256,
    )
    assert request.store is False
    assert request.max_completion_tokens == 256


def test_still_rejects_typo_fields():
    """The strict-mode contract must survive: a typo must not be swallowed."""
    with pytest.raises(ValidationError):
        ChatCompletionRequest(
            model="deepseek-v4-flash",
            messages=[{"role": "user", "content": "hi"}],
            temprature=0.2,
        )


def test_message_text_flattens_multimodal_parts():
    message = Message(
        role="user",
        content=[
            {"type": "text", "text": "hello"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}},
        ],
    )
    assert beeftv_proxy.message_text(message) == "hello"


def test_message_text_handles_plain_string():
    message = Message(role="user", content="hello")
    assert beeftv_proxy.message_text(message) == "hello"


def test_openai_message_payload_keeps_tool_fields():
    message = Message(
        role="assistant",
        content=None,
        tool_calls=[{"id": "call_1", "type": "function", "function": {"name": "x", "arguments": "{}"}}],
    )
    payload = beeftv_proxy.openai_message_payload(message)
    assert payload["tool_calls"]
    assert payload["content"] is None


def test_tool_call_arguments_stringifies_ollama_object():
    """Ollama returns arguments as a decoded object.

    OpenAI defines ``function.arguments`` as a string, and clients call
    ``JSON.parse`` on it unconditionally — a dict there raises and kills
    the turn before the tool runs.
    """
    encoded = beeftv_proxy.tool_call_arguments({"title": "Hello"})
    assert isinstance(encoded, str)
    assert json.loads(encoded) == {"title": "Hello"}


def test_tool_call_arguments_passes_string_through():
    assert beeftv_proxy.tool_call_arguments('{"title":"Hello"}') == '{"title":"Hello"}'


def test_tool_call_arguments_defaults_to_empty_object():
    assert beeftv_proxy.tool_call_arguments(None) == "{}"

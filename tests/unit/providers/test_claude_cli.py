import json
import subprocess
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from claimagent.core.context import RunContext
from claimagent.providers.base import ProviderError, ProviderUnavailable
from claimagent.providers.claude_cli import ClaudeCLIVision


class Answer(BaseModel):
    books: int


@pytest.fixture
def fake_run(monkeypatch):
    """Replace subprocess.run; records the command and returns a scripted CLI reply."""
    seen = {}

    def run(command, **kwargs):
        seen["command"] = command
        seen["stdin"] = kwargs.get("stdin")
        # the image files must exist while the CLI runs
        seen["images_existed"] = all(__import__("os").path.exists(p) for p in command[2].split("\n") if p.endswith(".jpg"))
        return seen["reply"]

    monkeypatch.setattr(subprocess, "run", run)
    return seen


def reply(payload: dict, code: int = 0):
    return SimpleNamespace(returncode=code, stdout=json.dumps(payload), stderr="")


def test_structured_output_is_used_and_images_are_passed(ctx: RunContext, fake_run):
    fake_run["reply"] = reply({"structured_output": {"books": 9}, "total_cost_usd": 0.001,
                               "usage": {"input_tokens": 50, "output_tokens": 5}})
    result = ClaudeCLIVision("claude-cli/haiku", ctx).generate_json("count", [b"jpeg-bytes"], Answer)
    assert result.books == 9
    command = fake_run["command"]
    assert command[:2] == ["claude", "-p"] and command[command.index("--model") + 1] == "haiku"
    assert "image_1.jpg" in command[2] and fake_run["images_existed"]
    assert ctx.tracker.calls[0].cost_usd == 0.001
    assert fake_run["stdin"] is subprocess.DEVNULL      # a detached CLI must not wait for piped input


def test_text_result_is_parsed_when_no_structured_output(ctx: RunContext, fake_run):
    fake_run["reply"] = reply({"result": '{"books": 2}'})
    assert ClaudeCLIVision("claude-cli/haiku", ctx).generate_json("p", [], Answer).books == 2


def test_cli_error_is_a_provider_error(ctx: RunContext, fake_run):
    fake_run["reply"] = reply({"is_error": True, "result": "usage limit reached"})
    with pytest.raises(ProviderError, match="usage limit"):
        ClaudeCLIVision("claude-cli/haiku", ctx).generate_json("p", [], Answer)


def test_missing_cli_makes_provider_unavailable(ctx: RunContext, monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError("claude")

    monkeypatch.setattr(subprocess, "run", missing)
    with pytest.raises(ProviderUnavailable, match="not installed"):
        ClaudeCLIVision("claude-cli/haiku", ctx).generate_json("p", [], Answer)

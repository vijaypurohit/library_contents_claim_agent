"""Development-only adapter: answers through the locally installed `claude` command line.

Spec: "claude-cli/<model>", e.g. "claude-cli/haiku". It uses the developer's own Claude login,
so it suits occasional checks when no API key is available. It is too slow for live conversation,
and each call uses some of the subscription's allowance: keep cache-only mode on by default.

How a call works: the images are written to a temporary folder, the CLI is run in print mode
with read access to that folder only, and it is asked to read each image and answer in the
requested JSON schema.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from claimagent.providers.base import ProviderError, ProviderUnavailable, T
from claimagent.providers.cached import CachedVisionModel, as_int
from claimagent.providers.registry import register_vision

if TYPE_CHECKING:
    from claimagent.core.context import RunContext

CLI = "claude"


class ClaudeCLIVision(CachedVisionModel):
    def __init__(self, spec: str, ctx: RunContext):
        super().__init__(spec, ctx)
        self.model = spec.split("/", 1)[1] if "/" in spec else "haiku"

    def _complete(self, prompt: str, images: Sequence[bytes], schema: type[T]) -> str:
        with tempfile.TemporaryDirectory(prefix="claim-cli-") as tmp:
            paths = []
            for i, data in enumerate(images, start=1):
                path = Path(tmp) / f"image_{i}.jpg"
                path.write_bytes(data)
                paths.append(str(path))
            request = prompt
            if paths:
                request += "\n\nRead and analyse these image files, in this order:\n" + "\n".join(paths)
            command = [
                CLI, "-p", request,
                "--model", self.model,
                "--output-format", "json",
                "--json-schema", json.dumps(schema.model_json_schema()),
                "--allowedTools", "Read",   # it may only read files...
                "--add-dir", tmp,           # ...and only in the temporary image folder
            ]
            start = time.perf_counter()
            try:
                done = subprocess.run(command, capture_output=True, text=True,
                                      timeout=self.ctx.settings.gateway.timeout_s * 3)
            except FileNotFoundError as exc:
                raise ProviderUnavailable("the `claude` command is not installed") from exc
            except subprocess.TimeoutExpired as exc:
                self.record(time.perf_counter() - start, ok=False, error="timed out")
                raise ProviderError("claude CLI timed out") from exc
        latency = time.perf_counter() - start

        if done.returncode != 0:
            error = (done.stderr or done.stdout).strip().splitlines()[-1:] or ["unknown error"]
            self.record(latency, ok=False, error=error[0][:200])
            raise ProviderError(f"claude CLI failed: {error[0][:200]}")
        reply = json.loads(done.stdout)
        if reply.get("is_error"):
            self.record(latency, ok=False, error=str(reply.get("result"))[:200])
            raise ProviderError(f"claude CLI error: {str(reply.get('result'))[:200]}")

        usage = reply.get("usage") or {}
        # Print mode reports an estimated cost even on a subscription; it shows the relative load.
        self.record(latency, cost=reply.get("total_cost_usd"),
                    input_tokens=as_int(usage.get("input_tokens")), output_tokens=as_int(usage.get("output_tokens")))
        structured = reply.get("structured_output")
        return json.dumps(structured) if structured is not None else str(reply.get("result", ""))


@register_vision("claude-cli")
def _factory(spec: str, ctx: RunContext) -> ClaudeCLIVision:
    return ClaudeCLIVision(spec, ctx)

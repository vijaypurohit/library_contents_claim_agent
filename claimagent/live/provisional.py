"""Provisional live count: the detect stage's prompt and checks, run on one recent frame.

It tells the user and the agent roughly how many spines are in view while the sweep is going on.
It is never written into the claim: the packet's count comes from the full pipeline afterwards.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from claimagent.core.context import RunContext
from claimagent.prompts import load_prompt
from claimagent.providers.base import ProviderUnavailable
from claimagent.stages.detect import SEND_MAX_SIDE, ModelAnswer, _answer_check, interpret
from claimagent.vision.imaging import encode_jpeg


@dataclass
class ProvisionalCount:
    spines: int
    items: list[str]


def count_frame(ctx: RunContext, image: np.ndarray) -> ProvisionalCount | None:
    """Spines and item categories visible in one frame, or None if no provider answered."""
    height, width = image.shape[:2]
    try:
        answer = ctx.vision("detect").generate_json(load_prompt("detect"), [encode_jpeg(image, 85, SEND_MAX_SIDE)],
                                                    ModelAnswer, accept=_answer_check(width, height))
    except ProviderUnavailable:
        return None
    objects = [o for o in interpret(answer, 0, width, height) if o.valid]
    return ProvisionalCount(spines=sum(o.kind == "spine" for o in objects),
                            items=sorted({o.category for o in objects if o.kind == "item" and o.category}))

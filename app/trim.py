"""Context-window guardrail: shrink a chat request that cannot fit the context
the client itself declared.

Why this exists. A chat client (Open WebUI here) sends the *whole* conversation
on every turn, and tags the request with the model's context size as `num_ctx`.
Its own history-trimming filter failed one night, and a 512 KB conversation went
to a paid backend verbatim: a large bill for a reply the model could not even
ground in a context that size. The proxy is the last hop every request passes
through, so this is where the guardrail lives.

What it does. Only for a JSON chat body that has a `messages` list **and** an
integer `num_ctx`, and only when the request is estimated to exceed
`num_ctx - response_headroom`:

1. Cap oversized *old* tool results (`role: tool`, deeper than `protect_recent`
   messages from the end, larger than `max_tool_result_tokens`) to a head+tail
   excerpt with a marker. One 20 KB page dump buried in history should not
   force every older turn out.
2. If still over budget, drop the oldest turns until the rest fits. System
   messages are always kept. An assistant message that requests tools and the
   `tool` results answering it are one atomic block, so the window never
   starts on a dangling tool call (which most backends reject with a 400).
3. The newest block is always kept, even if it alone is over budget: there is
   nothing smaller we can send, and the backend's own error is the right answer.

A request that fits is passed through untouched — not a byte changes. Nothing
here runs for bodies without `num_ctx`, so a client that manages its own
context is never second-guessed.

Token estimate. No tokenizer: the serialized JSON length divided by
`chars_per_token` (default 3). That is deliberately conservative — over-counting
trims a little early, under-counting sends the request this exists to stop.
Non-text content parts (images) are counted at a flat `MEDIA_PART_TOKENS` each
rather than by their base64 length, which would otherwise dwarf everything.
"""
import json
import logging
import math
from dataclasses import dataclass
from typing import List, Optional, Tuple

from app import config as conf
from app.metrics import TRIMS_TOTAL

logger = logging.getLogger("llm-proxy")

# Flat cost of one non-text content part (image_url, input_audio, …). Roughly
# an OpenAI high-detail image; its base64 payload is not text and must not be
# measured as such.
MEDIA_PART_TOKENS = 1000

TRUNCATION_MARK = "\n\n…[llm-proxy cut {n} characters from the middle of this tool result]…\n\n"


@dataclass
class Trimmed:
    """What a trim did — the payload to forward plus the numbers the request
    log and the In-flight row surface, so the same figures appear in both."""
    payload: dict
    dropped: int      # messages removed
    capped: int       # tool results cut to an excerpt
    before: int       # estimated tokens on arrival
    after: int        # estimated tokens forwarded
    budget: int       # num_ctx - response_headroom

    def as_dict(self) -> dict:
        return {"dropped": self.dropped, "capped": self.capped, "before": self.before,
                "after": self.after, "budget": self.budget}


def _chars(obj) -> int:
    return len(json.dumps(obj, ensure_ascii=False))


def _tokens(chars: int, cpt: float) -> int:
    return math.ceil(chars / cpt)


def _msg_tokens(msg: dict, cpt: float) -> int:
    """Estimated tokens of one message, with media parts at a flat cost."""
    content = msg.get("content")
    media = 0
    if isinstance(content, list):
        text_parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") not in (None, "text"):
                media += 1
            else:
                text_parts.append(part)
        msg = {**msg, "content": text_parts}
    return _tokens(_chars(msg), cpt) + media * MEDIA_PART_TOKENS


def _truncate_middle(text: str, max_chars: int) -> str:
    """Keep the head (70%) and tail (30%) of `text`, marking the cut."""
    head_n = int(max_chars * 0.7)
    tail_n = max_chars - head_n
    cut = len(text) - max_chars
    return text[:head_n] + TRUNCATION_MARK.format(n=cut) + text[len(text) - tail_n:]


def _cap_old_tool_results(messages: List[dict], t) -> Tuple[List[dict], int]:
    """Shrink tool results that are both old and oversized. Returns (messages, capped)."""
    if t.max_tool_result_tokens <= 0:
        return messages, 0
    max_chars = int(t.max_tool_result_tokens * t.chars_per_token)
    total = len(messages)
    out = []
    capped = 0
    for i, m in enumerate(messages):
        depth = total - 1 - i  # 0 = newest
        content = m.get("content")
        if (
            m.get("role") == "tool"
            and isinstance(content, str)
            and depth >= t.protect_recent
            and len(content) > max_chars
        ):
            m = {**m, "content": _truncate_middle(content, max_chars)}
            capped += 1
        out.append(m)
    return out, capped


def _blocks(indices: List[int], messages: List[dict]) -> List[List[int]]:
    """Group non-system message indices into atomic blocks.

    A block is one message, except that an assistant message carrying
    `tool_calls` absorbs the run of `tool` messages that follows it. Dropping
    history at a block boundary can therefore never leave a tool call without
    its results, or a result without its call.
    """
    blocks: List[List[int]] = []
    for i in indices:
        m = messages[i]
        if (
            m.get("role") == "tool"
            and blocks
            and (
                messages[blocks[-1][-1]].get("role") == "tool"
                or messages[blocks[-1][0]].get("tool_calls")
            )
        ):
            blocks[-1].append(i)
        else:
            blocks.append([i])
    return blocks


def trim_request(
    payload: dict, body_str: str, asked: Optional[str], request_id: Optional[int] = None
) -> Optional[Trimmed]:
    """Return the trimmed copy of `payload` with what was done to it, or None
    when it needs no change (the caller then forwards the original, untouched).

    `body_str` is the raw request body, used as a free upper bound: a body whose
    total length already fits the budget is never even inspected. `request_id`
    is the In-flight id, so the log line can be matched to its row.
    """
    t = conf.TRIM
    if not t.enabled:
        return None
    messages = payload.get("messages")
    num_ctx = payload.get("num_ctx")
    if not isinstance(messages, list) or not messages:
        return None
    if isinstance(num_ctx, bool) or not isinstance(num_ctx, int) or num_ctx <= 0:
        return None
    if not all(isinstance(m, dict) for m in messages):
        return None  # malformed; let the backend say so
    budget = num_ctx - t.response_headroom
    if budget <= 0:
        return None
    cpt = t.chars_per_token
    if _tokens(len(body_str), cpt) <= budget:
        return None

    # Everything that is not the conversation (tools, sampling params, …) is
    # sent regardless, so it is a fixed cost against the budget, like the system
    # prompt.
    fixed = _tokens(_chars({k: v for k, v in payload.items() if k != "messages"}), cpt)
    before = fixed + sum(_msg_tokens(m, cpt) for m in messages)
    if before <= budget:
        return None  # the raw-length bound was pessimistic (e.g. an image)

    messages, capped = _cap_old_tool_results(messages, t)
    costs = [_msg_tokens(m, cpt) for m in messages]
    system_idx = [i for i, m in enumerate(messages) if m.get("role") == "system"]
    other_idx = [i for i, m in enumerate(messages) if m.get("role") != "system"]
    running = fixed + sum(costs[i] for i in system_idx)

    # Greedy fill, newest block first; the newest block is kept unconditionally.
    blocks = _blocks(other_idx, messages)
    cut = len(messages)  # first non-system index that is kept
    for block in reversed(blocks):
        cost = sum(costs[i] for i in block)
        if running + cost > budget and cut < len(messages):
            break
        running += cost
        cut = block[0]

    # A `tool` message the client itself sent without its call (already broken
    # on arrival) can head the window; drop such orphans, but never empty it.
    kept_other = [i for i in other_idx if i >= cut]
    while len(kept_other) > 1 and messages[kept_other[0]].get("role") == "tool":
        kept_other.pop(0)
    kept = set(system_idx) | set(kept_other)
    new_messages = [m for i, m in enumerate(messages) if i in kept]

    after = fixed + sum(costs[i] for i in sorted(kept))
    dropped = len(messages) - len(new_messages)
    if dropped == 0 and capped == 0:
        return None

    TRIMS_TOTAL.labels(model=asked or "unknown").inc()
    logger.warning(
        "CONTEXT TRIM request #%s model '%s': estimated %d tokens > budget %d "
        "(num_ctx=%d - headroom %d) -> dropped %d of %d message(s), cut %d old tool "
        "result(s) to an excerpt, forwarding ~%d tokens%s",
        request_id if request_id is not None else "?", asked, before, budget, num_ctx,
        t.response_headroom, dropped, len(messages), capped, after,
        "" if after <= budget else " — STILL over budget, the newest turn alone does not fit",
    )
    out = dict(payload)
    out["messages"] = new_messages
    return Trimmed(out, dropped, capped, before, after, budget)

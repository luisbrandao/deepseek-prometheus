"""The context guardrail (`app/trim.py`), driven on `trim_request` directly.

What is pinned here: it only acts on a body that declares `num_ctx` and is over
it; a fitting body comes back *untouched* (None, not a copy); system messages
survive; the oldest turns go first; an assistant tool call and its results are
dropped or kept together; old oversized tool results are excerpted before any
turn is dropped while recent ones are protected; the newest turn is always sent.
"""
import json

import pytest

from app import config as conf
from app import trim


def body(payload):
    return json.dumps(payload, ensure_ascii=False)


def run(payload):
    res = trim.trim_request(payload, body(payload), payload.get("model"))
    return None if res is None else res.payload


def msg(role, text, **extra):
    return {"role": role, "content": text, **extra}


def convo(turns, num_ctx, **extra):
    """A chat: one system message then `turns` user/assistant pairs of ~300 chars."""
    messages = [msg("system", "S" * 300)]
    for i in range(turns):
        messages.append(msg("user", f"u{i} " + "x" * 296))
        messages.append(msg("assistant", f"a{i} " + "y" * 296))
    return {"model": "m", "num_ctx": num_ctx, "messages": messages, **extra}


@pytest.fixture
def cfg(monkeypatch):
    """Install a Trim config; small headroom so budgets are easy to reason about."""
    def _set(**kw):
        params = dict(response_headroom=0, chars_per_token=3.0, protect_recent=10,
                      max_tool_result_tokens=4000)
        params.update(kw)
        monkeypatch.setattr(conf, "TRIM", conf.Trim(**params))
        return conf.TRIM
    return _set


# ── When it must NOT act ─────────────────────────────────────────────────────

def test_no_num_ctx_means_hands_off(cfg):
    cfg()
    p = convo(50, num_ctx=1)
    del p["num_ctx"]
    assert run(p) is None


@pytest.mark.parametrize("bad", [None, "128000", 128000.0, True, 0, -5])
def test_non_integer_num_ctx_is_ignored(cfg, bad):
    cfg()
    p = convo(50, num_ctx=bad)
    assert run(p) is None


def test_a_request_that_fits_is_returned_as_none_not_a_copy(cfg):
    cfg()
    assert run(convo(3, num_ctx=100_000)) is None


def test_disabled_never_touches_anything(cfg):
    cfg(enabled=False)
    assert run(convo(200, num_ctx=100)) is None


def test_non_chat_json_is_ignored(cfg):
    cfg()
    assert run({"model": "m", "num_ctx": 10, "input": "x" * 5000}) is None
    assert run({"model": "m", "num_ctx": 10, "messages": []}) is None
    assert run({"model": "m", "num_ctx": 10, "messages": ["not a dict"]}) is None


def test_zero_or_negative_budget_is_left_to_the_backend(cfg):
    cfg(response_headroom=4000)
    assert run(convo(50, num_ctx=4000)) is None


# ── Dropping turns ───────────────────────────────────────────────────────────

def test_keeps_system_and_the_newest_turns_drops_the_oldest(cfg):
    cfg()
    p = convo(20, num_ctx=2000)  # ~41 msgs * ~105 tokens each; 2000 fits ~17
    out = run(p)
    assert out is not None
    roles = [m["role"] for m in out["messages"]]
    assert roles[0] == "system"
    assert roles.count("system") == 1
    # A contiguous suffix: the last message is still the last message...
    assert out["messages"][-1] == p["messages"][-1]
    # ...and what was kept is exactly the tail of the original conversation.
    n = len(out["messages"]) - 1
    assert out["messages"][1:] == p["messages"][-n:]
    assert 0 < n < 40
    # Estimated size now fits.
    est = sum(trim._msg_tokens(m, 3.0) for m in out["messages"])
    assert est <= 2000


def test_the_original_payload_is_not_mutated(cfg):
    cfg()
    p = convo(20, num_ctx=2000)
    snapshot = json.dumps(p)
    run(p)
    assert json.dumps(p) == snapshot


def test_everything_but_messages_is_kept_and_counted_as_fixed_cost(cfg):
    cfg()
    tools = [{"type": "function", "function": {"name": f"t{i}", "description": "d" * 200}}
             for i in range(10)]
    p = convo(20, num_ctx=2000, tools=tools, temperature=0.3, stream=True)
    out = run(p)
    assert out["tools"] == tools and out["temperature"] == 0.3 and out["stream"] is True
    # Tools eat ~800 tokens of the 2000, so fewer turns fit than without them.
    assert len(out["messages"]) < len(run(convo(20, num_ctx=2000))["messages"])


def test_a_mid_conversation_system_message_survives_in_place(cfg):
    cfg()
    p = convo(20, num_ctx=2000)
    p["messages"].insert(5, msg("system", "injected"))
    out = run(p)
    systems = [m for m in out["messages"] if m["role"] == "system"]
    assert [m["content"] for m in systems] == ["S" * 300, "injected"]
    assert out["messages"][0]["content"] == "S" * 300


def test_the_newest_turn_is_kept_even_when_it_alone_is_over_budget(cfg, caplog):
    cfg()
    p = convo(3, num_ctx=50)
    out = run(p)
    assert [m["role"] for m in out["messages"]] == ["system", "assistant"]
    assert out["messages"][-1] == p["messages"][-1]
    assert "STILL over budget" in caplog.text


def test_result_carries_the_numbers_the_row_and_log_show(cfg):
    cfg()
    p = convo(20, num_ctx=2000)
    res = trim.trim_request(p, body(p), "m", request_id=7)
    assert res.dropped == len(p["messages"]) - len(res.payload["messages"]) > 0
    assert res.capped == 0
    assert res.before > res.budget == 2000 >= res.after
    assert res.as_dict() == {"dropped": res.dropped, "capped": 0, "before": res.before,
                             "after": res.after, "budget": 2000}


# ── Tool calls stay paired ───────────────────────────────────────────────────

def tool_convo(num_ctx, result_len=300):
    call = {"id": "c1", "type": "function",
            "function": {"name": "search", "arguments": "{}"}}
    call2 = {"id": "c2", "type": "function",
             "function": {"name": "fetch", "arguments": "{}"}}
    return {
        "model": "m", "num_ctx": num_ctx,
        "messages": [
            msg("system", "sys"),
            msg("user", "old question " + "x" * 200),
            msg("assistant", "old answer " + "y" * 200),
            msg("user", "weather?"),
            msg("assistant", "", tool_calls=[call, call2]),
            msg("tool", "r" * result_len, tool_call_id="c1"),
            msg("tool", "z" * result_len, tool_call_id="c2"),
            msg("assistant", "It will be cold."),
            msg("user", "thanks, and tomorrow?"),
        ],
    }


def paired(messages):
    calls = {tc["id"] for m in messages for tc in (m.get("tool_calls") or [])}
    results = {m.get("tool_call_id") for m in messages if m["role"] == "tool"}
    return calls == results


@pytest.mark.parametrize("num_ctx", range(40, 400, 7))
def test_cut_never_separates_a_tool_call_from_its_results(cfg, num_ctx):
    cfg()
    out = run(tool_convo(num_ctx))
    kept = out["messages"] if out else tool_convo(num_ctx)["messages"]
    assert paired(kept), [m["role"] for m in kept]
    assert kept[-1]["content"] == "thanks, and tomorrow?"
    assert kept[0]["role"] == "system"


def test_a_tool_block_is_dropped_as_a_unit(cfg):
    cfg()
    # Budget fits everything after the tool block plus the block's own results? No:
    # each result is 100 tokens, so ~150 tokens keeps only the last two messages.
    out = run(tool_convo(150))
    roles = [m["role"] for m in out["messages"]]
    assert roles == ["system", "assistant", "user"]
    assert "tool" not in roles


def test_a_leading_orphan_tool_result_from_the_client_is_dropped(cfg):
    cfg()
    # The old user turn is what pushes it over; the window would then *start*
    # on the orphan, which fits — but is dropped anyway.
    p = {
        "model": "m", "num_ctx": 100,
        "messages": [
            msg("system", "sys"),
            msg("user", "old " + "x" * 300),
            msg("tool", "orphan " + "x" * 100, tool_call_id="ghost"),
            msg("user", "hello"),
        ],
    }
    out = run(p)
    assert [m["role"] for m in out["messages"]] == ["system", "user"]
    assert out["messages"][-1]["content"] == "hello"


# ── Old oversized tool results are excerpted before turns are dropped ───────

def test_old_oversized_tool_result_is_excerpted_and_nothing_is_dropped(cfg):
    cfg(protect_recent=2, max_tool_result_tokens=100)  # cap at 300 chars
    p = tool_convo(num_ctx=800, result_len=3000)
    out = run(p)
    assert len(out["messages"]) == len(p["messages"]), "capping alone should have sufficed"
    for m in out["messages"]:
        if m["role"] == "tool":
            assert len(m["content"]) < 400
            assert "llm-proxy cut" in m["content"]
            assert m["content"].startswith("r" * 50) or m["content"].startswith("z" * 50)
            assert m["content"].endswith("r" * 50) or m["content"].endswith("z" * 50)
            assert m["tool_call_id"] in ("c1", "c2")
    assert paired(out["messages"])


def test_recent_tool_results_are_protected_from_excerpting(cfg):
    cfg(protect_recent=10, max_tool_result_tokens=100)
    # 2200 fits the tool block whole (~2080) but not the two oldest turns.
    p = tool_convo(num_ctx=2200, result_len=3000)
    out = run(p)
    roles = [m["role"] for m in out["messages"]]
    assert roles == ["system", "user", "assistant", "tool", "tool", "assistant", "user"]
    # The results are within the newest 10 messages, so they went whole, not excerpted.
    assert all(len(m["content"]) == 3000 for m in out["messages"] if m["role"] == "tool")
    assert paired(out["messages"])


def test_excerpting_can_be_disabled(cfg):
    cfg(protect_recent=0, max_tool_result_tokens=0)
    out = run(tool_convo(num_ctx=800, result_len=3000))
    assert all(len(m["content"]) == 3000 for m in out["messages"] if m["role"] == "tool") or \
        "tool" not in [m["role"] for m in out["messages"]]


# ── Estimation details ───────────────────────────────────────────────────────

def test_image_parts_are_counted_flat_not_by_base64_length(cfg):
    cfg()
    data_uri = "data:image/png;base64," + "A" * 300_000  # ~100k tokens by chars
    p = {
        "model": "m", "num_ctx": 8000,
        "messages": [
            msg("system", "sys"),
            {"role": "user", "content": [
                {"type": "text", "text": "what is this?"},
                {"type": "image_url", "image_url": {"url": data_uri}},
            ]},
        ],
    }
    assert run(p) is None  # 1000 flat + a few dozen tokens — fits comfortably


def test_trim_section_is_parsed_and_hot_reloaded(load_config):
    load_config("""\
        providers:
          - name: only
            base_url: "http://only.invalid/v1"
        trim:
          enabled: false
          chars_per_token: 3.7
          response_headroom: 2048
          max_tool_result_tokens: 0
          protect_recent: 4
        """)
    t = conf.TRIM
    assert (t.enabled, t.chars_per_token, t.response_headroom,
            t.max_tool_result_tokens, t.protect_recent) == (False, 3.7, 2048, 0, 4)
    load_config("""\
        providers:
          - name: only
            base_url: "http://only.invalid/v1"
        """)
    assert conf.TRIM == conf.Trim()

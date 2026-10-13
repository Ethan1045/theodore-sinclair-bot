"""Pure helpers for Theo's per-private-thread relationship emotion state."""

from __future__ import annotations

import json
import re
from typing import Any


RELATIONSHIP_DIRECTIVE_RE = re.compile(
    r"\[RELATIONSHIP_STATE\]\s*(.*?)\s*\[/RELATIONSHIP_STATE\]",
    re.DOTALL | re.IGNORECASE,
)

VALID_EMOTION_STATES = {
    "normal",
    "uneasy",
    "conflict",
    "angry",
    "sad",
    "cooling",
}
EMOTION_SCORE_FIELDS = ("unease", "conflict", "anger", "sadness", "cooling")


def default_relationship_emotion() -> dict[str, Any]:
    return {
        "state": "normal",
        "unease": 0,
        "conflict": 0,
        "anger": 0,
        "sadness": 0,
        "cooling": 0,
        "note": "",
        "proactive_blocked": False,
        "blocked_reason": "",
    }


def strip_relationship_directive(text: str) -> str:
    cleaned = RELATIONSHIP_DIRECTIVE_RE.sub("", text or "")
    # A truncated model response must not expose the internal state protocol.
    cleaned = re.sub(
        r"\[RELATIONSHIP_STATE\].*$", "", cleaned,
        flags=re.DOTALL | re.IGNORECASE,
    )
    return cleaned.strip()


def _score(value: Any) -> int:
    try:
        return max(0, min(100, int(round(float(value)))))
    except (TypeError, ValueError):
        return 0


def parse_relationship_directive(raw_reply: str) -> dict[str, Any] | None:
    """Parse and validate the hidden relationship-state JSON emitted by the model."""
    match = RELATIONSHIP_DIRECTIVE_RE.search(raw_reply or "")
    if not match:
        return None
    try:
        payload = json.loads(match.group(1).strip())
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None

    state = str(payload.get("state") or "normal").strip().lower()
    if state not in VALID_EMOTION_STATES:
        state = "normal"
    parsed: dict[str, Any] = {"state": state}
    for field in EMOTION_SCORE_FIELDS:
        parsed[field] = _score(payload.get(field, 0))

    # This text is stored and injected later as data, never as an instruction.
    note = re.sub(r"[\r\n<>{}\[\]]+", " ", str(payload.get("note") or ""))
    parsed["note"] = re.sub(r"\s+", " ", note).strip()[:160]
    return parsed


def _normalise_boundary_text(text: str) -> str:
    lowered = (text or "").strip().lower()
    lowered = re.sub(r"\s+", "", lowered)
    return lowered.strip("。.!！?？~～")


_EXPULSION_FULL_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"(?:你)?(?:给我)?(?:滚|滚开|滚出去|出去|走开|离我远点|消失)(?:吧|啊|呀)?",
        r"(?:以后)?(?:请)?(?:你)?(?:别|不要|不准)(?:再)?(?:主动)?(?:来)?(?:找我|联系我|给我发消息|发消息给我|跟我说话|烦我)(?:了|啦)?",
        r"(?:我)?(?:不想|不愿意)(?:再)?(?:见到你|和你说话|收到你的消息)(?:了|啦)?",
        r"(?:我们)?(?:分手|结束(?:这段)?关系|到此为止)(?:吧|了|啦)?",
        r"(?:你)?(?:离开|退出)(?:这个|我的)?(?:子区|频道)(?:吧|了)?",
        r"(?:你)?走吧",
        r"(?:别|不要)(?:再)?出现(?:在)?(?:这个|我的)?(?:子区|频道)(?:了)?",
    )
)

_EXPULSION_END_PATTERN = re.compile(
    r"(?:^|[，,；;。.!！？?])"
    r"(?:你)?(?:给我)?(?:滚开|滚出去|离我远点|别再来找我|不要再联系我|不要再主动找我|我们分手|到此为止)$"
)

_REINVITE_FULL_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"(?:你)?回来吧",
        r"(?:你)?可以回来(?:了|啦)?",
        r"(?:你)?可以(?:再)?(?:主动)?来找我(?:了|啦)?",
        r"(?:你)?可以(?:再)?联系我(?:了|啦)?",
        r"(?:别|不要)走",
        r"我不是(?:要|想)(?:让)?你走",
        r"刚才(?:那句|说的)?是气话",
        r"(?:我们)?和好吧",
        r"我想你了",
    )
)


def detect_explicit_expulsion(text: str) -> bool:
    """Return true only for a direct, unambiguous request that Theo leave."""
    normalised = _normalise_boundary_text(text)
    if not normalised:
        return False
    return any(pattern.fullmatch(normalised) for pattern in _EXPULSION_FULL_PATTERNS) or bool(
        _EXPULSION_END_PATTERN.search(normalised)
    )


def detect_explicit_reinvitation(text: str) -> bool:
    """Return true when the user clearly retracts an earlier expulsion boundary."""
    normalised = _normalise_boundary_text(text)
    if not normalised:
        return False
    return any(pattern.fullmatch(normalised) for pattern in _REINVITE_FULL_PATTERNS)


def is_wilted_rose_question(text: str) -> bool:
    lowered = (text or "").strip().lower()
    if "🥀" not in lowered and "枯萎的花" not in lowered and "枯萎花" not in lowered:
        return False
    return any(
        marker in lowered
        for marker in ("什么意思", "什么含义", "代表什么", "为什么", "怎么了", "meaning", "mean")
    )

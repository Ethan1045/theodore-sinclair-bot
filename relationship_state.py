"""Persist the single-partner relationship emotion without leaking it into chat text."""

from __future__ import annotations

import json
from typing import Any

import config
import db as _db
from client import discord_client
from relationship_emotion import (
    EMOTION_SCORE_FIELDS,
    default_relationship_emotion,
    detect_explicit_expulsion,
    detect_explicit_reinvitation,
    parse_relationship_directive,
    strip_relationship_directive,
)


_memory_states: dict[str, dict[str, Any]] = {}


def _bot_id() -> str:
    user = getattr(discord_client, "user", None)
    return str(getattr(user, "id", None) or "theodore-template")


def _key(user_id: int | str) -> str:
    return f"{_bot_id()}:{user_id}"


def _normalise(raw: dict[str, Any] | None) -> dict[str, Any]:
    state = default_relationship_emotion()
    if not isinstance(raw, dict):
        return state
    if raw.get("state") in {"normal", "uneasy", "conflict", "angry", "sad", "cooling"}:
        state["state"] = raw["state"]
    for field in EMOTION_SCORE_FIELDS:
        try:
            state[field] = max(0, min(100, int(raw.get(field, 0))))
        except (TypeError, ValueError):
            state[field] = 0
    state["note"] = str(raw.get("note") or "")[:160]
    state["proactive_blocked"] = bool(raw.get("proactive_blocked"))
    state["blocked_reason"] = str(raw.get("blocked_reason") or "")[:160]
    return state


async def ensure_relationship_emotion_table() -> None:
    if not config.DATABASE_URL:
        return
    try:
        async with _db.db_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS theodore_relationship_emotions (
                        bot_id TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        emotion_state TEXT NOT NULL DEFAULT 'normal',
                        emotion_scores JSONB NOT NULL DEFAULT
                            '{"unease":0,"conflict":0,"anger":0,"sadness":0,"cooling":0}'::jsonb,
                        emotion_note TEXT NOT NULL DEFAULT '',
                        proactive_blocked BOOLEAN NOT NULL DEFAULT FALSE,
                        proactive_blocked_reason TEXT NOT NULL DEFAULT '',
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        PRIMARY KEY (bot_id, user_id)
                    )
                """)
                await conn.commit()
        print("✅ theodore_relationship_emotions 表已就绪")
    except Exception as exc:
        print(f"⚠️ 关系情绪表初始化失败，将使用进程内状态: {exc}")


async def fetch_relationship_emotion(user_id: int | str) -> dict[str, Any]:
    cache_key = _key(user_id)
    if not config.DATABASE_URL:
        return _normalise(_memory_states.get(cache_key))
    try:
        async with _db.db_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """SELECT emotion_state, emotion_scores, emotion_note,
                              proactive_blocked, proactive_blocked_reason
                       FROM theodore_relationship_emotions
                       WHERE bot_id=%s AND user_id=%s""",
                    (_bot_id(), str(user_id)),
                )
                row = await cur.fetchone()
        if not row:
            return default_relationship_emotion()
        scores = row[1] if isinstance(row[1], dict) else json.loads(row[1] or "{}")
        return _normalise({
            "state": row[0], **scores, "note": row[2],
            "proactive_blocked": row[3], "blocked_reason": row[4],
        })
    except Exception as exc:
        print(f"⚠️ 读取关系情绪失败，将使用进程内状态: {exc}")
        return _normalise(_memory_states.get(cache_key))


async def _save_relationship_emotion(user_id: int | str, state: dict[str, Any]) -> None:
    state = _normalise(state)
    _memory_states[_key(user_id)] = state.copy()
    if not config.DATABASE_URL:
        return
    scores = {field: state[field] for field in EMOTION_SCORE_FIELDS}
    try:
        async with _db.db_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """INSERT INTO theodore_relationship_emotions (
                           bot_id,user_id,emotion_state,emotion_scores,emotion_note,
                           proactive_blocked,proactive_blocked_reason
                       ) VALUES (%s,%s,%s,%s::jsonb,%s,%s,%s)
                       ON CONFLICT (bot_id,user_id) DO UPDATE SET
                           emotion_state=EXCLUDED.emotion_state,
                           emotion_scores=EXCLUDED.emotion_scores,
                           emotion_note=EXCLUDED.emotion_note,
                           proactive_blocked=EXCLUDED.proactive_blocked,
                           proactive_blocked_reason=EXCLUDED.proactive_blocked_reason,
                           updated_at=NOW()""",
                    (
                        _bot_id(), str(user_id), state["state"],
                        json.dumps(scores), state["note"], state["proactive_blocked"],
                        state["blocked_reason"],
                    ),
                )
                await conn.commit()
    except Exception as exc:
        print(f"⚠️ 保存关系情绪失败，已保留进程内状态: {exc}")


async def apply_relationship_boundary(
    user_id: int | str, text: str,
) -> dict[str, Any]:
    """Apply only explicit leave/reinvite language; ambiguous complaints never block."""
    state = await fetch_relationship_emotion(user_id)
    if detect_explicit_expulsion(text):
        state.update({
            "state": "conflict",
            "unease": max(state["unease"], 70),
            "conflict": max(state["conflict"], 85),
            "anger": max(state["anger"], 45),
            "sadness": max(state["sadness"], 65),
            "cooling": 0,
            "note": "她明确要求 Theodore 离开或停止主动联系",
            "proactive_blocked": True,
            "blocked_reason": "恋人明确驱逐或要求停止主动联系",
        })
        await _save_relationship_emotion(user_id, state)
    elif detect_explicit_reinvitation(text) and state.get("proactive_blocked"):
        state.update({
            "state": "cooling",
            "conflict": min(state["conflict"], 60),
            "anger": min(state["anger"], 45),
            "cooling": max(state["cooling"], 65),
            "note": "她明确邀请 Theodore 回来，关系开始缓和",
            "proactive_blocked": False,
            "blocked_reason": "",
        })
        await _save_relationship_emotion(user_id, state)
    return state


def relationship_prompt(state: dict[str, Any]) -> str:
    labels = {
        "normal": "正常", "uneasy": "不安", "conflict": "冲突",
        "angry": "生气", "sad": "难过", "cooling": "冷却",
    }
    boundary = (
        "她此前明确要求你离开或停止主动联系。现在只因她先开口才回应；"
        "不要预约主动关心、追着求和或假装边界不存在。"
        if state.get("proactive_blocked") else
        "她没有设置禁止主动联系的边界。"
    )
    return (
        "\n（系统关系情绪：这是你面对唯一恋人的持续状态。"
        f"当前主状态={labels.get(state.get('state'), '正常')}；"
        f"不安={state.get('unease', 0)}，冲突={state.get('conflict', 0)}，"
        f"生气={state.get('anger', 0)}，难过={state.get('sadness', 0)}，"
        f"冷却={state.get('cooling', 0)}；事实摘要={state.get('note') or '无'}。{boundary}\n"
        "这些情绪可以通过措辞、停顿、亲疏和边界表现出来，但仍须符合 Theodore 的人格："
        "面对恋人可以受伤、生气或需要距离，不羞辱、不报复、不用机械客服腔。"
        "不要因为一次普通分歧就剧烈升级，也不要未经对话自动恢复正常。\n"
        "回复正文和其他指令之后，追加且只追加一个隐藏状态块；state 只能是 "
        "normal、uneasy、conflict、angry、sad、cooling：\n"
        '[RELATIONSHIP_STATE]{"state":"normal","unease":0,"conflict":0,'
        '"anger":0,"sadness":0,"cooling":0,"note":"不超过80字的事实摘要"}'
        "[/RELATIONSHIP_STATE]\n"
        "五个分数是 0~100 的新目标值，只依据真实对话和现有状态适度变化；"
        "不要在正文解释状态块或分数。）"
    )


async def update_relationship_from_reply(
    user_id: int | str, raw_reply: str,
) -> tuple[str, dict[str, Any]]:
    """Persist a valid hidden model update and always remove the protocol from output."""
    current = await fetch_relationship_emotion(user_id)
    parsed = parse_relationship_directive(raw_reply)
    if parsed:
        parsed["proactive_blocked"] = current.get("proactive_blocked", False)
        parsed["blocked_reason"] = current.get("blocked_reason", "")
        await _save_relationship_emotion(user_id, parsed)
        current = _normalise(parsed)
    return strip_relationship_directive(raw_reply), current


async def proactive_is_blocked(user_id: int | str) -> bool:
    return bool((await fetch_relationship_emotion(user_id)).get("proactive_blocked"))

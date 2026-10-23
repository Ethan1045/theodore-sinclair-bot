import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("PARTNER_USER_ID", "1")

import ai_client
import config
import state
import tasks_bg
from history import _drop_unanswered_wakeup_contexts, get_history


def test_dangling_wakeup_context_is_removed_but_answered_pair_remains():
    dangling = {"role": "user", "content": "（睡着期间收到、醒后补看的消息）\n旧消息"}
    paired_user = {"role": "user", "content": "（睡着期间收到、醒后补看的消息）\n已回复"}
    paired_assistant = {"role": "assistant", "content": "已经回复"}
    real_user = {"role": "user", "content": "新的真实消息"}
    hist = [
        {"role": "system", "content": "system"},
        paired_user,
        paired_assistant,
        dangling,
        real_user,
    ]

    assert _drop_unanswered_wakeup_contexts(hist)
    assert dangling not in hist
    assert paired_user in hist
    assert paired_assistant in hist
    assert real_user in hist


@pytest.fixture
def isolated_wakeup_state(monkeypatch):
    state.sleep_pending_messages.clear()
    state.sleep_pending_lock = None
    state._histories.clear()
    state._bucket_locks.clear()
    state._dirty_history_buckets.clear()
    state._history_revisions.clear()
    tasks_bg._wakeup_catchup_task = None
    monkeypatch.setattr(config, "DATABASE_URL", None)
    monkeypatch.setattr(tasks_bg.random, "uniform", lambda *_: 0)
    monkeypatch.setattr(tasks_bg.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(tasks_bg, "_partner_blocked_proactive", AsyncMock(return_value=False))
    yield
    state.sleep_pending_messages.clear()


@pytest.mark.asyncio
async def test_wakeup_without_original_message_commits_pair_and_clears_queue(
    monkeypatch, isolated_wakeup_state
):
    entry = {
        "channel_id": 77,
        "message_id": 88,
        "user_id": 99,
        "display_name": "Partner",
        "text": "醒来回我",
        "is_dm": False,
        "ts": "2026-10-23T00:00:00+00:00",
        "drowsy": False,
    }
    state.sleep_pending_messages.append(entry)
    channel = SimpleNamespace(
        id=77,
        fetch_message=AsyncMock(side_effect=RuntimeError("message gone")),
        send=AsyncMock(),
    )
    monkeypatch.setattr(tasks_bg.discord_client, "fetch_channel", AsyncMock(return_value=channel))
    monkeypatch.setattr(ai_client, "call_ai", AsyncMock(return_value="I saw it.\n（我看到了。）"))

    await tasks_bg._wakeup_catchup()

    channel.send.assert_awaited_once()
    assert state.sleep_pending_messages == []
    hist = get_history("ch:77")
    assert [item["role"] for item in hist[-2:]] == ["user", "assistant"]
    assert hist[-1]["source"] == "wakeup:77:88"


@pytest.mark.asyncio
async def test_ignored_wakeup_stays_queued_without_dangling_history(
    monkeypatch, isolated_wakeup_state
):
    entry = {
        "channel_id": 77,
        "message_id": 89,
        "user_id": 99,
        "display_name": "Partner",
        "text": "醒来回我",
        "is_dm": False,
        "ts": "2026-10-23T00:00:00+00:00",
        "drowsy": False,
    }
    state.sleep_pending_messages.append(entry)
    channel = SimpleNamespace(id=77, fetch_message=AsyncMock(return_value=None), send=AsyncMock())
    monkeypatch.setattr(tasks_bg.discord_client, "fetch_channel", AsyncMock(return_value=channel))
    monkeypatch.setattr(ai_client, "call_ai", AsyncMock(return_value="[IGNORE]"))

    await tasks_bg._wakeup_catchup()

    assert state.sleep_pending_messages == [entry]
    assert all(
        not str(item.get("source") or "").startswith("wakeup:")
        for item in get_history("ch:77")
    )


@pytest.mark.asyncio
async def test_answered_wakeup_queue_is_cleared_without_resending(
    monkeypatch, isolated_wakeup_state
):
    entry = {
        "channel_id": 77,
        "message_id": 90,
        "user_id": 99,
        "display_name": "Partner",
        "text": "不要重复回",
        "is_dm": False,
        "ts": "2026-10-23T00:00:00+00:00",
        "drowsy": False,
    }
    state.sleep_pending_messages.append(entry)
    hist = get_history("ch:77")
    hist.extend([
        {"role": "user", "content": "旧消息", "source": "wakeup:77:90"},
        {"role": "assistant", "content": "已经回过", "source": "wakeup:77:90"},
    ])
    channel = SimpleNamespace(
        id=77, fetch_message=AsyncMock(return_value=None), send=AsyncMock(),
    )
    call_ai = AsyncMock()
    monkeypatch.setattr(tasks_bg.discord_client, "fetch_channel", AsyncMock(return_value=channel))
    monkeypatch.setattr(ai_client, "call_ai", call_ai)

    await tasks_bg._wakeup_catchup()

    call_ai.assert_not_awaited()
    channel.send.assert_not_awaited()
    assert state.sleep_pending_messages == []

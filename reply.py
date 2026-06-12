"""send_ai_reply 与 _keep_typing：处理 AI 输出并发送到 Discord。"""
import asyncio
import random
from datetime import datetime, timezone

import discord

import config
import state
from history import get_history, trim_history, history_key_for
from directives import parse_bot_directives
from actions import execute_action, classify_discord_error


async def _keep_typing(channel: discord.abc.Messageable, stop: asyncio.Event):
    # discord.py 2.x 已移除 Messageable.trigger_typing()，改用 typing() 上下文：
    # 它会在上下文存活期间自动每 ~5s 续期，直到 stop 被 set。
    try:
        async with channel.typing():
            await stop.wait()
    except Exception:
        pass


async def send_ai_reply(raw_bot_reply: str, trigger_message: discord.Message, channel: discord.abc.Messageable, as_reply=True):
    hist_key = history_key_for(channel=channel, message=trigger_message)
    bucket_lock = state.get_bucket_lock(hist_key)
    hist = get_history(hist_key)
    if "[IGNORE]" in raw_bot_reply.upper():
        async with bucket_lock:
            if hist and hist[-1].get("role") == "user":
                hist.pop()
        state.mark_history_dirty(hist_key)
        return

    clean_reply, messages_to_send, reaction_target, emojis_to_react, action_matches = parse_bot_directives(raw_bot_reply)

    history_text = clean_reply.replace('[SPLIT]', '\n')
    async with bucket_lock:
        if history_text:
            hist.append({"role": "assistant", "content": history_text})
        else:
            hist.append({"role": "assistant", "content": "（只挂了表情，没说话）"})
    await trim_history(hist_key)

    long_pause = (len(messages_to_send) == 2 and random.random() < 0.10)
    sent_message = None
    for i, msg_text in enumerate(messages_to_send):
        if i == 0:
            async with channel.typing():
                await asyncio.sleep(min(0.5 + len(msg_text) * 0.01, 2.0))
            if as_reply and hasattr(trigger_message, 'reply'):
                sent_message = await trigger_message.reply(msg_text)
            else:
                sent_message = await channel.send(msg_text)
        else:
            if long_pause:
                await asyncio.sleep(8)
                async with channel.typing():
                    await asyncio.sleep(7)
            else:
                async with channel.typing():
                    await asyncio.sleep(min(1.0 + len(msg_text) * 0.02, 3.5))
            sent_message = await channel.send(msg_text)

    if emojis_to_react:
        if reaction_target == "USER" or not sent_message:
            target_msg = trigger_message
        else:
            target_msg = sent_message
        for emoji in emojis_to_react:
            try:
                await target_msg.add_reaction(emoji)
            except Exception as e:
                print(f"挂表情失败 ({classify_discord_error(e)}): emoji={emoji} target_msg={getattr(target_msg,'id',None)}")

    for action_str in action_matches:
        await execute_action(action_str, trigger_message)

    if history_text:
        _eat_kws = ["吃了吗", "吃饭了吗", "吃了没", "have you eaten", "eaten yet", "吃东西", "记得吃"]
        _sleep_kws = ["睡了吗", "睡觉了吗", "睡了没", "go to sleep", "sleep", "晚安", "good night", "早点睡"]
        _lower_reply = history_text.lower()
        if any(k in _lower_reply for k in _eat_kws):
            state.care_reminder_last["eat"] = datetime.now(timezone.utc)
        if any(k in _lower_reply for k in _sleep_kws):
            state.care_reminder_last["sleep"] = datetime.now(timezone.utc)

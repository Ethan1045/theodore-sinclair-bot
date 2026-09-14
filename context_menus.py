"""Message context-menu commands with explicit authorization boundaries."""
from datetime import datetime, timezone

import discord
from discord import app_commands

import config
import db as _db
import state
from ai_client import call_ai
from client import discord_client, slash_tree
from directives import parse_bot_directives
from followups import add_manual_followup
from history import get_history, history_key_for


def _is_admin(interaction: discord.Interaction) -> bool:
    perms = getattr(interaction.user, "guild_permissions", None)
    return bool(perms and getattr(perms, "administrator", False))


async def _remember_message(interaction: discord.Interaction, message: discord.Message):
    if interaction.user.id != config.PARTNER_USER_ID:
        await interaction.response.send_message("只有恋人可以写进这本私人账本。", ephemeral=True)
        return
    if not config.DATABASE_URL:
        await interaction.response.send_message("数据库尚未配置，暂时记不下来。", ephemeral=True)
        return
    text = (message.content or "（无文字消息）").strip()[:500]
    try:
        async with _db.db_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO user_notes(user_id, note, category) VALUES(%s, %s, %s)",
                    (str(config.PARTNER_USER_ID), f"{message.author.display_name}说：{text}", "日常"),
                )
                await conn.commit()
        await interaction.response.send_message("记下了。", ephemeral=True)
    except Exception as exc:
        print(f"⚠️ 消息菜单记忆失败: {exc}")
        await interaction.response.send_message("这次没有记成功。", ephemeral=True)


async def _follow_up_message(interaction: discord.Interaction, message: discord.Message):
    if interaction.user.id != config.PARTNER_USER_ID:
        await interaction.response.send_message("这个跟进账本只对恋人开放。", ephemeral=True)
        return
    ok = await add_manual_followup(message, hours=24)
    await interaction.response.send_message(
        "好，明天这个时候之后我会自然问一句。" if ok else "现在没法存下这件事。",
        ephemeral=True,
    )


async def _ask_ts(interaction: discord.Interaction, message: discord.Message):
    now = datetime.now(timezone.utc)
    cooldown_key = ("context_ask", interaction.user.id)
    last = getattr(state, "context_command_cooldowns", {}).get(cooldown_key)
    if last and (now - last).total_seconds() < 15:
        await interaction.response.send_message("稍等片刻再让我看下一条。", ephemeral=True)
        return
    if not hasattr(state, "context_command_cooldowns"):
        state.context_command_cooldowns = {}
    state.context_command_cooldowns[cooldown_key] = now
    await interaction.response.defer(thinking=True)
    try:
        hist = get_history(history_key_for(channel=message.channel)).copy()
        hist.append({
            "role": "user",
            "content": (
                f"（系统提示：{interaction.user.display_name}通过消息右键菜单请你看看下面这条消息。"
                f"作者：{message.author.display_name}；内容：「{(message.content or '无文字内容')[:1500]}」。"
                "请直接给出你的看法或回应。严格保持英文一行、括号中文一行；"
                "不要执行任何ACTION，不要IGNORE。）" + state.life_hint_text()
            ),
        })
        raw = await call_ai(hist)
        _, messages, _, _, _ = parse_bot_directives(raw)
        await interaction.followup.send(messages[0] if messages else "I have nothing useful to add.\n（我暂时没有值得补充的。）")
    except Exception as exc:
        print(f"⚠️ 右键让T.S.看看失败: {exc}")
        await interaction.followup.send("I couldn't read that properly.\n（这次没能好好看清。）", ephemeral=True)


async def _forward_home(interaction: discord.Interaction, message: discord.Message):
    if interaction.guild is None or not (interaction.user.id == config.PARTNER_USER_ID or _is_admin(interaction)):
        await interaction.response.send_message("只有恋人或服务器管理员可以转发。", ephemeral=True)
        return
    target_id = config.PARTNER_HOME_CHANNEL_ID or config.PROACTIVE_CHANNEL_ID
    if not target_id:
        await interaction.response.send_message("尚未配置主场/主动频道。", ephemeral=True)
        return
    if config.MUTATING_CHANNEL_IDS and target_id not in config.MUTATING_CHANNEL_IDS:
        await interaction.response.send_message("目标频道不在允许列表内。", ephemeral=True)
        return
    try:
        target = await discord_client.fetch_channel(target_id)
        if getattr(getattr(target, "guild", None), "id", None) != interaction.guild.id:
            raise ValueError("目标频道不属于当前服务器")
        bot_member = interaction.guild.get_member(discord_client.user.id)
        perms = target.permissions_for(bot_member) if bot_member and hasattr(target, "permissions_for") else None
        can_send = bool(perms and perms.send_messages)
        if isinstance(target, discord.Thread) and perms:
            can_send = can_send and getattr(perms, "send_messages_in_threads", True)
        if perms and not can_send:
            raise ValueError("Bot 在目标频道缺少发送权限")
        await message.forward(target)
        await interaction.response.send_message("已转到主场。", ephemeral=True)
    except Exception as exc:
        print(f"⚠️ 右键转发失败: {exc}")
        await interaction.response.send_message("转发失败；请检查 View Channel 与 Send Messages 权限。", ephemeral=True)


def _register() -> None:
    commands = (
        app_commands.ContextMenu(name="记下这条消息", callback=_remember_message),
        app_commands.ContextMenu(name="明天跟进这件事", callback=_follow_up_message),
        app_commands.ContextMenu(name="让 T.S. 看看", callback=_ask_ts),
        app_commands.ContextMenu(name="转发到主场", callback=_forward_home),
    )
    existing = {cmd.name for cmd in slash_tree.get_commands(type=discord.AppCommandType.message)}
    for command in commands:
        if command.name not in existing:
            slash_tree.add_command(command)


_register()

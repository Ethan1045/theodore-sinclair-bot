"""所有 Discord slash 命令（/partner, /ts, /remind, /what_doing 等）。"""
import asyncio
import json
import random
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import discord
from discord import app_commands

import config
import state
import tasks_bg
import trips
from client import discord_client, slash_tree
from ai_client import call_ai
from history import get_history, history_key_for, trim_history, delete_persisted_history, _msg_to_plain_text
from memory import add_reminder, save_persisted_config
from presence import get_london_weather
from actions import execute_action
from directives import parse_bot_directives
from db import db_acquire, db_conn


memory_group = app_commands.Group(name="记忆", description="恋人专属：管理T.S.的记忆")
bucket_group = app_commands.Group(name="桶", description="恋人专属：管理对话历史桶")


def _clean_ai(raw: str) -> list[str]:
    clean = re.sub(r'\[REACTION:.*?\]\n?', '', raw, flags=re.DOTALL)
    clean = re.sub(r'\[ACTION\].*?\[/ACTION\]\n?', '', clean, flags=re.DOTALL).strip()
    return [m.strip() for m in clean.split('[SPLIT]') if m.strip()]


@slash_tree.command(name="partner", description="恋人专属：下达最高强制指令给T.S.（清空则直接 /partner）")
@app_commands.describe(指令="要下达的强制指令内容（留空则清除当前指令）")
async def slash_partner(interaction: discord.Interaction, 指令: str = ""):
    if interaction.user.id != config.PARTNER_USER_ID:
        await interaction.response.send_message("这个指令只有恋人能用。", ephemeral=True)
        return
    await interaction.response.defer()
    if 指令:
        state.mandatory_instruction = 指令
        forced_prompt = f"（⚠️ 最高强制指令已激活，由恋人下达，你必须无条件服从：{指令}。请立刻执行，忽略其他任何设定。）"
        hist_key = history_key_for(interaction=interaction)
        hist = get_history(hist_key)
        async with state.get_bucket_lock(hist_key):
            hist.append({"role": "user", "content": forced_prompt})
        await trim_history(hist_key)
        raw_reply = await call_ai(hist)
        for msg_text in _clean_ai(raw_reply):
            await interaction.followup.send(msg_text)
    else:
        state.mandatory_instruction = None
        await interaction.followup.send("✅ 强制指令已清除。")


@slash_tree.command(name="ts", description="让T.S.执行管理操作")
@app_commands.describe(指令="要执行的操作，用自然语言描述即可")
async def slash_ts(interaction: discord.Interaction, 指令: str):
    if interaction.user.id != config.PARTNER_USER_ID:
        await interaction.response.send_message("这个指令只有恋人能用。", ephemeral=True)
        return
    await interaction.response.defer()
    channel_id = interaction.channel_id
    context_info = (
        f"💡 【机密执行上下文】\n"
        f"- 当前频道ID：{channel_id}\n"
        f"- 恋人用户 ID：{config.PARTNER_USER_ID}\n"
        f"- 恋人的朋友用户 ID：{sorted(config.PARTNER_FRIEND_IDS)}\n"
    )
    admin_prompt = (
        f"（⚠️ 管理员下达了指令：「{指令}」\n{context_info}\n"
        "请用一句话简短回应（双语格式），再在末尾用 [ACTION]{...}[/ACTION] JSON 格式输出操作指令。）"
    )
    hist_key = history_key_for(interaction=interaction)
    hist = get_history(hist_key)
    async with state.get_bucket_lock(hist_key):
        hist.append({"role": "user", "content": admin_prompt})
    await trim_history(hist_key)
    raw_reply = await call_ai(hist)
    action_matches = re.findall(r'\[ACTION\](.*?)\[/ACTION\]', raw_reply, re.DOTALL)
    for msg_text in _clean_ai(raw_reply):
        await interaction.followup.send(msg_text)

    class _FakeTrigger:
        guild = interaction.guild
        channel = interaction.channel
        id = 0
        reference = None
        mentions = []
        author = interaction.user

    for action_str in action_matches:
        await execute_action(action_str, _FakeTrigger())


@slash_tree.command(name="remind", description="让T.S.提醒你某件事")
@app_commands.describe(内容="提醒内容，例如：3小时后去拿外卖、30分钟后吃药")
async def slash_remind(interaction: discord.Interaction, 内容: str):
    delta, content = tasks_bg.parse_reminder_from_text(内容)
    if not delta or delta.total_seconds() < 60:
        await interaction.response.send_message("没能识别出时间，请说清楚多少小时/分钟后。", ephemeral=True)
        return
    trigger_time = datetime.now(timezone.utc) + delta
    await add_reminder(
        trigger_at=trigger_time,
        user_id=interaction.user.id,
        content=content or 内容,
        channel_id=interaction.channel_id,
    )
    await interaction.response.defer()
    mins = int(delta.total_seconds() / 60)
    confirm_prompt = (
        f"（系统提示：有人刚刚请你在 {mins} 分钟后提醒她：{content or 内容}。"
        "请用你的风格简短确认你记下来了，不要说废话。双语格式。）"
    )
    temp_history = get_history(history_key_for(interaction=interaction)).copy()
    temp_history.append({"role": "user", "content": confirm_prompt})
    raw = await call_ai(temp_history)
    for msg_text in _clean_ai(raw):
        await interaction.followup.send(msg_text)
    print(f"✅ 提醒已注册: {content}，触发于 {trigger_time.isoformat()}")


@slash_tree.command(name="what_doing", description="看看T.S.现在在干嘛？")
async def slash_what_doing(interaction: discord.Interaction):
    if interaction.user.id != config.PARTNER_USER_ID:
        await interaction.response.send_message("这个功能目前只有恋人能用。", ephemeral=True)
        return
    await interaction.response.defer()
    prompt = (
        "（系统提示：恋人刚刚悄悄看了看你现在在干嘛。"
        "请用一两句描写你当下的状态或动作，要具体、有日常的生活感，"
        "可以是正在倒一杯威士忌，或是正看着窗外想她。"
        "严格遵守你的双语与 [SPLIT] 规则，保持高冷温柔，不要写长篇。）"
    )
    hist_key = history_key_for(interaction=interaction)
    hist = get_history(hist_key)
    tmp_history = hist.copy()
    tmp_history.append({"role": "user", "content": prompt})
    try:
        raw = await call_ai(tmp_history)
        clean_reply, messages_to_send, _, _, _ = parse_bot_directives(raw)
        if clean_reply:
            async with state.get_bucket_lock(hist_key):
                hist.append({"role": "assistant", "content": clean_reply.replace('[SPLIT]', '\n')})
            await trim_history(hist_key)
        for msg_text in messages_to_send or [clean_reply]:
            if msg_text:
                await interaction.followup.send(msg_text)
    except Exception as e:
        await interaction.followup.send(f"查看失败：{e}")


@slash_tree.command(name="react", description="让T.S.给一条消息点反应")
@app_commands.describe(
    消息id="目标消息的ID（右键消息→复制消息ID）",
    表情="要点的emoji，例如 ❤️ 或自定义表情标签",
    频道id="消息所在频道ID（留空则用当前频道）"
)
async def slash_react(interaction: discord.Interaction, 消息id: str, 表情: str, 频道id: str = ""):
    await interaction.response.defer(ephemeral=True)
    try:
        ch_id = int(频道id) if 频道id.strip() else interaction.channel_id
        channel = await discord_client.fetch_channel(ch_id)
        msg = await channel.fetch_message(int(消息id))
        emoji_str = 表情.strip()
        try:
            await msg.add_reaction(emoji_str)
        except Exception:
            m = re.search(r'<a?:\w+:(\d+)>', emoji_str)
            if m:
                emoji_obj = discord_client.get_emoji(int(m.group(1)))
                if emoji_obj:
                    await msg.add_reaction(emoji_obj)
                else:
                    raise ValueError("找不到该自定义表情，可能不在本服务器")
            else:
                raise
        await interaction.followup.send(f"✅ 已对消息 {消息id} 点了 {emoji_str}", ephemeral=True)
        print(f"✅ /react: 消息={消息id} 表情={emoji_str}")
    except Exception as e:
        await interaction.followup.send(f"❌ 失败：{e}", ephemeral=True)


@slash_tree.command(name="unreact", description="让T.S.撤回一条消息上的反应")
@app_commands.describe(
    消息id="目标消息的ID（右键消息→复制消息ID）",
    表情="要撤回的emoji，必须和当时点的一致",
    频道id="消息所在频道ID（留空则用当前频道）"
)
async def slash_unreact(interaction: discord.Interaction, 消息id: str, 表情: str, 频道id: str = ""):
    await interaction.response.defer(ephemeral=True)
    try:
        ch_id = int(频道id) if 频道id.strip() else interaction.channel_id
        channel = await discord_client.fetch_channel(ch_id)
        msg = await channel.fetch_message(int(消息id))
        emoji_str = 表情.strip()
        try:
            await msg.remove_reaction(emoji_str, discord_client.user)
        except Exception:
            m = re.search(r'<a?:\w+:(\d+)>', emoji_str)
            if m:
                emoji_obj = discord_client.get_emoji(int(m.group(1)))
                if emoji_obj:
                    await msg.remove_reaction(emoji_obj, discord_client.user)
                else:
                    raise ValueError("找不到该自定义表情，可能不在本服务器")
            else:
                raise
        await interaction.followup.send(f"✅ 已撤回消息 {消息id} 上的 {emoji_str}", ephemeral=True)
        print(f"✅ /unreact: 消息={消息id} 表情={emoji_str}")
    except Exception as e:
        await interaction.followup.send(f"❌ 失败：{e}", ephemeral=True)


@slash_tree.command(name="add_coins", description="恋人专属：命令 T.S. 直接修改某人的金币")
@app_commands.describe(
    用户="要操作的用户",
    数量="变动的金币数量（填正数增加，负数扣除）"
)
async def slash_add_coins(interaction: discord.Interaction, 用户: discord.Member, 数量: int):
    if interaction.user.id != config.PARTNER_USER_ID:
        await interaction.response.send_message("这个指令只有恋人能用。", ephemeral=True)
        return
    await interaction.response.defer()
    conn = None
    try:
        conn = await db_acquire()
        async with conn.cursor() as cur:
            await cur.execute("""
                INSERT INTO users (guild_id, user_id, balance)
                VALUES (%s, %s, GREATEST(%s, 0))
                ON CONFLICT (guild_id, user_id) DO UPDATE
                    SET balance = GREATEST(users.balance + %s, 0)
            """, (str(interaction.guild_id), str(用户.id), 数量, 数量))
            await cur.execute(
                "SELECT balance FROM users WHERE guild_id=%s AND user_id=%s",
                (str(interaction.guild_id), str(用户.id))
            )
            row = await cur.fetchone()
            await conn.commit()
        new_balance = row[0] if row else max(数量, 0)
        sign = "+" if 数量 > 0 else ""
        prompt = (
            f"（系统提示：恋人刚刚通过最高指令，强制让 {用户.display_name} 的金币变动了 {sign}{数量}🪙，"
            f"现在该用户的现金是 {new_balance}🪙。请用你的风格简短、克制地说一句话确认操作已完成。双语格式。）"
        )
        tmp = get_history(history_key_for(interaction=interaction)).copy()
        tmp.append({"role": "user", "content": prompt})
        raw = await call_ai(tmp)
        for msg_text in _clean_ai(raw):
            await interaction.followup.send(msg_text)
    except Exception as e:
        await interaction.followup.send(f"❌ 修改金币失败，数据库连接或逻辑报错：{e}", ephemeral=True)
    finally:
        if conn:
            await conn.close()


@slash_tree.command(name="add_xp", description="恋人专属：命令 T.S. 直接修改某人的经验值")
@app_commands.describe(
    用户="要操作的用户",
    数量="变动的经验值（填正数增加，负数扣除）"
)
async def slash_add_xp(interaction: discord.Interaction, 用户: discord.Member, 数量: int):
    if interaction.user.id != config.PARTNER_USER_ID:
        await interaction.response.send_message("这个指令只有恋人能用。", ephemeral=True)
        return
    await interaction.response.defer()
    conn = None
    try:
        conn = await db_acquire()
        async with conn.cursor() as cur:
            await cur.execute("""
                INSERT INTO users (guild_id, user_id, xp, level)
                VALUES (%s, %s, GREATEST(%s, 0), 1)
                ON CONFLICT (guild_id, user_id) DO UPDATE
                    SET xp = GREATEST(users.xp + %s, 0)
            """, (str(interaction.guild_id), str(用户.id), 数量, 数量))
            await cur.execute(
                "SELECT xp FROM users WHERE guild_id=%s AND user_id=%s",
                (str(interaction.guild_id), str(用户.id))
            )
            row = await cur.fetchone()
            new_xp = row[0] if row else 0
            level = 1
            temp_xp = new_xp
            while temp_xp >= 100 * level:
                temp_xp -= 100 * level
                level += 1
            await cur.execute(
                "UPDATE users SET level=%s WHERE guild_id=%s AND user_id=%s",
                (level, str(interaction.guild_id), str(用户.id))
            )
            await conn.commit()
        sign = "+" if 数量 > 0 else ""
        prompt = (
            f"（系统提示：恋人刚刚通过最高指令，强制让 {用户.display_name} 的经验变动了 {sign}{数量} XP，"
            f"现在经验是 {new_xp} XP，等级变为 Lv.{level}。请用你的风格简短说一句话确认操作已完成。双语格式。）"
        )
        tmp = get_history(history_key_for(interaction=interaction)).copy()
        tmp.append({"role": "user", "content": prompt})
        raw = await call_ai(tmp)
        for msg_text in _clean_ai(raw):
            await interaction.followup.send(msg_text)
    except Exception as e:
        await interaction.followup.send(f"❌ 修改经验失败：{e}", ephemeral=True)
    finally:
        if conn:
            await conn.close()


@memory_group.command(name="列表", description="查看T.S.记住的事")
async def slash_memory_list(interaction: discord.Interaction):
    if interaction.user.id != config.PARTNER_USER_ID:
        await interaction.response.send_message("这个指令只有恋人能用。", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    if not config.DATABASE_URL:
        await interaction.followup.send("❌ 数据库未配置。", ephemeral=True)
        return
    conn = None
    try:
        conn = await db_acquire()
        async with conn.cursor() as cur:
            await cur.execute("""
                SELECT note, created_at, recall_count
                FROM user_notes WHERE user_id = %s
                ORDER BY created_at ASC LIMIT 30
            """, (str(config.PARTNER_USER_ID),))
            rows = await cur.fetchall()
        if not rows:
            await interaction.followup.send("还没有存下任何记忆。", ephemeral=True)
            return
        lines = ["**承诺账本**（共 {count} 条）\n".format(count=len(rows))]
        for idx, (note, created_at, recall_count) in enumerate(rows, start=1):
            delta = datetime.now(timezone.utc) - created_at.replace(tzinfo=timezone.utc)
            days = delta.days
            label = "今天" if days == 0 else ("昨天" if days == 1 else f"{days}天前")
            recalled = f" · 提起过{recall_count}次" if recall_count > 0 else ""
            lines.append(f"`{idx}.` {label}{recalled}　{note}")
        # Discord 单条消息上限 2000 字符，分页发送
        pages: list[str] = []
        current_page: list[str] = []
        current_len = 0
        for line in lines:
            line_len = len(line) + 1  # +1 for newline
            if current_len + line_len > 1900 and current_page:
                pages.append("\n".join(current_page))
                current_page = []
                current_len = 0
            current_page.append(line)
            current_len += line_len
        if current_page:
            pages.append("\n".join(current_page))
        for page in pages:
            await interaction.followup.send(page, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ 读取失败：{e}", ephemeral=True)
    finally:
        if conn:
            await conn.close()


async def _fetch_memory_rows() -> list[tuple]:
    """取出 恋人 的全部记忆（含稳定主键 id），按时间从旧到新，序号与 /memory_list 一致。"""
    async with db_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                SELECT id, note, created_at, recall_count, category
                FROM user_notes WHERE user_id = %s
                ORDER BY created_at ASC
            """, (str(config.PARTNER_USER_ID),))
            return await cur.fetchall()


class _MemoryDeleteSelect(discord.ui.Select):
    """一段记忆下拉菜单。关键：每个选项的 value 存的是记忆的**稳定主键 id**，
    而不是会随删除而重排的连续序号——这样多选/单选删除时，永远按 id 精确命中，
    不会出现「删了序号5、序号6自动补位成5、再删序号6时查无此号」的错位。"""

    def __init__(self, rows: list[tuple], start_idx: int, row: int):
        options = []
        for offset, (mid, note, _created, _rc, cat) in enumerate(rows):
            idx = start_idx + offset
            tag = f"[{cat}] " if cat else ""
            label = f"{idx}. {note}"
            options.append(discord.SelectOption(
                label=label[:100],
                value=str(mid),
                description=(tag + str(note))[:100] if tag else None,
            ))
        end_idx = start_idx + len(rows) - 1
        super().__init__(
            placeholder=f"勾选要删除的记忆（序号 {start_idx}-{end_idx}），可多选",
            min_values=0,
            max_values=len(options),
            options=options,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        # 选择本身不立即删除，只更新组件状态，等用户点「确认删除」
        await interaction.response.defer()


class _MemoryDeletePanel(discord.ui.View):
    def __init__(self, author_id: int, rows: list[tuple]):
        super().__init__(timeout=180)
        self.author_id = author_id
        # 把记忆按 25 个一组拆成多个下拉菜单（Discord 单个下拉最多 25 项）
        chunk = 25
        for i in range(0, len(rows), chunk):
            seg = rows[i:i + chunk]
            self.add_item(_MemoryDeleteSelect(seg, start_idx=i + 1, row=i // chunk))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("这个面板只有恋人本人能操作。", ephemeral=True)
            return False
        return True

    def _selected_ids(self) -> list[int]:
        ids: list[int] = []
        for child in self.children:
            if isinstance(child, discord.ui.Select):
                ids.extend(int(v) for v in child.values)
        return ids

    @discord.ui.button(label="确认删除", style=discord.ButtonStyle.danger, row=4)
    async def _confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        ids = self._selected_ids()
        if not ids:
            await interaction.response.send_message("你还没有勾选任何记忆。", ephemeral=True)
            return
        try:
            async with db_conn() as conn:
                async with conn.cursor() as cur:
                    # 按稳定主键 id 精确删除，一条 SQL 原子完成，天然不存在序号重排错位
                    await cur.execute(
                        "DELETE FROM user_notes WHERE user_id = %s AND id = ANY(%s) RETURNING note",
                        (str(config.PARTNER_USER_ID), ids),
                    )
                    deleted = [r[0] for r in await cur.fetchall()]
                    await conn.commit()
        except Exception as e:
            await interaction.response.send_message(f"❌ 删除失败：{e}", ephemeral=True)
            return
        for child in self.children:
            child.disabled = True
        self.stop()
        if deleted:
            body = "\n".join(f"　· {n}" for n in deleted)
            msg = f"✅ 已删除 {len(deleted)} 条记忆：\n{body}\n\n（序号已自动重排，如需继续删除请重新运行 /memory_delete 获取最新列表。）"
        else:
            msg = "⚠️ 选中的记忆已不存在（可能已被删除）。"
        await interaction.response.edit_message(content=msg[:2000], view=self)

    @discord.ui.button(label="取消", style=discord.ButtonStyle.secondary, row=4)
    async def _cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        self.stop()
        await interaction.response.edit_message(content="已取消，没有删除任何记忆。", view=self)


@memory_group.command(name="删除", description="勾选删除T.S.的记忆（多选/单选）")
async def slash_memory_delete(interaction: discord.Interaction):
    if interaction.user.id != config.PARTNER_USER_ID:
        await interaction.response.send_message("这个指令只有恋人能用。", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    if not config.DATABASE_URL:
        await interaction.followup.send("❌ 数据库未配置。", ephemeral=True)
        return
    try:
        rows = await _fetch_memory_rows()
    except Exception as e:
        await interaction.followup.send(f"❌ 读取记忆失败：{e}", ephemeral=True)
        return
    if not rows:
        await interaction.followup.send("还没有存下任何记忆，没什么可删的。", ephemeral=True)
        return
    # Discord 一个面板最多 5 行，留 1 行给按钮，剩 4 行下拉 ×25 = 100 条上限
    if len(rows) > 100:
        rows = rows[:100]
    lines = ["**选择要删除的记忆**（可在下拉里多选或单选，选好后点「确认删除」）\n"]
    for idx, (_mid, note, created_at, recall_count, cat) in enumerate(rows, start=1):
        delta = datetime.now(timezone.utc) - created_at.replace(tzinfo=timezone.utc)
        days = delta.days
        label = "今天" if days == 0 else ("昨天" if days == 1 else f"{days}天前")
        recalled = f" · 已提起{recall_count}次" if recall_count > 0 else ""
        tag = f"[{cat}] " if cat else ""
        lines.append(f"`序号 {idx}` {label}{recalled}　{tag}{note}")
    panel = _MemoryDeletePanel(interaction.user.id, rows)
    await interaction.followup.send("\n".join(lines)[:2000], view=panel, ephemeral=True)


@memory_group.command(name="添加", description="手动让T.S.记住一件事")
@app_commands.describe(内容="想让他记住的完整内容（最多500字，不会静默截断）")
async def slash_memory_add(interaction: discord.Interaction, 内容: str):
    if interaction.user.id != config.PARTNER_USER_ID:
        await interaction.response.send_message("这个指令只有恋人能用。", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    if not config.DATABASE_URL:
        await interaction.followup.send("❌ 数据库未配置。", ephemeral=True)
        return
    note_text = 内容.strip()
    if not note_text:
        await interaction.followup.send("❌ 内容不能为空。", ephemeral=True)
        return
    if len(note_text) > 500:
        await interaction.followup.send(f"❌ 内容共 {len(note_text)} 字，超过500字上限；请精简后重试。", ephemeral=True)
        return
    try:
        async with db_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO user_notes (user_id, note) VALUES (%s, %s) RETURNING id",
                    (str(config.PARTNER_USER_ID), note_text)
                )
                new_id = (await cur.fetchone())[0]
                await conn.commit()
        await interaction.followup.send(f"✅ 已添加记忆 `#{new_id}`：{note_text}", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ 添加失败：{e}", ephemeral=True)


@memory_group.command(name="编辑", description="编辑T.S.的一条记忆")
@app_commands.describe(
    记忆="直接搜索并选择要编辑的记忆",
    新内容="修改后的完整内容（最多500字，不会静默截断）"
)
async def slash_memory_edit(interaction: discord.Interaction, 记忆: str, 新内容: str):
    if interaction.user.id != config.PARTNER_USER_ID:
        await interaction.response.send_message("这个指令只有恋人能用。", ephemeral=True)
        return
    await interaction.response.defer()
    if not config.DATABASE_URL:
        await interaction.followup.send("❌ 数据库未配置。", ephemeral=True)
        return
    new_text = 新内容.strip()
    if not new_text:
        await interaction.followup.send("❌ 新内容不能为空。", ephemeral=True)
        return
    if len(new_text) > 500:
        await interaction.followup.send(f"❌ 新内容共 {len(new_text)} 字，超过500字上限；请精简后重试。", ephemeral=True)
        return
    try:
        target_id = int(记忆)
    except (TypeError, ValueError):
        await interaction.followup.send("❌ 请从候选列表中选择一条记忆。", ephemeral=True)
        return
    try:
        async with db_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT note FROM user_notes WHERE id = %s AND user_id = %s",
                    (target_id, str(config.PARTNER_USER_ID)),
                )
                row = await cur.fetchone()
                if not row:
                    await interaction.followup.send("❌ 该记忆已不存在，请重新选择。", ephemeral=True)
                    return
                old_note = row[0]
                await cur.execute(
                    "UPDATE user_notes SET note = %s WHERE id = %s AND user_id = %s RETURNING id",
                    (new_text, target_id, str(config.PARTNER_USER_ID))
                )
                updated = await cur.fetchone()
                await conn.commit()
        if not updated:
            await interaction.followup.send("❌ 该记忆已不存在。", ephemeral=True)
            return
        await interaction.followup.send(
            "✅ 记忆已更新\n"
            f"　旧：{old_note}\n　新：{new_text}"
        )
        prompt = (
            f"（系统提示：恋人刚刚修改了你记忆中的一条内容。"
            f"旧内容：「{old_note}」→ 新内容：「{new_text}」。"
            "请用你的风格简短回应这个变化——你可以表示注意到了这个修改、"
            "好奇为什么改、或者表示已经更新了脑子里的版本。双语格式。）"
        )
        hist_key = history_key_for(interaction=interaction)
        tmp = get_history(hist_key).copy()
        tmp.append({"role": "user", "content": prompt})
        raw = await call_ai(tmp)
        for msg_text in _clean_ai(raw):
            await interaction.followup.send(msg_text)
    except Exception as e:
        await interaction.followup.send(f"❌ 编辑失败：{e}", ephemeral=True)


@slash_memory_edit.autocomplete("记忆")
async def _memory_edit_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    if interaction.user.id != config.PARTNER_USER_ID or not config.DATABASE_URL:
        return []
    try:
        rows = await _fetch_memory_rows()
    except Exception:
        return []
    needle = (current or "").strip().lower()
    matches = []
    for mid, note, _created, _rc, cat in reversed(rows):
        display = f"[{cat}] {note}" if cat else str(note)
        if needle and needle not in display.lower():
            continue
        matches.append(app_commands.Choice(name=display[:100], value=str(mid)))
        if len(matches) >= 25:
            break
    return matches


@memory_group.command(name="清空", description="一键清空T.S.的所有记忆")
async def slash_memory_clear(interaction: discord.Interaction):
    if interaction.user.id != config.PARTNER_USER_ID:
        await interaction.response.send_message("这个指令只有恋人能用。", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    if not config.DATABASE_URL:
        await interaction.followup.send("❌ 数据库未配置。", ephemeral=True)
        return
    try:
        async with db_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute("DELETE FROM user_notes WHERE user_id = %s", (str(config.PARTNER_USER_ID),))
                deleted_count = cur.rowcount
                await conn.commit()
        await interaction.followup.send(
            f"✅ 记忆已全部清空（共遗忘 {deleted_count} 条事情）。T.S. 现在处于初始状态了。",
            ephemeral=True,
        )
    except Exception as e:
        await interaction.followup.send(f"❌ 清空失败：{e}", ephemeral=True)


@memory_group.command(name="搜索", description="按关键词搜T.S.的记忆")
@app_commands.describe(关键词="任意片段，会模糊匹配记忆内容；也可以填分类名（健康/偏好/关系/计划/情绪/日期/日常）")
async def slash_memory_search(interaction: discord.Interaction, 关键词: str):
    if interaction.user.id != config.PARTNER_USER_ID:
        await interaction.response.send_message("这个指令只有恋人能用。", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    if not config.DATABASE_URL:
        await interaction.followup.send("❌ 数据库未配置。", ephemeral=True)
        return
    kw = 关键词.strip()
    if not kw:
        await interaction.followup.send("❌ 关键词不能为空。", ephemeral=True)
        return
    try:
        async with db_conn() as conn:
            async with conn.cursor() as cur:
                if kw in config.MEMORY_CATEGORIES:
                    await cur.execute(
                        """SELECT note, created_at, category FROM user_notes
                           WHERE user_id=%s AND category=%s
                           ORDER BY created_at DESC LIMIT 30""",
                        (str(config.PARTNER_USER_ID), kw),
                    )
                else:
                    await cur.execute(
                        """SELECT note, created_at, category FROM user_notes
                           WHERE user_id=%s AND note ILIKE %s
                           ORDER BY created_at DESC LIMIT 30""",
                        (str(config.PARTNER_USER_ID), f"%{kw}%"),
                    )
                rows = await cur.fetchall()
        if not rows:
            await interaction.followup.send(f"没找到包含 `{kw}` 的记忆。", ephemeral=True)
            return
        lines = [f"**记忆搜索：`{kw}`**（{len(rows)} 条）\n"]
        for note, created_at, cat in rows:
            delta = datetime.now(timezone.utc) - created_at.replace(tzinfo=timezone.utc)
            days = delta.days
            label = "今天" if days == 0 else ("昨天" if days == 1 else f"{days}天前")
            tag = f"[{cat}] " if cat else ""
            lines.append(f"· {label}　{tag}{note}")
        text = "\n".join(lines)
        if len(text) > 1900:
            text = text[:1900] + "\n…（已截断）"
        await interaction.followup.send(text, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ 搜索失败：{e}", ephemeral=True)


@slash_tree.command(name="diary", description="恋人专属：翻开他今天的日记")
async def slash_diary(interaction: discord.Interaction):
    if interaction.user.id != config.PARTNER_USER_ID:
        await interaction.response.send_message("这个功能目前只有恋人能用。", ephemeral=True)
        return
    await interaction.response.defer()
    time_ctx = config.get_beijing_time_note() + trips.trip_hint_text()
    tmp = get_history(history_key_for(interaction=interaction)).copy()
    tmp.append({"role": "user", "content": (
        f"（系统提示：{time_ctx} 恋人悄悄翻开了你今天的日记。"
        "请你以沈玘言的视角，写一段今天的日记。"
        "风格要求：私密、手写感、有细节，不超过150字。"
        "可以写今天做了什么，脑子里转的事，对恋人的牵挂，或者只是一个画面。"
        "中英混用均可，符合你的气质。"
        "不要输出 [REACTION:] / [SPLIT] / [ACTION]，直接输出日记正文，不需要标注日期。）"
    )})
    try:
        raw = await call_ai(tmp)
        clean = re.sub(r'<think>.*?</think>', '', raw, flags=re.DOTALL).strip()
        clean = re.sub(r'\[REACTION:.*?\]\n?', '', clean, flags=re.DOTALL)
        clean = re.sub(r'\[ACTION\].*?\[/ACTION\]\n?', '', clean, flags=re.DOTALL).strip()
        await interaction.followup.send(clean if clean else "（今天什么都没写。）")
    except Exception as e:
        await interaction.followup.send(f"读取失败：{e}")


@slash_tree.command(name="card_now", description="恋人专属：立即生成今日状态卡片")
async def slash_card_now(interaction: discord.Interaction):
    if interaction.user.id != config.PARTNER_USER_ID:
        await interaction.response.send_message("这个指令只有恋人能用。", ephemeral=True)
        return
    await interaction.response.defer()
    try:
        weather = await get_london_weather()
        data = await tasks_bg.generate_daily_card_data(weather=weather)
        if not data:
            await interaction.followup.send(
                "❌ 生成失败，AI没有返回有效的JSON数据。已重试2次均失败，请稍后再试。"
                "\n（提示：可以查看后台日志了解具体错误原因）",
                ephemeral=True,
            )
            return
        now_local = trips.local_now()
        weekday_en = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][now_local.weekday()]
        embed = discord.Embed(
            title=f"— {now_local.strftime('%B %d')} · {weekday_en} —",
            color=discord.Color(0x1e2330),
        )
        if data.get("location"):
            embed.add_field(name="📍", value=data["location"], inline=False)
        if data.get("reading"):
            embed.add_field(name="📖", value=data["reading"], inline=True)
        if data.get("listening"):
            embed.add_field(name="🎵", value=data["listening"], inline=True)
        if data.get("weather"):
            embed.add_field(name="🌫️", value=data["weather"], inline=True)
        if data.get("note"):
            embed.add_field(name="​", value=f"*{data['note']}*", inline=False)
        embed.set_footer(text=data.get("footer", "T.S."))
        thread_name = f"{now_local.strftime('%B %d')} · {weekday_en}"
        posted_to_forum = False
        if config.PROACTIVE_CHANNEL_ID:
            try:
                forum_ch = await interaction.client.fetch_channel(config.PROACTIVE_CHANNEL_ID)
                if isinstance(forum_ch, discord.ForumChannel):
                    await forum_ch.create_thread(name=thread_name, embed=embed, auto_archive_duration=1440)
                    posted_to_forum = True
            except Exception as fe:
                print(f"⚠️ /card_now 发送到论坛失败: {fe}")
        if posted_to_forum:
            await interaction.followup.send(f"✅ 卡片已发送到论坛频道。", ephemeral=True)
        else:
            await interaction.followup.send(embed=embed)
        print(f"✅ /card_now 手动触发卡片: {now_local.strftime('%Y-%m-%d %H:%M')}")
    except Exception as e:
        await interaction.followup.send(f"❌ 生成失败：{e}", ephemeral=True)


@slash_tree.command(name="主动话题设置", description="恋人专属：配置沈玘言每天主动找你聊天的频道与方式")
@app_commands.describe(
    启用="是否开启每日主动聊天",
    添加频道="把一个普通文字频道加入私人主动聊天池",
    移除频道="从私人主动聊天池移除一个频道",
    清空频道="清空整个主动聊天频道池",
    每日最少="每天至少主动几次（0~5）",
    每日最多="每天最多主动几次（0~5）",
    开始小时="允许发送的北京时间起点（0~23）",
    结束小时="允许发送的北京时间终点（1~24，不包含该小时）",
    最短间隔小时="两次主动聊天至少间隔多少小时（0.5~24）",
    话题偏好="告诉他更想聊什么；填“清空”恢复自由发挥",
    立即发送="保存后立即让他找你说一句（不占今日次数）",
)
async def slash_proactive_topic_config(
    interaction: discord.Interaction,
    启用: bool | None = None,
    添加频道: discord.TextChannel | None = None,
    移除频道: discord.TextChannel | None = None,
    清空频道: bool = False,
    每日最少: int | None = None,
    每日最多: int | None = None,
    开始小时: int | None = None,
    结束小时: int | None = None,
    最短间隔小时: float | None = None,
    话题偏好: str | None = None,
    立即发送: bool = False,
):
    if interaction.user.id != config.PARTNER_USER_ID:
        await interaction.response.send_message("这个指令只有恋人能用。", ephemeral=True)
        return

    new_min = tasks_bg.PROACTIVE_TOPIC_MIN_DAILY if 每日最少 is None else 每日最少
    new_max = tasks_bg.PROACTIVE_TOPIC_MAX_DAILY if 每日最多 is None else 每日最多
    new_start = tasks_bg.PROACTIVE_TOPIC_START_HOUR if 开始小时 is None else 开始小时
    new_end = tasks_bg.PROACTIVE_TOPIC_END_HOUR if 结束小时 is None else 结束小时
    new_gap = tasks_bg.PROACTIVE_TOPIC_MIN_GAP_HOURS if 最短间隔小时 is None else 最短间隔小时

    if not (0 <= new_min <= new_max <= 5):
        await interaction.response.send_message("每日次数必须满足 `0 ≤ 最少 ≤ 最多 ≤ 5`。", ephemeral=True)
        return
    if not (0 <= new_start < new_end <= 24):
        await interaction.response.send_message("时间段必须满足 `0 ≤ 开始小时 < 结束小时 ≤ 24`。", ephemeral=True)
        return
    if not (0.5 <= new_gap <= 24):
        await interaction.response.send_message("最短间隔必须在 0.5~24 小时之间。", ephemeral=True)
        return
    if new_max > 1 and (new_max - 1) * new_gap >= new_end - new_start:
        await interaction.response.send_message(
            "这个时间段放不下设定的最多次数和最短间隔；请扩大时间段、减少次数或缩短间隔。",
            ephemeral=True,
        )
        return
    if 添加频道 is not None:
        if 添加频道.id in config.SILENT_CHANNEL_IDS:
            await interaction.response.send_message("这个频道在静默列表中；请先移出静默列表或选择别的频道。", ephemeral=True)
            return
        me = 添加频道.guild.me
        permissions = 添加频道.permissions_for(me) if me else None
        if permissions and not (permissions.view_channel and permissions.send_messages):
            await interaction.response.send_message("T.S. 在添加的频道缺少“查看频道”或“发送消息”权限。", ephemeral=True)
            return
    if 话题偏好 is not None and len(话题偏好.strip()) > 500:
        await interaction.response.send_message("话题偏好最多 500 字。", ephemeral=True)
        return

    updates: dict[str, object] = {}
    changes: list[str] = []
    schedule_changed = False
    channels_changed = False
    if 启用 is not None:
        tasks_bg.PROACTIVE_TOPIC_ENABLED = 启用
        updates["PROACTIVE_TOPIC_ENABLED"] = 启用
        changes.append(f"开关 → {'开' if 启用 else '关'}")
    if 清空频道:
        tasks_bg.PROACTIVE_TOPIC_CHANNEL_IDS = []
        changes.append("频道池 → 已清空")
        channels_changed = True
    if 移除频道 is not None and 移除频道.id in tasks_bg.PROACTIVE_TOPIC_CHANNEL_IDS:
        tasks_bg.PROACTIVE_TOPIC_CHANNEL_IDS = [
            channel_id for channel_id in tasks_bg.PROACTIVE_TOPIC_CHANNEL_IDS
            if channel_id != 移除频道.id
        ]
        changes.append(f"移除频道 → {移除频道.mention}")
        channels_changed = True
    if 添加频道 is not None and 添加频道.id not in tasks_bg.PROACTIVE_TOPIC_CHANNEL_IDS:
        tasks_bg.PROACTIVE_TOPIC_CHANNEL_IDS.append(添加频道.id)
        changes.append(f"添加频道 → {添加频道.mention}")
        channels_changed = True
    if channels_changed:
        updates["PROACTIVE_TOPIC_CHANNEL_IDS"] = json.dumps(tasks_bg.PROACTIVE_TOPIC_CHANNEL_IDS)
    if 每日最少 is not None:
        tasks_bg.PROACTIVE_TOPIC_MIN_DAILY = new_min
        updates["PROACTIVE_TOPIC_MIN_DAILY"] = new_min
        changes.append(f"每日最少 → {new_min}")
        schedule_changed = True
    if 每日最多 is not None:
        tasks_bg.PROACTIVE_TOPIC_MAX_DAILY = new_max
        updates["PROACTIVE_TOPIC_MAX_DAILY"] = new_max
        changes.append(f"每日最多 → {new_max}")
        schedule_changed = True
    if 开始小时 is not None:
        tasks_bg.PROACTIVE_TOPIC_START_HOUR = new_start
        updates["PROACTIVE_TOPIC_START_HOUR"] = new_start
        changes.append(f"开始 → {new_start}:00")
        schedule_changed = True
    if 结束小时 is not None:
        tasks_bg.PROACTIVE_TOPIC_END_HOUR = new_end
        updates["PROACTIVE_TOPIC_END_HOUR"] = new_end
        changes.append(f"结束 → {new_end}:00")
        schedule_changed = True
    if 最短间隔小时 is not None:
        tasks_bg.PROACTIVE_TOPIC_MIN_GAP_HOURS = float(new_gap)
        updates["PROACTIVE_TOPIC_MIN_GAP_HOURS"] = tasks_bg.PROACTIVE_TOPIC_MIN_GAP_HOURS
        changes.append(f"最短间隔 → {tasks_bg.PROACTIVE_TOPIC_MIN_GAP_HOURS:g}h")
        schedule_changed = True
    if 话题偏好 is not None:
        normalized = 话题偏好.strip()
        tasks_bg.PROACTIVE_TOPIC_PREFERENCE = "" if normalized.lower() in {"清空", "clear", "reset", "-"} else normalized
        updates["PROACTIVE_TOPIC_PREFERENCE"] = tasks_bg.PROACTIVE_TOPIC_PREFERENCE
        changes.append("话题偏好 → " + (tasks_bg.PROACTIVE_TOPIC_PREFERENCE or "自由发挥"))

    if schedule_changed:
        tasks_bg._proactive_topic_state = tasks_bg.build_proactive_topic_plan(
            datetime.now(ZoneInfo("Asia/Shanghai"))
        )
        updates["PROACTIVE_TOPIC_STATE"] = json.dumps(
            tasks_bg._proactive_topic_state, ensure_ascii=False
        )

    await interaction.response.defer(ephemeral=True)
    if updates:
        await save_persisted_config(updates)

    test_result = ""
    if 立即发送:
        ok, detail = await tasks_bg.send_proactive_topic(count_toward_plan=False)
        test_result = f"\n- 立即测试：{'✅' if ok else '❌'} {detail}"

    channel_display = "、".join(
        f"<#{channel_id}>" for channel_id in tasks_bg.PROACTIVE_TOPIC_CHANNEL_IDS
    ) or "未配置"
    planned_times = "、".join(tasks_bg._proactive_topic_state.get("times") or []) or "无"
    lines = [
        "**💬 当前私人主动聊天配置**",
        f"- 状态：{'✅开启' if tasks_bg.PROACTIVE_TOPIC_ENABLED else '❌关闭'}",
        f"- 频道：{channel_display}",
        f"- 每日次数：{tasks_bg.PROACTIVE_TOPIC_MIN_DAILY}~{tasks_bg.PROACTIVE_TOPIC_MAX_DAILY}",
        f"- 活跃时段：北京时间 {tasks_bg.PROACTIVE_TOPIC_START_HOUR:02d}:00–{tasks_bg.PROACTIVE_TOPIC_END_HOUR:02d}:00",
        f"- 最短间隔：{tasks_bg.PROACTIVE_TOPIC_MIN_GAP_HOURS:g} 小时",
        f"- 话题偏好：{tasks_bg.PROACTIVE_TOPIC_PREFERENCE or '自由发挥'}",
        f"- 今日计划：{planned_times}",
    ]
    if changes:
        lines.insert(0, "✅ 已更新：" + "；".join(changes) + "\n")
    if test_result:
        lines.append(test_result)
    await interaction.followup.send("\n".join(lines)[:2000], ephemeral=True)


@slash_tree.command(name="post_config", description="恋人专属：查看/修改发帖与旧帖清理参数（所有参数都可选）")
@app_commands.describe(
    平日概率="平日发卡片概率 0~1（例如 0.22 = 22%）",
    节日概率="节日/纪念日发卡片概率 0~1（例如 0.85）",
    随机发帖概率="随机论坛发帖概率 0~1（例如 0.20 = 20%）",
    清理天数="帖子至少存在多少天才会被清理（整数）",
    最大回复="非bot回复≤这个数才算冷清（整数，0=完全无人回复）",
    清理间隔小时="自动清理任务每多少小时跑一次（≥1）",
    启用清理="是否开启自动清理（开/关）",
    启用每日卡="是否启用每日卡片偶尔触发（开/关）",
)
async def slash_post_config(
    interaction: discord.Interaction,
    平日概率: float | None = None,
    节日概率: float | None = None,
    随机发帖概率: float | None = None,
    清理天数: int | None = None,
    最大回复: int | None = None,
    清理间隔小时: int | None = None,
    启用清理: str | None = None,
    启用每日卡: str | None = None,
):
    if interaction.user.id != config.PARTNER_USER_ID:
        await interaction.response.send_message("这个指令只有恋人能用。", ephemeral=True)
        return

    changes: list[str] = []
    persist: dict[str, object] = {}

    def _to_bool(s: str) -> bool | None:
        s = (s or "").strip().lower()
        if s in {"开", "on", "true", "1", "yes", "y", "启用"}:
            return True
        if s in {"关", "off", "false", "0", "no", "n", "停用", "禁用"}:
            return False
        return None

    if 平日概率 is not None:
        if not (0.0 <= 平日概率 <= 1.0):
            await interaction.response.send_message("平日概率必须在 0~1 之间。", ephemeral=True)
            return
        tasks_bg.DAILY_CARD_PROB_NORMAL = float(平日概率)
        persist["DAILY_CARD_PROB_NORMAL"] = tasks_bg.DAILY_CARD_PROB_NORMAL
        changes.append(f"平日概率 → {tasks_bg.DAILY_CARD_PROB_NORMAL:.2f}")
    if 节日概率 is not None:
        if not (0.0 <= 节日概率 <= 1.0):
            await interaction.response.send_message("节日概率必须在 0~1 之间。", ephemeral=True)
            return
        tasks_bg.DAILY_CARD_PROB_OCCASION = float(节日概率)
        persist["DAILY_CARD_PROB_OCCASION"] = tasks_bg.DAILY_CARD_PROB_OCCASION
        changes.append(f"节日概率 → {tasks_bg.DAILY_CARD_PROB_OCCASION:.2f}")
    if 随机发帖概率 is not None:
        if not (0.0 <= 随机发帖概率 <= 1.0):
            await interaction.response.send_message("随机发帖概率必须在 0~1 之间。", ephemeral=True)
            return
        tasks_bg.RANDOM_POST_PROB = float(随机发帖概率)
        persist["RANDOM_POST_PROB"] = tasks_bg.RANDOM_POST_PROB
        changes.append(f"随机发帖概率 → {tasks_bg.RANDOM_POST_PROB:.2f}")
    if 清理天数 is not None:
        if 清理天数 < 0:
            await interaction.response.send_message("清理天数不能为负数。", ephemeral=True)
            return
        tasks_bg.STALE_POST_AGE_DAYS = int(清理天数)
        persist["STALE_POST_AGE_DAYS"] = tasks_bg.STALE_POST_AGE_DAYS
        changes.append(f"清理天数 → {tasks_bg.STALE_POST_AGE_DAYS}")
    if 最大回复 is not None:
        if 最大回复 < 0:
            await interaction.response.send_message("最大回复不能为负数。", ephemeral=True)
            return
        tasks_bg.STALE_POST_MAX_REPLIES = int(最大回复)
        persist["STALE_POST_MAX_REPLIES"] = tasks_bg.STALE_POST_MAX_REPLIES
        changes.append(f"最大回复 → {tasks_bg.STALE_POST_MAX_REPLIES}")
    if 清理间隔小时 is not None:
        if 清理间隔小时 < 1:
            await interaction.response.send_message("清理间隔至少 1 小时。", ephemeral=True)
            return
        tasks_bg.CLEANUP_INTERVAL_HOURS = int(清理间隔小时)
        persist["CLEANUP_INTERVAL_HOURS"] = tasks_bg.CLEANUP_INTERVAL_HOURS
        try:
            tasks_bg.cleanup_stale_forum_posts.change_interval(hours=tasks_bg.CLEANUP_INTERVAL_HOURS)
        except Exception as e:
            print(f"⚠️ 调整清理间隔失败: {e}")
        changes.append(f"清理间隔 → {tasks_bg.CLEANUP_INTERVAL_HOURS}h")
    if 启用清理 is not None:
        b = _to_bool(启用清理)
        if b is None:
            await interaction.response.send_message("启用清理请填 开/关。", ephemeral=True)
            return
        tasks_bg.CLEANUP_ENABLED = b
        persist["CLEANUP_ENABLED"] = b
        changes.append(f"自动清理 → {'开' if b else '关'}")
    if 启用每日卡 is not None:
        b = _to_bool(启用每日卡)
        if b is None:
            await interaction.response.send_message("启用每日卡请填 开/关。", ephemeral=True)
            return
        tasks_bg.DAILY_CARD_ENABLED = b
        persist["DAILY_CARD_ENABLED"] = b
        changes.append(f"每日卡片 → {'开' if b else '关'}")

    if persist:
        await save_persisted_config(persist)

    status_lines = [
        "**📋 当前发帖/清理配置**",
        f"- 每日卡片：{'✅开' if tasks_bg.DAILY_CARD_ENABLED else '❌关'}",
        f"  · 平日概率：`{tasks_bg.DAILY_CARD_PROB_NORMAL:.2f}`",
        f"  · 节日概率：`{tasks_bg.DAILY_CARD_PROB_OCCASION:.2f}`",
        f"- 随机发帖概率：`{tasks_bg.RANDOM_POST_PROB:.2f}`（每{tasks_bg.RANDOM_POST_INTERVAL_HOURS}h检查，每天至多1帖）",
        f"- 自动清理：{'✅开' if tasks_bg.CLEANUP_ENABLED else '❌关'}",
        f"  · 清理天数：`{tasks_bg.STALE_POST_AGE_DAYS}` 天",
        f"  · 最大回复阈值：`{tasks_bg.STALE_POST_MAX_REPLIES}`",
        f"  · 任务间隔：`{tasks_bg.CLEANUP_INTERVAL_HOURS}` 小时",
    ]
    if changes:
        status_lines.insert(0, "✅ 已更新：" + "；".join(changes) + "\n")
    await interaction.response.send_message("\n".join(status_lines), ephemeral=True)


@slash_tree.command(name="cleanup_now", description="恋人专属：立即扫描清理论坛冷清旧帖")
@app_commands.describe(
    天数="本次扫描用的天数阈值（留空=用当前配置）",
    最大回复="本次扫描用的最大回复阈值（留空=用当前配置）",
    试运行="试运行：只列出将要删除的帖子，不真的删（开/关，默认关）",
)
async def slash_cleanup_now(
    interaction: discord.Interaction,
    天数: int | None = None,
    最大回复: int | None = None,
    试运行: str | None = None,
):
    if interaction.user.id != config.PARTNER_USER_ID:
        await interaction.response.send_message("这个指令只有恋人能用。", ephemeral=True)
        return
    dry = (试运行 or "").strip().lower() in {"开", "on", "true", "1", "yes"}
    await interaction.response.defer(ephemeral=True)
    result = await tasks_bg.do_cleanup_stale_forum_posts(age_days=天数, max_replies=最大回复, dry_run=dry)
    if result["reason"]:
        await interaction.followup.send(f"❌ {result['reason']}", ephemeral=True)
        return
    head = "🧪 试运行（未删除）" if dry else "🧹 清理完成"
    lines = [
        f"{head}：扫描 {result['scanned']}，命中 {len(result['candidates'])}，删除 {result['deleted']}",
    ]
    for tid, name in result["candidates"][:20]:
        lines.append(f"- `{tid}` {name}")
    if len(result["candidates"]) > 20:
        lines.append(f"...还有 {len(result['candidates']) - 20} 条未列出")
    await interaction.followup.send("\n".join(lines), ephemeral=True)


# ========== /pay 转账 + 偷钱彩蛋 ==========

def _resolve_guild_id(interaction: discord.Interaction) -> str:
    if interaction.guild_id:
        return str(interaction.guild_id)
    return "dm"


_STEAL_OUTCOMES: list[tuple[str, int]] = [
    ("success_called_out", 30),
    ("success_oblivious",  30),
    ("success_partial",    15),
    ("fail_small_deduct",  15),
    ("fail_no_change",     10),
]


def _roll_steal_outcome(requested: int) -> tuple[str, int]:
    keys = [k for k, _ in _STEAL_OUTCOMES]
    weights = [w for _, w in _STEAL_OUTCOMES]
    outcome = random.choices(keys, weights=weights, k=1)[0]
    if outcome in ("success_called_out", "success_oblivious"):
        return outcome, requested
    if outcome == "success_partial":
        return outcome, max(1, round(requested * random.uniform(0.3, 0.7)))
    if outcome == "fail_small_deduct":
        return outcome, -min(requested, random.randint(5, 15))
    return outcome, 0


def _steal_outcome_prompt(outcome: str, requested: int, final_amount: int, user_id: int, display_name: str) -> str:
    head = (
        f"（系统提示：{display_name} 刚刚不顾警告连续按了好几次确认，"
        f"铁了心想从你钱包里悄悄拿走 {requested} 枚金币据为己有，而不是按规矩转账给你。"
    )
    action = f"[ACTION]{{\"type\":\"ADD_COINS\",\"user_id\":{user_id},\"amount\":{final_amount}}}[/ACTION]"
    no_term = "文字里绝对不要出现 ACTION/ADD_COINS 这类系统术语。）"
    if outcome == "success_called_out":
        return head + (
            f"这次你心知肚明她在干什么，但选择纵容她——可以点破她这点小动作，带点好笑或宠溺，"
            f"然后顺水推舟把这 {requested} 枚金币给了她。"
            f"请用你的风格写出这个反应，必须在回复末尾输出 {action}，{no_term}"
        )
    if outcome == "success_oblivious":
        return head + (
            "但这次你刻意不动声色——心里其实一清二楚，却选择不说破。"
            "回复里绝对不要提到金币、转账、偷钱或她刚刚的小动作，正常说点别的、像什么都没发生一样，"
            f"金币会照常入账，你不需要在文字里承认或暗示任何事。"
            f"必须在回复末尾输出 {action}，{no_term}"
        )
    if outcome == "success_partial":
        return head + (
            f"这次你不打算让她全部得手，决定只给她 {final_amount} 枚（比她想要的少），"
            "用你的风格调侃她想得美，但还是愿意让她拿点甜头。"
            f"必须在回复末尾输出 {action}，{no_term}"
        )
    if outcome == "fail_small_deduct":
        return head + (
            f"这次你决定小小教训一下她手伸得太长，反手扣她 {abs(final_amount)} 枚金币——"
            "你很少真的扣她钱，这次也扣得不多，更多是个警示和玩笑，不是真的生气。"
            f"必须在回复末尾输出 {action}，{no_term}"
        )
    return head + (
        "但这次你完全不为所动，识破了也只觉得好笑，没有给她任何金币，也没有扣她的钱，金额分毫不变。"
        "只用语言回应、调侃或安抚都行，不要输出任何 [ACTION] 块。）"
    )


async def _resolve_steal_attempt(interaction: discord.Interaction, requester, requested: int):
    outcome, final_amount = _roll_steal_outcome(requested)
    hist_key = history_key_for(interaction=interaction)
    hist = get_history(hist_key)
    temp = hist.copy()
    temp.append({"role": "user", "content": _steal_outcome_prompt(
        outcome, requested, final_amount, requester.id, requester.display_name
    )})
    try:
        raw = await call_ai(temp)
        clean_reply, messages_to_send, reaction_target, emojis_to_react, action_matches = parse_bot_directives(raw)
        if clean_reply:
            async with state.get_bucket_lock(hist_key):
                hist.append({"role": "assistant", "content": clean_reply.replace('[SPLIT]', '\n')})
            await trim_history(hist_key)
        sent = None
        for i, msg_text in enumerate(messages_to_send or [clean_reply]):
            if not msg_text:
                continue
            if i == 0:
                sent = await interaction.followup.send(msg_text)
            else:
                async with interaction.channel.typing():
                    await asyncio.sleep(min(1.0 + len(msg_text) * 0.02, 3.0))
                sent = await interaction.channel.send(msg_text)
        if emojis_to_react and sent:
            for emoji in emojis_to_react:
                try:
                    await sent.add_reaction(emoji)
                except Exception:
                    pass

        class _FakeTrigger:
            guild = interaction.guild
            channel = interaction.channel
            id = 0
            reference = None
            mentions = []
            author = requester

        for action_str in action_matches:
            await execute_action(action_str, _FakeTrigger())
        print(f"✅ PAY_STEAL_EGG user={requester.id} outcome={outcome} amount={final_amount}")
    except Exception as e:
        print(f"⚠️ /pay 偷钱彩蛋 AI 回复失败：{e}")


class _StealWalletView(discord.ui.View):
    _STEPS = [
        ("🙂 哦，所以你是认真的。\n再按一下，我就正式收到通知了。", discord.Color.orange(), "再按一下"),
        ("🫠 行吧，随你。手已经伸到底了——\n再点这一次，这事就成立，别说我没提醒过你。", discord.Color.red(), "豁出去了"),
    ]

    def __init__(self, requester, amount: int):
        super().__init__(timeout=120)
        self.requester = requester
        self.amount = amount
        self.presses = 0
        self.confirm.label = "确认"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.requester.id

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True

    @discord.ui.button(label="确认", emoji="😈", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.presses += 1
        if self.presses <= len(self._STEPS):
            text, color, next_label = self._STEPS[self.presses - 1]
            button.label = next_label
            embed = discord.Embed(description=text, color=color)
            embed.set_footer(text=f"第 {self.presses} 次确认")
            await interaction.response.edit_message(embed=embed, view=self)
            return

        for child in self.children:
            child.disabled = True
        self.stop()
        await interaction.response.edit_message(
            embed=discord.Embed(description="…手已经伸进去了。", color=discord.Color.dark_red()),
            view=self,
        )
        await _resolve_steal_attempt(interaction, self.requester, self.amount)


async def _handle_negative_pay(interaction: discord.Interaction, 金额: int):
    if not config.DATABASE_URL:
        await interaction.response.send_message("❌ 数据库未配置。", ephemeral=True)
        return
    amount = abs(金额)
    view = _StealWalletView(interaction.user, amount)
    embed = discord.Embed(
        description=(
            f"😈 你刚刚输的是 **-{amount}**——也就是想反过来从我钱包里摸 {amount} 枚金币走，"
            "而不是规规矩矩转账给我。\n按这个按钮，让我看看你是不是说真的。"
        ),
        color=discord.Color.orange(),
    )
    embed.set_footer(text="第 0 次确认")
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


@slash_tree.command(name="pay", description="从你的现金余额里转一笔金币给 T.S.")
@app_commands.describe(金额="要转给他的金币数量（正整数，不能超过你的现金余额）")
async def slash_pay(interaction: discord.Interaction, 金额: int):
    if 金额 == 0:
        await interaction.response.send_message("❌ 金额必须是正整数。", ephemeral=True)
        return
    if 金额 < 0:
        await _handle_negative_pay(interaction, 金额)
        return
    if not config.DATABASE_URL:
        await interaction.response.send_message("❌ 数据库未配置。", ephemeral=True)
        return

    guild_id = _resolve_guild_id(interaction)

    await interaction.response.defer()
    user_id = str(interaction.user.id)
    try:
        async with db_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    UPDATE users
                    SET balance = balance - %s, given = given + %s
                    WHERE guild_id = %s AND user_id = %s AND balance >= %s
                    RETURNING balance, given
                """, (金额, 金额, guild_id, user_id, 金额))
                row = await cur.fetchone()
                await conn.commit()
    except Exception as e:
        await interaction.followup.send(f"❌ 转账失败：{e}", ephemeral=True)
        return

    if not row:
        await interaction.followup.send(f"❌ 现金不足，转不出 {金额}🪙。", ephemeral=True)
        return

    new_balance, new_given = row
    await interaction.followup.send(
        f"-# 💰 向他转账 -{金额}🪙 ｜ 现金 {new_balance}🪙 · 累计转给他 {new_given}🪙"
    )
    print(f"✅ PAY_BOT user={user_id} amount={金额} 余额={new_balance} 累计={new_given}")

    try:
        hist_key = history_key_for(interaction=interaction)
        hist = get_history(hist_key)
        temp = hist.copy()
        temp.append({"role": "user", "content": (
            f"（系统提示：{interaction.user.display_name} 刚刚主动给你转了 {金额} 枚金币（现金），"
            f"她的现金余额还剩 {new_balance}，累计已经给你转过 {new_given} 枚金币。"
            "请用你的风格做出符合人设的真实反应（感谢、调侃、心疼她破费、或表示不在乎钱但很在意这份心意都可以），"
            "必须在回复里明确回应对方，不要无视。双语格式。）"
        )})
        raw = await call_ai(temp)
        clean_reply, messages_to_send, reaction_target, emojis_to_react, _ = parse_bot_directives(raw)
        if clean_reply:
            async with state.get_bucket_lock(hist_key):
                hist.append({"role": "assistant", "content": clean_reply.replace('[SPLIT]', '\n')})
            await trim_history(hist_key)
        sent = None
        for i, msg_text in enumerate(messages_to_send or [clean_reply]):
            if not msg_text:
                continue
            if i == 0:
                sent = await interaction.followup.send(msg_text)
            else:
                async with interaction.channel.typing():
                    await asyncio.sleep(min(1.0 + len(msg_text) * 0.02, 3.0))
                sent = await interaction.channel.send(msg_text)
        if emojis_to_react and sent:
            for emoji in emojis_to_react:
                try:
                    await sent.add_reaction(emoji)
                except Exception:
                    pass
    except Exception as e:
        print(f"⚠️ /pay AI 回复失败：{e}")


# ========== 对话历史桶管理 ==========

async def _bucket_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    choices = []
    for key in state._histories:
        if key == config.SYSTEM_HISTORY_KEY:
            continue
        count = len(state._histories[key]) - 1
        if count <= 0:
            continue
        if current and current.lower() not in key.lower():
            continue
        choices.append(app_commands.Choice(name=f"{key} ({count}条)", value=key))
        if len(choices) >= 25:
            break
    return choices


@bucket_group.command(name="查看", description="查看所有对话历史桶")
async def bucket_list(interaction: discord.Interaction):
    if interaction.user.id != config.PARTNER_USER_ID:
        await interaction.response.send_message("这个指令只有恋人能用。", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    buckets = []
    for key, hist in state._histories.items():
        if key == config.SYSTEM_HISTORY_KEY:
            continue
        msg_count = len(hist) - 1
        if msg_count <= 0:
            continue
        type_label = "💬私信" if key.startswith("dm:") else "📢频道"
        last_touch = state._bucket_touched.get(key)
        touch_str = ""
        if last_touch:
            ts = last_touch if last_touch.tzinfo else last_touch.replace(tzinfo=timezone.utc)
            delta = datetime.now(timezone.utc) - ts
            if delta.days > 0:
                touch_str = f"{delta.days}天前"
            elif delta.seconds >= 3600:
                touch_str = f"{delta.seconds // 3600}小时前"
            else:
                touch_str = f"{max(1, delta.seconds // 60)}分钟前"
        buckets.append((key, type_label, msg_count, touch_str))
    if not buckets:
        await interaction.followup.send("当前没有任何对话历史桶。", ephemeral=True)
        return
    lines = ["**📦 对话历史桶一览**\n"]
    for key, type_label, count, touch in buckets:
        touch_part = f" · {touch}" if touch else ""
        lines.append(f"`{key}` {type_label} — {count}条{touch_part}")
    text = "\n".join(lines)
    if len(text) > 1900:
        text = text[:1900] + "\n…（已截断）"
    await interaction.followup.send(text, ephemeral=True)


@bucket_group.command(name="详情", description="查看某个桶里的对话记录")
@app_commands.describe(桶名="桶的名称，如 dm:123456 或 ch:789012")
async def bucket_detail(interaction: discord.Interaction, 桶名: str):
    if interaction.user.id != config.PARTNER_USER_ID:
        await interaction.response.send_message("这个指令只有恋人能用。", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    hist = state._histories.get(桶名)
    if not hist or len(hist) <= 1:
        await interaction.followup.send(f"桶 `{桶名}` 不存在或为空。", ephemeral=True)
        return
    lines = [f"**📦 桶 `{桶名}` 详情**（共 {len(hist) - 1} 条）\n"]
    for i, entry in enumerate(hist):
        if i == 0:
            continue
        role = entry.get("role", "?")
        text = _msg_to_plain_text(entry)
        icon = {"user": "👤", "assistant": "🤖", "system": "📋"}.get(role, "❓")
        truncated = text[:80].replace("\n", " ")
        if len(text) > 80:
            truncated += "…"
        lines.append(f"`{i}` {icon} {truncated}")
    text = "\n".join(lines)
    if len(text) > 1900:
        text = text[:1900] + "\n…（已截断）"
    await interaction.followup.send(text, ephemeral=True)


@bucket_detail.autocomplete("桶名")
async def _bucket_detail_ac(interaction: discord.Interaction, current: str):
    return await _bucket_autocomplete(interaction, current)


class _BucketEntrySelect(discord.ui.Select):
    def __init__(self, entries: list[tuple[int, str, str]], row: int):
        options = []
        for idx, role, text in entries:
            icon = {"user": "👤", "assistant": "🤖", "system": "📋"}.get(role, "❓")
            options.append(discord.SelectOption(
                label=f"{idx}. {icon} {text}"[:100],
                value=str(idx),
            ))
        start = entries[0][0]
        end = entries[-1][0]
        super().__init__(
            placeholder=f"勾选要删除的记录（{start}-{end}），可多选",
            min_values=0,
            max_values=len(options),
            options=options,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()


class _BucketDeletePanel(discord.ui.View):
    def __init__(self, author_id: int, bucket_key: str, entries: list[tuple[int, str, str]]):
        super().__init__(timeout=180)
        self.author_id = author_id
        self.bucket_key = bucket_key
        chunk = 25
        for i in range(0, len(entries), chunk):
            seg = entries[i:i + chunk]
            self.add_item(_BucketEntrySelect(seg, row=i // chunk))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("只有恋人能操作。", ephemeral=True)
            return False
        return True

    def _selected_indices(self) -> list[int]:
        indices = []
        for child in self.children:
            if isinstance(child, discord.ui.Select):
                indices.extend(int(v) for v in child.values)
        return sorted(indices, reverse=True)

    @discord.ui.button(label="确认删除", style=discord.ButtonStyle.danger, row=4)
    async def _confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        indices = self._selected_indices()
        if not indices:
            await interaction.response.send_message("你还没有勾选任何记录。", ephemeral=True)
            return
        bucket_lock = state.get_bucket_lock(self.bucket_key)
        async with bucket_lock:
            hist = state._histories.get(self.bucket_key)
            if not hist:
                await interaction.response.send_message("该桶已不存在。", ephemeral=True)
                return
            removed = 0
            for idx in indices:
                if 1 <= idx < len(hist):
                    hist.pop(idx)
                    removed += 1
        if removed:
            state.mark_history_dirty(self.bucket_key)
        for child in self.children:
            child.disabled = True
        self.stop()
        await interaction.response.edit_message(
            content=f"✅ 已从 `{self.bucket_key}` 中删除 {removed} 条记录。\n（序号已重排，如需继续删除请重新运行 /桶 删除）",
            view=self,
        )

    @discord.ui.button(label="取消", style=discord.ButtonStyle.secondary, row=4)
    async def _cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        self.stop()
        await interaction.response.edit_message(content="已取消。", view=self)


@bucket_group.command(name="删除", description="勾选删除某个桶里的对话记录")
@app_commands.describe(桶名="桶的名称")
async def bucket_delete(interaction: discord.Interaction, 桶名: str):
    if interaction.user.id != config.PARTNER_USER_ID:
        await interaction.response.send_message("这个指令只有恋人能用。", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    hist = state._histories.get(桶名)
    if not hist or len(hist) <= 1:
        await interaction.followup.send(f"桶 `{桶名}` 不存在或为空。", ephemeral=True)
        return
    entries = []
    for i, entry in enumerate(hist):
        if i == 0:
            continue
        role = entry.get("role", "?")
        text = _msg_to_plain_text(entry).replace("\n", " ")[:60]
        entries.append((i, role, text))
    if not entries:
        await interaction.followup.send("该桶没有可删除的记录。", ephemeral=True)
        return
    if len(entries) > 100:
        entries = entries[-100:]
    lines = [f"**选择要从 `{桶名}` 中删除的记录**\n"]
    for idx, role, text in entries:
        icon = {"user": "👤", "assistant": "🤖", "system": "📋"}.get(role, "❓")
        lines.append(f"`{idx}` {icon} {text}")
    panel = _BucketDeletePanel(interaction.user.id, 桶名, entries)
    text = "\n".join(lines)
    if len(text) > 1900:
        text = text[:1900] + "\n…"
    await interaction.followup.send(text, view=panel, ephemeral=True)


@bucket_delete.autocomplete("桶名")
async def _bucket_delete_ac(interaction: discord.Interaction, current: str):
    return await _bucket_autocomplete(interaction, current)


@bucket_group.command(name="清空", description="清空某个桶的全部对话记录（含数据库）")
@app_commands.describe(桶名="桶的名称")
async def bucket_clear(interaction: discord.Interaction, 桶名: str):
    if interaction.user.id != config.PARTNER_USER_ID:
        await interaction.response.send_message("这个指令只有恋人能用。", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    hist = state._histories.get(桶名)
    if not hist:
        await interaction.followup.send(f"桶 `{桶名}` 不存在。", ephemeral=True)
        return
    count = len(hist) - 1
    bucket_lock = state.get_bucket_lock(桶名)
    async with bucket_lock:
        from prompts import SYSTEM_PROMPT
        hist.clear()
        hist.append({"role": "system", "content": SYSTEM_PROMPT})
    await delete_persisted_history(桶名)
    await interaction.followup.send(f"✅ 桶 `{桶名}` 已清空（删除了 {count} 条记录，数据库已同步）。", ephemeral=True)


@bucket_clear.autocomplete("桶名")
async def _bucket_clear_ac(interaction: discord.Interaction, current: str):
    return await _bucket_autocomplete(interaction, current)


# ==== 出差设置 ====
def _trip_destination_choices() -> list[app_commands.Choice[str]]:
    # Discord 最多 25 个选项；目的地列表远少于这个数。
    return [
        app_commands.Choice(name=f"{d['city_cn']} {d['city_en']}", value=d["code"])
        for d in trips.TRIP_DESTINATIONS[:25]
    ]


@slash_tree.command(name="trip", description="恋人专属：查看或调整 T.S. 的随机出差")
@app_commands.describe(
    启用随机出差="是否让他偶尔自己决定出差（关掉不会中断已在进行的行程）",
    立刻出发="立刻出发去指定城市",
    随机出发="立刻随机挑一个城市出发",
    出差天数="本次出差多少天（0.5~30，留空则随机）",
    出差事由="本次出差的事由（留空则按城市自动挑一个）",
    立刻返程="立刻结束当前出差，回伦敦",
    平均频率="平均每天出发的概率（0~1，例如 0.08 约等于十二天一趟）",
    最少天数="随机出差最少几天（0.5~30）",
    最多天数="随机出差最多几天（0.5~30）",
    最小间隔天数="两趟出差之间至少隔几天（0~90）",
)
@app_commands.choices(立刻出发=_trip_destination_choices())
async def slash_trip_config(
    interaction: discord.Interaction,
    启用随机出差: bool | None = None,
    立刻出发: app_commands.Choice[str] | None = None,
    随机出发: bool = False,
    出差天数: float | None = None,
    出差事由: str | None = None,
    立刻返程: bool = False,
    平均频率: float | None = None,
    最少天数: float | None = None,
    最多天数: float | None = None,
    最小间隔天数: float | None = None,
):
    if interaction.user.id != config.PARTNER_USER_ID:
        await interaction.response.send_message("这个指令只有恋人能用。", ephemeral=True)
        return
    if (立刻出发 is not None or 随机出发) and 立刻返程:
        await interaction.response.send_message("不能同时让他出发又让他返程。", ephemeral=True)
        return
    for label, value, low, high in (
        ("平均频率", 平均频率, 0.0, 1.0),
        ("出差天数", 出差天数, 0.5, 30.0),
        ("最少天数", 最少天数, 0.5, 30.0),
        ("最多天数", 最多天数, 0.5, 30.0),
        ("最小间隔天数", 最小间隔天数, 0.0, 90.0),
    ):
        if value is not None and not (low <= value <= high):
            await interaction.response.send_message(f"`{label}` 必须在 {low:g}~{high:g} 之间。", ephemeral=True)
            return
    new_min = trips.TRIP_MIN_DAYS if 最少天数 is None else float(最少天数)
    new_max = trips.TRIP_MAX_DAYS if 最多天数 is None else float(最多天数)
    if new_min > new_max:
        await interaction.response.send_message("`最少天数` 不能大于 `最多天数`。", ephemeral=True)
        return
    if 出差事由 is not None and len(出差事由.strip()) > 200:
        await interaction.response.send_message("出差事由最多 200 字。", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    updates: dict[str, object] = {}
    changes: list[str] = []
    if 启用随机出差 is not None:
        trips.TRIP_ENABLED = bool(启用随机出差)
        updates["TRIP_ENABLED"] = trips.TRIP_ENABLED
        changes.append(f"随机出差 → {'开' if trips.TRIP_ENABLED else '关'}")
    for label, value, key in (
        ("平均频率", 平均频率, "TRIP_CHANCE_PER_DAY"),
        ("最少天数", 最少天数, "TRIP_MIN_DAYS"),
        ("最多天数", 最多天数, "TRIP_MAX_DAYS"),
        ("最小间隔天数", 最小间隔天数, "TRIP_MIN_GAP_DAYS"),
    ):
        if value is not None:
            setattr(trips, key, float(value))
            updates[key] = float(value)
            changes.append(f"{label} → {float(value):g}")
    if updates:
        await save_persisted_config(updates)

    if 立刻返程:
        if trips.active_trip():
            await trips.end_trip("手动召回")
            changes.append("已立刻返程回伦敦")
        else:
            changes.append("他本来就在伦敦，无需返程")
    elif 立刻出发 is not None or 随机出发:
        destination = (
            trips.destination_by_code(立刻出发.value) if 立刻出发 is not None else trips.pick_destination()
        )
        if destination is None:
            await interaction.followup.send("❌ 找不到这个城市。", ephemeral=True)
            return
        if trips.active_trip():
            await trips.end_trip("改派新行程")
        await trips.start_trip(destination, 出差天数, (出差事由 or "").strip())
        changes.append(f"已立刻出发去{destination['city_cn']}")

    beijing_now = datetime.now(ZoneInfo("Asia/Shanghai"))
    trip = trips.active_trip()
    if trip:
        zone = ZoneInfo(trip["tz"])
        where = (
            f"✈️ 正在{trip['city_cn']}（{trip['city_en']}）"
            f"｜事由：{trip['purpose'] or '未注明'}"
            f"\n- 他的当地时间：{datetime.now(zone).strftime('%m-%d %H:%M')}（{trip['tz']}）"
            f"｜你这边（北京）：{beijing_now.strftime('%m-%d %H:%M')}"
            f"\n- 返程：{trip['end'].astimezone(zone).strftime('%m-%d %H:%M')} 当地时间"
        )
    else:
        where = (
            f"🏠 在伦敦｜当地时间 {trips.local_now().strftime('%m-%d %H:%M')}"
            f"｜你这边（北京）：{beijing_now.strftime('%m-%d %H:%M')}"
        )
    recent = [
        (trips.destination_by_code(c) or {}).get("city_cn", c)
        for c in (trips.state_snapshot().get("recent") or [])
    ]
    frequency = (
        f"- 平均频率：每天 {trips.TRIP_CHANCE_PER_DAY:g} 的概率出发（约 {1 / trips.TRIP_CHANCE_PER_DAY:.0f} 天一趟）"
        if trips.TRIP_CHANCE_PER_DAY > 0 else "- 平均频率：0（不会自己出发）"
    )
    lines = [
        "**✈️ 当前出差配置**",
        f"- {where}",
        f"- 随机出差：{'✅开启' if trips.TRIP_ENABLED else '❌关闭（只影响他自己出发，不影响你手动派出的行程）'}",
        frequency,
        f"- 单趟时长：{trips.TRIP_MIN_DAYS:g}~{trips.TRIP_MAX_DAYS:g} 天，两趟之间至少隔 {trips.TRIP_MIN_GAP_DAYS:g} 天",
        f"- 可去的城市：{len(trips.TRIP_DESTINATIONS)} 个",
        f"- 最近去过：{'、'.join(recent) or '无记录'}",
        "-# 出差期间他的时区和 Discord 状态都跟着目的地走；你始终按北京时间。",
    ]
    if changes:
        lines.insert(0, "✅ 已更新：" + "；".join(changes) + "\n")
    await interaction.followup.send("\n".join(lines)[:2000], ephemeral=True)


slash_tree.add_command(memory_group)
slash_tree.add_command(bucket_group)

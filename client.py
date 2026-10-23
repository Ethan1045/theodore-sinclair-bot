"""Discord client 与 slash command tree 的唯一创建点。

其他模块通过 `from client import discord_client, slash_tree` 获取引用。
本模块不导入任何内部模块，避免循环依赖。
"""
import discord
from discord import app_commands

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True
intents.reactions = True
intents.presences = True
intents.typing = True
intents.polls = True

class TheodoreClient(discord.Client):
    async def close(self) -> None:
        try:
            from history import flush_dirty_histories

            await flush_dirty_histories()
        except Exception as exc:
            print(f"⚠️ 关闭前保存对话历史失败: {exc}")
        await super().close()


discord_client = TheodoreClient(intents=intents)
slash_tree = app_commands.CommandTree(discord_client)

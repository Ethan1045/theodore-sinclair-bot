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

discord_client = discord.Client(intents=intents)
slash_tree = app_commands.CommandTree(discord_client)

"""Discord bot 入口：加载模块注册事件/命令，启动健康服务，运行客户端。"""
import asyncio
import os

import discord

import config
import events      # noqa: F401 — registers @discord_client.event handlers
import slash_cmds  # noqa: F401 — registers @slash_tree.command handlers
from client import discord_client


async def start_health_server():
    """回复 $PORT 上的健康检查，让 Zeabur/Railway 等平台认定服务已就绪。"""
    port = os.getenv("PORT")
    if not port:
        return
    try:
        from aiohttp import web
    except Exception as e:
        print(f"⚠️ 加载 aiohttp 失败，跳过健康检查服务: {e}")
        return

    async def _ok(_request):
        return web.Response(text="ok")

    try:
        app = web.Application()
        app.router.add_get("/", _ok)
        app.router.add_get("/health", _ok)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", int(port))
        await site.start()
        print(f"✅ 健康检查服务已监听 0.0.0.0:{port}")
    except Exception as e:
        print(f"⚠️ 启动健康检查服务失败: {e}")


async def run_bot():
    await start_health_server()
    retry_delay = 60
    while True:
        try:
            await discord_client.start(config.DISCORD_TOKEN)
        except discord.errors.HTTPException as e:
            if e.status == 429:
                print(f"⚠️ 被 Discord rate limit，{retry_delay} 秒后重试...")
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 600)
            else:
                raise
        except Exception as e:
            print(f"❌ 未知错误: {e}")
            await asyncio.sleep(30)


asyncio.run(run_bot())

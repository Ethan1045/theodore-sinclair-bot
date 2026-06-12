"""Postgres 连接池 + 兼容旧调用风格的 db_acquire()。

"""
import contextlib
import psycopg
from psycopg_pool import AsyncConnectionPool

import config  # config 无内部依赖，可安全 import，不会形成环。

# 可由 configure() 覆盖；为空时回落到 config.DATABASE_URL。

DATABASE_URL: str = ""

db_pool: "AsyncConnectionPool | None" = None


def configure(database_url: str) -> None:
    """可选：显式覆盖连接串。不调用时默认使用 config.DATABASE_URL。"""
    global DATABASE_URL
    DATABASE_URL = database_url or ""


def _resolve_url() -> str:
    """统一解析连接串：优先 configure() 注入的值，否则回落 config。"""
    return DATABASE_URL or config.DATABASE_URL or ""


async def init_db_pool():
    global db_pool
    url = _resolve_url()
    if not url or db_pool is not None:
        return
    try:
        db_pool = AsyncConnectionPool(
            conninfo=url,
            min_size=1,
            max_size=8,
            timeout=10,
            kwargs={"autocommit": False},
            open=False,
        )
        await db_pool.open()
        print("✅ Postgres 连接池已就绪")
    except Exception as e:
        db_pool = None
        print(f"⚠️ Postgres 连接池初始化失败，回落到逐次连接: {e}")


class _PooledConnWrapper:
    """
    模拟 psycopg.AsyncConnection 的 close() 协议，但实际把连接归还池子。
    用于保持现有 `conn = await connect; ...; await conn.close()` 调用风格不变。
    """
    def __init__(self, cm, conn):
        self._cm = cm
        self._conn = conn

    def __getattr__(self, item):
        return getattr(self._conn, item)

    async def close(self):
        try:
            await self._cm.__aexit__(None, None, None)
        except Exception as e:
            print(f"⚠️ 归还连接到连接池失败: {e}")

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()


async def db_acquire():
    """统一获取一个 Postgres 连接：优先池子，没池就临时拨号。

    旧调用方式：`conn = await db_acquire(); ...; await conn.close()`。
    新调用方式（推荐，避免异常路径泄漏）：`async with db_conn() as conn: ...`。
    """
    if db_pool is not None:
        cm = db_pool.connection()
        conn = await cm.__aenter__()
        return _PooledConnWrapper(cm, conn)
    url = _resolve_url()
    if not url:
        raise RuntimeError(
            "DATABASE_URL 未配置：无法建立 Postgres 连接。"
            "请设置环境变量 DATABASE_URL（或在 secrets 中提供）。"
        )
    return await psycopg.AsyncConnection.connect(url)


@contextlib.asynccontextmanager
async def db_conn():
    """统一上下文管理器：始终在退出时归还连接，异常路径也不漏。"""
    conn = await db_acquire()
    try:
        yield conn
    finally:
        try:
            await conn.close()
        except Exception as e:
            print(f"⚠️ 关闭/归还连接失败: {e}")

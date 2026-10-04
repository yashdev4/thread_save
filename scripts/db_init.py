import asyncio
import asyncpg

async def init_test_db():
    sys_conn = await asyncpg.connect("postgresql://postgres:@127.0.0.1:5432/postgres")
    exists = await sys_conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", "thread_save_test")
    if not exists:
        await sys_conn.execute("CREATE DATABASE thread_save_test")
        print("Created database thread_save_test")
    else:
        print("Database thread_save_test exists")
    db_conn = await asyncpg.connect("postgresql://postgres:@127.0.0.1:5432/thread_save_test")
    rows = await db_conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    print("Tables in thread_save_test:", [r["tablename"] for r in rows])
    await db_conn.close()

if __name__ == "__main__":
    asyncio.run(init_test_db())


# 生成示例 SQLite：模拟 App 本地库（资源位表 + 接口缓存表）
import os
import sqlite3

here = os.path.dirname(os.path.abspath(__file__))
db = os.path.join(here, "app.db")
if os.path.exists(db):
    os.remove(db)
conn = sqlite3.connect(db)
conn.executescript("""
CREATE TABLE res_plan(res_id TEXT, plan_id TEXT, plan_name TEXT, card_id TEXT, card_type TEXT);
CREATE TABLE api_cache(cache_key TEXT, content TEXT, updated_at INTEGER);
""")
conn.executemany("INSERT INTO res_plan VALUES (?,?,?,?,?)", [
    ("829731", "112729", "20260408-我的顶部氛围投放", "a_47jc0", "large_normal"),
    ("6065", "4754", "20260408-我的页运营位", "a_tuni", "large_normal"),
])
with open(os.path.join(here, "api_dump", "mine_page.json"), encoding="utf-8") as f:
    conn.execute("INSERT INTO api_cache VALUES ('mine_page', ?, 1763568000)", (f.read(),))
conn.commit()
conn.close()
print("ok ->", db)

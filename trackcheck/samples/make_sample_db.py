# 生成示例 SQLite：模拟 App 本地库（资源位表 + 接口缓存表 + 历史记录表）
# 输出到 samples/datasources/app.db —— 数据源目录里的文件会自动成为数据源（名字 = app）
import os
import sqlite3

here = os.path.dirname(os.path.abspath(__file__))
ds = os.path.join(here, "datasources")
db = os.path.join(ds, "app.db")
if os.path.exists(db):
    os.remove(db)
conn = sqlite3.connect(db)
conn.executescript("""
CREATE TABLE res_plan(res_id TEXT, plan_id TEXT, plan_name TEXT, card_id TEXT, card_type TEXT);
CREATE TABLE api_cache(cache_key TEXT, content TEXT, updated_at INTEGER);
CREATE TABLE history(id INTEGER PRIMARY KEY, type TEXT, title TEXT, updated_at INTEGER);
""")
conn.executemany("INSERT INTO res_plan VALUES (?,?,?,?,?)", [
    ("829731", "112729", "20260408-我的顶部氛围投放", "a_47jc0", "large_normal"),
    ("6065", "4754", "20260408-我的页运营位", "a_tuni", "large_normal"),
])
with open(os.path.join(ds, "mine_page.json"), encoding="utf-8") as f:
    conn.execute("INSERT INTO api_cache VALUES ('mine_page', ?, 1763568000)", (f.read(),))
# 测试结束时拉下来的 DB：小说 4、网页 2、视频 1，共 7 条
conn.executemany("INSERT INTO history(type, title, updated_at) VALUES (?,?,?)", [
    ("novel", "摇曳露营", 1), ("novel", "三体", 2), ("novel", "活着", 3), ("novel", "围城", 9),
    ("web", "android.com", 4), ("web", "github.com", 5), ("video", "露营 vlog", 6),
])
conn.commit()
conn.close()
print("ok ->", db)

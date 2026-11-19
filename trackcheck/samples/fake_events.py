# 模拟 App 打埋点：把 session.log 里的埋点行通过 `adb shell log` 写进手机 logcat，用来测试 live / run
# python trackcheck.py run --spec samples/trackcheck.yaml --case 我的页卡片 -- python samples/fake_events.py
import os
import re
import shlex
import subprocess
import sys
import time

here = os.path.dirname(os.path.abspath(__file__))
serial = sys.argv[1] if len(sys.argv) > 1 else None
adb = ["adb"] + (["-s", serial] if serial else [])
with open(os.path.join(here, "session.log"), encoding="utf-8") as f:
    for line in f:
        m = re.search(r"(Event: id: .*)$", line.rstrip("\n"))
        if not m:
            continue
        subprocess.run(adb + ["shell", "log", "-t", "Tracker", shlex.quote(m.group(1))], check=True)
        print("sent:", m.group(1)[:60], "...")
        time.sleep(0.3)

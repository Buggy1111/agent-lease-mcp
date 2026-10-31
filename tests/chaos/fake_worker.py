"""
Falešný „agent“ pro chaos test. Zaloguje start (čas, PID), pracuje 1–8 s, někdy
přes vnuka (vedoucí skončí dřív, vnuk zapíše efekt), někdy narazí na limit.
EFEKT = nevratná akce (řádek v logu). Každý úkol smí mít nejvýš jeden efekt.
Píše finální odpověď do {last_message} jako Codex.
"""
import os
import random
import re
import subprocess
import sys
import time
from pathlib import Path

last_message, prompt = sys.argv[-2], sys.argv[-1]
mid = re.search(r"\(#(\d+)\)", prompt).group(1)
log = os.environ["CHAOS_LOG"]


def write(kind: str) -> None:
    starttime = Path("/proc/self/stat").read_text().rpartition(") ")[2].split()[19]
    with open(log, "a") as fh:
        fh.write(f"{kind} {mid} {os.getpid()} {time.time():.6f} {starttime}\n")


write("start")
roll = random.random()
if roll < 0.08:
    write("limit")
    print("Usage limit reached, try again in 1 minutes")
    sys.exit(1)
if roll < 0.30:
    # vedoucí spustí vnuka, který efekt zapíše až po jeho konci
    code = ("import os,sys,time;time.sleep(float(sys.argv[3]));"
            "start=open('/proc/self/stat').read().rpartition(') ')[2].split()[19];"
            "open(sys.argv[1],'a').write("
            "f'effect {sys.argv[2]} {os.getpid()} {time.time():.6f} {start}\\n')")
    child = subprocess.Popen([sys.executable, "-c", code, log, mid,
                              str(random.uniform(0.5, 3))])
    try:
        starttime = Path(f"/proc/{child.pid}/stat").read_text().rpartition(") ")[2].split()[19]
    except FileNotFoundError:
        starttime = "0"
    with open(log, "a") as fh:
        fh.write(f"spawn {mid} {child.pid} {time.time():.6f} {starttime}\n")
    child.wait()
else:
    time.sleep(random.uniform(1, 8))
    write("effect")
if not last_message.endswith(".txt"):
    sys.exit(2)          # pojistka: nikdy nepsat do jiného souboru než výsledkového
with open(last_message, "w") as fh:
    fh.write(f"hotovo {mid}")

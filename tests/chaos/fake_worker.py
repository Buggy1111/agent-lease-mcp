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

last_message, prompt = sys.argv[-2], sys.argv[-1]
mid = re.search(r"\(#(\d+)\)", prompt).group(1)
log = os.environ["CHAOS_LOG"]


def write(kind: str) -> None:
    with open(log, "a") as fh:
        fh.write(f"{kind} {mid} {os.getpid()} {time.time():.3f}\n")


write("start")
roll = random.random()
if roll < 0.08:
    print("Usage limit reached, try again in 1 minutes")
    sys.exit(1)
if roll < 0.30:
    # vedoucí spustí vnuka, který efekt zapíše až po jeho konci
    code = ("import os,sys,time;time.sleep(%f);"
            "open(sys.argv[1],'a').write('effect %s %%d %%.3f\\n' %% (os.getpid(), time.time()))"
            % (random.uniform(0.5, 3), mid))
    child = subprocess.Popen([sys.executable, "-c", code, log])
    with open(log, "a") as fh:
        fh.write(f"spawn {mid} {child.pid} {time.time():.3f}\n")
    child.wait()
else:
    time.sleep(random.uniform(1, 8))
    write("effect")
if not last_message.endswith(".txt"):
    sys.exit(2)          # pojistka: nikdy nepsat do jiného souboru než výsledkového
with open(last_message, "w") as fh:
    fh.write(f"hotovo {mid}")

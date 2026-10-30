"""Falešný „agent“ pro chaos test: zaloguje start, chvíli pracuje, zapíše EFEKT.
Efekt = nevratná akce (zápis do souboru). Každý úkol smí mít nejvýš jeden efekt."""
import os
import random
import re
import sys
import time

prompt = sys.argv[-1]
mid = re.search(r"\(#(\d+)\)", prompt).group(1)
log = os.environ["CHAOS_LOG"]
with open(log, "a") as fh:
    fh.write(f"start {mid} {os.getpid()}\n")
time.sleep(random.uniform(0.5, 4.0))
if random.random() < 0.08:
    print("Usage limit reached, try again in 1 minutes")
    sys.exit(1)
with open(log, "a") as fh:
    fh.write(f"effect {mid} {os.getpid()}\n")
print(f"hotovo {mid}")

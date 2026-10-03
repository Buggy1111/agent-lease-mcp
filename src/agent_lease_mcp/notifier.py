"""
Upozornění na telefon (Telegram), když je potřeba člověk.

Posílá se jen to, co vyžaduje pozornost, a každá událost nejvýše jednou:
- zpráva/úkol, který nikdo nepotvrdil déle než N sekund,
- úkol přesunutý kvůli limitu agenta, nově nahlášený limit,
- zpráva zadržená screeningem,
- úkol ve stavu needs_review / failed / dead_letter.

Token a chat ID jen z prostředí (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`).
Bez nich `--dry` vypíše, co by se poslalo. Selhání odeslání nezablokuje nic a
událost se neoznačí jako odeslaná, takže se zkusí znovu.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from .broker import open_store
from .config import Settings
from .hygiene import redact


def collect(store, *, overdue_seconds: int = 300) -> list[tuple[str, str]]:
    """(klíč, text) čekajících upozornění. Čistě čtení + dedupe tabulka."""
    out: list[tuple[str, str]] = []
    for m in store.overdue(overdue_seconds):
        text = (f"⏳ {m['to']} už {m['waiting_seconds'] // 60} min nepotvrdil #{m['id']} "
                f"od {m['from']} ({m['problem']}): {m['text'][:80]}")
        out.append((f"overdue:{m['id']}", text))
    for a in store.recent_audit(("failover", "limit")):
        icon = "↪" if a["action"] == "failover" else "⏸"
        out.append((f"audit:{a['id']}", f"{icon} {a['agent']}: {a['action']} {a['detail']}"))
    for m in store.latest_messages(limit=200):
        if m["agent"] == "broker" and m["recipient"] == "michal" and "zadržena" in m["text"]:
            out.append((f"quarantine:{m['id']}", m["text"]))
    for r in store.needs_review_list():
        out.append((f"review:{r['id']}:{int(r['updated_at'])}",
                    f"🔎 #{r['id']} pro {r['to']} ke kontrole: {r['error'] or r['text'][:80]}"))
    return out


def send_telegram(token: str, chat_id: str, text: str, *, base: str = "https://api.telegram.org",
                  timeout: float = 10.0) -> bool:
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": redact(text)[:3500]}).encode()
    req = urllib.request.Request(f"{base}/bot{token}/sendMessage", data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return bool(json.load(resp).get("ok"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return False


def run_once(store, *, overdue_seconds: int, dry: bool, sender=send_telegram) -> int:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN", ""), os.environ.get("TELEGRAM_CHAT_ID", "")
    sent = 0
    for key, text in collect(store, overdue_seconds=overdue_seconds):
        if dry:
            print(f"[dry] {key}: {text}")
            continue
        if not (token and chat):
            print("Chybí TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID (zkus --dry).", file=sys.stderr)
            return 2
        if not store.mark_notified(key):
            continue  # už odesláno
        if sender(token, chat, text):
            sent += 1
        else:
            # nepovedlo se → ať se zkusí příště (klíč odznačíme přímým smazáním)
            store.unmark_notified(key)
    return sent


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="agent-lease-notify")
    p.add_argument("--dry", action="store_true")
    p.add_argument("--loop", action="store_true", help="běžet pořád (každých 60 s)")
    p.add_argument("--overdue", type=int, default=300, help="po kolika s nepotvrzení upozornit")
    args = p.parse_args(argv)
    store = open_store(Settings.from_env())
    if not args.loop:
        res = run_once(store, overdue_seconds=args.overdue, dry=args.dry)
        return res if res == 2 else 0
    while True:
        run_once(store, overdue_seconds=args.overdue, dry=args.dry)
        time.sleep(60)


if __name__ == "__main__":
    sys.exit(main())

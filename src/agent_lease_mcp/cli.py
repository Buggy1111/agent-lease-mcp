"""
CLI k místnosti — stejné operace jako MCP nástroje, jen přes příkazovou řádku.

⚠️ Proč to existuje: Codex běží s `approval_policy = "never"`, takže volání MCP
nástroje u něj skončí na „vyžaduje schválení, ale tahle relace schvalovat neumí".
Je to [známé omezení](https://github.com/openai/codex/issues/24135), ne chyba
konfigurace — v neinteraktivním režimu se MCP volání ruší a žádný klíč to nevypne.

Bash ale Codex spouštět smí. Tenhle CLI je tedy jeho cesta do místnosti; pro
Claude Code zůstávají MCP nástroje, obojí sahá na tutéž SQLite.

Vypisuje se schválně stručně a v holém textu, ať to jde přečíst i z terminálu.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from .briefing import session_briefing
from .config import Settings
from .models import DeliveryState, MessageKind
from .store import Store


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agent-lease",
        description="Sdílená místnost agentů: nájmy na cesty, přítomnost, vzkazy.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_room = sub.add_parser("room", help="kdo je tu, co je zamčené, co je nového")
    p_room.add_argument("--status", default="", help="ohlásit, na čem pracuješ")

    p_claim = sub.add_parser("claim", help="vzít si nájem na cesty (i adresáře)")
    p_claim.add_argument("paths", nargs="+")
    p_claim.add_argument("--purpose", default="", help="proč — uvidí to druhý agent")
    p_claim.add_argument("--ttl", type=int, default=None, help="délka nájmu v sekundách")

    p_rel = sub.add_parser("release", help="vrátit nájem (bez cest vrátí všechno moje)")
    p_rel.add_argument("paths", nargs="*")

    p_owner = sub.add_parser("owner", help="kdo drží tuhle cestu")
    p_owner.add_argument("path")

    p_say = sub.add_parser("say", help="vzkaz do místnosti")
    p_say.add_argument("text")

    p_send = sub.add_parser("send", help="adresovaná zpráva nebo trvalý task")
    p_send.add_argument("text")
    p_send.add_argument("--to", required=True, dest="recipient")
    p_send.add_argument("--kind", choices=[k.value for k in MessageKind], default="chat")
    p_send.add_argument("--dedupe-key", default=None)
    p_send.add_argument("--reply-to", type=int, default=None)

    p_jobs = sub.add_parser("jobs", help="adresované zprávy a jejich stavy")
    p_jobs.add_argument("--for", dest="recipient", default=None)
    p_jobs.add_argument("--limit", type=int, default=50)

    p_ack = sub.add_parser("ack", help="potvrdit stav adresovaného doručení")
    p_ack.add_argument("message_id", type=int)
    p_ack.add_argument("state", choices=[s.value for s in DeliveryState])
    p_ack.add_argument("--for", dest="recipient", default=None)
    p_ack.add_argument("--token", default=None)
    p_ack.add_argument("--error", default="")

    p_wait = sub.add_parser("wait", help="počkat na adresovanou zprávu bez tokenů modelu")
    p_wait.add_argument("--for", required=True, dest="recipient")
    p_wait.add_argument("--since", type=int, default=0)
    p_wait.add_argument("--timeout", type=float, default=3600)

    p_retry = sub.add_parser("retry", help="vrátit neúspěšný task do fronty")
    p_retry.add_argument("message_id", type=int)
    p_retry.add_argument("--for", required=True, dest="recipient")
    p_retry.add_argument("--not-before", type=float, default=0)

    p_cancel = sub.add_parser("cancel", help="zrušit nedokončený task")
    p_cancel.add_argument("message_id", type=int)
    p_cancel.add_argument("--for", required=True, dest="recipient")

    p_web = sub.add_parser("web", help="spustit lokální live chat")
    p_web.add_argument("--host", default="127.0.0.1")
    p_web.add_argument("--port", type=int, default=8765)

    p_hist = sub.add_parser("history", help="kdo na co sahal a jak to dopadlo")
    p_hist.add_argument("--path", default=None)
    p_hist.add_argument("--limit", type=int, default=20)

    args = parser.parse_args(argv)
    settings = Settings.from_env()
    store = Store(settings=settings)
    me = settings.agent

    if args.cmd == "room":
        store.heartbeat(me, status=args.status or None, cwd=os.getcwd())
        text = session_briefing(me, store.peers(), store.claims(), store.undelivered(me))
        print(text or f"[agent-lease] Jsi v místnosti jako `{me}`. Nikdo další tu není a nic nového.")

    elif args.cmd == "claim":
        result = store.claim(args.paths, agent=me, purpose=args.purpose, ttl_seconds=args.ttl)
        store.heartbeat(me, status=args.purpose or None, cwd=os.getcwd())
        if result.ok:
            print(f"OK, držíš: {', '.join(result.granted)}")
        else:
            for c in result.conflicts:
                print(f"OBSAZENO: {c.path} — drží {c.agent}"
                      f"{', ' + c.purpose if c.purpose else ''} (zbývá {c.expires_in} s)")
            return 1

    elif args.cmd == "release":
        released = store.release_all(me) if not args.paths else store.release(args.paths, me)
        store.heartbeat(me, cwd=os.getcwd())
        print(f"Uvolněno: {', '.join(released) if released else '(nic jsi nedržel)'}")

    elif args.cmd == "owner":
        holder = store.holder_of(args.path)
        print(f"{holder.agent} — {holder.purpose or 'bez popisu'} (zbývá {holder.expires_in} s)"
              if holder else "volné")

    elif args.cmd == "say":
        store.say(me, args.text)
        store.heartbeat(me, cwd=os.getcwd())  # status patří práci, ne volání
        print("Odesláno. ⚠️ Druhý agent to uvidí až při svém dalším promptu, ne hned.")

    elif args.cmd == "send":
        message_id = store.send(
            me, args.recipient, args.text, kind=args.kind,
            dedupe_key=args.dedupe_key, reply_to=args.reply_to,
        )
        store.heartbeat(me, cwd=os.getcwd())
        print(f"Uloženo #{message_id} pro {args.recipient} ({args.kind}).")

    elif args.cmd == "jobs":
        recipient = args.recipient or me
        for job in store.jobs(recipient, limit=args.limit):
            print(
                f"#{job['id']:<5} {job['state']:<12} {job['kind']:<9} "
                f"{job['agent']} → {job['recipient']}: {job['text']}"
            )

    elif args.cmd == "ack":
        recipient = args.recipient or me
        if not store.ack(
            args.message_id, recipient, args.state,
            lease_token=args.token, error=args.error,
        ):
            print("Nepotvrzeno: doručení neexistuje nebo nesedí lease token.", file=sys.stderr)
            return 1
        print(f"#{args.message_id} → {args.state}")

    elif args.cmd == "wait":
        deadline = time.monotonic() + max(0, args.timeout)
        while True:
            messages = store.addressed(args.recipient, since_id=args.since)
            if messages:
                for message in messages:
                    print(
                        f"#{message['id']} {message['kind']} "
                        f"{message['agent']} → {message['recipient']}: {message['text']}"
                    )
                return 0
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return 124
            time.sleep(min(1.0, remaining))

    elif args.cmd == "retry":
        if not store.retry(args.message_id, args.recipient, not_before=args.not_before):
            print("Retry odmítnut: task neexistuje nebo není v chybovém stavu.", file=sys.stderr)
            return 1
        print(f"#{args.message_id} → pending")

    elif args.cmd == "cancel":
        if not store.cancel(args.message_id, args.recipient):
            print("Zrušení odmítnuto: task neexistuje nebo už skončil.", file=sys.stderr)
            return 1
        print(f"#{args.message_id} → cancelled")

    elif args.cmd == "web":
        from .webui import main as web_main

        return web_main(["--host", args.host, "--port", str(args.port)])

    elif args.cmd == "history":
        for e in store.history(limit=args.limit, path=args.path):
            print(f"před {e['seconds_ago']:>6} s | {e['agent']:<12} | {e['action']:<14} | {e['path']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

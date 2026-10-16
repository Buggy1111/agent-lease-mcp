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
        description="Shared room for coding agents: path leases, presence, messages.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_room = sub.add_parser("room", help="who is here, what is held, what is new")
    p_room.add_argument("--status", default="", help="announce what you are working on")

    p_claim = sub.add_parser("claim", help="lease paths (directories too)")
    p_claim.add_argument("paths", nargs="+")
    p_claim.add_argument("--purpose", default="", help="why — the other agent will see it")
    p_claim.add_argument("--ttl", type=int, default=None, help="lease length in seconds")

    p_rel = sub.add_parser("release", help="return leases (no paths = everything you hold)")
    p_rel.add_argument("paths", nargs="*")

    p_owner = sub.add_parser("owner", help="who holds this path")
    p_owner.add_argument("path")

    p_say = sub.add_parser("say", help="message to the room")
    p_say.add_argument("text")

    p_send = sub.add_parser("send", help="addressed message or durable task")
    p_send.add_argument("text")
    p_send.add_argument("--to", required=True, dest="recipient")
    p_send.add_argument("--kind", choices=[k.value for k in MessageKind], default="chat")
    p_send.add_argument("--dedupe-key", default=None)
    p_send.add_argument("--reply-to", type=int, default=None)

    p_jobs = sub.add_parser("jobs", help="addressed items and their state")
    p_jobs.add_argument("--for", dest="recipient", default=None)
    p_jobs.add_argument("--limit", type=int, default=50)

    p_ack = sub.add_parser("ack", help="confirm the state of your delivery")
    p_ack.add_argument("message_id", type=int)
    p_ack.add_argument("state", choices=[s.value for s in DeliveryState])
    p_ack.add_argument("--token", default=None)
    p_ack.add_argument("--error", default="")

    p_wait = sub.add_parser("wait", help="wait for an addressed message without spending model tokens")
    p_wait.add_argument("--for", required=True, dest="recipient")
    p_wait.add_argument("--since", type=int, default=0)
    p_wait.add_argument("--timeout", type=float, default=3600)

    p_retry = sub.add_parser("retry", help="put a failed task back in the queue")
    p_retry.add_argument("message_id", type=int)
    p_retry.add_argument("--for", required=True, dest="recipient")
    p_retry.add_argument("--not-before", type=float, default=0)

    p_cancel = sub.add_parser("cancel", help="cancel an unfinished task")
    p_cancel.add_argument("message_id", type=int)
    p_cancel.add_argument("--for", required=True, dest="recipient")

    p_web = sub.add_parser("web", help="start the local live web room")
    p_web.add_argument("--host", default="127.0.0.1")
    p_web.add_argument("--port", type=int, default=8765)
    p_web.add_argument("--open", action="store_true", dest="open_browser")

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
        print(text or f"[agent-lease] You are in the room as `{me}`. Nobody else is here and nothing is new.")

    elif args.cmd == "claim":
        result = store.claim(args.paths, agent=me, purpose=args.purpose, ttl_seconds=args.ttl)
        store.heartbeat(me, status=args.purpose or None, cwd=os.getcwd())
        if result.ok:
            print(f"OK, you hold: {', '.join(result.granted)}")
        else:
            for c in result.conflicts:
                print(f"TAKEN: {c.path} — held by {c.agent}"
                      f"{', ' + c.purpose if c.purpose else ''} ({c.expires_in} s left)")
            return 1

    elif args.cmd == "release":
        released = store.release_all(me) if not args.paths else store.release(args.paths, me)
        store.heartbeat(me, cwd=os.getcwd())
        print(f"Released: {', '.join(released) if released else '(you held nothing)'}")

    elif args.cmd == "owner":
        holder = store.holder_of(args.path)
        print(f"{holder.agent} — {holder.purpose or 'no description'} ({holder.expires_in} s left)"
              if holder else "free")

    elif args.cmd == "say":
        store.say(me, args.text)
        store.heartbeat(me, cwd=os.getcwd())  # status patří práci, ne volání
        print("Sent. ⚠️ The other agent sees it on its next prompt, not immediately.")

    elif args.cmd == "send":
        message_id = store.send(
            me, args.recipient, args.text, kind=args.kind,
            dedupe_key=args.dedupe_key, reply_to=args.reply_to,
        )
        store.heartbeat(me, cwd=os.getcwd())
        print(f"Stored #{message_id} for {args.recipient} ({args.kind}).")

    elif args.cmd == "jobs":
        recipient = args.recipient or me
        for job in store.jobs(recipient, limit=args.limit, viewer=me):
            print(
                f"#{job['id']:<5} {job['state']:<12} {job['kind']:<9} "
                f"{job['agent']} → {job['recipient']}: {job['text']}"
            )

    elif args.cmd == "ack":
        # Potvrzovat jde jen vlastní doručení (APP-003) — dřív šlo `--for` cizí.
        if not store.ack(
            args.message_id, me, args.state,
            lease_token=args.token, error=args.error,
        ):
            print("Not confirmed: no such delivery, or the lease token does not match.", file=sys.stderr)
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
        if not store.retry(
            args.message_id, args.recipient, not_before=args.not_before, actor=me
        ):
            print("Retry refused: no such task, not in a failed state, or not yours.", file=sys.stderr)
            return 1
        print(f"#{args.message_id} → pending")

    elif args.cmd == "cancel":
        if not store.cancel(args.message_id, args.recipient, actor=me):
            print("Cancel refused: no such task, already finished, or not yours.", file=sys.stderr)
            return 1
        print(f"#{args.message_id} → cancelled")

    elif args.cmd == "web":
        from .webui import main as web_main

        web_args = ["--host", args.host, "--port", str(args.port)]
        if args.open_browser:
            web_args.append("--open")
        return web_main(web_args)

    elif args.cmd == "history":
        for e in store.history(limit=args.limit, path=args.path):
            print(f"{e['seconds_ago']:>6} s ago | {e['agent']:<12} | {e['action']:<14} | {e['path']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

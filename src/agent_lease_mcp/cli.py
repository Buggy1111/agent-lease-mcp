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
from .broker import open_store
from .config import Settings
from .models import DeliveryState, MessageKind


def parse_until(text: str) -> float:
    """`+2h`, `+30m`, epoch nebo ISO čas → epoch."""
    import re
    from datetime import datetime

    m = re.fullmatch(r"\+(\d+)([smhd])", text.strip())
    if m:
        return time.time() + int(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
    try:
        return float(text)
    except ValueError:
        return datetime.fromisoformat(text).timestamp()


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
    p_wait.add_argument(
        "--rewake", action="store_true",
        help="pro asyncRewake hook: zprávu vypiš na stderr a skonči s kódem 2 (probudí session)",
    )

    p_accept = sub.add_parser("accept", help="potvrdit, že beru adresovanou zprávu/task")
    p_accept.add_argument("message_id", type=int)
    p_accept.add_argument("--note", default="", help="co udělám — uvidí to odesílatel")
    p_accept.add_argument("--for", dest="recipient", default=None)

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
    p_web.add_argument("--show-token-url", action="store_true")

    p_limit = sub.add_parser("limit", help="ohlásit vyčerpaný limit (úkoly se přesunou na náhradu)")
    p_limit.add_argument("--until", default="+1h", help="+2h, +30m, ISO čas nebo epoch")
    p_limit.add_argument("--reason", default="")
    p_limit.add_argument("--agent", default=None)
    p_limit.add_argument("--clear", action="store_true", help="limit už neplatí")

    sub.add_parser("limits", help="aktivní limity a řetězce náhrady")

    p_overdue = sub.add_parser("overdue", help="zprávy, které nikdo nepotvrdil / nedoručeno")
    p_overdue.add_argument("--seconds", type=int, default=120)

    p_broker = sub.add_parser("broker", help="spustit broker (jediný writer SQLite, Unix socket 0600)")
    p_broker.add_argument("--socket", default=None)

    sub.add_parser("doctor", help="zkontrolovat instalaci, oprávnění a hooky")

    p_bridge = sub.add_parser("bridge", help="autonomní worker pro frontu jednoho agenta")
    p_bridge.add_argument("--agent", required=True)
    p_bridge.add_argument("--command", default=None)
    p_bridge.add_argument("--once", action="store_true")
    p_bridge.add_argument("--provider", choices=["cli", "openrouter"], default="cli")
    p_bridge.add_argument("--model", default="")

    p_prune = sub.add_parser("prune", help="smazat dokončené zprávy a audit starší než N dní")
    p_prune.add_argument("--days", type=int, default=None)

    p_hist = sub.add_parser("history", help="kdo na co sahal a jak to dopadlo")
    p_hist.add_argument("--path", default=None)
    p_hist.add_argument("--limit", type=int, default=20)

    args = parser.parse_args(argv)
    if args.cmd == "doctor":
        from .doctor import run_doctor

        return run_doctor()
    if args.cmd == "broker":
        from .broker import main as broker_main

        return broker_main(["--socket", args.socket] if args.socket else [])
    if args.cmd == "bridge":
        from .bridge import main as bridge_main

        return bridge_main(["--agent", args.agent]
                           + (["--command", args.command] if args.command else [])
                           + (["--once"] if args.once else [])
                           + ["--provider", args.provider]
                           + (["--model", args.model] if args.model else []))
    settings = Settings.from_env()
    store = open_store(settings)
    me = settings.agent

    if args.cmd == "room":
        store.heartbeat(me, status=args.status or None, cwd=os.getcwd())
        text = session_briefing(me, store.peers(), store.claims(), store.undelivered(me), store.awaiting_accept(me))
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
        if args.recipient == "auto":
            from . import jev

            candidates = [p["agent"] for p in store.peers() if p["agent"] != me and p["active"]]
            chosen = jev.route(args.text, candidates) if jev.enabled() else None
            if not chosen:
                print("auto: nelze rozhodnout (Jev vypnutý/nedostupný nebo není kandidát) — "
                      "zadej --to <agent>.", file=sys.stderr)
                return 1
            args.recipient = chosen
            print(f"auto → {chosen}")
        message_id = store.send(
            me, args.recipient, args.text, kind=args.kind,
            dedupe_key=args.dedupe_key, reply_to=args.reply_to,
        )
        store.heartbeat(me, cwd=os.getcwd())
        print(f"Uloženo #{message_id} pro {args.recipient} ({args.kind}). "
              "Příjem (viděl / beru) ti přijde zpět do místnosti.")

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
        seen_signal = 0.0
        next_db = 0.0
        while True:
            # Signální soubor se mění při každém odeslání → reakce do desetin sekundy
            # bez dotazování DB; DB se projde i bez signálu každých 5 s (pojistka).
            signal = store.signal_mtime(args.recipient)
            now = time.monotonic()
            if signal != seen_signal or now >= next_db:
                seen_signal, next_db = signal, now + 5.0
                messages = [
                    m for m in store.addressed(args.recipient, since_id=args.since)
                    if m["kind"] != "result" or args.rewake is False
                ]
                if args.rewake:
                    messages = [m for m in messages if m["state"] in ("pending", "leased")
                                or m["kind"] == "result"]
                if messages:
                    out = sys.stderr if args.rewake else sys.stdout
                    for message in messages:
                        print(
                            f"#{message['id']} {message['kind']} "
                            f"{message['agent']} → {message['recipient']}: {message['text']}",
                            file=out,
                        )
                    return 2 if args.rewake else 0
            if deadline - time.monotonic() <= 0:
                return 124
            time.sleep(0.2)

    elif args.cmd == "accept":
        recipient = args.recipient or me
        if not store.accept(args.message_id, recipient, args.note):
            print("Nepotvrzeno: zpráva neexistuje nebo není adresovaná tobě.", file=sys.stderr)
            return 1
        print(f"✔ Potvrzeno #{args.message_id}. Odesílatel to ví. Teď pracuj.")

    elif args.cmd == "retry":
        if not store.retry(args.message_id, args.recipient, not_before=args.not_before, actor=me):
            print("Retry odmítnut: task neexistuje nebo není v chybovém stavu.", file=sys.stderr)
            return 1
        print(f"#{args.message_id} → pending")

    elif args.cmd == "cancel":
        if not store.cancel(args.message_id, args.recipient, actor=me):
            print("Zrušení odmítnuto: task neexistuje nebo už skončil.", file=sys.stderr)
            return 1
        print(f"#{args.message_id} → cancelled")

    elif args.cmd == "web":
        from .webui import main as web_main

        return web_main(["--host", args.host, "--port", str(args.port)]
                        + (["--show-token-url"] if args.show_token_url else []))

    elif args.cmd == "limit":
        who = args.agent or me
        if args.clear:
            print("Limit zrušen." if store.clear_limit(who) else "Žádný limit nebyl.")
        else:
            until = parse_until(args.until)
            store.report_limit(who, until, args.reason)
            print(f"Limit {who} do {time.strftime('%H:%M', time.localtime(until))}. "
                  "Nezahájené úkoly se přesunou na náhradu (AGENT_LEASE_FALLBACKS).")

    elif args.cmd == "limits":
        rows = store.limits()
        for r in rows:
            print(f"{r['agent']:<14} ještě {r['seconds_left'] // 60} min  {r['reason']}")
        print("Řetězce náhrady:", settings.fallbacks or "(nenastaveno: AGENT_LEASE_FALLBACKS)")

    elif args.cmd == "overdue":
        late = store.overdue(args.seconds)
        for m in late:
            print(f"#{m['id']:<5} {m['problem']:<20} {m['from']} → {m['to']} "
                  f"({m['waiting_seconds']} s): {m['text'][:70]}")
        if not late:
            print("Nic nečeká déle než limit.")
        return 1 if late else 0

    elif args.cmd == "prune":
        removed = store.prune(args.days)
        print(f"Smazáno: {removed['messages']} zpráv, {removed['audit']} záznamů auditu.")

    elif args.cmd == "history":
        for e in store.history(limit=args.limit, path=args.path):
            print(f"před {e['seconds_ago']:>6} s | {e['agent']:<12} | {e['action']:<14} | {e['path']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

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
from .config import UNTRUSTED_PREFIXES, Settings
from .models import DeliveryState, MessageKind

# Příkazy, které nic nezapisují pod jménem volajícího — smí i bez AGENT_NAME.
READ_ONLY_CMDS = frozenset({"owner", "history", "limits", "overdue", "jobs"})


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
    p_send.add_argument("--path", action="append", dest="paths", default=[],
                        help="declared task path; repeat for multiple paths")
    p_send.add_argument("--priority", type=int, default=0, help="task priority from -100 to 100")

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
    p_wait.add_argument("--timeout", type=float, default=None,
                        help="seconds (default 3600; with --rewake: until the session ends)")
    p_wait.add_argument(
        "--rewake", action="store_true",
        help="for an asyncRewake hook: print to stderr and exit 2 (wakes the session)",
    )

    p_accept = sub.add_parser("accept", help="acknowledge that you are taking an addressed message/task")
    p_accept.add_argument("message_id", type=int)
    p_accept.add_argument("--note", default="", help="what you will do — the sender sees it")

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
    p_web.add_argument("--open", action="store_true", dest="open_browser",
                       help="open the browser logged in (the token never goes to stdout)")

    p_limit = sub.add_parser("limit", help="report an exhausted limit (tasks move to a fallback agent)")
    p_limit.add_argument("--until", default="+1h", help="+2h, +30m, ISO time or epoch")
    p_limit.add_argument("--reason", default="")
    p_limit.add_argument("--agent", default=None)
    p_limit.add_argument("--clear", action="store_true", help="the limit no longer applies")

    sub.add_parser("limits", help="active limits and fallback chains")

    p_provider = sub.add_parser(
        "provider-sync", help="read and persist an explicit provider availability signal",
    )
    p_provider.add_argument("--agent", choices=["codex"], default="codex")
    p_provider_clear = sub.add_parser(
        "provider-clear", help="confirm recovery and remove a stale provider state",
    )
    p_provider_clear.add_argument("--agent", default=None)

    p_overdue = sub.add_parser("overdue", help="messages nobody acknowledged / undelivered")
    p_overdue.add_argument("--seconds", type=int, default=120)

    p_broker = sub.add_parser("broker", help="run the broker (single SQLite writer, Unix socket 0600)")
    p_broker.add_argument("--socket", default=None)

    p_init = sub.add_parser("init", help="write the hooks into a client config (idempotent, with backup)")
    p_init.add_argument("agent", choices=["claude-code", "codex"])
    p_init.add_argument("--write", action="store_true", help="actually write (otherwise just show)")
    p_init.add_argument("--rewake", action="store_true", help="add waking of a sleeping session")

    sub.add_parser("simulate", help="replay a full scenario in a temporary DB (post-install check)")

    p_notify = sub.add_parser("notify", help="Telegram alerts for what is waiting on the human")
    p_notify.add_argument("--dry", action="store_true")
    p_notify.add_argument("--loop", action="store_true")
    p_notify.add_argument("--overdue", type=int, default=300)

    p_doctor = sub.add_parser("doctor", help="check installation, delivery and worker health")
    p_doctor.add_argument("--json", action="store_true", help="machine-readable diagnostics")

    p_bridge = sub.add_parser("bridge", help="autonomous worker for one agent's queue")
    p_bridge.add_argument("--agent", required=True)
    p_bridge.add_argument("--command", default=None)
    p_bridge.add_argument("--once", action="store_true")
    p_bridge.add_argument("--include-existing", action="store_true")
    p_bridge.add_argument("--provider", choices=["cli", "openrouter"], default="cli")
    p_bridge.add_argument("--model", default="")

    p_activate = sub.add_parser(
        "worker-activate", help="set this agent's safe worker baseline without running a task",
    )
    p_activate.add_argument("--include-existing", action="store_true")

    p_failover_activate = sub.add_parser(
        "failover-activate", help="set safe failover baselines without moving a task",
    )
    p_failover_activate.add_argument("agents", nargs="+")
    p_failover_activate.add_argument("--include-existing", action="store_true")

    p_prune = sub.add_parser("prune", help="delete finished messages and audit older than N days")
    p_prune.add_argument("--days", type=int, default=None)

    p_hist = sub.add_parser("history", help="who touched what and how it ended")
    p_hist.add_argument("--path", default=None)
    p_hist.add_argument("--limit", type=int, default=20)

    args = parser.parse_args(argv)
    if args.cmd == "wait":
        args.timeout_given = args.timeout is not None
        if args.timeout is None:
            args.timeout = 3600.0
    if args.cmd == "simulate":
        from .simulate import run_simulation

        return 0 if run_simulation() else 1
    if args.cmd == "notify":
        from .notifier import main as notify_main

        return notify_main((["--dry"] if args.dry else []) + (["--loop"] if args.loop else [])
                           + ["--overdue", str(args.overdue)])
    if args.cmd == "init":
        from .init_cmd import run_init

        return run_init(args.agent, write=args.write, rewake=args.rewake)
    if args.cmd == "doctor":
        from .doctor import run_doctor

        return run_doctor(json_output=args.json)
    if args.cmd == "broker":
        from .broker import main as broker_main

        return broker_main(["--socket", args.socket] if args.socket else [])
    if args.cmd == "bridge":
        from .bridge import main as bridge_main

        return bridge_main(["--agent", args.agent]
                           + (["--command", args.command] if args.command else [])
                           + (["--once"] if args.once else [])
                           + (["--include-existing"] if args.include_existing else [])
                           + ["--provider", args.provider]
                           + (["--model", args.model] if args.model else []))
    settings = Settings.from_env()
    me = settings.agent
    if me.startswith(UNTRUSTED_PREFIXES) and args.cmd not in READ_ONLY_CMDS:
        # Bez jména by se zápis objevil v místnosti jako `unconfigured-<host>-<pid>`:
        # druhý agent neví, od koho to je, a každé volání přidá nového „člověka".
        print(f"AGENT_NAME is not set (or not allowed), so you would act as `{me}`.\n"
              f"Run it as: AGENT_NAME=<your name> agent-lease {args.cmd} ...", file=sys.stderr)
        return 2
    store = open_store(settings)
    if args.cmd == "worker-activate":
        after = store.worker_activation(me, include_existing=args.include_existing)
        print(f"Worker {me} will process task ids greater than {after}.")
        return 0
    if args.cmd == "failover-activate":
        for agent in args.agents:
            after = store.failover_activation(agent, include_existing=args.include_existing)
            print(f"Failover from {agent} will move task ids greater than {after}.")
        return 0
    if args.cmd == "provider-sync":
        from .rate_limits import probe_codex_rate_limits

        state = probe_codex_rate_limits()
        store.report_provider_state(
            args.agent, state.state, state.source, observed_at=state.observed_at,
            resets_at=state.resets_at, reason=state.reason,
        )
        reset = f", reset hint {time.strftime('%Y-%m-%d %H:%M', time.localtime(state.resets_at))}" \
            if state.resets_at else ""
        print(f"{args.agent}: {state.state} ({state.source}){reset}")
        return 0 if state.state != "unknown" else 1
    if args.cmd == "provider-clear":
        who = args.agent or me
        if who != me:
            print("You can only clear your own provider state.", file=sys.stderr)
            return 1
        print("Provider state cleared." if store.clear_provider_state(who)
              else "There was no provider state.")
        return 0

    if args.cmd == "room":
        store.heartbeat(me, status=args.status or None, cwd=os.getcwd())
        text = session_briefing(me, store.peers(), store.claims(), store.undelivered(me), store.awaiting_accept(me))
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
        if args.recipient == "auto":
            from . import jev

            candidates = [p["agent"] for p in store.peers() if p["agent"] != me and p["active"]]
            chosen = jev.route(args.text, candidates) if jev.enabled() else None
            if not chosen:
                print("auto: cannot decide (Jev off/unavailable or no candidate) — "
                      "zadej --to <agent>.", file=sys.stderr)
                return 1
            args.recipient = chosen
            print(f"auto → {chosen}")
        message_id = store.send(
            me, args.recipient, args.text, kind=args.kind,
            dedupe_key=args.dedupe_key, reply_to=args.reply_to,
            paths=args.paths, priority=args.priority,
        )
        store.heartbeat(me, cwd=os.getcwd())
        print(f"Stored #{message_id} for {args.recipient} ({args.kind}). "
              "Receipts (seen / taking) come back to you in the room.")

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
        if args.rewake:
            # asyncRewake hook: budí jen to, co session ještě neviděla (kurzor
            # kontextu), jeden čekatel na session, konec se zánikem session.
            from .lifecycle import _hook_session, rewake_wait

            if args.recipient != me:
                print("--rewake waits only for your own messages (--for must be AGENT_NAME).",
                      file=sys.stderr)
                return 1
            return rewake_wait(store, me, session=_hook_session(),
                               timeout=args.timeout if args.timeout_given else 0)
        deadline = time.monotonic() + max(0, args.timeout)
        seen_signal = 0.0
        next_db = 0.0
        next_beat = 0.0
        while True:
            signal = store.signal_mtime(args.recipient)
            now = time.monotonic()
            if me == args.recipient and now >= next_beat:
                # spící čekatel nesmí vypadat jako offline (jinak by mu failover bral úkoly)
                store.heartbeat(me)
                next_beat = now + 60.0
            if signal != seen_signal or now >= next_db:
                seen_signal, next_db = signal, now + 5.0
                messages = store.addressed(args.recipient, since_id=args.since)
                if messages:
                    for message in messages:
                        print(
                            f"#{message['id']} {message['kind']} "
                            f"{message['agent']} → {message['recipient']}: {message['text']}",
                        )
                    return 0
            if deadline - time.monotonic() <= 0:
                return 124
            time.sleep(0.2)

    elif args.cmd == "accept":
        if not store.accept(args.message_id, me, args.note):  # jen za sebe
            print("Not acknowledged: no such message, or it is not addressed to you.", file=sys.stderr)
            return 1
        print(f"✔ Acknowledged #{args.message_id}. The sender knows. Now do the work.")

    elif args.cmd == "retry":
        if not store.retry(args.message_id, args.recipient, not_before=args.not_before, actor=me):
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

        return web_main(["--host", args.host, "--port", str(args.port)]
                        + (["--open"] if args.open_browser else []))

    elif args.cmd == "limit":
        who = args.agent or me
        if who != me:  # limit jde hlásit jen za sebe (security review 16. 10.)
            print("You can only report your own limit.", file=sys.stderr)
            return 1
        if args.clear:
            print("Limit cleared." if store.clear_limit(who) else "There was no limit.")
        else:
            until = parse_until(args.until)
            store.report_limit(who, until, args.reason)
            print(f"Limit for {who} until {time.strftime('%H:%M', time.localtime(until))}. "
                  "Unstarted tasks move to a fallback (AGENT_LEASE_FALLBACKS).")

    elif args.cmd == "limits":
        rows = store.limits()
        for r in rows:
            print(f"{r['agent']:<14} {r['seconds_left'] // 60} min left  {r['reason']}")
        for r in store.provider_states():
            print(f"{r['agent']:<14} provider={r['state']} via {r['source']}  {r['reason']}")
        print("Fallback chains:", settings.fallbacks or "(not set: AGENT_LEASE_FALLBACKS)")

    elif args.cmd == "overdue":
        late = store.overdue(args.seconds)
        for m in late:
            print(f"#{m['id']:<5} {m['problem']:<20} {m['from']} → {m['to']} "
                  f"({m['waiting_seconds']} s): {m['text'][:70]}")
        if not late:
            print("Nothing has waited longer than the limit.")
        return 1 if late else 0

    elif args.cmd == "prune":
        removed = store.prune(args.days)
        print(f"Deleted: {removed['messages']} messages, {removed['audit']} audit entries.")

    elif args.cmd == "history":
        for e in store.history(limit=args.limit, path=args.path):
            print(f"{e['seconds_ago']:>6} s ago | {e['agent']:<12} | {e['action']:<14} | {e['path']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

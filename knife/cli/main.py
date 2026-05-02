"""knife — command line interface.

Run `knife --help` for an overview, or `knife <subcommand> --help`.
"""
from __future__ import annotations

import argparse
import logging
import os
import shutil
import signal
import sys
import time
from typing import List, Optional

import psutil

from .. import __version__
from ..core.config import Config
from ..core.memory import MemoryAction, MemoryGuard
from ..core.monitor import Monitor, ProcessSnapshot
from ..core.network import NetworkAction, NetworkGuard
from ..core.policy import Policy, PolicyAction, PolicyEnforcer, PolicyMode
from ..core.priority import PriorityLevel, PriorityManager
from ..utils.units import format_rate, format_size, parse_rate, parse_size


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def _setup_logging(verbose: int) -> None:
    level = logging.WARNING
    if verbose == 1:
        level = logging.INFO
    elif verbose >= 2:
        level = logging.DEBUG
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _resolve_pids(target: str) -> List[int]:
    """Resolve a PID, name, or pattern to a list of PIDs."""
    if target.isdigit():
        return [int(target)]
    target_l = target.lower()
    pids: List[int] = []
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if target_l == name or target_l in name:
                pids.append(proc.info["pid"])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return pids


def _term_width(default: int = 100) -> int:
    try:
        return shutil.get_terminal_size().columns
    except Exception:
        return default


def _format_table(rows: List[List[str]], headers: List[str]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    sep = "  ".join("-" * widths[i] for i in range(len(headers)))
    body = []
    for row in rows:
        body.append("  ".join(str(c).ljust(widths[i]) for i, c in enumerate(row)))
    return "\n".join([line, sep] + body)


# ----------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------
def cmd_list(args: argparse.Namespace) -> int:
    mon = Monitor()
    time.sleep(0.5)  # let cpu_percent settle
    snaps = mon.sample()
    snaps.sort(key=lambda s: (s.cpu_percent, s.memory_bytes), reverse=True)
    if args.top:
        snaps = snaps[: args.top]

    rows = []
    for s in snaps:
        rows.append([
            s.pid,
            (s.name or "")[:30],
            f"{s.cpu_percent:5.1f}",
            format_size(s.memory_bytes),
            f"{s.memory_percent:4.1f}%",
            s.num_connections,
            s.username or "",
        ])
    print(_format_table(rows, ["PID", "NAME", "CPU%", "MEM", "MEM%", "CONN", "USER"]))
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    mon = Monitor()
    try:
        while True:
            time.sleep(args.interval)
            snaps = mon.sample()
            if args.target:
                pids = set(_resolve_pids(args.target))
                snaps = [s for s in snaps if s.pid in pids]
            snaps.sort(key=lambda s: (s.cpu_percent, s.memory_bytes), reverse=True)
            snaps = snaps[: args.top]
            os.system("cls" if os.name == "nt" else "clear")
            print(f"knife watch — {time.strftime('%H:%M:%S')}  (Ctrl-C to exit)\n")
            rows = []
            for s in snaps:
                rows.append([
                    s.pid,
                    (s.name or "")[:30],
                    f"{s.cpu_percent:5.1f}",
                    format_size(s.memory_bytes),
                    format_rate(s.net_sent_per_sec),
                    format_rate(s.net_recv_per_sec),
                    s.num_connections,
                ])
            print(_format_table(rows, ["PID", "NAME", "CPU%", "MEM", "TX", "RX", "CONN"]))
    except KeyboardInterrupt:
        return 0


def cmd_limit(args: argparse.Namespace) -> int:
    cfg = Config()
    pids = _resolve_pids(args.target)
    if not pids:
        print(f"no process matched: {args.target}", file=sys.stderr)
        return 1
    if args.memory:
        bytes_limit = parse_size(args.memory)
        for pid in pids:
            cfg.update("memory_limits", {str(pid): {
                "bytes": bytes_limit, "action": args.action,
            }})
            print(f"set memory limit pid={pid} -> {format_size(bytes_limit)} ({args.action})")
    if args.network:
        bps = parse_rate(args.network)
        for pid in pids:
            cfg.update("network_limits", {str(pid): {
                "bps": bps, "action": args.action, "direction": args.direction,
            }})
            print(f"set network limit pid={pid} -> {format_rate(bps)} {args.direction} ({args.action})")
    cfg.save()
    if not args.memory and not args.network:
        print("specify --memory and/or --network", file=sys.stderr)
        return 2
    print(f"\nlimits saved to {cfg.path}")
    print("run 'knife daemon' to start enforcing limits in the background")
    return 0


def cmd_unlimit(args: argparse.Namespace) -> int:
    cfg = Config()
    pids = _resolve_pids(args.target)
    if not pids and not args.target.isdigit():
        # also check if target is a string key in config
        pids = []
    removed = 0
    for collection in ("memory_limits", "network_limits"):
        d = dict(cfg.get(collection) or {})
        for pid in pids + [int(args.target)] if args.target.isdigit() else pids:
            if str(pid) in d:
                d.pop(str(pid))
                removed += 1
        if str(args.target) in d:
            d.pop(str(args.target))
            removed += 1
        cfg.set(collection, d)
    cfg.save()
    print(f"removed {removed} limit(s)")
    return 0


def cmd_priority(args: argparse.Namespace) -> int:
    mgr = PriorityManager()
    pids = _resolve_pids(args.target)
    if not pids:
        print(f"no process matched: {args.target}", file=sys.stderr)
        return 1
    level = PriorityLevel(args.level)
    for pid in pids:
        result = mgr.set(pid, level)
        status = "OK" if result.applied else "FAIL"
        print(f"[{status}] pid={pid} -> {level.value}  {result.note}")
    return 0


def cmd_allow(args: argparse.Namespace) -> int:
    cfg = Config()
    policy = Policy.from_dict(cfg.get("policy") or {})
    for name in args.names:
        if name not in policy.allow:
            policy.allow.append(name)
    cfg.set("policy", policy.to_dict())
    cfg.save()
    print(f"allow list now: {policy.allow}")
    return 0


def cmd_block(args: argparse.Namespace) -> int:
    cfg = Config()
    policy = Policy.from_dict(cfg.get("policy") or {})
    for name in args.names:
        if name not in policy.block:
            policy.block.append(name)
    cfg.set("policy", policy.to_dict())
    cfg.save()
    print(f"block list now: {policy.block}")
    return 0


def cmd_policy(args: argparse.Namespace) -> int:
    cfg = Config()
    policy = Policy.from_dict(cfg.get("policy") or {})
    if args.mode:
        policy.mode = PolicyMode(args.mode)
    if args.action:
        policy.action = PolicyAction(args.action)
    if args.clear_allow:
        policy.allow = []
    if args.clear_block:
        policy.block = []
    cfg.set("policy", policy.to_dict())
    cfg.save()

    print(f"mode      : {policy.mode.value}")
    print(f"action    : {policy.action.value}")
    print(f"allow     : {', '.join(policy.allow) or '(empty)'}")
    print(f"block     : {', '.join(policy.block) or '(empty)'}")
    print(f"protected : {', '.join(policy.protected[:5])}...")
    return 0


def cmd_daemon(args: argparse.Namespace) -> int:
    cfg = Config()
    print("knife daemon starting — Ctrl-C to stop")

    mem_guard = MemoryGuard(poll_interval=args.interval, on_event=_print_event)
    net_guard = NetworkGuard(poll_interval=args.interval, on_event=_print_event)
    policy = Policy.from_dict(cfg.get("policy") or {})
    pol_enf = PolicyEnforcer(policy, poll_interval=max(args.interval, 2.0),
                              on_event=lambda evt, info: print(f"[POLICY] {evt}: {info}"))

    # Hydrate from config
    for key, info in (cfg.get("memory_limits") or {}).items():
        try:
            pid = int(key)
        except ValueError:
            continue
        mem_guard.set_limit(pid, int(info.get("bytes", 0)),
                             action=MemoryAction(info.get("action", "warn")))

    for key, info in (cfg.get("network_limits") or {}).items():
        try:
            pid = int(key)
        except ValueError:
            continue
        net_guard.set_limit(pid, int(info.get("bps", 0)),
                             action=NetworkAction(info.get("action", "warn")),
                             direction=info.get("direction", "both"))

    mem_guard.start()
    net_guard.start()
    if policy.mode != PolicyMode.OFF:
        pol_enf.start()

    print(f"  memory limits : {len(mem_guard.list_limits())}")
    print(f"  network limits: {len(net_guard.list_limits())}")
    print(f"  policy mode   : {policy.mode.value} (action={policy.action.value})")
    print(f"  poll interval : {args.interval}s\n")

    stop = {"requested": False}

    def _sig(_signum, _frame):
        stop["requested"] = True

    signal.signal(signal.SIGINT, _sig)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _sig)

    try:
        while not stop["requested"]:
            time.sleep(0.5)
    finally:
        print("\nshutting down…")
        mem_guard.stop()
        net_guard.stop()
        pol_enf.stop()
    return 0


def _print_event(event: str, lim, info: dict) -> None:
    if hasattr(lim, "bytes_limit"):
        print(f"[MEM ] {event}: pid={lim.pid} rss={format_size(info.get('rss', 0))} "
              f"limit={format_size(lim.bytes_limit)}")
    else:
        print(f"[NET ] {event}: pid={lim.pid} rate={format_rate(info.get('rate', 0))} "
              f"limit={format_rate(lim.bytes_per_sec)}")


def cmd_status(args: argparse.Namespace) -> int:
    cfg = Config()
    print(f"knife {__version__}")
    print(f"config: {cfg.path}\n")
    print(f"memory limits ({len(cfg.get('memory_limits') or {})}):")
    for k, v in (cfg.get("memory_limits") or {}).items():
        print(f"  {k}: {format_size(v.get('bytes', 0))} ({v.get('action')})")
    print(f"\nnetwork limits ({len(cfg.get('network_limits') or {})}):")
    for k, v in (cfg.get("network_limits") or {}).items():
        print(f"  {k}: {format_rate(v.get('bps', 0))} {v.get('direction')} ({v.get('action')})")
    policy = Policy.from_dict(cfg.get("policy") or {})
    print(f"\npolicy mode={policy.mode.value} action={policy.action.value}")
    print(f"  allow: {policy.allow}")
    print(f"  block: {policy.block}")
    return 0


def cmd_gui(args: argparse.Namespace) -> int:
    try:
        from ..gui.app import main as gui_main
    except ImportError as e:
        if "_tkinter" in str(e) or "tkinter" in str(e):
            sys.stderr.write(
                "Tkinter is not available in this Python.\n\n"
                "macOS (Homebrew Python):\n"
                "    brew install python-tk        # default version\n"
                "    # or match your Python version, e.g.\n"
                "    brew install python-tk@3.12\n\n"
                "After installing, rebuild the venv:\n"
                "    rm -rf .venv && ./setup.sh\n\n"
                "Linux (Debian/Ubuntu):  sudo apt install python3-tk\n"
                "Linux (Fedora):         sudo dnf install python3-tkinter\n\n"
                "You can still use the CLI without Tkinter:\n"
                "    knife list / knife watch / knife daemon …\n"
            )
            return 1
        raise
    return gui_main()


# ----------------------------------------------------------------------
# Parser
# ----------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="knife",
        description="Slice up CPU, memory and network resources between your apps.",
    )
    p.add_argument("--version", action="version", version=f"knife {__version__}")
    p.add_argument("-v", "--verbose", action="count", default=0, help="verbose logging")

    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("list", help="list running processes")
    sp.add_argument("--top", type=int, default=20, help="show top N (default 20)")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("watch", help="real-time process watcher")
    sp.add_argument("target", nargs="?", help="optional pid or name filter")
    sp.add_argument("--top", type=int, default=20)
    sp.add_argument("--interval", type=float, default=1.5)
    sp.set_defaults(func=cmd_watch)

    sp = sub.add_parser("limit", help="set a memory or network limit on a process")
    sp.add_argument("target", help="pid or process name")
    sp.add_argument("--memory", help="memory limit, e.g. 512MB, 2G")
    sp.add_argument("--network", help="network rate, e.g. 5MB/s, 1Mbps")
    sp.add_argument("--direction", choices=["send", "recv", "both"], default="both")
    sp.add_argument("--action", choices=[a.value for a in MemoryAction] + ["throttle"],
                    default="warn",
                    help="what to do on violation")
    sp.set_defaults(func=cmd_limit)

    sp = sub.add_parser("unlimit", help="remove limits from a process")
    sp.add_argument("target", help="pid or process name")
    sp.set_defaults(func=cmd_unlimit)

    sp = sub.add_parser("priority", help="set process priority")
    sp.add_argument("target", help="pid or process name")
    sp.add_argument("level", choices=[lvl.value for lvl in PriorityLevel])
    sp.set_defaults(func=cmd_priority)

    sp = sub.add_parser("allow", help="add names to the allow list")
    sp.add_argument("names", nargs="+")
    sp.set_defaults(func=cmd_allow)

    sp = sub.add_parser("block", help="add names to the block list")
    sp.add_argument("names", nargs="+")
    sp.set_defaults(func=cmd_block)

    sp = sub.add_parser("policy", help="show or update the active policy")
    sp.add_argument("--mode", choices=[m.value for m in PolicyMode])
    sp.add_argument("--action", choices=[a.value for a in PolicyAction])
    sp.add_argument("--clear-allow", action="store_true")
    sp.add_argument("--clear-block", action="store_true")
    sp.set_defaults(func=cmd_policy)

    sp = sub.add_parser("daemon", help="run guard daemon enforcing saved limits & policy")
    sp.add_argument("--interval", type=float, default=1.0)
    sp.set_defaults(func=cmd_daemon)

    sp = sub.add_parser("status", help="show saved configuration")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("gui", help="launch the graphical interface")
    sp.set_defaults(func=cmd_gui)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        if args.verbose:
            raise
        return 1


if __name__ == "__main__":
    sys.exit(main())

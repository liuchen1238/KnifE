"""knife GUI — Tkinter dashboard.

Layout (single window with tabbed notebook):

  ┌─ Dashboard ─────────────────────────────────────┐
  │ system gauges  |  top processes table           │
  ├─ Limits ───────────────────────────────────────┤
  │ memory limits / network limits / priorities    │
  ├─ Policy ───────────────────────────────────────┤
  │ allow/block list editor + mode + action        │
  └─ Log ──────────────────────────────────────────┘

Tkinter ships with Python so no extra dependency is required. The GUI
communicates with the same core modules as the CLI, so anything you can
do in the GUI is reflected in the saved config and vice-versa.
"""
from __future__ import annotations

import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk
from typing import Dict, List, Optional

from .. import __version__
from ..core.config import Config
from ..core.memory import MemoryAction, MemoryGuard
from ..core.monitor import Monitor, ProcessSnapshot
from ..core.network import NetworkAction, NetworkGuard
from ..core.policy import Policy, PolicyAction, PolicyEnforcer, PolicyMode
from ..core.priority import PriorityLevel, PriorityManager
from ..utils.units import format_rate, format_size, parse_rate, parse_size


REFRESH_MS = 1500


class KnifeApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(f"knife {__version__} — Resource Allocator")
        self.root.geometry("960x600")
        self.root.minsize(760, 480)

        self.cfg = Config()
        self.monitor = Monitor()
        self.mem_guard = MemoryGuard(on_event=self._on_event)
        self.net_guard = NetworkGuard(on_event=self._on_event)
        self.priority_mgr = PriorityManager()
        self.policy = Policy.from_dict(self.cfg.get("policy") or {})
        self.policy_enforcer = PolicyEnforcer(self.policy, on_event=self._on_policy_event)

        self._event_queue: "queue.Queue[str]" = queue.Queue()
        self._snapshot_cache: List[ProcessSnapshot] = []

        self._build_ui()
        self._restore_from_config()
        self.mem_guard.start()
        self.net_guard.start()
        if self.policy.mode != PolicyMode.OFF:
            self.policy_enforcer.start()

        self._try_load_icon()

        self.root.after(500, self._refresh)
        self.root.after(200, self._drain_events)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _try_load_icon(self) -> None:
        """Best-effort: load icon.png from project root if present."""
        from pathlib import Path
        candidates = [
            Path(__file__).resolve().parent.parent.parent / "icon.png",
            Path.cwd() / "icon.png",
        ]
        for path in candidates:
            if path.exists():
                try:
                    img = tk.PhotoImage(file=str(path))
                    self.root.iconphoto(True, img)
                    self._icon_ref = img  # keep reference
                    return
                except tk.TclError:
                    continue

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True, padx=8, pady=8)

        self.dash_tab = ttk.Frame(notebook)
        self.limits_tab = ttk.Frame(notebook)
        self.policy_tab = ttk.Frame(notebook)
        self.log_tab = ttk.Frame(notebook)

        notebook.add(self.dash_tab, text="Dashboard")
        notebook.add(self.limits_tab, text="Limits")
        notebook.add(self.policy_tab, text="Policy")
        notebook.add(self.log_tab, text="Log")

        self._build_dashboard(self.dash_tab)
        self._build_limits(self.limits_tab)
        self._build_policy(self.policy_tab)
        self._build_log(self.log_tab)

        # Status bar
        self.status_var = tk.StringVar(value="ready")
        status = ttk.Label(self.root, textvariable=self.status_var, anchor="w",
                           relief="sunken", padding=(8, 2))
        status.pack(fill="x", side="bottom")

    def _build_dashboard(self, parent: ttk.Frame) -> None:
        # Top: gauges
        gauges = ttk.LabelFrame(parent, text="System")
        gauges.pack(fill="x", padx=8, pady=(8, 4))

        self.cpu_var = tk.StringVar(value="CPU: …")
        self.mem_var = tk.StringVar(value="Memory: …")
        self.net_var = tk.StringVar(value="Network: …")
        ttk.Label(gauges, textvariable=self.cpu_var, width=24).pack(side="left", padx=8, pady=6)
        ttk.Label(gauges, textvariable=self.mem_var, width=32).pack(side="left", padx=8)
        ttk.Label(gauges, textvariable=self.net_var, width=40).pack(side="left", padx=8)

        # Bottom: process table
        wrap = ttk.LabelFrame(parent, text="Processes")
        wrap.pack(fill="both", expand=True, padx=8, pady=4)

        cols = ("pid", "name", "cpu", "mem", "mempct", "tx", "rx", "conn", "user")
        self.proc_tree = ttk.Treeview(wrap, columns=cols, show="headings", height=18)
        widths = {"pid": 70, "name": 220, "cpu": 70, "mem": 100, "mempct": 70,
                  "tx": 90, "rx": 90, "conn": 60, "user": 120}
        labels = {"pid": "PID", "name": "Name", "cpu": "CPU%", "mem": "Memory",
                  "mempct": "Mem%", "tx": "TX/s", "rx": "RX/s", "conn": "Conn", "user": "User"}
        for c in cols:
            self.proc_tree.heading(c, text=labels[c],
                                   command=lambda col=c: self._sort_by(col))
            self.proc_tree.column(c, width=widths[c], anchor="w")
        self.proc_tree.pack(side="left", fill="both", expand=True)

        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.proc_tree.yview)
        sb.pack(side="right", fill="y")
        self.proc_tree.configure(yscrollcommand=sb.set)

        # Action bar
        bar = ttk.Frame(parent)
        bar.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(bar, text="Set Memory Limit…", command=self._dialog_memory_limit).pack(side="left", padx=4)
        ttk.Button(bar, text="Set Network Limit…", command=self._dialog_network_limit).pack(side="left", padx=4)
        ttk.Button(bar, text="Set Priority…", command=self._dialog_priority).pack(side="left", padx=4)
        ttk.Button(bar, text="Suspend", command=lambda: self._do_signal("suspend")).pack(side="left", padx=4)
        ttk.Button(bar, text="Resume", command=lambda: self._do_signal("resume")).pack(side="left", padx=4)
        ttk.Button(bar, text="Terminate", command=lambda: self._do_signal("terminate")).pack(side="left", padx=4)

        self._sort_state = {"col": "cpu", "reverse": True}

    def _build_limits(self, parent: ttk.Frame) -> None:
        # Memory limits frame
        mem_frame = ttk.LabelFrame(parent, text="Memory limits")
        mem_frame.pack(fill="x", padx=8, pady=(8, 4))
        self.mem_tree = self._make_kv_tree(mem_frame, ("PID/Name", "Limit", "Action"))

        net_frame = ttk.LabelFrame(parent, text="Network limits")
        net_frame.pack(fill="x", padx=8, pady=4)
        self.net_tree = self._make_kv_tree(net_frame, ("PID/Name", "Rate", "Direction", "Action"))

        ttk.Button(parent, text="Remove selected limit",
                   command=self._remove_selected_limit).pack(anchor="w", padx=12, pady=8)

    def _make_kv_tree(self, parent: ttk.Frame, columns) -> ttk.Treeview:
        tree = ttk.Treeview(parent, columns=columns, show="headings", height=6)
        for c in columns:
            tree.heading(c, text=c)
            tree.column(c, width=160, anchor="w")
        tree.pack(fill="x", padx=6, pady=6)
        return tree

    def _build_policy(self, parent: ttk.Frame) -> None:
        ctrl = ttk.Frame(parent)
        ctrl.pack(fill="x", padx=8, pady=(8, 4))
        ttk.Label(ctrl, text="Mode:").pack(side="left")
        self.mode_var = tk.StringVar(value=self.policy.mode.value)
        mode_cb = ttk.Combobox(ctrl, textvariable=self.mode_var, state="readonly",
                                values=[m.value for m in PolicyMode], width=10)
        mode_cb.pack(side="left", padx=4)
        ttk.Label(ctrl, text="Action:").pack(side="left", padx=(12, 0))
        self.action_var = tk.StringVar(value=self.policy.action.value)
        action_cb = ttk.Combobox(ctrl, textvariable=self.action_var, state="readonly",
                                  values=[a.value for a in PolicyAction], width=10)
        action_cb.pack(side="left", padx=4)
        ttk.Button(ctrl, text="Apply", command=self._apply_policy).pack(side="left", padx=12)

        lists = ttk.Frame(parent)
        lists.pack(fill="both", expand=True, padx=8, pady=4)

        # Allow list
        af = ttk.LabelFrame(lists, text="Allow list")
        af.pack(side="left", fill="both", expand=True, padx=(0, 4))
        self.allow_lb = tk.Listbox(af, height=12)
        self.allow_lb.pack(fill="both", expand=True, padx=4, pady=4)
        ab = ttk.Frame(af)
        ab.pack(fill="x")
        ttk.Button(ab, text="Add…", command=lambda: self._add_to(self.allow_lb, self.policy.allow)).pack(side="left", padx=4, pady=4)
        ttk.Button(ab, text="Remove", command=lambda: self._rm_from(self.allow_lb, self.policy.allow)).pack(side="left", padx=4)

        # Block list
        bf = ttk.LabelFrame(lists, text="Block list")
        bf.pack(side="left", fill="both", expand=True, padx=(4, 0))
        self.block_lb = tk.Listbox(bf, height=12)
        self.block_lb.pack(fill="both", expand=True, padx=4, pady=4)
        bb = ttk.Frame(bf)
        bb.pack(fill="x")
        ttk.Button(bb, text="Add…", command=lambda: self._add_to(self.block_lb, self.policy.block)).pack(side="left", padx=4, pady=4)
        ttk.Button(bb, text="Remove", command=lambda: self._rm_from(self.block_lb, self.policy.block)).pack(side="left", padx=4)

        self._refresh_policy_lists()

    def _build_log(self, parent: ttk.Frame) -> None:
        self.log_text = tk.Text(parent, height=20, wrap="word", state="disabled")
        self.log_text.pack(fill="both", expand=True, padx=8, pady=8)
        ttk.Button(parent, text="Clear", command=self._clear_log).pack(anchor="w", padx=12, pady=(0, 8))

    # ------------------------------------------------------------------
    # Refresh loop
    # ------------------------------------------------------------------
    def _refresh(self) -> None:
        try:
            snaps = self.monitor.sample()
            self._snapshot_cache = snaps
            self._render_dashboard(snaps)
            self._render_limits()
        except Exception as e:
            self._log(f"refresh error: {e}")
        finally:
            self.root.after(REFRESH_MS, self._refresh)

    def _render_dashboard(self, snaps: List[ProcessSnapshot]) -> None:
        import psutil  # imported here so the module loads even if Tk fails first
        self.cpu_var.set(f"CPU: {psutil.cpu_percent():5.1f}%  ({psutil.cpu_count()} cores)")
        vm = psutil.virtual_memory()
        self.mem_var.set(f"Memory: {format_size(vm.used)} / {format_size(vm.total)}  ({vm.percent}%)")
        io = psutil.net_io_counters()
        self.net_var.set(f"Net total — sent {format_size(io.bytes_sent)}  recv {format_size(io.bytes_recv)}")

        # sort
        col = self._sort_state["col"]
        rev = self._sort_state["reverse"]
        keymap = {
            "pid": lambda s: s.pid,
            "name": lambda s: (s.name or "").lower(),
            "cpu": lambda s: s.cpu_percent,
            "mem": lambda s: s.memory_bytes,
            "mempct": lambda s: s.memory_percent,
            "tx": lambda s: s.net_sent_per_sec,
            "rx": lambda s: s.net_recv_per_sec,
            "conn": lambda s: s.num_connections,
            "user": lambda s: s.username or "",
        }
        snaps_sorted = sorted(snaps, key=keymap.get(col, keymap["cpu"]), reverse=rev)[:300]

        # Update tree without flicker
        self.proc_tree.delete(*self.proc_tree.get_children())
        for s in snaps_sorted:
            self.proc_tree.insert("", "end", iid=str(s.pid), values=(
                s.pid,
                (s.name or "")[:60],
                f"{s.cpu_percent:.1f}",
                format_size(s.memory_bytes),
                f"{s.memory_percent:.1f}%",
                format_rate(s.net_sent_per_sec),
                format_rate(s.net_recv_per_sec),
                s.num_connections,
                s.username or "",
            ))

    def _render_limits(self) -> None:
        self.mem_tree.delete(*self.mem_tree.get_children())
        for lim in self.mem_guard.list_limits():
            self.mem_tree.insert("", "end", iid=f"m{lim.pid}",
                                 values=(lim.pid, format_size(lim.bytes_limit), lim.action.value))
        self.net_tree.delete(*self.net_tree.get_children())
        for lim in self.net_guard.list_limits():
            self.net_tree.insert("", "end", iid=f"n{lim.pid}",
                                 values=(lim.pid, format_rate(lim.bytes_per_sec),
                                         lim.direction, lim.action.value))

    def _sort_by(self, col: str) -> None:
        if self._sort_state["col"] == col:
            self._sort_state["reverse"] = not self._sort_state["reverse"]
        else:
            self._sort_state = {"col": col, "reverse": True}

    # ------------------------------------------------------------------
    # Selected process helpers
    # ------------------------------------------------------------------
    def _selected_pid(self) -> Optional[int]:
        sel = self.proc_tree.selection()
        if not sel:
            return None
        try:
            return int(sel[0])
        except ValueError:
            return None

    def _dialog_memory_limit(self) -> None:
        pid = self._selected_pid()
        if pid is None:
            messagebox.showinfo("knife", "Select a process first.")
            return
        text = simpledialog.askstring("Memory limit",
                                       f"Set memory limit for PID {pid} (e.g. 512MB, 2G):",
                                       parent=self.root)
        if not text:
            return
        try:
            bytes_limit = parse_size(text)
        except ValueError as e:
            messagebox.showerror("knife", str(e))
            return
        action = self._ask_action("Action on violation",
                                   [a.value for a in MemoryAction]) or "warn"
        self.mem_guard.set_limit(pid, bytes_limit, action=MemoryAction(action))
        self.cfg.update("memory_limits", {str(pid): {"bytes": bytes_limit, "action": action}})
        self.cfg.save()
        self._log(f"memory limit pid={pid} -> {format_size(bytes_limit)} ({action})")

    def _dialog_network_limit(self) -> None:
        pid = self._selected_pid()
        if pid is None:
            messagebox.showinfo("knife", "Select a process first.")
            return
        text = simpledialog.askstring("Network limit",
                                       f"Set network rate for PID {pid} (e.g. 5MB/s, 1Mbps):",
                                       parent=self.root)
        if not text:
            return
        try:
            bps = parse_rate(text)
        except ValueError as e:
            messagebox.showerror("knife", str(e))
            return
        direction = self._ask_action("Direction", ["both", "send", "recv"]) or "both"
        action = self._ask_action("Action on violation",
                                   [a.value for a in NetworkAction]) or "warn"
        self.net_guard.set_limit(pid, bps, action=NetworkAction(action), direction=direction)
        self.cfg.update("network_limits", {str(pid): {
            "bps": bps, "action": action, "direction": direction
        }})
        self.cfg.save()
        self._log(f"network limit pid={pid} -> {format_rate(bps)} {direction} ({action})")

    def _dialog_priority(self) -> None:
        pid = self._selected_pid()
        if pid is None:
            messagebox.showinfo("knife", "Select a process first.")
            return
        level = self._ask_action("Priority level",
                                  [lvl.value for lvl in PriorityLevel]) or "normal"
        result = self.priority_mgr.set(pid, PriorityLevel(level))
        if result.applied:
            self._log(f"priority pid={pid} -> {level}: {result.note}")
        else:
            messagebox.showerror("knife", f"Failed: {result.note}")

    def _do_signal(self, what: str) -> None:
        import psutil
        pid = self._selected_pid()
        if pid is None:
            return
        try:
            proc = psutil.Process(pid)
            getattr(proc, what)()
            self._log(f"{what} pid={pid}")
        except Exception as e:
            messagebox.showerror("knife", str(e))

    def _ask_action(self, title: str, choices: List[str]) -> Optional[str]:
        dlg = tk.Toplevel(self.root)
        dlg.title(title)
        dlg.transient(self.root)
        dlg.resizable(False, False)
        ttk.Label(dlg, text=title).pack(padx=12, pady=(12, 4))
        var = tk.StringVar(value=choices[0])
        cb = ttk.Combobox(dlg, textvariable=var, state="readonly", values=choices)
        cb.pack(padx=12, pady=4)
        result = {"value": None}

        def ok():
            result["value"] = var.get()
            dlg.destroy()

        def cancel():
            dlg.destroy()

        bf = ttk.Frame(dlg)
        bf.pack(pady=8)
        ttk.Button(bf, text="OK", command=ok).pack(side="left", padx=4)
        ttk.Button(bf, text="Cancel", command=cancel).pack(side="left", padx=4)
        dlg.grab_set()
        self.root.wait_window(dlg)
        return result["value"]

    # ------------------------------------------------------------------
    # Limits tab actions
    # ------------------------------------------------------------------
    def _remove_selected_limit(self) -> None:
        for tree, kind, guard, key in (
            (self.mem_tree, "memory", self.mem_guard, "memory_limits"),
            (self.net_tree, "network", self.net_guard, "network_limits"),
        ):
            for iid in tree.selection():
                pid = int("".join(c for c in iid if c.isdigit()))
                guard.remove_limit(pid)
                d = dict(self.cfg.get(key) or {})
                d.pop(str(pid), None)
                self.cfg.set(key, d)
                self._log(f"removed {kind} limit pid={pid}")
        self.cfg.save()

    # ------------------------------------------------------------------
    # Policy tab
    # ------------------------------------------------------------------
    def _refresh_policy_lists(self) -> None:
        self.allow_lb.delete(0, "end")
        for n in self.policy.allow:
            self.allow_lb.insert("end", n)
        self.block_lb.delete(0, "end")
        for n in self.policy.block:
            self.block_lb.insert("end", n)

    def _add_to(self, lb: tk.Listbox, target: List[str]) -> None:
        text = simpledialog.askstring("Add", "Process name pattern:", parent=self.root)
        if not text:
            return
        target.append(text.strip())
        self._refresh_policy_lists()

    def _rm_from(self, lb: tk.Listbox, target: List[str]) -> None:
        for idx in reversed(lb.curselection()):
            del target[idx]
        self._refresh_policy_lists()

    def _apply_policy(self) -> None:
        self.policy.mode = PolicyMode(self.mode_var.get())
        self.policy.action = PolicyAction(self.action_var.get())
        self.cfg.set("policy", self.policy.to_dict())
        self.cfg.save()
        # restart enforcer with new policy
        self.policy_enforcer.stop()
        self.policy_enforcer = PolicyEnforcer(self.policy, on_event=self._on_policy_event)
        if self.policy.mode != PolicyMode.OFF:
            self.policy_enforcer.start()
        self._log(f"policy: mode={self.policy.mode.value} action={self.policy.action.value} "
                  f"allow={self.policy.allow} block={self.policy.block}")

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------
    def _log(self, message: str) -> None:
        self._event_queue.put(message)

    def _drain_events(self) -> None:
        try:
            while True:
                msg = self._event_queue.get_nowait()
                self.log_text.configure(state="normal")
                self.log_text.insert("end", f"[{time.strftime('%H:%M:%S')}] {msg}\n")
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
                self.status_var.set(msg)
        except queue.Empty:
            pass
        self.root.after(200, self._drain_events)

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _on_event(self, event: str, lim, info: dict) -> None:
        self._log(f"{event}: pid={lim.pid} info={info}")

    def _on_policy_event(self, event: str, info: dict) -> None:
        self._log(f"{event}: {info}")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def _restore_from_config(self) -> None:
        for key, info in (self.cfg.get("memory_limits") or {}).items():
            try:
                pid = int(key)
            except ValueError:
                continue
            self.mem_guard.set_limit(pid, int(info.get("bytes", 0)),
                                      action=MemoryAction(info.get("action", "warn")))
        for key, info in (self.cfg.get("network_limits") or {}).items():
            try:
                pid = int(key)
            except ValueError:
                continue
            self.net_guard.set_limit(pid, int(info.get("bps", 0)),
                                      action=NetworkAction(info.get("action", "warn")),
                                      direction=info.get("direction", "both"))

    def _on_close(self) -> None:
        self.mem_guard.stop()
        self.net_guard.stop()
        self.policy_enforcer.stop()
        self.root.destroy()


def main() -> int:
    try:
        root = tk.Tk()
    except tk.TclError as e:
        print(f"GUI failed to start (Tkinter): {e}", file=sys.stderr)
        return 1
    KnifeApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

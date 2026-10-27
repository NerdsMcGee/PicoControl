#!/usr/bin/env python3
"""
pico_gui.py -- GUI front-end for pico_type.py and pico_macro.py.

Run:  python pico_gui.py
Build the exe on Windows: double-click build_exe.bat

Macro editing
-------------
The macro is shown as an editable list of steps. Every change is saved
automatically to macro.json next to the script (or next to the .exe), so
your edits survive restarts. "Reset to Default" restores the STEPS list
from pico_macro.py.

Loading a text file
-------------------
"Load File..." takes any plain text file. Every line is replayed exactly as
written (typed, then Enter), with the "Delay between lines" value from the
window after each one. Blank lines inside the file become a bare Enter;
trailing blank lines at the end of the file are dropped.

.json files (what "Save As..." writes) load back as a full macro with each
step's own delay and Enter settings.
"""

import json
import os
import queue
import socket
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, scrolledtext, filedialog, messagebox

# ----- pull settings and the default macro from the existing scripts -----
try:
    from pico_type import DEFAULT_HOST, DEFAULT_PORT, DEFAULT_TOKEN, CONNECT_TIMEOUT
except ImportError:
    DEFAULT_HOST, DEFAULT_PORT, DEFAULT_TOKEN, CONNECT_TIMEOUT = \
        "192.168.1.100", 8822, "change-this-to-a-long-random-string", 5

try:
    from pico_macro import STEPS as DEFAULT_STEPS
except ImportError:
    DEFAULT_STEPS = []


def app_dir():
    """Folder of the .exe when frozen, else the folder of this script."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


AUTOSAVE_PATH = os.path.join(app_dir(), "macro.json")
BARE_ENTER = "{enter}"


# ======================= macro data helpers =======================
def normalize_step(d):
    try:
        delay = max(0, int(d.get("delay_ms", 0)))
    except (TypeError, ValueError):
        delay = 0
    return {
        "text": str(d.get("text", "")),
        "enter": bool(d.get("enter", True)),
        "enter_key": "keypad" if d.get("enter_key") == "keypad" else "main",
        "delay_ms": delay,
    }


def read_text_file(path):
    """Read a text file, tolerating UTF-8 (with/without BOM) or Windows-1252."""
    with open(path, "rb") as f:
        raw = f.read()
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def plain_text_to_steps(content, delay_ms):
    """Each line -> one step: type it exactly, press Enter, wait delay_ms."""
    lines = content.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    return [{"text": line, "enter": True, "enter_key": "main", "delay_ms": delay_ms}
            for line in lines]


# ----- indentation handling for editors that auto-indent -----
INDENT_MODES = [
    ("compensate", "Compensate for editor auto-indent"),
    ("asis", "Type as written"),
    ("strip", "Strip all indentation"),
]
INDENT_LABELS = dict(INDENT_MODES)
INDENT_KEYS = {label: key for key, label in INDENT_MODES}
BACKSPACE = "\b"


TAB_WIDTH = 4
MONO_FONT = "PicoMono"   # named font, created in PicoGUI.__init__


def display_text(text):
    """How a step's text is shown in the list: leading spaces as '·',
    tabs as '→' padded to the next tab stop, empty text as '(Enter)'.
    Display only -- the real text is unchanged."""
    if text == "":
        return "(Enter)"
    out = []
    col = 0
    leading = True
    for ch in text:
        if ch == "\t":
            pad = TAB_WIDTH - (col % TAB_WIDTH)
            out.append("\u2192" + " " * (pad - 1))
            col += pad
        elif ch == " " and leading:
            out.append("\u00b7")
            col += 1
        elif ch == BACKSPACE:
            out.append("\u232b")
            col += 1
        else:
            leading = False
            out.append(ch)
            col += 1
    return "".join(out)


def leading_ws(text):
    return len(text) - len(text.lstrip(" \t"))


def apply_indent_mode(steps, mode):
    """Return copies of steps adjusted for how the target editor indents.

    compensate: the editor copies the previous line's indentation after Enter,
                so type only the difference -- extra spaces/tabs to go deeper,
                Backspaces to come back out.
    strip:      drop all leading spaces/tabs.
    asis:       unchanged.
    Assumes the cursor starts on an unindented line when the macro begins.
    """
    out = []
    prev = 0            # indentation the editor will auto-insert on the next line
    line_start = True   # is the next step's text at the start of a new line?
    for st in steps:
        st = dict(st)
        text = st["text"]
        if line_start and mode != "asis":
            n = leading_ws(text)
            body = text[n:]
            if mode == "strip":
                st["text"] = body
            elif body == "":
                st["text"] = ""          # blank line: editor keeps its indentation
            else:
                if n > prev:
                    st["text"] = text[prev:]                  # extra indentation + body
                elif n < prev:
                    st["text"] = BACKSPACE * (prev - n) + body
                else:
                    st["text"] = body
                prev = n
        line_start = st["enter"] or (line_start and text == "")
        out.append(st)
    return out


def load_json_macro(path):
    """Returns (steps, settings dict)."""
    data = json.loads(read_text_file(path))
    settings = {}
    if isinstance(data, dict):
        settings = {k: v for k, v in data.items() if k != "steps"}
        data = data.get("steps", [])
    if not isinstance(data, list):
        raise ValueError("JSON macro must be a list of steps or {\"steps\": [...]}.")
    return [normalize_step(s) for s in data if isinstance(s, dict)], settings


def save_json_macro(path, steps, line_delay_ms=None, caps_lock=None, indent_mode=None,
                    key_delay_ms=None, extra=None):
    data = {"steps": steps}
    if extra:
        data.update(extra)
    if key_delay_ms is not None:
        data["key_delay_ms"] = key_delay_ms
    if indent_mode is not None:
        data["indent_mode"] = indent_mode
    if line_delay_ms is not None:
        data["line_delay_ms"] = line_delay_ms
    if caps_lock is not None:
        data["caps_lock"] = bool(caps_lock)
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, indent=2) + "\n")


# ======================= networking =======================
class TypingTimeout(OSError):
    """The Pico accepted the text but didn't finish typing in time."""


DEFAULT_KEY_DELAY = 15   # ms, matches KEY_DELAY_MS in main.py


def typing_timeout(text, key_delay_ms=DEFAULT_KEY_DELAY):
    # Each char = key down + key up, each followed by key_delay_ms, plus USB
    # overhead. Be generous so slow speeds don't trip the timeout.
    per_char = (2 * key_delay_ms + 15) / 1000.0
    return 5 + 1.5 * per_char * (len(text) + 1)


def send_text(host, port, token, text, press_enter, enter_key="main",
              key_delay_ms=DEFAULT_KEY_DELAY):
    """Deliver <token>\\n<text> and wait until the Pico has finished typing it.

    The Pico closes the connection only after it's done typing, so we wait for
    that close. This keeps the next line from arriving while the Pico is still
    busy (which used to pile up connections until the Pico refused them)."""
    payload = token.encode() + b"\n" + text.encode("utf-8")
    if press_enter:
        payload += b"\r" if enter_key == "keypad" else b"\n"
    s = socket.socket()
    s.settimeout(CONNECT_TIMEOUT)
    try:
        s.connect((host, port))
        s.sendall(payload)
        try:
            s.shutdown(socket.SHUT_WR)   # EOF -> Pico starts typing
        except OSError:
            pass
        s.settimeout(typing_timeout(text, key_delay_ms))
        try:
            while s.recv(64):            # Pico closes when typing is done
                pass
        except socket.timeout:
            raise TypingTimeout()
        except OSError:
            pass                         # reset on close is fine
    finally:
        s.close()


CAPS_ON_CMD = b"\x01"
CAPS_OFF_CMD = b"\x02"
REPLY_TIMEOUT = 4  # seconds to wait for the Pico's Caps Lock reply


VERSION_CMD = b"\x03"
NEEDED_FOR_BACKSPACE = 2
KEY_DELAY_CMD = b"\x04"
NEEDED_FOR_KEY_DELAY = 3


def send_command(host, port, token, cmd):
    """Send a one-byte command and return the Pico's reply ("" if none).
    Raises OSError if the Pico can't be reached."""
    payload = token.encode() + b"\n" + cmd
    s = socket.socket()
    s.settimeout(CONNECT_TIMEOUT)
    try:
        s.connect((host, port))
        s.sendall(payload)
        try:
            s.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        s.settimeout(REPLY_TIMEOUT)
        reply = b""
        try:
            while True:
                chunk = s.recv(64)
                if not chunk:
                    break
                reply += chunk
        except OSError:
            pass
        return reply.decode("ascii", "ignore").strip()
    finally:
        s.close()


def send_caps(host, port, token, on):
    """Force Caps Lock on/off. Reply: "ON", "OFF", "NOREPORT", or "" (old firmware)."""
    return send_command(host, port, token, CAPS_ON_CMD if on else CAPS_OFF_CMD)


def set_key_delay(host, port, token, ms):
    """Set the Pico's per-key delay. Returns the reply, e.g. "OK 40"."""
    return send_command(host, port, token, KEY_DELAY_CMD + str(int(ms)).encode())


def firmware_version(host, port, token):
    """Pico firmware version, or 0 for firmware too old to answer."""
    reply = send_command(host, port, token, VERSION_CMD)
    if reply.startswith("PICO "):
        try:
            return int(reply.split()[1])
        except (IndexError, ValueError):
            pass
    return 0


def friendly_error(e, host, port):
    if isinstance(e, TypingTimeout):
        return ("The Pico took the text but never finished typing it -- it may "
                "have hung or reset. Check its LED / serial output.")
    if isinstance(e, ConnectionRefusedError):
        return ("Connection refused -- reached %s but nothing is listening on "
                "port %d. Is the Pico booted and running main.py?" % (host, port))
    if isinstance(e, socket.timeout):
        return ("Timed out connecting to %s:%d -- wrong IP, or the Pico isn't "
                "on the network." % (host, port))
    if isinstance(e, socket.gaierror):
        return "Can't resolve host '%s' -- check the address." % host
    return "Network error: %s" % e


# ======================= step editor dialog =======================
class StepDialog(tk.Toplevel):
    def __init__(self, parent, title, step=None):
        super().__init__(parent)
        self.title(title)
        self.transient(parent)
        self.resizable(False, False)
        self.result = None
        step = normalize_step(step or {"text": "", "enter": True, "delay_ms": 1000})

        self.text_var = tk.StringVar(value=step["text"])
        self.enter_var = tk.BooleanVar(value=step["enter"])
        self.key_var = tk.StringVar(value=step["enter_key"])
        self.delay_var = tk.StringVar(value=str(step["delay_ms"]))

        f = ttk.Frame(self, padding=12)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="Command text").grid(row=0, column=0, columnspan=3, sticky="w")
        entry = ttk.Entry(f, textvariable=self.text_var, width=52, font=MONO_FONT)
        entry.grid(row=1, column=0, columnspan=3, sticky="we", pady=(2, 8))

        ttk.Checkbutton(f, text="Press Enter after", variable=self.enter_var,
                        command=self._toggle).grid(row=2, column=0, sticky="w")
        ttk.Label(f, text="Enter key").grid(row=2, column=1, sticky="e", padx=(8, 4))
        self.key_cb = ttk.Combobox(f, textvariable=self.key_var, values=("main", "keypad"),
                                   state="readonly", width=8)
        self.key_cb.grid(row=2, column=2, sticky="w")

        ttk.Label(f, text="Delay after (ms)").grid(row=3, column=0, sticky="w", pady=(8, 0))
        ttk.Spinbox(f, from_=0, to=600000, increment=100, textvariable=self.delay_var,
                    width=10).grid(row=3, column=1, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Label(f, text="Leave the text empty for a bare Enter press.",
                  foreground="gray").grid(row=4, column=0, columnspan=3, sticky="w", pady=(8, 0))

        btns = ttk.Frame(f)
        btns.grid(row=5, column=0, columnspan=3, sticky="e", pady=(12, 0))
        ttk.Button(btns, text="OK", command=self._ok).pack(side="right", padx=(6, 0))
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="right")

        self.bind("<Return>", lambda e: self._ok())
        self.bind("<Escape>", lambda e: self.destroy())
        self._toggle()

        self.update_idletasks()
        x = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_width()) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - self.winfo_height()) // 3
        self.geometry("+%d+%d" % (max(x, 0), max(y, 0)))
        try:
            self.grab_set()
        except tk.TclError:
            pass
        entry.focus_set()
        entry.icursor("end")
        self.wait_window(self)

    def _toggle(self):
        self.key_cb.configure(state="readonly" if self.enter_var.get() else "disabled")

    def _ok(self):
        try:
            delay = int(self.delay_var.get().strip())
            if delay < 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid delay",
                                 "Delay must be a whole number of milliseconds (0 or more).",
                                 parent=self)
            return
        text = self.text_var.get()
        if text == "" and not self.enter_var.get():
            messagebox.showerror("Empty step", "A step needs text, an Enter press, or both.",
                                 parent=self)
            return
        self.result = {"text": text, "enter": self.enter_var.get(),
                       "enter_key": self.key_var.get(), "delay_ms": delay}
        self.destroy()


# ======================= main window =======================
class PicoGUI:
    def __init__(self, root):
        self.root = root
        root.title("Pico Control")
        root.minsize(720, 640)

        # Keep a reference: a tkfont.Font deletes its named Tk font when garbage-collected.
        families = set(tkfont.families(root))
        mono_family = next((f for f in ("Consolas", "Cascadia Mono", "Courier New")
                            if f in families),
                           tkfont.nametofont("TkFixedFont").actual("family"))
        self.mono = tkfont.Font(root, name=MONO_FONT, family=mono_family, size=10)
        style = ttk.Style(root)
        style.configure("Macro.Treeview", font=self.mono,
                        rowheight=self.mono.metrics("linespace") + 6)

        self.log_q = queue.Queue()
        self.busy = False
        self.stop_evt = threading.Event()
        self.autosave_warned = False

        pad = {"padx": 8, "pady": 4}

        # --- connection settings ---
        conn = ttk.LabelFrame(root, text="Connection")
        conn.pack(fill="x", padx=10, pady=(10, 4))
        self.host_var = tk.StringVar(value=DEFAULT_HOST)
        self.port_var = tk.StringVar(value=str(DEFAULT_PORT))
        self.token_var = tk.StringVar(value=DEFAULT_TOKEN)
        ttk.Label(conn, text="Host").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(conn, textvariable=self.host_var, width=22).grid(row=0, column=1, sticky="w", **pad)
        ttk.Label(conn, text="Port").grid(row=0, column=2, sticky="w", **pad)
        ttk.Entry(conn, textvariable=self.port_var, width=8).grid(row=0, column=3, sticky="w", **pad)
        ttk.Label(conn, text="Token").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(conn, textvariable=self.token_var, width=44, show="*").grid(
            row=1, column=1, columnspan=3, sticky="w", **pad)
        ttk.Label(conn, text="Key delay (ms)").grid(row=0, column=4, sticky="w", **pad)
        self.key_delay_var = tk.StringVar(value=str(DEFAULT_KEY_DELAY))
        ttk.Spinbox(conn, from_=1, to=1000, increment=5, width=7,
                    textvariable=self.key_delay_var).grid(row=0, column=5, sticky="w", **pad)
        ttk.Label(conn, text="higher = slower typing", foreground="gray").grid(
            row=1, column=4, columnspan=2, sticky="w", **pad)
        self.key_delay_var.trace_add("write", lambda *a: self._save_autosave())
        for var in (self.host_var, self.port_var, self.token_var):
            var.trace_add("write", lambda *a: self._save_autosave())
        self.old_fw_warned = False

        # --- macro editor ---
        macro = ttk.LabelFrame(root, text="Macro")
        macro.pack(fill="both", expand=True, padx=10, pady=4)

        body = ttk.Frame(macro)
        body.pack(fill="both", expand=True, padx=6, pady=(6, 0))

        cols = ("num", "text", "enter", "delay")
        self.tree = ttk.Treeview(body, columns=cols, show="headings",
                                 selectmode="browse", height=9, style="Macro.Treeview")
        self.tree.heading("num", text="#")
        self.tree.heading("text", text="Command")
        self.tree.heading("enter", text="Enter")
        self.tree.heading("delay", text="Delay after (ms)")
        self.tree.column("num", width=36, anchor="center", stretch=False)
        self.tree.column("text", width=330, anchor="w")
        self.tree.column("enter", width=90, anchor="center", stretch=False)
        self.tree.column("delay", width=110, anchor="e", stretch=False)
        vsb = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="left", fill="y")
        self.tree.bind("<Double-1>", lambda e: self.edit_step())
        self.tree.bind("<Delete>", lambda e: self.remove_step())

        side = ttk.Frame(body)
        side.pack(side="left", fill="y", padx=(8, 0))
        self.edit_widgets = []
        for label, cmd in (("Add...", self.add_step), ("Edit...", self.edit_step),
                           ("Duplicate", self.duplicate_step), ("Remove", self.remove_step),
                           (None, None),
                           ("Move Up", lambda: self.move_step(-1)),
                           ("Move Down", lambda: self.move_step(1))):
            if label is None:
                ttk.Separator(side).pack(fill="x", pady=6)
                continue
            b = ttk.Button(side, text=label, command=cmd, width=12)
            b.pack(fill="x", pady=2)
            self.edit_widgets.append(b)

        bar = ttk.Frame(macro)
        bar.pack(fill="x", padx=6, pady=6)
        self.run_btn = ttk.Button(bar, text="Run Macro", command=self.on_run_macro)
        self.run_btn.pack(side="left")
        self.stop_btn = ttk.Button(bar, text="Stop", command=self.on_stop, state="disabled")
        self.stop_btn.pack(side="left", padx=(6, 12))
        for label, cmd in (("Load File...", self.load_file), ("Save As...", self.save_as),
                           ("Reset to Default", self.reset_default)):
            b = ttk.Button(bar, text=label, command=cmd)
            b.pack(side="left", padx=2)
            self.edit_widgets.append(b)
        self.summary_var = tk.StringVar()
        ttk.Label(bar, textvariable=self.summary_var, foreground="gray").pack(side="right")

        bar2 = ttk.Frame(macro)
        bar2.pack(fill="x", padx=6, pady=(0, 6))
        self.caps_var = tk.BooleanVar(value=False)
        ttk.Label(bar2, text="Caps Lock").pack(side="left", padx=(0, 6))
        for label, value in (("ON", True), ("OFF", False)):
            rb = ttk.Radiobutton(bar2, text=label, value=value, variable=self.caps_var,
                                 style="Toolbutton", width=5, command=self._caps_clicked)
            rb.pack(side="left", padx=1)
            self.edit_widgets.append(rb)
        ttk.Separator(bar2, orient="vertical").pack(side="left", fill="y", padx=10)
        ttk.Label(bar2, text="Delay between lines (ms)").pack(side="left")
        self.line_delay_var = tk.StringVar(value="1000")
        sb = ttk.Spinbox(bar2, from_=0, to=600000, increment=100,
                         textvariable=self.line_delay_var, width=9)
        sb.pack(side="left", padx=(6, 6))
        self.edit_widgets.append(sb)
        b = ttk.Button(bar2, text="Apply to All Steps", command=self.apply_delay_all)
        b.pack(side="left")
        self.edit_widgets.append(b)
        self.line_delay_var.trace_add("write", lambda *a: self._save_autosave())
        self._caps_changed(save=False)

        bar3 = ttk.Frame(macro)
        bar3.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Label(bar3, text="Indentation").pack(side="left", padx=(0, 6))
        self.indent_var = tk.StringVar(value=INDENT_LABELS["compensate"])
        cb = ttk.Combobox(bar3, textvariable=self.indent_var, state="readonly", width=34,
                          values=[label for _, label in INDENT_MODES])
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", lambda e: self._save_autosave())
        self.edit_widgets.append(cb)

        # --- custom command ---
        custom = ttk.LabelFrame(root, text="Custom command")
        custom.pack(fill="x", padx=10, pady=4)
        self.cmd_var = tk.StringVar()
        self.enter_var = tk.BooleanVar(value=True)
        entry = ttk.Entry(custom, textvariable=self.cmd_var, font=MONO_FONT)
        entry.pack(side="left", fill="x", expand=True, padx=8, pady=8)
        entry.bind("<Return>", lambda e: self.on_send_custom())
        ttk.Checkbutton(custom, text="+Enter", variable=self.enter_var).pack(side="left", padx=4)
        self.send_btn = ttk.Button(custom, text="Send", command=self.on_send_custom)
        self.send_btn.pack(side="left", padx=4)
        add_btn = ttk.Button(custom, text="Add to Macro", command=self.custom_to_macro)
        add_btn.pack(side="left", padx=(0, 8))
        self.edit_widgets.append(add_btn)

        # --- log ---
        logf = ttk.LabelFrame(root, text="Log")
        logf.pack(fill="both", expand=False, padx=10, pady=(4, 10))
        self.log = scrolledtext.ScrolledText(logf, height=8, state="disabled", wrap="word",
                                             font=MONO_FONT)
        self.log.pack(fill="both", expand=True, padx=6, pady=6)

        # --- initial macro: autosave if present, else pico_macro.py defaults ---
        self.steps = [normalize_step(s) for s in DEFAULT_STEPS]
        if os.path.exists(AUTOSAVE_PATH):
            try:
                self.steps, saved = load_json_macro(AUTOSAVE_PATH)
                d = saved.get("line_delay_ms")
                if isinstance(d, int) and d >= 0:
                    self.line_delay_var.set(str(d))
                for key, var in (("host", self.host_var), ("port", self.port_var),
                                 ("token", self.token_var)):
                    if isinstance(saved.get(key), (str, int)) and str(saved[key]):
                        var.set(str(saved[key]))
                kd = saved.get("key_delay_ms")
                if isinstance(kd, int) and 1 <= kd <= 1000:
                    self.key_delay_var.set(str(kd))
                if saved.get("indent_mode") in INDENT_LABELS:
                    self.indent_var.set(INDENT_LABELS[saved["indent_mode"]])
                if isinstance(saved.get("caps_lock"), bool):
                    self.caps_var.set(saved["caps_lock"])
                    self._caps_changed(save=False)
                self.say("Loaded saved macro (%d steps) from %s" % (len(self.steps), AUTOSAVE_PATH))
            except (OSError, ValueError) as e:
                self.say("! Couldn't read %s (%s); using pico_macro.py defaults." % (AUTOSAVE_PATH, e))
        self.refresh_tree()
        self.root.after(100, self._drain_log)

    # ---------- logging (thread-safe) ----------
    def say(self, msg):
        self.log_q.put(msg)

    def _drain_log(self):
        while True:
            try:
                msg = self.log_q.get_nowait()
            except queue.Empty:
                break
            self.log.configure(state="normal")
            self.log.insert("end", msg + "\n")
            self.log.see("end")
            self.log.configure(state="disabled")
        self.root.after(100, self._drain_log)

    # ---------- macro list management ----------
    def refresh_tree(self, select=None):
        self.tree.delete(*self.tree.get_children())
        for i, st in enumerate(self.steps):
            if not st["enter"]:
                enter = "No"
            elif st["enter_key"] == "keypad":
                enter = "Yes (keypad)"
            else:
                enter = "Yes"
            text = display_text(st["text"])
            self.tree.insert("", "end", iid=str(i),
                             values=(i + 1, text, enter, st["delay_ms"]))
        if select is not None and 0 <= select < len(self.steps):
            self.tree.selection_set(str(select))
            self.tree.see(str(select))
        total = sum(st["delay_ms"] for st in self.steps) / 1000.0
        self.summary_var.set("%d steps  ·  ~%.1f s of delays" % (len(self.steps), total))

    def selected_index(self):
        sel = self.tree.selection()
        return int(sel[0]) if sel else None

    def _key_delay(self, default=DEFAULT_KEY_DELAY):
        try:
            v = int(self.key_delay_var.get().strip())
            if 1 <= v <= 1000:
                return v
        except ValueError:
            pass
        return default

    def _indent_mode(self):
        return INDENT_KEYS.get(self.indent_var.get(), "compensate")

    def _line_delay(self):
        """Current 'Delay between lines' value, or None if it's not valid."""
        try:
            v = int(self.line_delay_var.get().strip())
            return v if v >= 0 else None
        except ValueError:
            return None

    def _changed(self, select=None):
        self.refresh_tree(select)
        self._save_autosave()

    def _save_autosave(self):
        if not hasattr(self, "steps"):
            return
        try:
            save_json_macro(AUTOSAVE_PATH, self.steps, self._line_delay(),
                            self.caps_var.get(), self._indent_mode(),
                            self._key_delay(None),
                            # connection settings live only in the local autosave
                            extra={"host": self.host_var.get(),
                                   "port": self.port_var.get(),
                                   "token": self.token_var.get()})
        except OSError as e:
            if not self.autosave_warned:
                self.say("! Couldn't autosave macro to %s: %s" % (AUTOSAVE_PATH, e))
                self.autosave_warned = True

    def add_step(self):
        d = StepDialog(self.root, "Add step")
        if d.result is None:
            return
        i = self.selected_index()
        pos = len(self.steps) if i is None else i + 1
        self.steps.insert(pos, d.result)
        self._changed(pos)

    def edit_step(self):
        if self.busy:
            return
        i = self.selected_index()
        if i is None:
            return
        d = StepDialog(self.root, "Edit step %d" % (i + 1), self.steps[i])
        if d.result is None:
            return
        self.steps[i] = d.result
        self._changed(i)

    def duplicate_step(self):
        i = self.selected_index()
        if i is None:
            return
        self.steps.insert(i + 1, dict(self.steps[i]))
        self._changed(i + 1)

    def remove_step(self):
        if self.busy:
            return
        i = self.selected_index()
        if i is None:
            return
        del self.steps[i]
        self._changed(min(i, len(self.steps) - 1))

    def move_step(self, delta):
        i = self.selected_index()
        if i is None:
            return
        j = i + delta
        if not 0 <= j < len(self.steps):
            return
        self.steps[i], self.steps[j] = self.steps[j], self.steps[i]
        self._changed(j)

    def custom_to_macro(self):
        text = self.cmd_var.get()
        if text == "" and not self.enter_var.get():
            return
        i = self.selected_index()
        pos = len(self.steps) if i is None else i + 1
        delay = self._line_delay() or 0
        self.steps.insert(pos, {"text": text, "enter": self.enter_var.get(),
                                "enter_key": "main", "delay_ms": delay})
        self._changed(pos)
        self.say("Added to macro as step %d (delay %d ms -- double-click to edit)." % (pos + 1, delay))

    def apply_delay_all(self):
        delay = self._line_delay()
        if delay is None:
            messagebox.showerror("Invalid delay",
                                 "Delay between lines must be a whole number of ms (0 or more).")
            return
        if not self.steps:
            return
        for st in self.steps:
            st["delay_ms"] = delay
        self._changed(self.selected_index())
        self.say("Set every step's delay to %d ms." % delay)

    def load_file(self):
        delay = self._line_delay()
        if delay is None:
            messagebox.showerror("Invalid delay",
                                 "Set 'Delay between lines' to a whole number of ms first.")
            return
        path = filedialog.askopenfilename(
            title="Load text to replay",
            filetypes=[("Text files", "*.txt"), ("Saved macro (JSON)", "*.json"),
                       ("All files", "*.*")])
        if not path:
            return
        try:
            if path.lower().endswith(".json"):
                steps, _ = load_json_macro(path)
                desc = "%d steps (saved macro)" % len(steps)
            else:
                steps = plain_text_to_steps(read_text_file(path), delay)
                desc = "%d lines, %d ms apart" % (len(steps), delay)
        except (OSError, ValueError) as e:
            messagebox.showerror("Load failed", "Couldn't load %s:\n%s" % (path, e))
            return
        if not steps:
            messagebox.showwarning("Empty file", "No lines found in that file.")
            return
        if self.steps and not messagebox.askyesno(
                "Replace macro", "Replace the current %d-step macro with %s from\n%s?"
                % (len(self.steps), desc, os.path.basename(path))):
            return
        self.steps = steps
        self._changed(0)
        self.say("Loaded %s from %s" % (desc, path))

    def save_as(self):
        path = filedialog.asksaveasfilename(
            title="Save macro as", defaultextension=".json",
            filetypes=[("Saved macro (JSON)", "*.json")])
        if not path:
            return
        try:
            save_json_macro(path, self.steps, self._line_delay(), self.caps_var.get(),
                            self._indent_mode(), self._key_delay(None))
        except OSError as e:
            messagebox.showerror("Save failed", str(e))
            return
        self.say("Saved %d steps to %s" % (len(self.steps), path))

    def reset_default(self):
        if not messagebox.askyesno("Reset macro",
                                   "Replace the current macro with the default steps "
                                   "from pico_macro.py?"):
            return
        self.steps = [normalize_step(s) for s in DEFAULT_STEPS]
        self._changed(0)
        self.say("Macro reset to pico_macro.py defaults (%d steps)." % len(self.steps))

    # ---------- settings / state helpers ----------
    def _conn(self):
        host = self.host_var.get().strip()
        try:
            port = int(self.port_var.get().strip())
        except ValueError:
            raise ValueError("Port must be a number.")
        return host, port, self.token_var.get()

    def _set_busy(self, busy, macro=False):
        self.busy = busy
        state = "disabled" if busy else "normal"
        self.run_btn.configure(state=state)
        self.send_btn.configure(state=state)
        self.stop_btn.configure(state="normal" if (busy and macro) else "disabled")
        for w in self.edit_widgets:
            if not busy and isinstance(w, ttk.Combobox):
                w.configure(state="readonly")
            else:
                w.configure(state=state)

    # ---------- Caps Lock ----------
    def _caps_changed(self, save=True):
        if save:
            self._save_autosave()

    def _caps_clicked(self):
        """ON/OFF pressed: remember the choice and apply it to the target now."""
        self._save_autosave()
        self.on_caps_now()

    def _apply_caps(self, host, port, token):
        """Force Caps Lock on the target. Returns False only if the Pico is unreachable."""
        want = "ON" if self.caps_var.get() else "OFF"
        try:
            reply = send_caps(host, port, token, want == "ON")
        except OSError as e:
            self.say("! Caps Lock: " + friendly_error(e, host, port))
            return False
        if reply == want:
            self.say("Caps Lock is %s" % want)
        elif reply == "NOREPORT":
            self.say("! Caps Lock: the target didn't report its Caps Lock state, so it "
                     "couldn't be confirmed (Caps Lock may have been pressed once).")
        elif reply == "":
            self.say("! Caps Lock: no reply from the Pico -- is the updated main.py on it? "
                     "(Also check the token.)")
        else:
            self.say("! Caps Lock: unexpected reply %r" % reply)
        return True

    def on_caps_now(self):
        if self.busy:
            return
        try:
            host, port, token = self._conn()
        except ValueError as e:
            self.say("! " + str(e))
            return
        self._set_busy(True)

        def work():
            try:
                self._apply_caps(host, port, token)
            finally:
                self.root.after(0, lambda: self._set_busy(False))
        threading.Thread(target=work, daemon=True).start()

    # ---------- Pico preparation (speed + capability check) ----------
    def _prepare_pico(self, host, port, token, key_delay, need_backspace, quiet=False):
        """Check firmware, apply the typing speed. Returns False to stop.
        Raises OSError if the Pico can't be reached."""
        ver = firmware_version(host, port, token)
        if need_backspace and ver < NEEDED_FOR_BACKSPACE:
            self.say("! The Pico's firmware can't press Backspace, which "
                     "'Compensate for editor auto-indent' needs to step "
                     "indentation back out. Copy the latest main.py to the "
                     "Pico, reset it, and run again.")
            self.say("  Stopped (nothing was typed).")
            return False
        if ver >= NEEDED_FOR_KEY_DELAY:
            reply = set_key_delay(host, port, token, key_delay)
            if reply.startswith("OK"):
                if not quiet:
                    self.say("Pico firmware v%d, key delay %d ms" % (ver, key_delay))
            else:
                self.say("! Couldn't set the typing speed (reply %r)." % reply)
        elif not self.old_fw_warned:
            self.old_fw_warned = True
            self.say("! The Pico's firmware is too old to change typing speed, so "
                     "it types at its built-in speed. Copy the latest main.py to "
                     "the Pico to use the Key delay setting.")
        return True

    # ---------- actions ----------
    def on_send_custom(self):
        if self.busy:
            return
        text = self.cmd_var.get()
        if text == "":
            return
        try:
            host, port, token = self._conn()
        except ValueError as e:
            self.say("! " + str(e))
            return
        enter = self.enter_var.get()
        self.run_key_delay = self._key_delay()
        self._set_busy(True)
        threading.Thread(target=self._send_worker,
                         args=(host, port, token, text, enter), daemon=True).start()

    def _send_worker(self, host, port, token, text, enter):
        try:
            if not self._prepare_pico(host, port, token, self.run_key_delay,
                                      need_backspace=False, quiet=True):
                return
            send_text(host, port, token, text, enter, key_delay_ms=self.run_key_delay)
            self.say("sent: %s%s" % (text, "  +Enter" if enter else ""))
        except OSError as e:
            self.say("! " + friendly_error(e, host, port))
        finally:
            self.root.after(0, lambda: self._set_busy(False))

    def on_run_macro(self):
        if self.busy:
            return
        if not self.steps:
            self.say("! The macro has no steps.")
            return
        try:
            host, port, token = self._conn()
        except ValueError as e:
            self.say("! " + str(e))
            return
        self.stop_evt.clear()
        self._set_busy(True, macro=True)
        steps = [dict(s) for s in self.steps]
        steps = apply_indent_mode(steps, self._indent_mode())
        self.run_key_delay = self._key_delay()
        threading.Thread(target=self._macro_worker,
                         args=(host, port, token, steps), daemon=True).start()

    def on_stop(self):
        self.stop_evt.set()
        self.say("Stopping...")

    def _highlight(self, i):
        if self.tree.exists(str(i)):
            self.tree.selection_set(str(i))
            self.tree.see(str(i))

    def _macro_worker(self, host, port, token, steps):
        n = len(steps)
        self.say("--- Macro: %s:%d  (%d steps) ---" % (host, port, n))
        try:
            if not self._apply_caps(host, port, token):
                self.say("  Stopped.")
                return
            try:
                ok = self._prepare_pico(host, port, token, self.run_key_delay,
                                        any(BACKSPACE in st["text"] for st in steps))
            except OSError as e:
                self.say("  ! " + friendly_error(e, host, port))
                self.say("  Stopped.")
                return
            if not ok:
                return
            if self.stop_evt.wait(0.2):      # let the target settle
                self.say("Stopped.")
                return
            for i, st in enumerate(steps):
                if self.stop_evt.is_set():
                    self.say("Stopped before step %d." % (i + 1))
                    return
                self.root.after(0, self._highlight, i)
                label = st["text"].replace(BACKSPACE, "\u232b") if st["text"] else "(Enter)"
                suffix = ("  +Enter[%s]" % st["enter_key"]) if st["enter"] else ""
                self.say("[%d/%d] %s%s" % (i + 1, n, label, suffix))
                try:
                    send_text(host, port, token, st["text"], st["enter"], st["enter_key"],
                              key_delay_ms=self.run_key_delay)
                except OSError as e:
                    self.say("  ! " + friendly_error(e, host, port))
                    self.say("  Stopped.")
                    return
                if st["delay_ms"] and self.stop_evt.wait(st["delay_ms"] / 1000.0):
                    self.say("Stopped after step %d." % (i + 1))
                    return
            self.say("done")
        finally:
            self.root.after(0, lambda: self._set_busy(False))


def main():
    root = tk.Tk()
    app = PicoGUI(root)  # keep a reference for the window's lifetime
    root.mainloop()


if __name__ == "__main__":
    main()

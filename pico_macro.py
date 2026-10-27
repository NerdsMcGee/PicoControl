#!/usr/bin/env python3
"""
pico_macro.py -- send a timed sequence of commands for the Pico to type.

Edit the STEPS list below, then run:  python pico_macro.py

Each step types some text, optionally presses Enter, then waits delay_ms
before the next step. Settings (host/port/token) are reused from
pico_type.py if it sits in the same folder; otherwise edit the fallback
DEFAULTS just below.
"""

import os
import socket
import sys
import time

# ----- settings: reuse pico_type.py if present, else edit these -----
try:
    from pico_type import DEFAULT_HOST, DEFAULT_PORT, DEFAULT_TOKEN
except ImportError:
    DEFAULT_HOST  = "192.168.1.100"
    DEFAULT_PORT  = 8822
    DEFAULT_TOKEN = "change-this-to-a-long-random-string"

HOST  = os.environ.get("PICO_HOST", DEFAULT_HOST)
PORT  = int(os.environ.get("PICO_PORT", DEFAULT_PORT))
TOKEN = os.environ.get("PICO_TOKEN", DEFAULT_TOKEN)

# ===================== THE SEQUENCE: EDIT HERE =====================
# Each step:  text to type, whether to press Enter after, pause after (ms).
#   "delay_ms" is how long to wait AFTER this step before the next one.
#   For a bare Enter with no text, use  {"text": "", "enter": True, ...}
STEPS = [
    # "enter_key": "main" (normal Enter, default) or "keypad" (numeric-pad Enter)
    {"text": 'load "txt:ns2"',                   "enter": True, "delay_ms": 2650},
    {"text": "",             			 "enter": True, "delay_ms": 3000},  # additional Enter
    {"text": "run",                              "enter": True, "delay_ms": 2750},
    {"text": "remove nsr,&h60000000,&h3fffffff", "enter": True, "delay_ms": 2000},
    {"text": "V=START(39380,192,168,1,200,0,0,0,0)", "enter": True, "delay_ms": 1200},
]
# ===================================================================

CONNECT_TIMEOUT = 5


def send_text(text, press_enter, enter_key="main"):
    payload = TOKEN.encode() + b"\n" + text.encode("utf-8")
    if press_enter:
        payload += b"\r" if enter_key == "keypad" else b"\n"
    s = socket.socket()
    s.settimeout(CONNECT_TIMEOUT)
    try:
        s.connect((HOST, PORT))
        s.sendall(payload)
        try:
            s.shutdown(socket.SHUT_WR)   # EOF -> Pico starts typing
        except OSError:
            pass
    finally:
        s.close()


def main():
    print("Target: %s:%d  (%d steps)" % (HOST, PORT, len(STEPS)))
    print("Make sure the right window is focused on the target machine.\n")
    for i, step in enumerate(STEPS, 1):
        text = step["text"]
        enter = step.get("enter", True)
        enter_key = step.get("enter_key", "main")
        delay_ms = step.get("delay_ms", 0)
        label = text if text else "(Enter)"
        suffix = ("  +Enter[%s]" % enter_key) if enter else ""
        print("[%d/%d] %s%s" % (i, len(STEPS), label, suffix))
        try:
            send_text(text, enter, enter_key)
        except OSError as e:
            print("  ! failed: %s" % e)
            print("  Stopped. Is the Pico running and the token correct?")
            return 1
        if delay_ms:
            time.sleep(delay_ms / 1000.0)
    print("\ndone")
    return 0


if __name__ == "__main__":
    sys.exit(main())

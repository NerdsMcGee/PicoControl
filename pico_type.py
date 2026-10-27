#!/usr/bin/env python3
"""
pico_type.py -- send text to the Pico 2 W so it types it out as a USB keyboard.

Runs on YOUR computer (not the Pico). Talks to the TCP listener in main.py.

Quick start
-----------
  Set your board's details once (edit the three DEFAULTS below, or use env vars
  PICO_HOST / PICO_PORT / PICO_TOKEN), then:

  One-shot:     python pico_type.py "hello world"
  With Enter:   python pico_type.py --enter "sudo reboot"
  From a file:  python pico_type.py --file notes.txt
  From a pipe:  echo "piped text" | python pico_type.py
  Interactive:  python pico_type.py
                (then type a line, press Enter, the Pico types it; Ctrl-C quits)

  Override per-run:  python pico_type.py --host 192.168.1.100 --token abc "hi"
"""

import argparse
import os
import socket
import sys

# ===================== DEFAULTS: EDIT THESE =======================
DEFAULT_HOST  = "192.168.1.100"    # example -- replace with your Pico's IP address
DEFAULT_PORT  = 8822
DEFAULT_TOKEN = "change-this-to-a-long-random-string"
# ==================================================================

CONNECT_TIMEOUT = 5  # seconds


def send_text(host, port, token, text, press_enter):
    """Open one connection, deliver <token>\\n<text>, let the Pico type it."""
    payload = token.encode() + b"\n" + text.encode("utf-8")
    if press_enter:
        payload += b"\n"
    s = socket.socket()
    s.settimeout(CONNECT_TIMEOUT)
    try:
        s.connect((host, port))
        s.sendall(payload)
        try:
            s.shutdown(socket.SHUT_WR)  # signal EOF so the Pico starts typing
        except OSError:
            pass
    finally:
        s.close()


def friendly_error(e, host, port):
    if isinstance(e, ConnectionRefusedError):
        return ("Connection refused -- reached %s but nothing is listening on "
                "port %d. Is the Pico booted and running main.py? (Watch the "
                "onboard LED for the idle heartbeat.)" % (host, port))
    if isinstance(e, socket.timeout):
        return ("Timed out connecting to %s:%d -- wrong IP, or the Pico isn't "
                "on the network." % (host, port))
    if isinstance(e, socket.gaierror):
        return "Can't resolve host '%s' -- check the address." % host
    return "Network error: %s" % e


def interactive(host, port, token, press_enter):
    print("Connected target: %s:%d" % (host, port))
    print("Type a line and press Enter to have the Pico type it. Ctrl-C to quit.\n")
    while True:
        try:
            line = input("type> ")
        except (EOFError, KeyboardInterrupt):
            print("\nbye")
            return 0
        if line == "":
            continue
        try:
            send_text(host, port, token, line, press_enter)
        except OSError as e:
            print("  ! " + friendly_error(e, host, port))


def main():
    ap = argparse.ArgumentParser(
        description="Send text for the Pico 2 W to type as a USB keyboard.")
    ap.add_argument("text", nargs="*", help="text to type (omit for interactive/stdin)")
    ap.add_argument("--host",  default=os.environ.get("PICO_HOST", DEFAULT_HOST))
    ap.add_argument("--port",  type=int,
                    default=int(os.environ.get("PICO_PORT", DEFAULT_PORT)))
    ap.add_argument("--token", default=os.environ.get("PICO_TOKEN", DEFAULT_TOKEN))
    ap.add_argument("--file",  help="read the text to type from this file")
    ap.add_argument("--enter", action="store_true",
                    help="press Enter on the target after typing")
    args = ap.parse_args()

    # Decide where the text comes from: --file > args > stdin pipe > interactive
    if args.file:
        try:
            with open(args.file, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError as e:
            print("Can't read file: %s" % e, file=sys.stderr)
            return 1
    elif args.text:
        text = " ".join(args.text)
    elif not sys.stdin.isatty():
        text = sys.stdin.read()
    else:
        return interactive(args.host, args.port, args.token, args.enter)

    try:
        send_text(args.host, args.port, args.token, text, args.enter)
    except OSError as e:
        print(friendly_error(e, args.host, args.port), file=sys.stderr)
        return 1
    print("sent %d chars to %s:%d" % (len(text), args.host, args.port))
    return 0


if __name__ == "__main__":
    sys.exit(main())

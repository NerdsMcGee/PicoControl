# main.py -- Raspberry Pi Pico 2 W: network-driven USB HID keyboard
#
# Runs on stock MicroPython (v1.25+) for the Pico 2 W (RP2350).
# Requires the keyboard HID library (one-time install, see README notes):
#     mpremote mip install usb-device-keyboard
# Also copy pico_secrets.py (WiFi + token, see pico_secrets.example.py)
# onto the Pico next to this file.
#
# What it does:
#   * On boot, connects to WiFi.
#   * Presents itself to the host it's PLUGGED INTO as a USB keyboard.
#   * Listens on a TCP port. A client sends:   <token>\n<text to type>
#     Everything after the first newline is typed out on the HID keyboard.
#
# Special characters typed as keys: \n Enter, \r Keypad Enter, \t Tab,
# \b Backspace.
#
# Caps Lock command:
#   If the payload after the token is exactly one byte, 0x01 or 0x02, it is
#   a command instead of text:
#       0x01 -> force Caps Lock ON      0x02 -> force Caps Lock OFF
#   The Pico reads the host's keyboard LED state, taps Caps Lock only if
#   needed, and replies on the same connection with ON, OFF, or NOREPORT
#   (the host never sent its LED state, so the result couldn't be confirmed).
#   Older firmware simply ignores these bytes (they aren't typeable).
#   A payload of exactly 0x03 asks for the firmware version; the reply is
#   "PICO <n>" (2 = supports the Backspace key, 3 = typing speed command).
#   A payload of 0x04 followed by digits (e.g. 0x04 "40") sets the delay
#   in ms used for each key press and release (until the Pico resets);
#   the reply is "OK <n>". Lower KEY_DELAY_MS below is the boot default.
#
# SAFE MODE (recovery): within the first 3 seconds after boot, TAP the
# BOOTSEL button to skip HID + networking and drop to a clean REPL, so you
# can always fix or re-flash the code. (Do NOT hold BOOTSEL through reset --
# that enters the UF2 bootloader instead. Let it boot, then tap.)

import sys
import time
import socket
import network
import rp2
from machine import Pin

# --- Onboard LED feedback -----------------------------------------
# On the Pico W / Pico 2 W the onboard LED hangs off the wireless chip,
# addressed as Pin("LED").
try:
    _led_pin = Pin("LED", Pin.OUT)
except Exception:
    _led_pin = None

def _led(on):
    if _led_pin is not None:
        _led_pin.value(1 if on else 0)

def blink(times, on_ms=120, off_ms=120):
    for _ in range(times):
        _led(True);  time.sleep_ms(on_ms)
        _led(False); time.sleep_ms(off_ms)

# --- Secrets: WiFi + auth token live in pico_secrets.py (not in git) ---
# Copy pico_secrets.example.py to pico_secrets.py, fill it in, and copy it
# onto the Pico next to main.py.
try:
    from pico_secrets import WIFI_SSID, WIFI_PASSWORD, AUTH_TOKEN
except ImportError:
    WIFI_SSID = WIFI_PASSWORD = AUTH_TOKEN = None

# ======================= CONFIG: EDIT THESE =======================
LISTEN_PORT   = 8822

PRESS_ENTER_AFTER = False   # tap Enter automatically after typing
KEY_DELAY_MS      = 15      # delay between keystrokes (raise if chars drop)
RECV_TIMEOUT_S    = 5       # per-connection read timeout
MAX_PAYLOAD       = 8192    # safety cap on bytes typed per connection
CAPS_SETTLE_MS    = 500     # how long to wait for the host's LED update
# ==================================================================


# --- Safe-mode window: tap BOOTSEL to bail out to the REPL ---------
print("Safe mode: tap BOOTSEL within 3s to skip HID + network...")
_t0 = time.ticks_ms()
while time.ticks_diff(time.ticks_ms(), _t0) < 3000:
    if rp2.bootsel_button():
        print("Safe mode entered. HID/network skipped; REPL is free.")
        sys.exit()
    time.sleep_ms(20)


# --- US-layout ASCII -> HID usage id ------------------------------
# Convention of usb.device.keyboard.send_keys(): a NEGATIVE value is a
# modifier bit (0x02 = Left Shift), a POSITIVE value is a key usage id.
SHIFT = 0x02

_KEYS = {
    'a':0x04,'b':0x05,'c':0x06,'d':0x07,'e':0x08,'f':0x09,'g':0x0a,
    'h':0x0b,'i':0x0c,'j':0x0d,'k':0x0e,'l':0x0f,'m':0x10,'n':0x11,
    'o':0x12,'p':0x13,'q':0x14,'r':0x15,'s':0x16,'t':0x17,'u':0x18,
    'v':0x19,'w':0x1a,'x':0x1b,'y':0x1c,'z':0x1d,
    '1':0x1e,'2':0x1f,'3':0x20,'4':0x21,'5':0x22,'6':0x23,'7':0x24,
    '8':0x25,'9':0x26,'0':0x27,
    '\n':0x28,'\r':0x58,'\t':0x2b,' ':0x2c,   # \n = Enter/Return, \r = Keypad Enter
    '\b':0x2a,                                 # \b = Backspace
    '-':0x2d,'=':0x2e,'[':0x2f,']':0x30,'\\':0x31,';':0x33,
    "'":0x34,'`':0x35,',':0x36,'.':0x37,'/':0x38,
}

_SHIFTED = {
    '!':'1','@':'2','#':'3','$':'4','%':'5','^':'6','&':'7','*':'8',
    '(':'9',')':'0','_':'-','+':'=','{':'[','}':']','|':'\\',':':';',
    '"':"'",'~':'`','<':',','>':'.','?':'/',
}

CAPS_LOCK_KEY = 0x39       # HID usage id for Caps Lock
LED_CAPS_LOCK = 0x02       # bit in the host's keyboard LED report
CMD_CAPS_ON   = b"\x01"
CMD_CAPS_OFF  = b"\x02"
CMD_VERSION   = b"\x03"
CMD_KEY_DELAY = b"\x04"    # followed by ASCII digits, e.g. b"\x0440"
FIRMWARE_VERSION = 3       # 1 = Caps Lock, 2 = + Backspace, 3 = + typing speed


def _report_for(ch):
    if ch in _KEYS:
        return [_KEYS[ch]]
    if 'A' <= ch <= 'Z':
        return [-SHIFT, _KEYS[ch.lower()]]
    if ch in _SHIFTED:
        return [-SHIFT, _KEYS[_SHIFTED[ch]]]
    return None  # unsupported char (e.g. non-ASCII) -> skipped


def make_keyboard():
    import usb.device
    from usb.device.keyboard import KeyboardInterface

    class LedKeyboard(KeyboardInterface):
        """Keyboard that remembers the LED state (Caps/Num/Scroll) the host sends."""
        def __init__(self):
            super().__init__()
            self.leds = None          # unknown until the host sends an LED report

        def on_led_update(self, led_mask):
            self.leds = led_mask

    kbd = LedKeyboard()
    usb.device.get().init(kbd, builtin_driver=True)  # keep USB-CDC serial too
    while not kbd.is_open():                          # wait for host to enumerate
        time.sleep_ms(50)
    time.sleep_ms(300)                                # settle
    return kbd


def type_string(kbd, s):
    for ch in s:
        rep = _report_for(ch)
        if rep is None:
            continue
        kbd.send_keys(rep)
        time.sleep_ms(KEY_DELAY_MS)
        kbd.send_keys([])          # release
        time.sleep_ms(KEY_DELAY_MS)


# --- Caps Lock control ---------------------------------------------
def _tap_caps(kbd):
    kbd.send_keys([CAPS_LOCK_KEY])
    time.sleep_ms(30)
    kbd.send_keys([])
    time.sleep_ms(30)


def _wait_led_change(kbd, before, timeout_ms):
    t0 = time.ticks_ms()
    while time.ticks_diff(time.ticks_ms(), t0) < timeout_ms:
        if kbd.leds != before:
            return True
        time.sleep_ms(10)
    return False


def set_caps(kbd, want_on):
    """Force Caps Lock to want_on. Returns b"ON", b"OFF" or b"NOREPORT"."""
    if kbd.leds is None:
        # Host hasn't told us its state yet: tap once and see what it reports.
        _tap_caps(kbd)
        if not _wait_led_change(kbd, None, CAPS_SETTLE_MS):
            return b"NOREPORT"
    if bool(kbd.leds & LED_CAPS_LOCK) != want_on:
        before = kbd.leds
        _tap_caps(kbd)
        if not _wait_led_change(kbd, before, CAPS_SETTLE_MS):
            return b"NOREPORT"
    return b"ON" if (kbd.leds & LED_CAPS_LOCK) else b"OFF"


# --- WiFi ----------------------------------------------------------
def wifi_connect():
    if WIFI_SSID is None or AUTH_TOKEN is None:
        raise RuntimeError("pico_secrets.py missing -- copy it onto the Pico next to main.py")
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if not wlan.isconnected():
        wlan.connect(WIFI_SSID, WIFI_PASSWORD)
        for _ in range(40):                 # ~20s
            if wlan.isconnected():
                break
            time.sleep_ms(500)
    if not wlan.isconnected():
        raise RuntimeError("WiFi connect failed")
    return wlan.ifconfig()[0]


# --- TCP server ----------------------------------------------------
def handle(conn, kbd):
    global KEY_DELAY_MS
    conn.settimeout(RECV_TIMEOUT_S)
    buf = b""
    try:
        while len(buf) < MAX_PAYLOAD:
            chunk = conn.recv(256)
            if not chunk:
                break
            buf += chunk
    except OSError:
        pass  # read timeout -> process what we have

    if b"\n" not in buf:
        return
    token, _, payload = buf.partition(b"\n")
    if token.strip() != AUTH_TOKEN.encode():
        print("auth rejected")
        blink(3, 60, 60)          # 3 quick blinks = token rejected
        return

    if payload[:1] == CMD_KEY_DELAY and len(payload) > 1:
        try:
            KEY_DELAY_MS = max(1, min(1000, int(payload[1:])))
            reply = b"OK %d" % KEY_DELAY_MS
        except ValueError:
            reply = b"ERR"
        print("key delay ->", KEY_DELAY_MS)
        try:
            conn.send(reply)
        except OSError:
            pass
        return

    if payload == CMD_VERSION:
        try:
            conn.send(b"PICO %d" % FIRMWARE_VERSION)
        except OSError:
            pass
        return

    if payload in (CMD_CAPS_ON, CMD_CAPS_OFF):
        want = payload == CMD_CAPS_ON
        result = set_caps(kbd, want)
        print("caps lock -> %s (wanted %s)" % (result.decode(), "ON" if want else "OFF"))
        try:
            conn.send(result)
        except OSError:
            pass
        blink(1, 60, 60)          # 1 quick blink = command handled
        return

    text = payload.decode("utf-8", "ignore")
    print("typing %d chars" % len(text))
    _led(True)                     # solid ON while typing
    type_string(kbd, text)
    if PRESS_ENTER_AFTER:
        type_string(kbd, "\n")
    _led(False)
    blink(2, 60, 60)              # 2 quick blinks = done typing


def serve(ip, kbd):
    addr = socket.getaddrinfo("0.0.0.0", LISTEN_PORT)[0][-1]
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(addr)
    s.listen(1)
    s.settimeout(2)              # wake periodically to heartbeat the LED
    print("listening on %s:%d" % (ip, LISTEN_PORT))
    while True:
        try:
            conn, peer = s.accept()
        except OSError:
            # no connection this cycle -> brief heartbeat flash (idle/listening)
            _led(True); time.sleep_ms(30); _led(False)
            continue
        print("conn from %s" % (peer[0],))
        try:
            handle(conn, kbd)
        except Exception as e:
            print("handler error:", e)
        finally:
            try:
                conn.close()
            except Exception:
                pass


# --- Main ----------------------------------------------------------
def main():
    ip = wifi_connect()
    print("WiFi up, IP =", ip)
    blink(2, 150, 150)           # 2 slow blinks = WiFi connected
    kbd = make_keyboard()
    print("HID keyboard ready")
    blink(3, 150, 150)           # 3 slow blinks = HID keyboard ready
    serve(ip, kbd)


try:
    main()
except Exception as e:
    # Fall through to the REPL rather than silently dying, and flag it.
    print("FATAL:", e)
    while True:                  # fast continuous blink = startup failed
        _led(True);  time.sleep_ms(100)
        _led(False); time.sleep_ms(100)

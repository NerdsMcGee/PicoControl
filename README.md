# Pico Control

Turn a **Raspberry Pi Pico 2 W** into a network-controlled USB keyboard, and drive it from a Windows GUI.

The Pico plugs into the target device as an ordinary USB keyboard. Your PC sends it text over WiFi, and it types that text into the target. That makes it handy for replaying long programs or command sequences into devices where pasting isn't possible.

## What's in the repo

| File | Runs on | Purpose |
|---|---|---|
| `main.py` | Pico | MicroPython firmware: WiFi + TCP listener + USB HID keyboard |
| `pico_secrets.example.py` | Pico | Template for your WiFi details and auth token |
| `pico_gui.py` | PC | **Pico Control** GUI: macro editor, custom commands, Caps Lock, typing speed |
| `pico_type.py` | PC | Command-line tool to type a single string or file |
| `pico_macro.py` | PC | Command-line macro runner; its `STEPS` are also the GUI's default macro |
| `build_exe.bat` | PC | Builds a standalone `PicoControl.exe` with PyInstaller |

## Pico setup

1. Flash stock **MicroPython (v1.25+)** for the Pico 2 W. (I used Thonny.)
2. Install the HID keyboard library:
   ```
   mpremote mip install usb-device-keyboard
   ```
3. Copy `pico_secrets.example.py` to `pico_secrets.py` and fill in your WiFi details and a long random token:
   ```
   python -c "import secrets; print(secrets.token_urlsafe(24))"
   ```
4. Copy both files onto the Pico:
   ```
   mpremote cp main.py :main.py
   mpremote cp pico_secrets.py :pico_secrets.py
   ```
5. Plug the Pico into the target device and reset it.

**LED signals:**

| Pattern | Meaning |
|---|---|
| 2 slow blinks | WiFi connected |
| 3 slow blinks | Keyboard ready |
| Brief flash every 2 s | Idle, listening |
| Solid on | Typing |
| 2 quick blinks | Finished typing |
| 3 quick blinks | Wrong token |
| Fast continuous blink | Startup failed (check the serial console) |

**Safe mode:** tap BOOTSEL within 3 seconds of boot to skip HID and networking and get a clean REPL.

## PC setup

Requires Python 3 on Windows (Tkinter is included).

```
python pico_gui.py
```

Enter the Pico's IP address, the port (default 8822) and your token in the **Connection** box. They're remembered locally in `macro.json`, which is git-ignored and never committed.

### Building the .exe

Double-click `build_exe.bat`. It creates a clean virtual environment, installs PyInstaller and produces `dist\PicoControl.exe`.

The exe doesn't contain your token. It remembers settings in a `macro.json` file next to itself.

## Using Pico Control

- **Macro editor:** add, edit, duplicate, remove and reorder steps. Each step has text, an optional Enter (main or keypad) and a delay afterwards. Double-click a step to edit it. Changes are saved automatically.
- **Load File…:** loads a plain text file. Each line is typed exactly as written and followed by Enter, with the **Delay between lines** value after it. A `.json` file saved with **Save As…** loads back as a full macro.
- **Indentation:**
  - **Compensate for editor auto-indent** is for editors that copy the previous line's indentation. It types only the change in indentation, using Backspace to step back out.
  - **Type as written** sends the text unchanged.
  - **Strip all indentation** removes leading spaces and tabs.

  In the step list, leading spaces show as `·` and tabs as `→`.
- **Caps Lock ON/OFF:** sets Caps Lock on the target, using the keyboard LED state the target reports. It's applied immediately when clicked and again before every macro run.
- **Key delay (ms):** the delay after each key press and release. Raise it if the target drops characters.
- **Run / Stop:** each step waits for the Pico to finish typing before its delay starts, so lines never pile up.

## Protocol

The Pico accepts one TCP connection per message on port 8822:

```
<token>\n<payload>
```

Any payload is typed as text, except these single-command payloads:

| Payload | Action | Reply |
|---|---|---|
| `0x01` | Force Caps Lock on | `ON` / `OFF` / `NOREPORT` |
| `0x02` | Force Caps Lock off | `ON` / `OFF` / `NOREPORT` |
| `0x03` | Firmware version | `PICO <n>` |
| `0x04` + digits | Set key delay in ms (e.g. `\x0440`) | `OK <n>` |

In typed text, `\n` is Enter, `\r` is keypad Enter, `\t` is Tab and `\b` is Backspace. Characters outside US-layout ASCII are skipped.

The Pico closes the connection when it has finished typing, so clients can wait for the close to know the text is done.

## Security note

Anyone on your network who knows the token can type into the device the Pico is plugged into. Use a long random token, keep `pico_secrets.py` private, and run the Pico only on networks you trust. Traffic is not encrypted.

#!/usr/bin/env python3
"""HomeVoice -- a plain serial console for a Tasmota board.

WHY THIS EXISTS. Every other path to the board changes it. Option 15
re-sends its settings, the app talks over MQTT, and the web page needs the
board to be on the network -- which is the very thing in doubt. When a board
will not join Wi-Fi there is exactly one place the truth is printed, and it
is the cable. This opens it and nothing else.

It is READ-ONLY until you type something, and it never writes a file, so it
cannot make a confused board worse.

    python board_console.py                 pick the port, watch it boot
    python board_console.py --port COM7     skip the question
    python board_console.py --check         send the four Status commands
                                            that answer "what does it think
                                            it is", print, and exit

WHAT TO LOOK FOR while it boots:

    WIF: Connecting to AP1 ...        it is trying -- good
    WIF: Connected                    Wi-Fi is fine, look at MQT lines
    WIF: Connect failed ...           wrong password, or 5GHz-only hotspot
    MQT: Attempting connection...     it has network and is dialling
    MQT: Connect failed to X:8883, rc -2
                                      the socket never opened: firewall, or
                                      nothing listening on that address
    MQT: Connect failed ... rc 5      it reached the broker and was refused:
                                      username or password
    MQT: Connected                    done -- it is in

Type a Tasmota command and press Enter to send it. `Status 0` dumps
everything. Ctrl-C leaves.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    print("pyserial is not installed:  pip install pyserial")
    raise SystemExit(1) from None

BAUD = 115200

#: Asked in --check. Between them they cover every setting the flash was
#: supposed to write, so the answer to "did it actually land" is one command
#: rather than a guess.
CHECKS = (
    ("Status 5", "network -- IP, hostname, the AP it is on"),
    ("Status 6", "MQTT -- host, port, user, client, and whether it is up"),
    ("Status 2", "firmware -- version and the features compiled into it"),
    ("Status 1", "device -- topic, name, uptime, why it last restarted"),
)


def pick_port(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    ports = list(list_ports.comports())
    if not ports:
        print("No serial port found.")
        print("A charge-only USB cable looks identical to a data one and is")
        print("the usual cause. Try a different cable before anything else.")
        return None
    if len(ports) == 1:
        print(f"Using {ports[0].device} ({ports[0].description})")
        return ports[0].device
    for n, p in enumerate(ports, 1):
        print(f"  {n}. {p.device}  {p.description}")
    try:
        choice = int(input("Which port? ").strip())
    except (ValueError, EOFError, KeyboardInterrupt):
        return None
    # 0 or a negative number would index from the END of the list and
    # silently open a port nobody picked.
    if not 1 <= choice <= len(ports):
        return None
    return ports[choice - 1].device


def reader(link: "serial.Serial", stop: threading.Event) -> None:
    """Print whatever the board says, one line at a time.

    Errors are swallowed on purpose: unplugging the cable mid-session is a
    normal way to end this, not a crash worth a traceback.
    """
    buffer = b""
    while not stop.is_set():
        try:
            chunk = link.read(link.in_waiting or 1)
        except Exception:
            break
        if not chunk:
            continue
        buffer += chunk
        while b"\n" in buffer:
            line, _, buffer = buffer.partition(b"\n")
            text = line.decode("utf-8", "replace").rstrip("\r")
            if text:
                print(f"  {text}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", help="COM7, /dev/ttyUSB0, ...")
    parser.add_argument("--check", action="store_true",
                        help="ask the board what it thinks it is, then exit")
    args = parser.parse_args()

    port = pick_port(args.port)
    if port is None:
        return 1

    try:
        link = serial.Serial(port, BAUD, timeout=0.2)
    except Exception as exc:
        print(f"Could not open {port}: {exc}")
        print("Close anything else using it -- main.py, Arduino IDE, another")
        print("console. Windows allows exactly one at a time.")
        return 1

    stop = threading.Event()
    pump = threading.Thread(target=reader, args=(link, stop), daemon=True)
    pump.start()

    try:
        if args.check:
            print(f"\nAsking {port} what it is.\n")
            # A blank line first: if the board is mid-boot it may be
            # ignoring input entirely, and this is a cheap way to find out
            # before the questions that matter scroll past unanswered.
            link.write(b"\r\n")
            time.sleep(1.0)
            for command, why in CHECKS:
                print(f"\n-- {command}: {why}")
                link.write(command.encode() + b"\r\n")
                link.flush()
                time.sleep(1.5)
            time.sleep(1.0)
            print("\nIf nothing came back at all, the board is not listening")
            print("on this cable: it may still be booting, or the cable")
            print("carries power only.")
            return 0

        print(f"\nWatching {port}. Unplug and replug the board's power to")
        print("see it boot -- that is where the useful lines are.")
        print("Type a Tasmota command to send it. Ctrl-C to stop.\n")
        while True:
            try:
                typed = input()
            except EOFError:
                break
            if typed.strip():
                link.write(typed.strip().encode() + b"\r\n")
                link.flush()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        stop.set()
        time.sleep(0.3)
        try:
            link.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
#!/usr/bin/env python3
"""The HomeVoice hub service -- what systemd runs and the phone app talks to.

Thin launcher kept at the project root so existing commands and the systemd
unit keep working; the code lives in app/ws_server.py.
"""
from app.ws_server import run

if __name__ == "__main__":
    run()

#!/usr/bin/env python3
"""The HomeVoice console (the menu shell at the hub's own keyboard).

Thin launcher kept at the project root so existing commands and the systemd
unit keep working; the code lives in app/shell.py.
"""
from app.shell import run

if __name__ == "__main__":
    run()

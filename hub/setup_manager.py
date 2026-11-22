#!/usr/bin/env python3
"""HomeVoice installer and doctor (--check, --doctor, --broker, --service, ...).

Thin launcher kept at the project root so existing commands and the systemd
unit keep working; the code lives in app/setup_manager.py.
"""
from app.setup_manager import cli

if __name__ == "__main__":
    cli()

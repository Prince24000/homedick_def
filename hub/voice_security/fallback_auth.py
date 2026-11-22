#!/usr/bin/env python3
"""
fallback_auth.py — fallback authentication manager (PIN only).

Used when voice auth fails or the user chooses PIN fallback. A thin,
uniform-response wrapper around PinAuth -- no new logic of its own.

RATE LIMITING lives in PinAuth, not here. main._offer_pin_fallback asks for a
PIN exactly ONCE per command and returns, so this class cannot itself throttle
anything -- a caller could send commands indefinitely and get one guess each
time. What stops that is PinAuth's own persisted attempt counter, surfaced
through the "locked_out" reason and get_lockout_remaining() below.

RECOVERY. PinAuth implements the whole flow -- ordered 5-word verification,
per-position lockout, a global attempt budget, and reset_pin_with_recovery.
None of it was reachable: this wrapper exposed only authenticate/is_enrolled/
set_pin, so the phrase printed under "WRITE THIS DOWN NOW -- shown ONCE and
cannot be recovered if lost" had nowhere to be typed. A forgotten PIN meant
re-enrolling from scratch, with a working recovery system sitting one layer
down untouched. The two delegating methods below close that.
"""

from typing import Any, Dict, List, Optional, Tuple
from voice_security.pin_auth import PinAuth


class FallbackAuthManager:
    """
    Fallback auth: PIN only.
    Used when voice auth fails or user chooses PIN fallback.
    """

    def __init__(self):
        self.pin = PinAuth()

    def authenticate(self, user_id: str, pin: str) -> Tuple[bool, Dict[str, Any]]:
        """
        PIN fallback authentication.

        Args:
            user_id: User ID to authenticate
            pin: PIN digits string

        Returns:
            (success: bool, details: dict)
        """
        results: Dict[str, Any] = {
            "pin_passed": False,
            "pin_reason": "invalid_user_id",
            "lockout_remaining": 0,
        }
        if user_id and isinstance(user_id, str):
            try:
                results["pin_passed"], results["pin_reason"] = self.pin.verify(user_id, pin)
                results["lockout_remaining"] = self.pin.get_lockout_remaining(user_id)
            except Exception as e:
                print(f"[FALLBACK] PIN verification error: {e}")
                results["pin_passed"], results["pin_reason"] = False, "system_error"

        # PinAuth's boolean is the decision. The reason is a LABEL (PinAuth
        # returns True only with "success"), so it is not a second condition.
        success = bool(results["pin_passed"])
        if success:
            results["status"] = "pin_fallback_accepted"
        else:
            # PinAuth's reason string, UNCHANGED. main.py and ws_server compare
            # status against "locked_out", "wrong_pin", "no_pin_set",
            # "invalid_format", "bcrypt_unavailable"; a renamed copy
            # ("pin_locked_out") matched none of them, so a lockout read as a
            # plain wrong PIN and the countdown was never shown.
            results["status"] = results["pin_reason"]
            print(f"[FALLBACK] PIN failed for '{user_id}': reason={results['pin_reason']}, "
                  f"lockout={results['lockout_remaining']}s")

        return success, results

    def is_enrolled(self, user_id: str) -> Dict[str, bool]:
        """Check what auth methods are available for a user. PinAuth persists
        to disk, so has_pin() returns correct state even after a restart."""
        return {
            "pin": self.pin.has_pin(user_id),
        }

    def set_pin(self, user_id: str, pin: str) -> Tuple[bool, Optional[List[str]]]:
        """Set or update a PIN. Returns (success, recovery_phrase_or_None).

        EVERY set mints a fresh phrase and voids the previous one (see
        PinAuth.set_pin) -- the words shown last are the words that work.
        None only when no real BIP-39 wordlist is installed.
        """
        return self.pin.set_pin(user_id, pin)

    # ── Recovery ──────────────────────────────────────────────────────────
    # Thin delegations, same shape as authenticate(): PinAuth owns the logic,
    # this class owns the surface main.py talks to.

    def recovery_status(self, user_id: str) -> Dict[str, Any]:
        """has_recovery, total_words, locked_positions, created_at."""
        return self.pin.get_recovery_status(user_id)

    def renew_recovery(self, user_id: str) -> Optional[List[str]]:
        """Five new recovery words, PIN unchanged (PinAuth.renew_recovery_phrase).
        Only after the current PIN has been proven."""
        return self.pin.renew_recovery_phrase(user_id)

    def reset_pin_with_recovery(self, user_id: str, words: List[str],
                                new_pin: str) -> Tuple[bool, str]:
        """Set a new PIN by proving the recovery phrase.

        Failure reasons are deliberately coarse -- PinAuth answers
        "invalid_phrase" whatever went wrong, so an attacker learns neither
        which word was wrong nor how many tries remain. Pass that through
        unchanged rather than trying to be helpful here.
        """
        return self.pin.reset_pin_with_recovery(user_id, words, new_pin)
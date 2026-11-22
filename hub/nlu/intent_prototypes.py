"""
intent_prototypes.py — seed utterances for the MPNet-built intent index.

Trimmed prototype: IR/media-transport is gone, so the media intents
(play/pause/resume/next/previous/mute/unmute) that existed only to select an
IR remote code have no purpose here and are removed along with ir_control.py.

Only two intents remain, and both are genuinely ambiguous phrasing that would
otherwise collide with normal device commands:
  - dismiss: "drop it" / "let it go" collide with real commands
  - announce: routes to broadcast

power_on/off, lock/unlock, open/close, adjust_up/down are handled
deterministically upstream of the classifier: literal name resolution +
lexicon directionals + bare-name toggle. No seeds needed for unambiguous verbs.
"""

INTENT_PROTOTYPES = {
    "dismiss": [
        "dismiss",
        "never mind",
        "forget it",
        "scratch that",
        "ignore that",
        "disregard that",
        "just kidding",
        "don't do that",
        "abort that request",
        "cancel what i just said",
        "actually never mind",
        "wait forget it",
        "don't worry about it",
        "leave it alone",
        "let it go",
        "drop it",
        "i changed my mind",
        "don't bother",
        "hold on forget that",
        "actually scratch that",
        "ignore my last message",
        "disregard my previous request",
        "cancel that last thing",
        "don't execute that",
        "abort what i said",
        "never mind that",
        "forget i said anything",
        "just ignore that",
        "that was a mistake",
        "wait don't do it",
        "hold up cancel that",
        "retract that",
        "take that back",
        "don't process that",
        "skip that command",
        "void that request",
        "nullify what i said",
        "erase that instruction",
        "i didn't mean that",
        "call it off",
        "stand down",
        # Spoken cancel while the hub waits or works (2026-09-30). Sentences
        # only: one-word replies are the fixed list's (lexicon.CANCEL_PHRASES).
        "i did not say that",
        "that is not what i said",
        "that is not what i meant",
        "you heard me wrong",
        "you got that wrong",
        "no that is wrong",
        "that is the wrong one",
        "stop what you are doing",
    ],
    "announce": [
        "announce",
        "broadcast a message to the whole house",
        "make an announcement",
        "say this out loud on all speakers",
        "broadcast that dinner is ready",
        "announce to the family that we are leaving",
        "send a voice broadcast",
        "shout this message through the intercom",
        "make an audio announcement",
        "transmit an announcement across all units",
        "broadcast text to speech",
        "announce a notice to everyone",
        "read this alert over the loudspeaker",
        "send a broadcast message",
        "state an announcement to the household",
        "make a public announcement on the system",
        "proclaim this notice to all rooms",
        "issue a verbal announcement",
        "tell everyone through the intercom",
        "broadcast a system-wide announcement",
        "play this spoken alert",
        "tell the house dinner's ready",
        "broadcast a message everywhere",
        "page everyone in the home",
        "say it on every speaker",
        "make a house-wide call",
        "announce that we're leaving",
        "put a notice over the intercom",
        "send word to all rooms",
        "speak this to the household",
        "blast an announcement",
        "broadcast a message",
        "tell everyone",
        "page the house",
        "intercom message",
        "public address",
        "speaker broadcast",
        "announce to all",
        "household announcement",
        "broadcast over speakers",
        "voice page everyone",
    ],
}

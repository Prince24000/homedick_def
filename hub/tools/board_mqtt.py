"""One broker conversation for the command-line tools.

diagnose_board.py and fix_boards.py each carried their own copy of the same
dance -- hub client, settings, a CONNACK handler, connect, pump, disconnect
-- and each handled a refused login differently. This is the one copy.
"""
import time


def converse(esp, ask, seconds, done=None, before_close=None):
    """Sign in as the hub; once the broker ACCEPTS, call ask(client) to
    subscribe and publish; collect every (topic, text) that arrives until
    `seconds` pass, the broker refuses, or done(messages) is true.
    before_close(client) runs on an accepted session before disconnecting.

    Returns (messages, rc): rc 0 is accepted, None is no CONNACK at all,
    anything else is the broker's refusal code. Raises what _mqtt_client
    and connect() raise.
    """
    heard, box = [], {"rc": None}
    client = esp._mqtt_client()
    settings = esp._mqtt_settings()

    def on_connect(c, _u, _f, rc, *_r):
        # The hub client runs the VERSION1 callback API: rc is a plain int.
        box["rc"] = rc
        if rc == 0:
            ask(c)

    client.on_connect = on_connect
    client.on_message = lambda _c, _u, m: heard.append(
        (m.topic, m.payload.decode("utf-8", "replace")))
    try:
        client.connect(settings["host"], int(settings["port"]), 20)
        end = time.monotonic() + seconds
        while (time.monotonic() < end and box["rc"] in (None, 0)
               and not (done and done(heard))):
            client.loop(timeout=0.3)
        if before_close and box["rc"] == 0:
            before_close(client)
    finally:
        try:
            client.disconnect()
        except Exception:                              # noqa: BLE001
            pass
    return heard, box["rc"]

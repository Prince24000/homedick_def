"""Central configuration for HomeVoice Smart Control System.

v3: No import-time side effects. Strict API key validation. Configurable paths.
    TLS paths resolved consistently. PIN recovery config.
"""

import os
from pathlib import Path


# ── The secrets file: ONE place the keys live ───────────────────────────────
#
# HOMEVOICE_API_KEY, HOMEVOICE_FERNET_KEY and HOMEVOICE_HA_TOKEN used to
# exist only as shell variables, which meant every terminal, every restart
# and every forgotten `source ~/.bashrc` was a chance for the hub and the
# `--pair` code to hold DIFFERENT keys -- an evening was lost to exactly
# that on 2026-09-07 ("HMAC did not match", with one key in .bashrc and
# another in the running hub). A service started by systemd reads no
# .bashrc at all.
#
# So: ~/.homevoice/env, KEY=VALUE lines, owner-only, written once by
# `setup_manager.py --service` (or by hand). Every process -- the hub, the
# console, --pair, the doctor, the tests -- reads it HERE, at import, and
# for the HOMEVOICE_* keys it holds THE FILE WINS over whatever the shell
# had, so there is one truth. systemd reads the same file through
# EnvironmentFile=. A test that must not see the real keys sets
# HOMEVOICE_ENV_FILE to an empty string, which skips the file entirely.
# (The "no import-time side effects" line above still holds in spirit:
# this reads one file and touches only HOMEVOICE_* variables.)
SECRETS_FILE = os.environ.get("HOMEVOICE_ENV_FILE",
                              str(Path.home() / ".homevoice" / "env"))
SECRETS_LOADED = []          #: the keys the file supplied, for the doctor


def _load_secrets_file() -> None:
    if not SECRETS_FILE:
        return
    try:
        with open(SECRETS_FILE, encoding="utf-8-sig") as f:
            lines = f.read().splitlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key.startswith("HOMEVOICE_") or key == "HOMEVOICE_ENV_FILE":
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ[key] = value
        SECRETS_LOADED.append(key)


_load_secrets_file()


def set_secret(key: str, value: "str | None") -> None:
    """Write (or, with None, remove) ONE HOMEVOICE_* line of the secrets
    file, and the running process's copy with it. The rest of the file is
    kept as it was, line for line. Written to a temp file, owner-only,
    then renamed over -- a crash mid-write never leaves half a key file.
    With SECRETS_FILE empty (the tests), only the process copy changes.
    Used for keys the hub itself is handed later, from the phone -- the
    GitHub token -- rather than at install."""
    if not key.startswith("HOMEVOICE_") or "\n" in (value or ""):
        raise ValueError(f"not a settable secret: {key}")
    if value is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = value
    if not SECRETS_FILE:
        return
    try:
        with open(SECRETS_FILE, encoding="utf-8-sig") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        lines = []
    kept = []
    for raw in lines:
        name = raw.strip()
        if name.startswith("export "):
            name = name[7:].lstrip()
        if name.partition("=")[0].strip() == key:
            continue
        kept.append(raw)
    if value is not None:
        kept.append(f"{key}={value}")
    folder = os.path.dirname(SECRETS_FILE) or "."
    os.makedirs(folder, mode=0o700, exist_ok=True)
    temp = SECRETS_FILE + ".tmp"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(kept) + ("\n" if kept else ""))
    os.replace(temp, SECRETS_FILE)
    try:
        os.chmod(SECRETS_FILE, 0o600)
    except OSError:
        pass


def _env_number(name: str, default, cast):
    """HOMEVOICE_<name> as a number, or `default` if unset or unparseable.

    A typo in the env file (HOMEVOICE_OTA_PORT=87a6) used to raise ValueError
    at import of this module, which took down every entry point -- and under
    systemd, a restart loop -- over one optional tuning knob.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return cast(default)
    try:
        return cast(raw.strip())
    except ValueError:
        import sys as _sys
        print(f"[config] ignoring {name}={raw!r}: not a number; using {default}",
              file=_sys.stderr)
        return cast(default)


def _env_int(name: str, default: str) -> int:
    return _env_number(name, default, int)


def _env_float(name: str, default: str) -> float:
    return _env_number(name, default, float)


# ── Base paths ──────────────────────────────────────────────────────────────
# Resolved from this file (core/config.py) for systemd/cron safety.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
_BASE_DIR = PROJECT_ROOT

# Allow override via environment for a genuinely different layout (e.g. an
# external SSD on the Pi). Defaults to the project's own directory, which
# works out of the box on both the Windows dev machine and the Pi with no
# environment setup — config.py was previously unused, so this default never
# actually mattered in practice until now.
_HOME = Path(os.environ.get("HOMEVOICE_HOME", str(_BASE_DIR)))
MODELS_DIR = _HOME / "models"
NLU_MODELS_DIR = MODELS_DIR / "nlu"
VOICE_SECURITY_MODELS_DIR = MODELS_DIR / "voice_security"
DATA_DIR = _HOME / "data"

# ── ESP boards ──────────────────────────────────────────────────────────────
#
# ESPHOME_CONFIG_DIR USED TO LIVE HERE and is deliberately gone. It pointed
# at the folder a compiler read YAML from. Nothing compiles any more: the
# boards run one prebuilt Tasmota image, identical on every chip, and are
# told their wiring over the network afterwards. There is no config
# directory for a compiler to read and no build folder to fill the SD card.

#: One JSON file per board -- its wiring and its own broker credentials --
#: plus a shared wifi.json holding the network they all join.
#:
#: OUTSIDE THE SOURCE TREE, deliberately, and for the same reason
#: secrets.yaml was: these files hold the Wi-Fi password and every board's
#: password. Note this does NOT use _HOME: _HOME defaults to the project
#: directory, which is exactly where these must not be.
BOARDS_DIR = os.environ.get(
    "HOMEVOICE_BOARDS_DIR",
    str(Path.home() / ".homevoice" / "boards"))

#: The phones allowed to drive this hub -- one record each, holding a PUBLIC
#: Ed25519 key and nothing secret. In ~/.homevoice for the same reason the
#: board records are: it is state about this installation rather than
#: source, and it has to survive a fresh clone of the repo.
#:
#: BESIDE the boards directory, never inside it. It lived inside for a
#: while, on the lazy reasoning that the folder already existed with the
#: right permissions -- but that folder is not a folder, it is a REGISTRY:
#: every *.json in it IS a board, so the phone registry showed up in every
#: board picker as a board called "phones", and wiping it crashed on the
#: MQTT credentials it never had. phones.py moves an old installation's
#: file up a level on its first run, so nobody pairs twice.
#:
#: Unlike the board files, losing this one leaks no credential. It is still
#: written 0600, because the list of devices in someone's home is private
#: even when none of the keys in it are.
PHONES_FILE = os.environ.get(
    "HOMEVOICE_PHONES_FILE",
    str(Path.home() / ".homevoice" / "phones.json"))

#: The prebuilt firmware. ONE file, the same on every board, fetched once by
#: setup_manager and then never again -- this is the whole offline story.
#:
#: It lives BESIDE esp_builder.py rather than in a data folder, because it
#: is an asset of the builder in the way a template is: the two ship
#: together and neither is any use without the other. _BASE_DIR is the
#: project root, so this resolves to esp_firmware/firmware/tasmota32.factory.bin.
#:
#: ONE FOLDER FOR EVERY IMAGE (2026-09-24): esp_firmware/firmware/ holds
#: every family's factory image (<env>.factory.bin, cable, 0x0) AND its app
#: image (<env>.bin, over the air) side by side -- exactly the two files
#: each GitHub artifact carries, so unzipping every artifact into this one
#: folder is the whole install. Before, factory images sat in esp_firmware/
#: and app images in esp_firmware/ota/; settle_firmware_folder() moves an
#: older install's files here once.
FIRMWARE_DIR = str(_BASE_DIR / "esp_firmware" / "firmware")
TASMOTA_FIRMWARE = "tasmota32.factory.bin"
TASMOTA_FIRMWARE_PATH = os.path.join(FIRMWARE_DIR, TASMOTA_FIRMWARE)

#: Firmware for every OTHER family (S3, S2, C3, C6, C2): each family's OWN
#: factory image, in this folder, under the name its board profile gives
#: (esp_firmware/chips.py: Board.firmware -- tasmota32s3.factory.bin and
#: so on). The profile is the one place a family's file name is written;
#: this folder is the one place they live. The classic ESP32 keeps
#: TASMOTA_FIRMWARE_PATH above and its wider search (esp_builder
#: _firmware_candidates), because existing installs put it there.
#:
#: setup_manager downloads Tasmota's STOCK image for each family here; a
#: build of this project (the GitHub workflow) replaces
#: it under the same name -- stock lacks the house drivers (RC522, LCD,
#: MPU6050...), and the hub says so when a board's wiring needs one.
#: All factory builds, all written at 0x0, same rule as the classic image.
#: Absent on purpose: ESP8266 (a different firmware lineage entirely) and
#: H2/P4 (no Wi-Fi of their own -- nothing for a smart-home node to join).
TASMOTA_FAMILY_FIRMWARE_DIR = FIRMWARE_DIR


def firmware_path_for(board) -> str:
    """The factory image a board of this profile is flashed with (a
    chips.Board, or anything with .family and .firmware). The classic
    family is TASMOTA_FIRMWARE_PATH, exactly as before profiles."""
    if getattr(board, "family", "esp32") == "esp32":
        return TASMOTA_FIRMWARE_PATH
    return os.path.join(TASMOTA_FAMILY_FIRMWARE_DIR, str(board.firmware))


def ota_path_for(board) -> str:
    """The app image running boards of this profile update from over the
    air: <board.ota_name> BESIDE its factory image (tasmota32c3.bin next to
    tasmota32c3.factory.bin). hub/ota_server.py caches its cut there too."""
    return os.path.join(os.path.dirname(firmware_path_for(board)),
                        str(board.ota_name))


#: Where images lived before the one folder: factory images straight in
#: esp_firmware/, app images in esp_firmware/ota/.
_OLD_FIRMWARE_DIRS = (_BASE_DIR / "esp_firmware",
                      _BASE_DIR / "esp_firmware" / "ota")


def settle_firmware_folder(say=None) -> list:
    """Move an older install's images into FIRMWARE_DIR, once. Only
    tasmota32*.bin files, and never over a file already there (the one in
    the new folder is the newer arrival by construction). Returns the
    moved paths. Safe to call every start: a settled install moves nothing."""
    import shutil
    target = Path(FIRMWARE_DIR)
    moved = []
    for folder in _OLD_FIRMWARE_DIRS:
        if not folder.is_dir():
            continue
        for old in sorted(folder.glob("tasmota32*.bin")):
            new = target / old.name
            if new.exists():
                continue
            try:
                target.mkdir(parents=True, exist_ok=True)
                shutil.move(str(old), str(new))
            except OSError as exc:
                if say:
                    say(f"  could not move {old} into {target}: {exc}")
                continue
            moved.append(new)
            if say:
                say(f"  firmware: moved {old.name} into esp_firmware/firmware/")
    # The old ota/ folder has no use left; once it is empty it goes.
    # Anything else in it (a file someone put there) keeps it.
    old_ota = _OLD_FIRMWARE_DIRS[-1]
    try:
        if old_ota.is_dir() and not any(old_ota.iterdir()):
            old_ota.rmdir()
    except OSError:
        pass
    return moved


#: The factory image is written at 0x0. A plain tasmota32.bin goes at
#: 0x10000 and will brick a first flash if written to 0x0, so these two
#: settings move together or not at all.
TASMOTA_FLASH_OFFSET = "0x0"

#: WHERE A BOARD LOOKS FOR FIRMWARE IF SOMEBODY TRIGGERS AN UPGRADE -- and,
#: by default, DELIBERATELY NOT THE INTERNET.
#:
#: A Tasmota board ships with OtaUrl pointing at ota.tasmota.com. On a house
#: or offline network (the whole premise here -- a router with no SIM data,
#: or the Pi's own hotspot) that server is unreachable, and Tasmota's OTA
#: works by rebooting into the tiny SAFEBOOT recovery partition FIRST and
#: only then downloading the new image. A download that cannot happen leaves
#: the board stranded in safeboot with no working app -- every command comes
#: back "Unknown" and it must be re-flashed over the cable to recover. That
#: is exactly how the Kitchen board bricked (2026-09-09): pointed at a dead
#: Windows hotspot, an upgrade was triggered, the download failed, safeboot
#: had nowhere to fall back to.
#:
#: This hub updates firmware over the CABLE (a full erase + factory write,
#: which cannot fail this way), so a board never needs an internet OTA at
#: all. Provisioning therefore repoints OtaUrl at the hub's OWN address, so
#: an accidental "Firmware Upgrade" stays on the LAN and never phones out.
#: Since 2026-09-10 the hub SERVES that image itself (hub/ota_server.py, on
#: OTA_HTTP_PORT below): the app image tasmota32.bin, cut out of the very
#: factory image the cable writes. A board stranded in safeboot is told
#: OtaUrl + Upgrade 1 the moment it announces itself (tele/<board>/INFO1)
#: and heals over Wi-Fi; before this, the URL named a hub that served
#: nothing and every failed update ended at the cable (Kitchen, twice).
#:
#:   unset (None)  -> the hub builds a LOCAL url from its own address.
#:   a string      -> sent verbatim (point it at a real local mirror if you
#:                    run one; the app image is tasmota32.bin, NOT .factory).
#:   "-"           -> HOMEVOICE_OTA_URL=- leaves the board's OtaUrl UNTOUCHED,
#:                    for a hub that genuinely has internet and wants the
#:                    stock behaviour back.
_ota_env = os.environ.get("HOMEVOICE_OTA_URL")
OTA_URL = None if _ota_env is None else ("" if _ota_env == "-" else _ota_env)
#: The port on which the hub serves tasmota32.bin to the boards over plain
#: HTTP (hub/ota_server.py) -- the address the default OTA_URL names. A board
#: that lands in Tasmota's SAFEBOOT recovery partition fetches its firmware
#: from here and heals itself; nothing else is served. 0 disables it.
OTA_HTTP_PORT = _env_int("HOMEVOICE_OTA_PORT", "8766")

# ── The message broker ──────────────────────────────────────────────────────
#
#: Defined up here only because MQTT_PORT below has to know it. The reasoning
#: for it lives with MQTT_TLS further down, which is where to read it.
#:
#: HOMEVOICE_MQTT_TLS=0 turns the encryption off everywhere in one move: the
#: hub stops wrapping its own connection, the port drops to 1883, and boards
#: are provisioned with SetOption103 0 and no fingerprint. That is the switch
#: to reach for when you need to know whether TLS is the thing standing
#: between you and a working board.
_MQTT_TLS = os.environ.get("HOMEVOICE_MQTT_TLS", "1") == "1"
#
# Mosquitto, on this machine. Boards connect out to it, Home Assistant
# connects in to it, nothing leaves the house.
#
# EMPTY MEANS WORK IT OUT, which is the right default and now the normal
# case. esp_builder asks the operating system which of this machine's
# addresses it would use to reach the network, every time a board is
# provisioned or told about a change -- so moving to a different router, or
# a hotspot that hands out a new address after a restart, needs no edit
# here and no re-flashing.
#
# It deliberately ignores two answers: 127.0.0.1, because from a board's
# point of view that is the board itself, and anything in Tailscale's 100.x
# range, because a tailnet address is real, routable, and completely
# unreachable from an ESP32.
#
# Set it only to override -- a machine with several networks where the
# automatic choice picks the wrong one.
MQTT_HOST = os.environ.get("HOMEVOICE_MQTT_HOST", "")

#: THE PORT FOLLOWS TLS. Read MQTT_TLS below first -- this line depends on it.
#:
#: It used to be a flat 8883, which quietly booby-trapped the one thing you
#: would ever want to do in a hurry: turning TLS off to find out whether TLS
#: was the problem. The boards would have been pointed at 8883 in plaintext,
#: where Mosquitto is expecting a handshake, and the failure looks identical
#: to the failure you were trying to diagnose. Switching TLS off must move
#: the port too, or it is not a switch.
MQTT_PORT = _env_int("HOMEVOICE_MQTT_PORT", "8883" if _MQTT_TLS else "1883")

# ── The broker's own files ──────────────────────────────────────────────
#
# Mosquitto needs three things from us: a config, a password file, and the
# certificate it presents. Two of those we AUTHOR -- they belong with the
# project, next to the other certificates and next to the code that writes
# them, so a fresh checkout on the Pi is one folder. The third kind is what
# the broker itself scribbles while running -- a pid file, a log, its
# persistence database -- and that is not ours to keep. It stays out of the
# tree in BROKER_DIR, where a stale pid or a fat log cannot end up in a
# commit.
#
# NOTE certs/broker.key is a real private key and configs/passwd holds the
# broker's password hashes. Both directories are in .gitignore for that
# reason; if you move this project by copying the folder rather than by
# cloning it, they come along, which is the point.
BROKER_DIR = os.environ.get(
    "HOMEVOICE_BROKER_DIR",
    str(Path.home() / ".homevoice" / "broker"))
#: THE BROKER'S OWN CERTIFICATE, separate from local.crt on purpose.
#:
#: local.crt serves the phone: the app pins it, so changing it locks every
#: phone out until the app is rebuilt and redistributed. broker.crt serves
#: the BOARDS, which are re-provisioned by the hub in seconds and need
#: nothing from you.
#:
#: They were one file, and that coupling cost weeks. The boards' certificate
#: has to be shaped for a tiny TLS stack -- a proper CA, no critical
#: extension it cannot parse, a key it can afford -- and every time that had
#: to be corrected, the phone's certificate was dragged along with it. Two
#: files, two independent lifetimes: fixing the boards' end never touches a
#: phone, and rotating the phone's never disturbs a board.
BROKER_TLS_CERT = str(_BASE_DIR / "certs" / "broker.crt")
BROKER_TLS_KEY = str(_BASE_DIR / "certs" / "broker.key")

MQTT_CONF_FILE = str(_BASE_DIR / "configs" / "mosquitto.conf")
MQTT_PASSWD_FILE = str(_BASE_DIR / "configs" / "passwd")

#: The account HOME ASSISTANT signs in with. Boards get one each; Home
#: Assistant is not a board and gets its own, so revoking a board never
#: touches it.
MQTT_HA_USER = "homeassistant"

#: THE HUB'S OWN ACCOUNT (audit F3, 2026-09-02). The engine used to borrow
#: the first board's login, which was only possible because the broker had
#: no authorization at all. With the ACL below a board can see only its own
#: topics, so the hub -- which must hear every board and command every board
#: -- signs in as itself. Password generated once by --broker and kept 0600
#: in BROKER_DIR/hub_account.json.
MQTT_HUB_USER = "homevoice"

#: WHO MAY TALK ABOUT WHAT. Written by setup_manager.write_acl_file: every
#: board is confined by pattern to cmnd/stat/tele/<its own name>/# plus a
#: write to tasmota/discovery/#; only homeassistant and the hub see #. This
#: is what makes "revoke one board" mean something -- before it, any one
#: board's password switched every relay in the house via cmnd/tasmotas/#.
MQTT_ACL_FILE = str(_BASE_DIR / "configs" / "acl")

#: Which interface the PLAINTEXT listener answers on.
#:
#: "127.0.0.1" is the safe default and means Home Assistant must be running
#: on this same machine. Its traffic then never touches a network card and
#: cannot be observed by anything that is not already on this host, which is
#: what makes an unencrypted listener defensible at all.
#:
#: Home Assistant IN DOCKER is not on this machine, whatever the browser
#: URL suggests -- inside the container, 127.0.0.1 is the container. Set
#: this to "" to accept connections from the container (and from the rest of
#: the network with it), and connect Home Assistant to
#: host.docker.internal:1883.
#:
#: BE HONEST ABOUT WHAT THAT COSTS. With "" the Home-Assistant-to-broker
#: link is authenticated but unencrypted on your LAN. The boards are
#: unaffected -- they use the TLS listener either way. The clean fix is to
#: put Home Assistant on 8883 as well, which needs the broker certificate to
#: name the address HA dials; that means regenerating it, which invalidates
#: the phone app's pinned copy until the app is rebuilt. Worth doing on
#: purpose, on the Pi, not in the middle of a first flash.
MQTT_PLAIN_BIND = os.environ.get("HOMEVOICE_MQTT_PLAIN_BIND", "127.0.0.1")

#: How to make Mosquitto re-read the password file after a board is added or
#: revoked. It reloads on SIGHUP and does NOT poll, so without this a new
#: board is rejected until the broker happens to restart -- which looks
#: exactly like a wrong password.
#:
#: Left as a list so a Pi running it under systemd and a bench running it by
#: hand can differ without code changes. Empty means "do not try", and the
#: hub says so rather than pretending.

#: HOW LONG A RULE'S LINE STAYS ON A PANEL before the glass is handed back
#: to Tasmota's own clock (her call, 2026-09-14: "i set power to display in
#: lcd it did show but then disappeared ... i want anything for display to
#: show for more time").
#:
#: It was thirty seconds, hard-coded, and nothing told anybody that -- so a
#: reading deliberately put on the panel read as a fault when it vanished.
#: TEN MINUTES since 2026-09-14 ("the display of anything set in automations
#: fr like 10 mins"), which is her own number and is also the long half of
#: the cycle below.
#:
#: A RULE MAY OVERRIDE IT PER MESSAGE with `for_s`, and `for_s: 0` means
#: "hold until something else takes the glass" -- the spelling for a reading
#: that should simply stay up. A panel that REBOOTS drops its hold either
#: way, so a board that restarts is never left showing its power-on garbage
#: behind a hold that outlived the message.
#:
#: The cost of a long hold is that Tasmota's clock is off the panel for that
#: long -- which is what DISPLAY_CLOCK_SECONDS below exists to give back.
DISPLAY_HOLD_SECONDS = _env_float("HOMEVOICE_DISPLAY_HOLD_SECONDS", "600")

#: HOW LONG THE PANEL GOES BACK TO TASMOTA'S CLOCK before the automation
#: line returns -- the short half of the cycle (her design, 2026-09-14: "at
#: first it pushes the default of date and time then the display of anything
#: set in automations fr like 10 mins ... then date and time for like 5s,
#: then the display").
#:
#: Until now the line's hold ENDED THE STORY: the glass went back to the
#: clock and stayed there until some rule happened to fire again. A rule
#: that fires rarely -- a threshold crossed once a day -- lit the panel once
#: and then the panel was a clock for ever, which is not what anybody meant
#: by putting a reading on it.
#:
#: So the line is the panel's RESTING state and the clock gets a glance:
#: ten minutes of the reading, five seconds of the time and date, back to
#: the reading. And the line that comes back is RE-RENDERED, not replayed --
#: `{kitchen.meter:power}W` resolves again against the live cache, so the
#: number on the glass is the number now, not the one that was true when the
#: rule fired.
#:
#: 0 TURNS THE CYCLE OFF: the clock keeps the glass until something else is
#: shown, which is exactly how this behaved before today.
#:
#: The floor is the tick (5 s): the engine looks at its panels once every
#: five seconds, so a turn shorter than that still lasts one tick, and a
#: turn of 7 rounds up to 10. Nothing here is worth a faster tick.
DISPLAY_CLOCK_SECONDS = _env_float("HOMEVOICE_DISPLAY_CLOCK_SECONDS", "5")

#: HOW OFTEN THE HUB ASKS A PANEL'S BOARD WHAT IS ACTUALLY ON ITS I2C BUS
#: (her ask, 2026-09-14: "the system does I2Cscan to search for display if
#: found, then display").
#:
#: The hub used to take the panel's existence entirely on trust: the board's
#: PLAN says an LCD is on it, so `display_targets()` lists one, so every
#: rule that writes to it reports success -- to an empty bus, for ever, with
#: the journal saying "showed '146W' for 600s" the whole time. That is the
#: one thing this project does not do: claim it did something it did not do.
#:
#: `I2Cscan` is Tasmota's own answer and it is cheap: one command, one
#: RESULT, a list of addresses. A panel whose address is NOT in a scan that
#: really answered is a panel that is not there -- said once, in words, with
#: the address it looked for -- and rules stop claiming they wrote to it.
#:
#: FAIL OPEN, ALWAYS. No answer, an answer that cannot be parsed, a board
#: whose firmware has no I2C at all: the panel is treated as present and
#: written to exactly as before. Only a POSITIVE scan that lists other
#: things and not the panel is evidence of absence.
#:
#: Re-asked on this period so a screen that is plugged in, powered, or
#: rewired comes back by itself. 0 turns the scan off entirely.
DISPLAY_SCAN_EVERY_SECONDS = _env_float("HOMEVOICE_DISPLAY_SCAN_EVERY", "300")

#: RE-TELL EVERY PANEL EVERYTHING, THIS OFTEN (her ruling, 2026-09-14:
#: "allow display one to always be on loop ... just anything, let's just be
#: on loop").
#:
#: The hub's panel handling was all bookkeeping — a wake that runs once, a
#: stage counter, a hold, a standing set — and every piece of it is the hub
#: REMEMBERING what it thinks the glass shows. A character LCD forgets
#: without telling anybody: it browns out, misses its init, comes up after
#: the ESP32 does. Every drift so far has ended with somebody typing
#: `DisplayModel 1` into a console.
#:
#: So the panel is re-told on a timer: driver settings, then whatever
#: belongs on it now. If the glass already agrees the commands are a no-op
#: costing three MQTT publishes on a local broker; if it has drifted, it is
#: fixed within this many seconds and nobody has to notice. Raise it if the
#: broker is loaded, set it to 0 to go back to the one-shot wake.
DISPLAY_REPEAT_SECONDS = _env_float("HOMEVOICE_DISPLAY_REPEAT_SECONDS", "30")

#: HOW OFTEN A `change`-TRIGGERED RULE MAY FIRE when everything it does is
#: SHOW something -- write a line to a panel, send a notification.
#:
#: The ordinary rule cooldown is a minute, and it exists for a hard reason:
#: a sensor reporting once a second must not be able to drive a relay once a
#: second. That reason does not apply to a line of text. A rule whose whole
#: purpose is "keep the power reading in front of me" is useless at one
#: update a minute -- the panel would show the value as it was when the rule
#: last fired, frozen, while the real meter moved on.
#:
#: THE SHORTER WAIT IS EARNED BY THE ACTIONS, NEVER BY THE TRIGGER. The
#: engine reads every branch of the rule -- then, else and revert -- and any
#: action that is not a display or a notification puts the rule straight
#: back on the full minute. So this number can never reach a relay.
#:
#: The cost is broker traffic and panel redraws: a meter reporting every
#: second, at 2.0, writes at most one line every two seconds. Raise it if
#: the broker is loaded; lower it only on a bench.
CHANGE_COOLDOWN_SECONDS = _env_float("HOMEVOICE_CHANGE_COOLDOWN_SECONDS", "2")

#: KEEP THE PAIRING DOOR OPEN PERMANENTLY -- NOW OFF BY DEFAULT (her call,
#: 2026-09-24: "the original phone has to open pairing window, or the hub
#: restarted or a button if available"). It was on from 2026-09-14 ("for
#: now always allow the pairing window be open no need for buttons"); with
#: other people's phones joining, a window somebody opened on purpose is
#: the fence again. The three ways it opens: Settings -> Pair a new phone
#: on a paired phone (PIN/voice checked), the hub starting
#: (HOMEVOICE_PAIR_ON_BOOT, OFF since 2026-10-15), the Pi's button held 2 s -- and
#: `setup_manager.py --pair` at the console.
#:
#: True means no button hold, no `--pair`, no menu action, no boot window:
#: a phone may pair whenever it likes.
#:
#: WHAT THIS DOES NOT WAIVE, and it is why this is a reasonable setting
#: rather than an open door: enrolment still requires the HMAC of the API
#: key, computed over a message bound to THIS hub's certificate. A stranger
#: on the network without that key gets nowhere, window or no window. The
#: window was never the thing keeping people out -- it was a second,
#: human-timed fence standing behind the cryptographic one.
#:
#: WHAT IT COSTS: anybody who HAS the API key can pair at any moment,
#: instead of only during a window somebody deliberately opened. If that key
#: ever leaks -- a screenshot of the pairing screen, a shared photo, an old
#: backup -- the window is what would have held the damage to five minutes.
#:
#: Set it False (or HOMEVOICE_PAIRING_ALWAYS_OPEN=0) and the door shuts on
#: the very next call: it is read at call time, never cached, and nothing is
#: written to disk that would have to be cleaned up afterwards. That last
#: part is deliberate -- the easy version of this feature was to write a
#: far-future expiry into the window file, and such a file SURVIVES turning
#: the setting back off, which is the one thing a security control must
#: never do.
PAIRING_ALWAYS_OPEN = os.environ.get(
    "HOMEVOICE_PAIRING_ALWAYS_OPEN", "0") == "1"

#: THE WINDOW AT EVERY START -- OFF (her call, 2026-10-15). It opened the
#: pairing window for the first minutes after every start, for the house
#: whose only phone is dead. A crash and restart opened it too, with nobody
#: choosing to, and a leaked hub key could ride it. A house with two phones,
#: the hub's button and `--pair` does not need it. "1" puts it back.
PAIR_ON_BOOT = os.environ.get("HOMEVOICE_PAIR_ON_BOOT", "0") == "1"


#: THE PI'S HEADER LEDS AND BUTTON (hub/hub_gpio.py; BCM numbers). The FIRST
#: LED is the hub itself: solid = ws_server is listening, fast blink =
#: pairing window open. The SECOND is the phones: slow blink = the door is
#: open and nobody has come through, three flashes = a phone just paired,
#: solid = a phone is connected right now, off = none is. Button held 2 s =
#: open pairing. Only driven on a Raspberry Pi with gpiozero installed
#: (HOMEVOICE_GPIO=0 disables, =1 forces). GPIO 2/3 are the I2C pair with
#: fixed pull-ups -- usable, but taken if the Pi ever drives an I2C device
#: itself, which is why the defaults avoid them. Set the pair LED to 0 to
#: run with one LED, exactly as before.
GPIO_LED_PIN = _env_int("HOMEVOICE_LED_GPIO", "17")
GPIO_BUTTON_PIN = _env_int("HOMEVOICE_BUTTON_GPIO", "26")
GPIO_PAIR_LED_PIN = _env_int("HOMEVOICE_PAIR_LED_GPIO", "27")

MQTT_RELOAD_COMMAND = (
    ["systemctl", "reload", "mosquitto"] if os.name != "nt" else [])

#: TLS ON, pinned by fingerprint -- BOTH legs of the path are encrypted.
#:
#: The phone trusts local.crt because its exact bytes are baked into the app
#: at build time. Each board trusts the SAME certificate because its
#: fingerprint is written over the USB cable while the board is physically in
#: your hand. No certificate authority is involved at either end, nothing is
#: trusted because of a name or a DNS lookup, and nothing leaves the house.
#:
#: esp_builder computes the fingerprint from LOCAL_TLS_CERT and degrades
#: honestly: no certificate file means no fingerprint, which means TLS off
#: and the plain port -- rather than a board told to use TLS with nothing to
#: verify against.
#:
#: ONE CONSEQUENCE, worth knowing before it surprises you: regenerating the
#: certificate invalidates every board's pin, exactly as it invalidates the
#: app's. That is why setup_manager makes --regen-cert deliberate and rare.
#: After one, boards need re-provisioning.
#:
#: Read from _MQTT_TLS at the top of this section rather than from the
#: environment a second time, so the flag and the port cannot disagree.
MQTT_TLS = _MQTT_TLS

#: The four Tasmota commands this rests on. All are read from config rather
#: than hard-coded in the builder, because all four are DOCUMENTED rather
#: than read from source -- and the failure mode of a wrong option number is
#: the worst available: the board comes up, talks happily, and is
#: unencrypted while believing otherwise. The first real board is the test.
#: What clears a Tasmota string setting back to empty.
#:
#: Used to REMOVE the second Wi-Fi network once a migration has finished. A
#: stale network a board will still join is a standing invitation: nothing
#: legitimate broadcasts that name any more, so anyone holding the old
#: credentials can put up an access point with them and the board walks onto
#: it. The fallback is only defensible because it gets deleted.
#:
#: Documented rather than read from source, like the SetOption numbers below,
#: so the first real migration is the test. If the second network survives
#: the promotion step, this token is wrong for your firmware -- and the hub
#: says so rather than assuming it worked.
#: HOW HARD A POWER READING HAS TO MOVE BEFORE THE BOARD SAYS SO.
#: Tasmota's `PowerDelta`, in percent: the whole ENERGY block is published
#: the moment power changes by more than this, instead of waiting for the
#: next TelePeriod. A kettle appears the second it is switched on, and a
#: quiet house still sends nothing. 0 turns it off and goes back to the
#: timer alone.
POWER_DELTA_PERCENT = 5

#: The telemetry floor for a board that carries a meter, in seconds --
#: Tasmota's default is 300, which meant every number the app showed was
#: up to five minutes old. This catches what PowerDelta does not watch:
#: voltage, power factor, the running total. Only boards with a meter get
#: it; a board of relays has nothing to report more often.
METER_TELE_PERIOD = 60

#: Learning what each device draws, from the jump in the main meter's reading
#: when it switches. The meter's last reading counts as "just before" the
#: switch only if it is this new: a meter board reports at least every
#: METER_TELE_PERIOD (and at once on a PowerDelta change), so half a period
#: more allows for a late message. Anything older may hide another change.
METER_FRESH_SECONDS = METER_TELE_PERIOD * 1.5

#: After a switch, meter readings sooner than DRAW_SETTLE_SECONDS are skipped
#: (the meter has not caught up yet); the first one after that, up to
#: DRAW_WINDOW_SECONDS, is the "after". No device may switch in that window,
#: nor in the DRAW_WINDOW_SECONDS before, or the jump is not this device's.
DRAW_SETTLE_SECONDS = 3.0
DRAW_WINDOW_SECONDS = 30.0

WIFI_RESET_TOKEN = '"'

MQTT_TLS_COMMAND = "SetOption103"          # 1 = use TLS for MQTT
MQTT_FINGERPRINT_MODE = "SetOption132"     # 1 = validate by fingerprint
MQTT_FINGERPRINT_COMMAND = "MqttFingerprint1"

# ── Audio ───────────────────────────────────────────────────────────────────
MIC_SR = 16000


# ── Model paths: NLU (nlu/) ─────────────────────────────────────────────────
MPNET_MODEL_PATH = str(NLU_MODELS_DIR / "all-mpnet-base-v2")
LLM_MODEL_PATH = str(NLU_MODELS_DIR / "qwen2.5-1.5b-instruct-q4_k_m.gguf")
INTENT_INDEX_PATH = str(_HOME / "intent_index.npz")
DEVICES_JSON_PATH = str(DATA_DIR / "devices.json")
SCENES_JSON_PATH = str(DATA_DIR / "scenes.json")

# ── Model paths: voice_security/ ────────────────────────────────────────────
SHERPA_SV_MODEL = str(VOICE_SECURITY_MODELS_DIR / "nemo_en_titanet_large.onnx")
# NO CHALLENGE WORDS (Defense, 2026-11-16): the spoken challenge, its
# wordlists and the Moonshine model the Pi heard it with are gone. A voice
# is checked on the command itself; the PIN is the fallback.

# TEST_ASR_MODEL_PATH IS GONE, and so is the faster-whisper model it named.
#
# It pointed at a SECOND, entirely separate ASR engine whose only job was to
# spare you typing a sentence during a local CLI test. The hub has never
# transcribed a command -- the phone does that. That made faster-whisper an
# engine plus a model folder on the Pi's disk, installed and carried for a
# code path the Pi never takes.


# ── Network / TLS ───────────────────────────────────────────────────────────
#
# WS_HOST USED TO LIVE HERE and is deliberately gone. It set one bind address
# for everything, and its comment told you to narrow it to the Tailscale
# address on the Pi -- advice that had stopped being true: NETWORK_BINDINGS
# below replaced it, ws_server binds each entry's own host, and nothing had
# read WS_HOST for some time. Editing it changed nothing, which is worse than
# a missing setting.
WS_PORT = 8765
# The static HTTPS page that serves ws_test_client.html. Independent of
# WS_PORT -- ws_server had 8443 hardcoded in a module that argues at length
# against hardcoded ports.
HTTPS_PORT = 8443

#: THIS MACHINE'S NAME ON THE TAILNET, worked out rather than typed.
#:
#: It used to be the literal string "desktop-2jkh971.tail0d1738.ts.net" --
#: one developer's laptop, hard-coded in a file that ships to every machine.
#: On the Pi it is simply wrong, and nothing says so: the tailnet binding
#: never matches, so the server silently falls back to the access point and
#: remote access appears to be "not working yet" rather than misconfigured.
#:
#: Asked of Tailscale itself, which is the only thing that actually knows.
#: HOMEVOICE_WS_HOSTNAME overrides it for the case where the machine has
#: several names, or where Tailscale is not installed but a name is served
#: some other way.
#:
#: Empty when there is no tailnet. That is a real answer and it is handled:
#: the binding whose host is empty is skipped, so a machine with no Tailscale
#: serves the access point and nothing else -- which is exactly right.
def _tailnet_name() -> str:
    """This machine's MagicDNS name, or "" if it has none."""
    # Asked through core.tailnet (name_now), which knows where the binary
    # lives (PATH, or Program Files on Windows), how to read `status --json`,
    # that DNSName comes back with a trailing dot, and the override.
    from core import tailnet as _tailnet
    return _tailnet.name_now()


WS_HOSTNAME = _tailnet_name()


def hub_display_name() -> str:
    """What a phone calls this hub in its list of paired hubs.
    HOMEVOICE_HUB_NAME when set ("Home", "Office"); else the machine's
    own name, from its tailnet name or its hostname. Only a label -- a
    hub is RECOGNISED by its key, never by this."""
    chosen = os.environ.get("HOMEVOICE_HUB_NAME", "").strip()
    if chosen:
        return chosen[:40]
    if WS_HOSTNAME:
        return WS_HOSTNAME.split(".")[0][:40]
    import socket
    try:
        return socket.gethostname().split(".")[0][:40]
    except OSError:
        return ""

# ── Where to listen: preference order, not a single address ────────────────
# The server binds EVERY one of these whose address actually exists on this
# machine, and re-checks every BIND_RECHECK_SECONDS -- near, the phone finds
# the access point; away, it finds the tailnet; on a house router, the `lan`
# entry. (This used to say "ONE at a time, never both"; the code stopped
# doing that when _reconcile replaced the preference-ordered failover.)
#
# Why an order rather than one host. Two different situations want two
# different boundaries:
#
#   local   -- the phone is a few metres away on an access point the Pi runs
#              itself. No router, no internet, no Tailscale. The boundary is
#              radio range plus the WiFi passphrase, and NOTHING else is on
#              that network -- no TV, no guests, no IoT.
#   tailnet -- the phone is elsewhere. The boundary is the tailnet.
#
# Preferring `local` also fails safe: if neither exists the server does not
# bind at all, rather than falling back to 0.0.0.0 and quietly publishing the
# secret-entry page on every interface in the house.
#
# It solves a boot race too. tailscaled routinely takes several seconds after
# the network comes up to acquire its address; a server that resolved once at
# t=0 and gave up would fail on every reboot. The supervisor re-checks.
#
# `host` is what gets bound. `origin` is the name the BROWSER will put in its
# Origin header -- they differ when you reach the page by name rather than by
# address. Both must appear in the TLS certificate's SANs or the client
# refuses to connect: it needs a secure context for WebCrypto.
# EDIT THIS LIST, NOT CODE. Remove an entry and that route stops existing;
# reorder and the preference changes; add a machine and it is one more line.
# An entry whose address this machine does not have is skipped, so the SAME
# file works on the Pi and on a Windows dev box with nothing conditional in it.
#
# The two access-point entries are the same idea on two platforms: the Pi runs
# hostapd (192.168.4.1 by convention), Windows runs Mobile Hotspot (192.168.137.1,
# the long-standing ICS gateway). Whichever exists is the one that binds.
#
# Put the addresses you TRUST MOST first. Both AP entries sit above the tailnet
# because an access point the machine runs itself has a boundary you can see --
# radio range and a passphrase, with nothing else on the network -- while the
# tailnet depends on a third party's control plane that does not survive a
# reboot without internet.
# SECURITY: TLS cert paths resolve relative to config file (not CWD)
# so systemd/cron/different working directories don't break them.
# The TAILNET certificate. Issued by `tailscale cert <name>.<tailnet>.ts.net`,
# which is a real Let's Encrypt certificate obtained via a DNS-01 challenge
# against a TXT record Tailscale publishes for you.
#
# IT CAN ONLY EVER COVER THE .ts.net NAME. Let's Encrypt does not issue for
# private addresses -- there is no way to prove you control 192.168.4.1 -- so
# this certificate cannot be made to serve the access-point bindings no matter
# what is added to it. That is why there are two.
#
# Two further consequences worth knowing:
#   - it is a 90-day certificate and renewal needs internet, so a Pi kept
#     offline for a quarter loses its tailnet HTTPS;
#   - enabling the feature publishes your machine name and tailnet name on the
#     public Certificate Transparency ledger. Tailscale asks you to acknowledge
#     that when you turn it on.
TLS_CERT = str(_BASE_DIR / "certs" / "server.crt")
TLS_KEY = str(_BASE_DIR / "certs" / "server.key")

# The ACCESS-POINT certificate. Self-signed, generated by setup_manager from
# the addresses in NETWORK_BINDINGS, and trusted once on the phone.
#
# Self-signed is not a compromise here, it is the only option: no public CA
# will ever certify a private address. What it costs you is one "trust this
# certificate" step per device; what it buys is a secure context, which the
# client REQUIRES -- WebCrypto is unavailable over plain HTTP, so without a
# certificate the phone cannot sign the auth nonce at all.
LOCAL_TLS_CERT = str(_BASE_DIR / "certs" / "local.crt")
LOCAL_TLS_KEY = str(_BASE_DIR / "certs" / "local.key")

# `cert`/`key` are per-entry because ONE certificate cannot serve all of these
# -- see TLS_CERT above. Every local network shares one self-signed
# certificate (local.crt); the phone pins its KEY, so no address needs to
# be in it.
NETWORK_BINDINGS = [
    # EVERY LOCAL NETWORK. "lan" is not an address; it is the word the hub
    # opens out, at start and again every re-check, into one listener per
    # PRIVATE address this machine holds -- the house router's, a hotspot
    # it runs (Pi or Windows), a phone's hotspot on demo day, whatever the
    # number. No address is written down anywhere: the phone finds the hub
    # from the pairing code (or the beacon) and proves it by this
    # certificate's key. Never a public address, never the tailnet's
    # 100.64/10 (its own entry below), never a virtual adapter (WSL,
    # Docker). HOMEVOICE_LAN_HOST pins it to one address if ever needed.
    #
    # The phone pins this hub's public key, not a name, so the same
    # local.crt serves here; the certificate's SANs are for the browser
    # page only. What this entry changes is the BOUNDARY: the hub is now
    # reachable by whoever else is on that router. Reachable is not open
    # -- every connection still signs the nonce with a paired key -- and
    # AUTH_FAIL_* below is the extra fence a shared network earns.
    {"name": "lan", "host": "lan", "origin": "lan",
     "cert": LOCAL_TLS_CERT, "key": LOCAL_TLS_KEY},
    # Present only when this machine actually has a tailnet name. A binding
    # with an empty host can never match an address, so including it would
    # be a permanently dead entry that reads like a configured route.
    #
    # THE TAILNET SERVES local.crt TOO -- the PINNED certificate, the same
    # one every other road serves -- not the Let's Encrypt one from
    # `tailscale cert`. That was the design until 2026-09-09, and it failed
    # on the first real phone: the app reaches the tailnet by IP (the .ts.net
    # NAME does not resolve through Dart's resolver on a budget Android, and
    # Android's system root store does not carry Let's Encrypt's newer root
    # that Chrome's own store does), and an IP is PINNED by the app -- it
    # demands local.crt's key and refuses anything else. The field log said
    # it exactly: `key from 100.106.168.60:8765 -> MISMATCH (presented 120B
    # key, pinned 294B)`. Serving local.crt here makes the tailnet identical
    # to every other road: the phone pins the hub's own key, no DNS, no root
    # store, nothing on the phone to trust or rotate. The Let's Encrypt
    # certificate is still issued (tailnet.py) for the browser page on the
    # .ts.net name; the APP never needed it.
    *([{"name": "tailnet", "host": WS_HOSTNAME, "origin": WS_HOSTNAME,
        "cert": LOCAL_TLS_CERT, "key": LOCAL_TLS_KEY}] if WS_HOSTNAME else []),
]

# How often to look for a more-preferred address. A switch only happens when
# no client is connected, so this is not as disruptive as it sounds -- but it
# is still a reconnect, so do not make it aggressive.
BIND_RECHECK_SECONDS = 20.0


# SECURITY: no default API key, and no module-level copy of it either.
#
# API_KEY and _API_KEY used to be read here at import time. Nothing read
# them: require_api_key() below goes to the environment itself, and that is
# what both entry points call. A stale copy of a secret, taken once at import
# and never refreshed, is exactly the thing not to leave lying around.

# ESPHOME_API_KEY USED TO LIVE HERE and is deliberately gone.
#
# It was one encryption key, compiled into the firmware of every board, and
# it forced an exception to this project's own rule that secrets live in the
# environment and nowhere else -- because a compiler needed a copy on disk.
#
# There is no compiler and no shared key. Each board is issued its own
# password when it is set up, stored in that board's file under BOARDS_DIR,
# and revocable on its own: a board unscrewed from a wall is no longer a key
# to the whole house. HOMEVOICE_ESPHOME_KEY can be removed from the
# environment.

# ── Auth thresholds ───────────────────────────────────────────────────────────
SV_SCORE_THRESHOLD = 0.45

# The enrolled voice never changes, and nothing is learned beside it: no
# template update, no voice variations (removed 2026-10-30).

# TALKING OVER A COMMAND (ws_server, "hold"): the longest the hub waits,
# before touching a device, for the phone to say what was said over the
# command. Long enough for New Moonshine Small on her phone (3-7 s).
INTERRUPT_HOLD_MAX_S = 8.0

# The voice of every command is checked; there is no periodic full check.

# ── Biometric store ───────────────────────────────────────────────────────────
BIOMETRIC_STORE_PATH = str(DATA_DIR / "voiceprints.json")
MAX_BACKUPS = 3
COSINE_DEDUP_THRESHOLD = 0.999   # re-saving a near-identical voice sample is a no-op


# ── PIN ───────────────────────────────────────────────────────────────────────
PIN_MAX_ATTEMPTS = 3
PIN_LOCKOUT_SECONDS = 900
# EXACTLY SIX, by the owner's spec (2026-08-26): "let it be six pin as
# must" -- the app renders entry as six boxes that submit on the last
# digit, which only works when six is the one true length. MIGRATION: a
# PIN set under the old 4-6 rule can no longer be TYPED (the format check
# runs on the input), so re-set the PIN once after this update -- easiest
# from the hub's own console (Set PIN), which is gate-free at the
# keyboard; Forgot PIN with the recovery words also works.
PIN_MIN_LENGTH = 6
PIN_MAX_LENGTH = 6

# How long one successful gate check (voice or PIN) vouches for the SAME
# person's next gated menu actions -- the sudo model. Without it every
# gated action re-ran the full check, so adding a device and then
# renaming it asked for the PIN twice in one minute ("theres pin for
# everything now", field test 2026-08-26). Five minutes is long enough to
# finish a batch of housework and short enough that a phone left on the
# sofa expires. 0 disables the grace entirely.
MENU_GATE_GRACE_SECONDS = 300.0
PIN_BCRYPT_ROUNDS = 12


# ── PIN Recovery (BIP-39 style) ─────────────────────────────────────────────
# Wordlist path (download from: https://github.com/bitcoin/bips/blob/master/bip-0039/english.txt)
BIP39_WORDLIST_PATH = str(DATA_DIR / "challenge_words.txt")

# Crypto-style recovery phrase for PIN reset (no voice required)
PIN_RECOVERY_PHRASE_LENGTH = 5          # Number of words in recovery phrase (== words needed)
PIN_RECOVERY_MAX_ATTEMPTS_PER_WORD = 3   # 3 tries per word position
PIN_RECOVERY_WORD_LOCKOUT = 3600         # Lockout per word after 3 fails (seconds)
PIN_RECOVERY_RATE_LIMIT_WINDOW = 3600    # Global recovery attempt window (seconds)
PIN_RECOVERY_MAX_ATTEMPTS = 3            # Max total recovery attempts per window


# ── Resolution tuning (nlu/resolver.py, nlu/device_manager.py) ──────────────
# Cosine-similarity cutoffs and bands. These are NUMBERS, not word judgments --
# vocabulary lives in nlu/lexicon.py. They were spread across resolver.py and
# device_manager.py as private module constants, which is the one place nobody
# looks when a match is too loose or too tight.

# A device must score at least this to count as a semantic match at all.
# resolver.py carried a SECOND copy of this value as an import fallback, which
# would have silently disagreed the moment this one was tuned.
SEMANTIC_THRESHOLD_MPNET = 0.15
SEMANTIC_THRESHOLD_TFIDF = 0.30

# Plural semantic query ("all the lights"): keep every device within this band
# of the top match, so sibling bulbs that just missed the singular threshold
# still come, and the AC does not.
PLURAL_MATCH_BAND = 0.12

# Plural LITERAL match ("locks"): a semantic candidate only joins the literal
# hit if it scores this close to literal's own confirmed match. Tighter than
# the band above on purpose -- this is for a near-synonym (padlock ~
# lock), not for anything on the same general topic (a reading lamp is not a
# bulb).
UNION_SYNONYM_BAND = 0.06

# When several devices match, cut the list at the largest gap between
# consecutive scores rather than at a fixed floor. A gap only counts as a real
# break if it is at least this wide, so a smooth run of similar scores is not
# split arbitrarily.
CLUSTER_MIN_GAP = 0.04

# THE WINNER AND ITS LEAD (nlu/resolver._resolve_semantic, singular). A
# device found by meaning is offered only when it scores within this of the
# best; nothing that close -> the best alone is the one meant. Replaces the
# cut at the largest gap for singular commands, which kept everything above
# the widest break and so offered a motion sensor and a display for "turn on
# the light". MPNet value, derived from the two numbers above rather than
# taken from the EmbeddingGemma copy: twice CLUSTER_MIN_GAP (the smallest
# break this config already trusts as real, so the winner must lead by two
# of them to stand alone) and inside PLURAL_MATCH_BAND (the width of one
# kind of device). Tune from the "[CMD] by meaning:" log lines.
SEMANTIC_WINNER_MARGIN_MPNET = 0.08

# A PLURAL ORDER DONE WITHOUT ASKING ("turn on the lights"): two or more
# devices within TIGHT of the best, and the next one at least GAP below the
# last of them, are switched when the sentence says what to do and none of
# them is a security device. Anything looser asks, as before. MPNet values,
# derived from CLUSTER_MIN_GAP: TIGHT stays under it (no real break inside
# the group), GAP sits above it (a real break after the group, with a little
# to spare because this one acts instead of asking).
PLURAL_GROUP_TIGHT_MPNET = 0.03
PLURAL_GROUP_GAP_MPNET = 0.05

# How long a back-reference stays meaningful. "Turn it off" twenty minutes
# after the command it refers to is a guess, and on a shell that outlives
# every connection it is a guess about somebody else's devices.
CONTEXT_TTL_SECONDS = 600.0


# ── Command interpretation (nlu/executor.py, main.py) ───────────────────────
# The extractor reports non-explicit magnitudes on a 0..1 intensity scale
# ("a bit" ~ 0.1, "a lot" ~ 0.5). Multiplying by this converts that scale into
# a count of the device's own `step`: 0.1 -> one step, 0.5 -> five. Coupled to
# lexicon.py's magnitude values -- change one and re-check the other.
INTENSITY_STEPS_PER_UNIT = 10.0

# Intent classifier acceptance. classify() nulls an intent below either bar,
# so these decide whether "dismiss"/"announce" ever fire.
INTENT_MIN_SCORE = 0.55
INTENT_MIN_MARGIN = 0.08

# "What is it FOR", by meaning (nlu/lexicon.purpose_by_meaning): a question
# the shape rules did not take counts as asking what a device does only this
# close to an example of that, and this far ahead of the nearest "what state
# is it in" example. The same kind of comparison as the intent classifier
# (a short sentence against short example phrases, one MPNet pass), so it
# starts from that classifier's two MPNet numbers above. First guesses: both
# scores are logged. Getting it wrong costs a description instead of a
# reading -- nothing is ever switched from it.
PURPOSE_MIN_SCORE = 0.55
PURPOSE_MIN_MARGIN = 0.08

# Spoken APP ACTIONS ("add a device", "make a new rule"), matched in
# nlu/app_nav.py against a few prototype phrasings per action with the same
# encoder and the same kind of short-phrase comparison the intent classifier
# makes -- so they start from its two numbers. Raise the score if a house
# command is ever taken for an action; lower it if honest paraphrases are
# refused. The console prints both values for every semantic hit
# ("[UI] ... via semantic score=... margin=...").
APP_ACTION_MIN_SCORE = 0.55
APP_ACTION_MIN_MARGIN = 0.08

# Consecutive unusable recordings tolerated during one enrollment before
# giving up, and total enrollment attempts during first-run setup.
MAX_ENROLL_RETRIES = 6
MAX_FIRST_RUN_ATTEMPTS = 5


# ── WebSocket server (ws_server.py) ─────────────────────────────────────────
# Largest base64 audio payload accepted on one frame (~7 MB, roughly 90s of
# 16k mono s16). Sits under the 8 MB websockets frame cap.
MAX_AUDIO_B64_LEN = 7_000_000

# How long the server waits for a human to answer a question -- and what
# expiry MEANS. It was 60.0 and expiry meant ejection: staring at the
# device list inside Add device for a minute cancelled the whole flow and
# dumped the person on the home screen (field test 2026-08-26) -- a timeout
# dressed up as their own cancel.
#
# The owner's design instead: expiry is when the hub ASKS. The app shows
# "Still there?", and only STILL_THERE_GRACE of further silence puts the
# flow away -- back to home through the ribbon's cancelled path, progress
# checkpointed wherever a record exists. A tap on the popup (or simply
# answering the question) restarts the window, indefinitely. So this
# number is "how long before we check on you", never "how long before we
# throw your work out" -- the only thing the pair ever ends is a flow
# nobody is behind, which must not hold the shell lock forever
# (ping_timeout is disabled, so this is the only backstop).
#
# (The setup/ordinary split -- a longer value during first-run setup
# because a new user reads slowly -- kept its name so tests and tuning can
# still separate them, but both carry the same value now: with a
# still-there ask in front, neither has any business being tighter.)
PROMPT_TIMEOUT = 900.0
SETUP_PROMPT_TIMEOUT = 900.0

# The grace after "Still there?" before the flow is put away.
STILL_THERE_GRACE = 300.0


# ── Automations (automations.py) ────────────────────────────────────────────
# The hub's own reflexes: trigger + optional condition + scene/device/notify
# action, evaluated on the hub's MQTT — NOT Home Assistant's engine, because
# HA only ever sees a subset of this house (event devices never appear there
# by design, stateful ones arrive late). Decided 2026-08-26.

AUTOMATIONS_JSON_PATH = str(DATA_DIR / "automations.json")

# The hub's own 24-hour reading history (min/max/avg on the Statistics
# screen), kept by the automation engine from board telemetry and from
# integration sensors as they are read. Replaces the read of Home
# Assistant's recorder.
READINGS_HISTORY_PATH = str(DATA_DIR / "readings_history.json")

# THE MANUAL-OVERRIDE WINDOW: a device a person explicitly commanded is
# immune to automations for this long.
#
# NOW ZERO — AUTOMATIONS OUTRANK COMMANDS (her ruling, 2026-09-14: "let the
# automations out rank the commands / pir fired but cant fire anything").
#
# IT WAS 900. FIFTEEN MINUTES. And that is the whole of "the PIR fires and
# cannot fire anything": every time she switched the kitchen bulb from the
# app to check it worked, she armed a fifteen-minute window in which no
# rule was allowed to touch that bulb. Motion was heard, the rule fired,
# the journal recorded "skipped bulb_2: you commanded it yourself recently,
# and you outrank the machine" — and to anybody not reading the journal the
# automation was simply dead. Testing the light was what broke the light.
#
# WHAT THE WINDOW WAS FOR, so it is not lost: rule says motion → lights on,
# you say "turn it off", you move ten seconds later, and it fights you
# (2026-08-16). That is a real failure and this is a real trade. At 0 the
# machine wins that argument: the lamp comes back on. For a house whose
# rules are motion lighting, that is what motion lighting IS; for a house
# whose rules fight their owner, set this back to 900 — or to 60, which
# settles the ten-second argument without killing the rule for a quarter of
# an hour.
#
# A TIME TRIGGER WAS ALWAYS EXEMPT (2026-09-02), and still is: an
# appointment is not a reaction, so touching a lamp at 15:55 never blocked
# the 16:03 rule, whatever this number says.
#
# 60, AND THE NUMBER WAS ALWAYS THE PROBLEM (2026-09-14, after both
# complaints). 900 made the PIR look dead: switch the bulb from the app to
# see if it works, and no rule may touch it for the next quarter of an
# hour. 0 was the other ditch: turn the light off and the next movement
# turns it straight back on, so you cannot win an argument with your own
# house. One minute is the actual answer -- your command wins while you are
# standing there, and the rule is alive again a minute later.
#
#   0    the machine always wins
#   60   you win the argument you are having (the default)
#   900  the old behaviour
AUTOMATION_OVERRIDE_SECONDS = _env_float("HOMEVOICE_AUTOMATION_OVERRIDE_SECONDS", "60")

# The same rule never fires twice inside this window — a chattering PIR
# must not machine-gun a scene.
AUTOMATION_COOLDOWN_SECONDS = 60

# How long an automation's approval request stands before it lapses. An
# `ask` action puts a question on the phone ("a card was tapped -- unlock
# the door?") and drives NOTHING until a person answers it with their PIN.
# Short on purpose: an approval that outlives the moment it belongs to is
# a key left in a door.
AUTOMATION_APPROVAL_SECONDS = 120

# THE HA EVENT EAR IS GONE (owner's decision, 2026-09-02). Home Assistant is
# used only to add and control integration devices; nothing listens to its
# event stream, and rules are started by boards on the hub's own MQTT.
# Statistics are kept locally -- see READINGS_HISTORY_PATH.

# How long one connection waits for the shell lock before reporting the
# server busy, rather than queueing behind an unbounded number of others.
# Only SETUP flows take that lock (adding devices, boards, people, firmware,
# remote access); commands and the read-only screens never wait for it.
LOCK_ACQUIRE_TIMEOUT = 20.0

# Bound on queued reply frames, so a client spamming replies cannot grow
# memory without limit.
REPLY_QUEUE_MAX = 8

# Auth. Timestamp skew is tolerated because the timestamp is MAC'd alongside
# the nonce; the timeout exists because a peer that never answers the
# challenge would otherwise sit there forever with ping_timeout disabled.
AUTH_MAX_SKEW = 300
AUTH_TIMEOUT = 15.0
#: A PEER THAT KEEPS FAILING TO AUTHENTICATE is refused for a while. On an
#: access point the hub runs itself this never fires -- nobody else is on
#: it -- but on a house router's Wi-Fi other people are, and a socket that
#: can be hammered with bad signatures is a socket worth throttling. After
#: AUTH_FAIL_LIMIT failures from one address inside AUTH_FAIL_WINDOW
#: seconds, that address is answered `too_many_attempts` and closed for
#: AUTH_FAIL_COOLDOWN seconds. A paired phone never trips it: its first
#: attempt succeeds. Set the limit to 0 to switch it off.
AUTH_FAIL_LIMIT = 5
AUTH_FAIL_WINDOW = 60.0
AUTH_FAIL_COOLDOWN = 60.0

# Concurrent connections. Without a cap, an unauthenticated peer could open
# sockets until the process ran out of file descriptors.
MAX_CONNECTIONS = 8

# Mismatched reply frames discarded while waiting for the answer to a specific
# question, before giving up on it.
MAX_STALE_REPLY_FRAMES = 8


# ── Home Assistant client (ha_client.py) ────────────────────────────────────
HA_TIMEOUT_SECONDS = 5
HA_LIVENESS_TIMEOUT_SECONDS = 3


# ── Keeping Home Assistant's readings current ───────────────────────────────
# HOME ASSISTANT PUSHES NOTHING TO THIS HUB. There is no event ear, no
# websocket subscription and no poller anywhere in the codebase, so an HA
# device's stored state used to be only as fresh as the last time a PERSON
# caused something to read it -- and the home screen redrew that same number
# every fifteen seconds for as long as nobody asked.
#
# The cure is ONE `get_states` for the whole house on a slow cadence, run in a
# worker thread (DeviceManager.refresh_ha_states). Not one call per device:
# that is the blocking-per-tile fault of 2026-09-03, and the rule that nothing
# on the event loop ever waits on Home Assistant is unchanged.
#
# 30s is a compromise between a phone tile that feels live and a request every
# second against a Pi that is also running the model. It is NOT the tile push
# rate -- that stays event-driven off MQTT, with its own 15s floor.
HA_POLL_SECONDS = 30.0

# How long an HA device's last answer is treated as still true. Three polls:
# one missed round trip is a hiccup, three in a row is a sensor that has
# stopped answering, and the tile should say so rather than keep drawing its
# last number as though it were current.
HA_STALE_SECONDS = 120.0


# ── Which Home Assistant entities may be registered ─────────────────────────
# HA's raw entity list is everything it knows about -- the sun's position, your
# calendar, update notifications. This is the allowlist of DOMAINS the device
# picker will offer, so registration is not a scroll past dozens of things that
# were never candidates.
#
# It lived in ha_client.py, which meant adding a domain was a code edit. It is
# a setting, so it lives here with the rest.
#
# An ALLOWLIST rather than a blocklist, deliberately. A blocklist is
# permissive by default: a domain nobody thought to exclude appears in the
# picker looking actionable. For a system that opens doors, the safer failure
# is a domain you forgot to add simply not showing up.
#
# "sensor" is here so a power meter can be REGISTERED AND READ. It cannot be
# acted on -- there is no sensor.turn_on in Home Assistant, because HA receives
# a sensor's state and has nothing to send one to. device_manager derives that
# from the shape of the state it translates; see is_readable_only().
CONTROLLABLE_DOMAINS = {
    "light", "switch", "lock", "climate", "fan", "cover",
    "media_player", "valve", "humidifier", "water_heater",
    "vacuum", "alarm_control_panel",
    "sensor",
    # A CONTACT: a door reed, a PIR, a leak or smoke detector, a window
    # sensor (2026-09-12). It was missing, and it is the commonest thing
    # anybody adds to Home Assistant -- so a door sensor bought for this
    # house could not be registered in it at all, while two other places in
    # the code already expected one to exist: HA_READ_ONLY_DOMAINS lists
    # binary_sensor, and the Statistics screen accepts a binary_sensor.*
    # entity. The picker was the only thing that could not make one.
    #
    # READ-ONLY for the same reason as "sensor", and by the same mechanism:
    # is_readable_only() reads the domain, so the executor refuses to drive
    # it without anything here having to say so. There is no
    # binary_sensor.turn_on in Home Assistant either.
    "binary_sensor",
    # A camera is REGISTERED AND VIEWED, never acted on: Home Assistant
    # decodes its stream and serves it behind a per-camera token; the hub
    # hands the app a tokenised URL (main.menu_view_camera) and the feed
    # plays in the app. Nothing here ever holds a frame.
    "camera",
    # Momentary: pressing is the whole vocabulary. This is how IR arrives --
    # a learned remote code becomes a button.* entity, whether captured by
    # hand into ESPHome YAML or by an integration. See
    # DeviceManager.HA_PRESS_SERVICES.
    "button", "scene", "script",
}


# ── Adding a device by address ──────────────────────────────────────────────
# There is deliberately NO list of integrations here.
#
# Home Assistant supports well over a thousand and adds more every release, so
# any copy kept on this side would be a snapshot that silently went stale --
# and the failure would be invisible: a device HA supports perfectly well
# simply missing from the menu. main._start_manual_flow asks HA for the list
# instead, and asks for the manifests too, which carry the friendly name and
# the iot_class saying whether an integration talks to a device directly or
# through somebody's cloud.
#
# This route exists because discovery works by mDNS, and multicast does not
# cross Docker's bridge network, never leaves a Docker Desktop VM, and is
# blocked outright by some access points. On those setups, typing an address
# is not a fallback -- it is the only way in.


# ── IR learning (OpenIRBlaster) ─────────────────────────────────────────────
# The integration's config entry id, which its services take instead of an
# entity id -- learn_start arms the blaster itself, not one of its buttons.
#
# Find it in the URL of Settings -> Devices & Services -> OpenIRBlaster, or
# from the dropdown in the UI service editor. Paste it once.
#
# Empty means IR learning is simply not offered. That is a valid state: the
# rest of the system works without it, and a missing id should not break
# anything else.
OPENIRBLASTER_ENTRY_ID = os.environ.get("HOMEVOICE_IRBLASTER_ENTRY", "")

# Seconds the blaster waits for a remote button after learning is armed. The
# integration's own default is 30; the person has to hear the prompt, pick up
# the remote and aim it, so this is deliberately unhurried.
IR_LEARN_TIMEOUT = 45


# ── Scenes (nlu/scene_manager.py) ───────────────────────────────────────────
# A command must reach this cosine similarity against a scene's stored
# embedding before the scene fires at all.
SCENE_MATCH_THRESHOLD = 0.65

# If the runner-up is within this of the winner AND also clears the threshold,
# the request is ambiguous and the user is asked rather than guessed at.
SCENE_AMBIGUITY_MARGIN = 0.08

# Fallback embedding width, used only when SceneManager is constructed without
# one. main.py passes model_module.embedding_dim(), which is probed from the
# model actually loaded -- prefer that. This exists so the class is
# constructible in a test without a model.
SCENE_EMBED_DIM = 768


# ── Enrollment (voice_security/enrollment_handler.py) ───────────────────────
# Shortest phrase worth embedding. Expressed in SECONDS: the handler derives
# samples from MIC_SR, rather than carrying a hardcoded 8000 that silently
# means something else the moment the sample rate changes.
ENROLL_MIN_PHRASE_SECONDS = 0.5

# RMS below which a recording is "too quiet to use" and the user is asked
# again. Raise it and marginal rooms get rejected; lower it and room tone
# starts contributing to voiceprints.
ENROLL_MIN_RMS = 0.005

# How long an enrollment session survives without activity.
#
# MUST EXCEED SETUP_PROMPT_TIMEOUT. Both were 300.0, so a user who took
# almost the full time allowed to answer one prompt had their session expire
# the moment they answered it -- the two timers raced and the tighter one was
# invisible. (Raised again 2026-08-26 in step with SETUP_PROMPT_TIMEOUT
# going to 900 -- equal values are the same race with bigger numbers.)
ENROLL_SESSION_TIMEOUT = 1800.0

# Usable phrases required out of the full set. Fewer clear recordings than
# this and the averaged template is not worth keeping.
ENROLL_MIN_USABLE_PHRASES = 2


# ── Speaker verification preprocessing (voice_security/sv.py) ───────────────
# What TitaNet actually hears. These decide the score more than any threshold
# does, so they belong where they can be found and tuned. Applied identically
# to enrolled templates and live probes -- that symmetry is the whole point of
# sv._prep, so changing one of these changes both at once.
SV_VAD_FRAME_MS = 25          # analysis frame for the energy VAD
SV_VAD_KEEP_RATIO = 0.10      # keep frames above this fraction of the loudest RMS
SV_VAD_PAD_MS = 50            # keep either side of speech; onsets carry identity
SV_MIN_SPEECH_SAMPLES = 4000  # 0.25s at 16k -- below this much SPEECH, reject
SV_TARGET_PEAK = 0.95         # peak-normalise so mic gain isn't a variable


# ── Audio decode (voice_security/audio_io.py) ───────────────────────────────
# Seconds ffmpeg is allowed for one decode before being killed.
FFMPEG_TIMEOUT_SECONDS = 60


# ── Explicit initialization (call from main.py, NOT at import time) ─────────
def init_directories():
    """
    Create data/model directories. Call once from main.py at startup.

    NOTE: This is NOT run at import time to avoid side effects during
    testing, CLI --list-users, or import by other tools.
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(NLU_MODELS_DIR, exist_ok=True)
    os.makedirs(VOICE_SECURITY_MODELS_DIR, exist_ok=True)
    # Board records. Created here rather than lazily, so that a permission
    # problem surfaces at startup instead of halfway through provisioning a
    # board with a cable in your hand.
    os.makedirs(BOARDS_DIR, exist_ok=True)
    os.makedirs(BROKER_DIR, exist_ok=True)
    # The broker's certificate and its config/password file. Separate
    # directories now, so both have to exist before --broker writes to them.
    os.makedirs(os.path.dirname(BROKER_TLS_CERT), exist_ok=True)
    os.makedirs(os.path.dirname(MQTT_CONF_FILE), exist_ok=True)


# ── Startup validation ────────────────────────────────────────────────────
# Asset-existence checking lives in setup_manager.py now, not here -- a
# second copy of "which paths are required" was a real risk of silently
# drifting out of sync with the real list (it did: this used to also check
# Whisper/Piper, which nothing in the project has ever actually used).
# One place decides what's required; see setup_manager.py's MANUAL_ASSETS
# and URL_ASSETS.

def require_api_key() -> str:
    """
    Validate HOMEVOICE_API_KEY is set. Call once from server startup (not at import time).
    Raises RuntimeError if unset so imports, tests, and CLI tools are unaffected.
    """
    key = os.environ.get("HOMEVOICE_API_KEY")
    if not key:
        raise RuntimeError(
            "HOMEVOICE_API_KEY is not set. Required for WebSocket authentication.\n"
            "Generate a key before starting the service:\n"
            "    export HOMEVOICE_API_KEY="
            "$(python3 -c \"import secrets; print(secrets.token_urlsafe(32))\")\n"
            "Never commit this key to version control."
        )
    return key


def require_fernet_key() -> str:
    """
    Validate HOMEVOICE_FERNET_KEY is set. Voiceprints are encrypted at rest with
    this key — losing it means losing every enrolled voiceprint, so it's
    validated loudly at startup rather than failing wherever BiometricStore
    happens to be constructed.
    """
    key = os.environ.get("HOMEVOICE_FERNET_KEY")
    if not key:
        raise RuntimeError(
            "HOMEVOICE_FERNET_KEY is not set. Required to encrypt voiceprints.\n"
            "Generate one before enrolling any voice:\n"
            "    export HOMEVOICE_FERNET_KEY="
            "$(python3 -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\")\n"
            "Keep this key safe — losing it means losing all voiceprints."
        )
    return key


# ── Home Assistant ────────────────────────────────────────────────────────
HA_BASE_URL = os.environ.get("HOMEVOICE_HA_URL", "http://localhost:8123")


def require_ha_token() -> str:
    """
    Validate HOMEVOICE_HA_TOKEN is set. This is a Home Assistant
    Long-Lived Access Token, generated from HA's own UI (profile ->
    Security -> Long-Lived Access Tokens -> Create Token) -- HA shows it
    exactly once, so it must be saved somewhere safe immediately.
    """
    token = os.environ.get("HOMEVOICE_HA_TOKEN")
    if not token:
        raise RuntimeError(
            "HOMEVOICE_HA_TOKEN is not set. Required to talk to Home Assistant.\n"
            "Generate one in HA's own UI: profile -> Security -> "
            "Long-Lived Access Tokens -> Create Token.\n"
            "HA shows the token exactly once -- copy it immediately, then:\n"
            "    setx HOMEVOICE_HA_TOKEN \"paste-the-token-here\"   (Windows)\n"
            "    export HOMEVOICE_HA_TOKEN=\"paste-the-token-here\" (Linux/Pi)"
        )
    return token
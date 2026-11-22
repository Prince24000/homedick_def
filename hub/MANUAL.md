# HomeVoice Hub Manual — Defense edition

One-time setup, daily use and troubleshooting for the HomeVoice hub, the phone app and the ESP32 boards. This is the **defense** edition: MPNet encoder, no network beacon — phones join by QR code or typed address.

Commands are written `python3 …` (Raspberry Pi / Linux). On Windows type `python …` instead. Run them from inside the hub folder (the folder that holds `setup_manager.py` and `ws_server.py`).

## 1. What you are setting up

| Part | What it does | Installed by |
|---|---|---|
| Python 3.10+ | Runs the hub | You, once |
| Hub packages and models | Speech, voice ID, the sentence encoder, pairing | `setup_manager.py` — automatic |
| Mosquitto | The MQTT broker the ESP boards and Home Assistant talk to | You, once (section 6) |
| Home Assistant (Docker) | Dashboards and the Tasmota device integration | You, once (section 7) |
| Tailscale | Reach the hub from anywhere, over the internet | You, once — optional but recommended (section 8) |
| Phone app | Voice, control, pairing, board setup | Build and install the APK (section 11) |

## 2. What needs the internet

These jobs need the hub to have internet:

| Job | Why |
|---|---|
| First run of `setup_manager.py` | Installs Python packages and downloads the models |
| Installing Mosquitto, Docker, Home Assistant, Tailscale | Downloads |
| GitHub firmware builds, checking the GitHub token | The hub talks to github.com |
| Home Assistant first start and updates | Downloads its image |
| Stock Tasmota firmware updates | Downloads from ota.tasmota.com |

Keep the hub on a network with internet (the home router) and these just work. Without internet they wait; voice control, the boards and the phone keep working locally. Away from home, the phone reaches the hub through Tailscale (section 8).

## 3. Install Python and get the hub folder

**Windows:** install Python 3.11 or 3.12 from python.org. On the first installer screen tick **Add python.exe to PATH**.

**Raspberry Pi OS / Debian:** Python is already there. Add the tools once:

```
sudo apt update
sudo apt install -y python3-pip git
```

Copy or unzip the hub folder somewhere simple, for example `C:\HomeVoice` or `~/homevoice`. Or clone it from GitHub (the way back after an SD card dies, section 17):

```
git clone https://github.com/Prince24000/homedick_def.git ~/homevoice
cd ~/homevoice/hub
```

The hub is the `hub` folder of the app's repo; run every hub command from there.

## 4. Run setup (automatic)

```
python3 setup_manager.py
```

Setup no longer asks questions. It:

1. Checks the internet. If there is none, it says so and stops only when something required is missing.
2. Installs every missing Python package, one progress-bar line per package.
3. Downloads every missing model and file, with an overall bar and a bar for each file: the BIP‑39 wordlist, Tasmota firmware for each ESP32 family, the MPNet encoder (it installs the exporter and exports `all-mpnet-base-v2` into `models/nlu/all-mpnet-base-v2` — a few minutes, and a large one-time download), TitaNet (voice ID), and the optional LLM describer (Qwen2.5‑1.5B, about 1 GB — downloaded only if its engine, `llama-cpp-python`, installed).
4. Generates the hub's certificate (`certs/local.crt`) if there is none.
5. Lists what you still install yourself: Mosquitto, Docker, Tailscale and the secrets.

It is safe to run again at any time: whatever is already there is skipped, so a run that stopped halfway resumes where it stopped. To check without installing anything:

```
python3 setup_manager.py --check
```

`llama-cpp-python` (the LLM's engine) is installed from ready-made packages, so no compiler is needed on Windows or a normal PC. If none fits the machine it has to be built, which can take a long time on a Pi; if that fails, setup just skips the LLM — it is optional and the hub works without it.

**What the MPNet step is.** The sentence encoder the hub uses (MPNet) is published on Hugging Face in a format the hub cannot run directly. "Exporting" converts it once into an ONNX file the hub can run, and saves it in `models/nlu/all-mpnet-base-v2`. Setup does this for you: it installs the converter (a tool called `optimum`, which brings PyTorch with it — a large download) and runs it. You never do it again after that.

If it fails (no internet, or a Pi with too little memory), run setup on a laptop instead, then copy the finished `models/nlu/all-mpnet-base-v2` folder into the same place on the hub.

## 5. Secrets

The hub needs two keys, and optionally a third. They live in one file, readable only by you:

- Windows: `C:\Users\<you>\.homevoice\env`
- Pi / Linux: `~/.homevoice/env`

Create it with both keys in one go (run after setup, which installs `cryptography`; it never overwrites an existing file):

```
python3 -c "import secrets,pathlib;from cryptography.fernet import Fernet;p=pathlib.Path.home()/'.homevoice';p.mkdir(exist_ok=True);f=p/'env';f.exists() or f.write_text(f'HOMEVOICE_API_KEY={secrets.token_urlsafe(32)}\nHOMEVOICE_FERNET_KEY={Fernet.generate_key().decode()}\n');print(f)"
```

On Pi / Linux lock it down: `chmod 600 ~/.homevoice/env`. To add lines later, open it in a text editor (Windows: `notepad %USERPROFILE%\.homevoice\env`; Pi: `nano ~/.homevoice/env`). It looks like this, no quotes:

```
HOMEVOICE_API_KEY=<generated>
HOMEVOICE_FERNET_KEY=<generated>
HOMEVOICE_HA_TOKEN=<Home Assistant token, section 7>
```

Restart the hub after changing it.

Rules:

- Never share or post these values. The API key is never broadcast by the hub.
- The Fernet key encrypts the stored voiceprints. Lose it and everyone must enroll their voice again. Keep a private backup.
- On a Pi, `python3 setup_manager.py --service` writes this file for you from the keys in your shell, and the start‑at‑boot service reads it.
- The GitHub token (section 12) is saved into the same file by the hub. It is never logged and never sent to a phone.

## 6. Mosquitto (MQTT broker)

### Install

**Windows:** download the 64‑bit installer from mosquitto.org/download and install it to the default folder (`C:\Program Files\mosquitto`). You do not need to add it to PATH; the hub finds it there.

**Pi / Linux:**

```
sudo apt install -y mosquitto mosquitto-clients
```

### Configure (once)

```
python3 setup_manager.py --broker
```

This writes `configs/mosquitto.conf`, the password and access files, and the broker certificate `certs/broker.crt`. It creates the Home Assistant account and prints its password **once** — copy it now, it is stored nowhere. Lost it? `python3 setup_manager.py --reset-ha-password`.

Ports: **8883** (TLS, used by the boards) and **1883** (plain, local only, used by Home Assistant).

### Start it

| Goal | Command |
|---|---|
| Start now and check | `python3 setup_manager.py --start` |
| Stop | `python3 setup_manager.py --stop` |
| Start at every boot (asks "Go ahead?" first) | `python3 setup_manager.py --autostart` — on Windows, in a terminal opened with "Run as administrator"; on Linux it uses sudo |
| Check the whole chain | `python3 setup_manager.py --doctor` |

## 7. Home Assistant

### Pi / Linux

Install Docker once, then Home Assistant:

```
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
# log out and back in, then:
docker run -d --name homeassistant --restart=unless-stopped --network=host -e TZ=Africa/Lagos -v ~/homeassistant:/config ghcr.io/home-assistant/home-assistant:stable
```

Change `TZ` to your time zone. Keep the name `homeassistant`: the hub finds the container by that name and starts it with the stack.

### Windows

1. Install Docker Desktop and start it.
2. Home Assistant in Docker Desktop cannot reach `127.0.0.1` on your PC, so the broker's plain port must listen for it. Add this line to your secrets file, then run `python setup_manager.py --broker` again:

```
HOMEVOICE_MQTT_PLAIN_BIND=
```

3. Start Home Assistant:

```
docker run -d --name homeassistant --restart=unless-stopped -p 8123:8123 -e TZ=Africa/Lagos -v %USERPROFILE%\homeassistant:/config ghcr.io/home-assistant/home-assistant:stable
```

### Connect it to the hub

1. Open `http://localhost:8123` on the hub machine (or `http://<hub address>:8123`) and create the owner account.
2. Profile (bottom left) → Security → **Long‑lived access tokens** → Create. Put it in the secrets file as `HOMEVOICE_HA_TOKEN=…`.
3. Settings → Devices & Services → Add integration → **MQTT**. Use the details `--broker` printed: host `127.0.0.1` (Pi/Linux) or `host.docker.internal` (Windows Docker Desktop), port `1883`, the Home Assistant username and password.
4. Add the **Tasmota** integration. Boards then appear on their own once they are set up.
5. `python3 setup_manager.py --doctor` — Home Assistant and MQTT should be OK.

## 8. Tailscale (reach the hub from anywhere)

Tailscale must be **installed** on the hub once. After that the **app joins the hub to your Tailscale network** — no typing on the hub.

**Once, on the hub machine:**

- Windows: install from tailscale.com/download. You do not need to sign in there.
- Pi / Linux:

```
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale set --operator=$USER
```

The second line lets the hub run Tailscale without sudo, so the app can drive it.

**Then, from the phone:**

1. Install the Tailscale app on the phone and sign in (create a free account at tailscale.com if you have none). Leave it on.
2. Make an auth key: open the Tailscale admin console (login.tailscale.com) → Settings → Keys → **Generate auth key** (one-off). Copy it — it starts with `tskey-`.
3. In the HomeVoice app: Settings → **Remote access**. Paste the key and tap **Join with key**. Or leave the box empty and tap **Sign in** to get a sign-in link instead.
4. Confirm with your PIN. The hub joins, gets its Tailscale name and adds the "tailnet" road; your phone learns it straight away.

The hub needs internet for this (section 2). If it has none, the app says so and the key is not used up. If the hub is already on a Tailscale network, the app asks before moving it to another one.

If the app says the hub has no Tailscale, it is not joined yet: use Remote access, or check with `tailscale status` on the hub.

## 9. The network

Put the phone and the hub on the **same network** — the home router, a hotspot, any of them. The hub shows itself on whatever address it has there; the phone finds it (from the pairing code), checks the hub's certificate, and the key unlocks the connection. No IP address is typed or fixed anywhere.

A few facts worth knowing:

- Home Assistant, GitHub builds and downloads need internet, so the network the hub is on should have it (the home router does).
- ESP32 boards only see **2.4 GHz** Wi‑Fi.
- Boards remember the hub's address from when they were set up. If the hub later gets a different address, tell them once: Devices → ⋮ → **Wi‑Fi every board joins**.
- Windows firewall: when Windows asks about the hub or Mosquitto, allow **Private networks**.

## 10. Running the hub

**Windows:** open a terminal in the hub folder and run:

```
python ws_server.py
```

Leave the window open. Clicking inside it no longer pauses the hub (the hub switches off QuickEdit for its own window only). Stop it with Ctrl+C: it shuts the broker and Home Assistant down cleanly.

**Pi (start at boot):**

```
python3 setup_manager.py --service
python3 setup_manager.py --clock-sudo
sudo systemctl status homevoice
journalctl -u homevoice -f
```

`--clock-sudo` lets a paired phone set the Pi's clock (a Pi has no clock battery). Only a paired phone that passed its signed login can do it.

## 11. Pairing a phone and using the app

### Building the app (APK)

The app is a Flutter project. Its large speech model (Moonshine base, about 280 MB) is not sent with the project; `fetch_models.py` downloads it once. The small models (`assets/models/kws/`, `silero_vad.onnx`, `gtcrn_simple.onnx`) come with the project.

You need Flutter and the Android SDK installed, and internet for the first two commands. From the app's project folder (the one that holds `pubspec.yaml`):

```
python fetch_models.py
flutter pub get
flutter build apk --release
```

| Step | What it does |
|---|---|
| `python fetch_models.py` | Downloads Moonshine into `assets/models/moonshine/`. Skips it if it is already there, so it is safe to run again |
| `flutter pub get` | Fetches the app's packages |
| `flutter build apk --release` | Builds `build\app\outputs\flutter-apk\app-release.apk` |

Install the APK by copying it to the phone and opening it, or with the phone on USB: `adb install -r build\app\outputs\flutter-apk\app-release.apk`. The phone needs Android 8 or newer.

### Pairing

1. On the hub: `python3 setup_manager.py --pair`. It opens a 5‑minute window and shows a QR code (also saved in `certs/`, one picture per road).
2. In the app: **Scan hub code**, or type the hub's address. Enter your PIN when asked.
3. The app shows the countdown and connects. After that it reconnects by itself: retries at 2, 5 and 10 seconds, then every 30 seconds, forever.

| Task | Command |
|---|---|
| List paired phones | `python3 setup_manager.py --phones` |
| Remove a phone | `python3 setup_manager.py --unpair <id>` (the first few characters are enough) |
| Just redraw the code | `python3 setup_manager.py --pair-qr` |

Never run `--regen-cert` casually: every phone pinned the old key and must pair again afterwards.

## 12. Firmware builds

Stock Tasmota runs on every board. The house drivers (RC522, LCD, MPU6050) need a build of this project's firmware.

**On GitHub (cloud):**

1. On github.com fork `arendst/Tasmota`. Create a branch named `homevoice` in the fork.
2. Copy from the hub into that branch: `ci/homevoice-build.yml` → `.github/workflows/homevoice-build.yml`; `ci/prove.py` → `.github/homevoice/prove.py`; `esp_firmware/user_config_override.h` → `tasmota/user_config_override.h`.
3. Create a token: GitHub → Settings → Developer settings → Personal access tokens → **Fine‑grained tokens** → Generate. Repository access: **only your fork**. Permissions: **Contents: Read and write**, **Actions: Read and write**.
4. App → Settings → **GitHub builds**: paste the token and the fork name (`yourname/Tasmota`), then Check. From there, build the standard firmware or download the latest finished build.

The hub machine needs internet for this (section 2). The phone only has to reach the hub.

**On a computer (local):** `python3 esp_firmware/build_firmware.py --family ...`. Needs a 64‑bit system, about 4 GB RAM and 6 GB free disk, and internet the first time (PlatformIO downloads its toolchain).

**From the phone:** App → Settings → **Install firmware on hub** sends `.bin` files you already have.

## 13. ESP32 boards

### Setting up a board

1. Plug the board into the **hub machine** with a USB **data** cable.
2. In the app: add a device, pick the board family (ESP32, S3, S2, C3, C6, C2) and the pins.
3. The hub flashes the firmware, gives the board its Wi‑Fi, its own broker password and the broker certificate.
4. The board joins Wi‑Fi, connects to the broker on port 8883 and appears in the app and in Home Assistant.

Pins saved without a board are kept as a draft. They are flashed when you ask, and cleared after a successful flash.

### Will the ESP part be problematic?

The code path is covered by 77 automated checks, but it has not yet been run end to end on real boards in this setup, so expect the usual first‑time hiccups. Almost all of them are in the table below: cable, driver, permissions, 2.4 GHz Wi‑Fi, firewall, or the broker not running. Work down the table in order.

### If a board is not showing

| Symptom | Cause | Fix |
|---|---|---|
| No port appears when plugged in | Charge‑only cable, or no USB driver | Try another cable. Windows: Device Manager → Ports should show a COM port; if not, install the CP210x (Silicon Labs) or CH340 (WCH) driver that matches the chip printed near the USB socket |
| Linux: "permission denied" on `/dev/ttyUSB0` | User not in the `dialout` group | `sudo usermod -aG dialout $USER`, then log out and back in. Check the port with `ls /dev/ttyUSB* /dev/ttyACM*` |
| Flashing never starts or times out | Board not in download mode | Hold **BOOT**, tap **EN/RST**, release BOOT, then flash again |
| "Wrong chip" or flash refused | Wrong family chosen | Pick the family printed on the chip (e.g. ESP32‑S3) and flash again |
| Firmware missing | Image not downloaded | `python3 setup_manager.py` (downloads it), then `python3 tools/check_firmware.py` |
| Flashed, but never appears | Board cannot join Wi‑Fi (5 GHz only, wrong password, router off) | Make sure the router's 2.4 GHz Wi‑Fi is on and the password is right. Watch the board: `python3 tools/board_console.py --check` |
| Joins Wi‑Fi, never reaches the broker | Broker stopped, firewall, or the hub's address changed (Devices → ⋮ → Wi‑Fi every board joins) | `python3 setup_manager.py --doctor`, then `--start`. Windows: allow Mosquitto on Private networks (port 8883) |
| Broker log says certificate error | Broker certificate replaced since the board was set up | Re‑provision the board. Only if the certificate itself is bad: `python3 setup_manager.py --regen-broker-cert`, then re‑provision every board |
| Board shows, then goes quiet | Power‑save sleep | `python3 tools/fix_boards.py <name> --apply` (sends `Sleep 0`) |
| Not in Home Assistant | MQTT or Tasmota integration missing | Section 7, steps 3–4 |

### Board diagnosis commands

```
python3 setup_manager.py --doctor              # the whole chain, step by step
python3 tools/diagnose_board.py                # listen for every board
python3 tools/diagnose_board.py kitchen 30     # one board, listen 30 s
python3 tools/board_console.py --port COM7     # watch a board boot over USB
python3 tools/board_console.py --check         # ask the board for its status
python3 tools/fix_boards.py kitchen --apply    # send Sleep 0
python3 tools/check_firmware.py                # which firmware images are present
```

## 14. Connection troubleshooting (phone ↔ hub)

| Symptom | Check | Fix |
|---|---|---|
| App cannot find the hub | Is the hub running? Same network? | Start `ws_server.py`, then scan the code again with `--pair` |
| Connects, then drops every few seconds | The network, not the hub | Ping test below. A laptop using one Wi‑Fi as hotspot and client at the same time drops packets — use one or the other |
| "Pairing closed" | The 5‑minute window ended | `python3 setup_manager.py --pair` again |
| Login refused after re‑installing the app | The phone was removed or its key changed | `--phones`, `--unpair <id>`, then pair again |
| Works at home, not outside | Tailscale off on one side | Tailscale app on and signed in; `tailscale status` on the hub |
| Hub window seems stuck | A freeze was recorded | Look in `data/freeze_stacks.txt`. A dump that shows only the watchdog line means Windows paused the whole program (sleep, power saver, efficiency mode) — set the power plan to Balanced/High performance and never let the laptop sleep while it is the hub |

**Ping test.** With the hub stopped, from the hub machine ping the phone (or the phone ping the hub) for 30 seconds:

```
ping -n 30 <phone address>      (Windows)
ping -c 30 <phone address>      (Linux)
```

Steady times under ~50 ms: the network is fine. Times jumping to hundreds of ms or timeouts: fix the network first — no hub setting can make up for it.

**Watching the phone's log (developer):** `adb logcat -s flutter`.

## 15. Storage (Raspberry Pi)

- Boot the Pi 4 from a USB SSD rather than an SD card. SD cards wear out.
- To test a suspect card: SD Card Formatter (sdcard.org) and H2testw. If H2testw reports errors or the size is wrong, replace the card.
- Recommended layout: read‑only OS plus a separate data partition mounted at `/srv/homevoice`, with Docker's data moved there, so power cuts cannot corrupt the system.

## 16. Command cheat sheet

| Command | What it does |
|---|---|
| `python3 setup_manager.py` | Install and download everything missing (automatic) |
| `python3 setup_manager.py --check` | Report only, change nothing |
| `python3 setup_manager.py --doctor` | Check broker, accounts, ports, Home Assistant, boards |
| `python3 setup_manager.py --broker` | Configure Mosquitto (once) |
| `python3 setup_manager.py --start` / `--stop` | Start / stop the broker and Home Assistant |
| `python3 setup_manager.py --autostart` | Broker starts at boot (admin) |
| `python3 setup_manager.py --reset-ha-password` | New Home Assistant MQTT password |
| `python3 setup_manager.py --service` | Pi: hub starts at boot |
| `python3 setup_manager.py --clock-sudo` | Pi: let a paired phone set the clock |
| `python3 setup_manager.py --pair` | Open pairing for 5 minutes and show the code |
| `python3 setup_manager.py --phones` / `--unpair <id>` | List / remove phones |
| `python3 setup_manager.py --regen-broker-cert` | New broker certificate (re‑provision boards after) |
| `python3 setup_manager.py --regen-cert` | New hub certificate (every phone must pair again) |
| `python3 ws_server.py` | Run the hub |

## 17. If the SD card dies

The code is on GitHub. Your house is not: these live only on the hub, so back them up.

| What | Where | Lost without a backup |
|---|---|---|
| The keys (API, Fernet, Home Assistant token) and the paired phones | `~/.homevoice/` | Every phone pairs again; saved voices cannot be read |
| The hub's certificates | `certs/` in the hub folder | Every phone pairs again |
| Voices, boards, rooms, scenes | `data/` in the hub folder | Everyone enrols again; boards are set up again |
| Broker logins | `configs/` in the hub folder | Boards are set up again |

### Back up (now, and after big changes)

With the hub in `~/homevoice/hub` (the clone above):

```
cd ~ && tar czf hub-backup-$(date +%F).tar.gz --ignore-failed-read --exclude=.homevoice/recordings .homevoice homevoice/hub/data homevoice/hub/certs homevoice/hub/configs homevoice/hub/custom_drivers
```

A hub still in an older folder (for example `Voice AI`): put that folder's name in place of `homevoice/hub`, in quotes.

A `Cannot stat` warning for a folder you don't have is fine. Copy the `.tar.gz` off the Pi (phone, USB stick, cloud drive). It holds the keys, so keep it private. Also keep the API and Fernet keys in a password manager (`cat ~/.homevoice/env` shows them).

### Rebuild on a new card

1. Flash Raspberry Pi OS, then get the code:

```
sudo apt update && sudo apt install -y git python3-pip
git clone https://github.com/Prince24000/homedick_def.git ~/homevoice
```

2. If you have a backup, put it back **before** setup:

```
cd ~ && tar xzf hub-backup-YYYY-MM-DD.tar.gz
```

3. Run setup. With the backup it keeps your keys. Without it, answer **y** when it asks for the old keys and paste them from your password manager.

```
cd ~/homevoice/hub && python3 setup_manager.py
```

4. Check the whole chain: `python3 setup_manager.py --doctor`
5. Remote access: sign the hub in to Tailscale again from the app (Remote access). Remove the old machine in the Tailscale admin page first, so the hub keeps its name.

With the backup, phones reconnect by themselves. Without `certs/`, each phone pairs again (Settings → Pair a new phone, or `--pair`).

### Public or private repo

- **Public:** anyone can clone it, no login (pushing still needs your token). Before making it public, run this in the repo folder; it must print nothing:

```
git log --all --name-only --format= | grep -E "^(data|certs|configs)/|\.key$|(^|/)env$|passwd"
```

  The hub folder's `.gitignore` keeps `data/`, `certs/`, `configs/` and the models out of every push. Never delete it. The app's build workflow skips pushes that only change `hub/` (`paths-ignore`).
- **Private:** `git clone` asks for your GitHub username and a token (GitHub → Settings → Developer settings → Personal access tokens). It does not save them.

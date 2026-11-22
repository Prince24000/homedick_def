# Code review: HomeVoice (September 2026)

## 1. Overall assessment

The domain logic is ambitious and mostly sound. The weak points are structural and cut across the whole codebase:

- **God files.** `main.py` (10k lines, one ~8.8k-line `InteractiveShell` class), `setup_manager.py` (4.4k), `esp_builder.py` (5.1k), `automations.py` (4.4k) and `ws_server.py` (3.5k). These files mix I/O, business rules and persistence, which is why many of the bugs below sat at seams nobody could see whole.
- **Multiple processes sharing JSON files with no coordination.** `ws_server` and a console `main.py` each keep their own copy of the PIN store, voiceprints, phone registry, devices and scenes. Before this pass, whichever process saved last won, so a lockout or a revocation could be silently undone.
- **Security state measured with the wall clock** on a Pi that has no RTC. Grace windows, pairing windows and lockouts could stretch or shrink when the clock was stepped.
- **Blocking work on the asyncio loop.** Examples: model inference, Home Assistant REST calls, DNS lookups, subprocesses and a TLS handshake. One slow call froze every connected phone.
- **Two paths building the same MQTT commands**: the direct board driver and the automation engine's driver. They had drifted apart; see the shutter bug below.
- **Comments as change-logs.** Many comments are dated narratives ("2026-09-12, her call …") rather than descriptions of the code. Per your instruction they were ignored and left in place. A later pass that cuts them down to *what* and *why* would shrink several files by 30–40%.
- **Dead legacy code shipped alongside live code**: a second CLI, a second voice pipeline, a stale copy of the phone registry, compiled `__pycache__` files and a duplicate model index.

## 2. Restructure

The files were split into `app/`, `core/`, `tools/`, `tests/` and `web/`; see `README.md`. Imports are now package-qualified, for example `from core import config`. File-relative paths resolve through `config.PROJECT_ROOT`. Thin launchers at the root keep `python3 ws_server.py`, `python3 main.py`, `python3 setup_manager.py …` and the systemd unit working unchanged. Line endings were normalised to LF, and `.gitattributes` / `.gitignore` were added.

**Removed (dead or broken):**

- `phones.py` (a stale copy of `identity/phones.py`).
- `nlu/main.py`: a legacy CLI that imported a config name that no longer exists.
- `voice_security/main_voice.py`: imported a missing `anti_spoof` module.
- `nlu/intent_index.npz` (a duplicate).
- Every `__pycache__`.
- About 40 unused functions, classes and constants, each confirmed with a repository-wide search including string dispatch. Examples: `_edit_envelope`, `envelope_meta`, `SherpaASRListener`, `change_pin`, `damaged_records`, `repair_account`, `heal_safeboot`, `push_wiring`, `remove_board`, `detect_direction`, `_a_board_login`, `_mosquitto_passwd` and 12 unused `config` constants.

First pass: roughly 3,200 lines of dead code removed (Python 54,253 → 52,400 lines). The second pass (section 4) removed a further ~1,650 code lines.

## 3. Bugs fixed (by area, most severe first)

### Security / auth
| Where | Severity | Problem → fix |
|---|---|---|
| `app/ws_server.py` `_reader_loop` | **Critical** | A revoked phone kept full control for as long as its socket stayed open, because auth ran only at the handshake. Revocation is now checked on every frame. |
| `nlu/lexicon.py` `NEGATION_RE` | **Critical** | With a curly apostrophe (as speech-to-text produces), "don’t unlock the front door" was split and the unlock ran. Both apostrophes are now handled. |
| `app/ws_server.py` `_auth_for_enrollment` | High | A non-ASCII `hmac` made `compare_digest` raise, crashing auth before the strike counter. An attacker could retry without throttling. |
| `voice_security/pin_auth.py`, `biometric_store.py` | High | No cross-process locking, so a second process could overwrite lockouts and attempt counters. Each change is now lock → reload → modify → atomic write. A failed write now fails closed with `system_error` instead of allowing unlimited guesses. |
| `identity/phones.py` | High | The same lost-update race on the phone registry: an `--unpair` could be undone by a concurrent `touch()`. Also, an I/O error was treated as a corrupt file, which moved the registry aside and reopened the "anyone may enrol" state. |
| `voice_security/biometric_store.py` `update_voice` | High | The adaptive update blended a normalised template with a raw embedding, so one sample was roughly 75% of the result (a poisoning path). Both are now normalised, and NaN is rejected. |
| `hub/homevoice-setclock` | High | CRLF line endings meant the root wrapper could never run. A value over 10 digits also skipped the range check. |
| `voice_security/pin_auth.py` `verify_recovery_phrase` | Medium | Answered "no PIN set" instantly and for free, which revealed which users exist. A dry-run mode allowed unlimited guesses. |
| `app/shell.py` grace window | Medium | Used `time.time()`, so a clock step stretched a PIN pass. It now uses the monotonic clock. |
| `app/shell.py` `menu_enroll_user` | Medium | A phone could create a voice-only person with no PIN or recovery phrase. |
| `app/setup_manager.py` keys | Medium | Private keys were written world-readable, then `chmod`-ed afterwards. |
| `app/setup_manager.py` archive extraction | Low | The path-traversal guard missed symlink and hardlink members. |
| `app/setup_manager.py` input handling | Low | `su -c` quoting was wrong, and `$USER` was trusted. |
| `hub/ota_server.py` | Medium | A client that never read the response held its connection forever and blocked shutdown. |

### ESP firmware / MQTT
| Where | Severity | Problem → fix |
|---|---|---|
| `esp_builder._talk_to_board` | High | Serial reads were split mid-line. JSON read-backs failed, "Unknown command" refusals were missed, and Wi-Fi passwords escaped redaction in the log. Lines are now buffered. |
| `automations._board_driver` | High | Shutters were sent `Shutter<n> <pct>`, which is not a Tasmota command, and it still reported success. Both drivers now share `EspBuilder.drive_commands` (`ShutterPosition`/`ShutterOpen`/`ShutterClose`). |
| Both drivers | Medium | Restoring `{"on": False, "level": 50}` switched a dimmer on at 50%. `on=False` now wins. |
| `esp_builder._provisioning_commands` | Medium | Open Wi-Fi sent a bare `Password3`, which Tasmota treats as a query, so the board never joined. |
| `esp_builder._publish` | Medium | A publish the broker never acknowledged was still reported as "sent". |
| `esp_builder._write_json` | Medium | All writers shared one `.tmp` file and nothing was fsynced. |
| `tools/diagnose_board.py` | High | Reported a healthy board as "IN SAFEBOOT" whenever a display driver was simply absent. |
| `esp_builder.drive_item_sync` | Low | A two-value colour reported "sent" after driving only red and green. |

### LCD panels / DisplayModel (second pass)

In Tasmota, `DisplayModel <n>` saves the model and **restarts the board** about a second later. The codebase says this itself in `_note_presence`, `maintenance_commands` and `_REASSERTABLE_HEADS`. It is also why typing `DisplayModel 1` by hand "fixed" a dark panel: the restart re-initialises the LCD once it has power. Several code paths did not respect that fact.

| Where | Severity | Problem → fix |
|---|---|---|
| `automations._lcd_tick` repeat loop | **Critical** | Sent the panel's wake list, including `DisplayModel 1`, every `DISPLAY_REPEAT_SECONDS` (30 s). That restarted every board with a panel about every 30 s, forever. Relays blipped, readings dropped out, and settings were written to flash on each restart. The re-wake brake (`LCD_REWAKE_GAP`/`LIMIT`) never applied to this path. It now re-asserts only `DisplayDimmer` and the content. |
| `esphome_catalog.configuration_commands` | High | `DisplayModel 1` sat wherever the LCD was in the item list. Its restart abandoned every Backlog command queued behind it: other devices' `SwitchMode`, `SetOption114`, interlocks and friendly names. This is the same bug the code already fixed for `Module 0`. `DisplayModel` now always goes last. |
| `automations._lcd_tick` wake | High | Stage 0 sends `DisplayModel` and the board restarts. The clear, the clock and the "is the driver configured?" question then went into a rebooting board, and were lost while being counted as sent. The board's return also counted as a new "return", which triggered a second restart. The wake now pauses until the board is back (`_lcd_restarting`, up to `LCD_RESTART_WAIT` = 45 s), treats that Online as its own restart, and then continues. The same applies to `_note_display_model`'s one-shot re-send. |
| `automations._lcd_rows` | Medium | Non-ASCII characters, such as "°C" in a reading or an accent in a typed line, are two UTF-8 bytes. They showed as junk and pushed the line one cell past the row. On a 20x4 that spills into another row, overwriting another rule's line. A `[` in text started a DisplayText command. Text is now folded to the panel's ASCII set, and brackets become parentheses. |
| `automations._show` / `_paint` | Medium | Ignored paho's return code, so a write while the broker was disconnected was dropped and still journalled as "showed". Both now report failure, and `_paint` retries on the next tick. |
| `esp_builder.display_targets` | Low | A board with two LCD items produced two panels on one topic, so every wake and repaint was doubled. Now one panel per board, since Tasmota drives one display. |
| `automations.refresh_lcd_targets` | Low | `_lcd_repeated` was never pruned for removed boards. |
| `esp_builder._display_types` | Medium | Any catalogue part whose setup named `DisplayModel` plus `DisplayCols`/`DisplayRows` was treated as a character LCD. That includes a TM1637, a MAX7219 or an OLED. It would then be sent `[x0y<row>]` text, which such displays can't read, and woken like an LCD. Text panels are now only `DisplayModel 1`, Tasmota's character-LCD driver, and the list is read live. |
| `automations._note_i2c_scan` | Low | Re-addressing assumed every panel was a PCF8574 backpack. A panel at any other address (for example 0x3C) would have been moved to 0x27 if some other part answered there. It now only applies to panels configured at 0x27 or 0x3F. |

### Home Assistant
| Where | Severity | Problem → fix |
|---|---|---|
| `ha_flows._run_config_flow` | High | Devices that HA discovered could never be set up ("Unexpected step type None"). |
| `ha_flows` | Medium | Form answers were sent as strings, so ports, booleans and selects were rejected until the step limit. |
| `ha_client` | Medium | The WebSocket client's default 1 MiB limit made large registries silently return empty. `..` entity IDs escaped to `/api/`. A `None` flow ID raised. |
| `app/shell.py` | Medium | `list_controllable_entities()` blocked the event loop. |

### NLU
| Where | Severity | Problem → fix |
|---|---|---|
| `lexicon.find_number_span` | High | "oh, turn on the light" was read as "0", which switched the light off. |
| `lexicon.NEGATION_RE` | High | Catastrophic backtracking: about 31 s per utterance on some inputs. |
| `choice_match._ordinal_index` | High | Saying "second floor light" picked the *first* floor light. |
| `lexicon.words_to_number` | Medium | "two and a half" gave 1.0, and "a hundred thousand" gave 1100. |
| `command_splitter` | Medium | Split names containing "and", such as "salt and pepper lamp". |
| `executor.build_command` | Medium | "50%" on a 16–30 °C device gave 30. A percentage now maps to that share of the range. |
| `model_module` | Medium | The tokenizer was not thread-safe. The index was written non-atomically, so a corrupt file disabled the classifier permanently. |
| `llm_describer._gen_range` | Medium | "16-30" was parsed as 16 and -30. |
| `device_manager._push_to_ha` | Medium | Turning off an HA light while a level was set turned it on first. |
| `scene_manager._save` | Medium | Shared temp file between processes. |

### Automations
| Where | Severity | Problem → fix |
|---|---|---|
| `_observe` | High | Fired on every repeated report, so "door opened" alerted every TelePeriod. It now fires on the change. It also re-fires if the revert already ran while the sensor is still active. |
| `tick` | Medium | Persist and absent state was shared between rules. Absent fired while the sensor was still on. Time rules were missed after a skipped tick. Moving a rule's time later on the same day never fired. One failing rule stopped all the others. |
| `_condition_holds` | Medium | Read stale device records instead of live state. |
| `start` | Medium | Racing calls started two listeners, so every rule fired twice. |
| `_load` | Medium | An unreadable `automations.json` was overwritten, losing all rules. |
| `app/shell.py` `_save_built_rule` | Medium | A condition, else-branch or revert removed in the app survived the save. |

### Setup, service and hub
| Where | Severity | Problem → fix |
|---|---|---|
| `setup_manager._acl_in_conf` | High | Split on spaces, and the project folder is "Voice AI", so `--autostart` always rolled back. |
| `setup_manager._broker_ports` | High | With TLS off it checked 8883, which is never opened, so `--autostart` always rolled back. |
| `setup_manager._service_unit` | High | `ExecStart` was unquoted in a path containing a space. `Group=` assumed a group named after the user. |
| `core/config.py` | Medium | One bad `HOMEVOICE_*` environment value crashed at import, causing a systemd restart loop. A BOM in the secrets file dropped its first key. |
| `hub/hub_clock.recheck` | Medium | Cleared the "1970 clock" verdict right away, so phones were refused. |
| `app/ws_server.py` | Medium | Blocking work ran on the event loop: NLU navigation, DNS/route probes, `httpd.shutdown`, QR drawing. A static HTTPS client that never sent a hello froze the page. A listener on a stale tailnet IP was kept forever. |
| `app/setup_manager.py` | Low | `requests` was missing from the dependency check, so Home Assistant was silently disabled on fresh installs. |
| `web/ws_test_client.html` | High | Could not authenticate against the current protocol at all. It also sent a menu action that no longer exists. |

The spec test `tests/test_nlu_spec.py` crashed before the first pass; it ran 28/28 after it.

## 4. Second deep pass: bugs, dead logic, line reduction

Scope: code only. Comments and docstrings were left as they were, as you asked. Code lines (excluding comments and docstrings) went from **29,159 to 27,508** (−1,651, about −6%). That is after about 400 lines of new locking, button handling and tests were added.

### Bugs found and fixed

| Area | Severity | Problem → fix |
|---|---|---|
| `app/ws_server.py` auth throttle | **High** | The per-IP throttle read the peer address *after* a failed proof. websockets clears it once the socket closes, so a client that sent a bad pairing proof and hung up was never throttled: unlimited guesses at the pairing key. The address is now captured at connect. |
| `nlu/lexicon.py` scope detection | **High** | "turn off the lamp, that is all" switched off the **whole house**. "…and that's all" switched it all on. A closing "all" now needs a switch word, particle or pronoun. |
| `nlu/command_splitter.py` | **High** | "what's the status of the front door and the back gate" **toggled the back gate**. "what is on the tv" split off "on the tv" and toggled the TV. Questions now keep their parts together. "make the door half open" no longer fully opens it. |
| `app/shell.py` `_carry_out` locks | **High** | A lock that refused the command still printed "-> front door: lock" and told the phone ok. Now reported per device. |
| `app/setup_manager.py` `--autostart` | **High** | Could never succeed on Debian/Pi OS: mosquitto 2 refuses a second `persistence_location`. Reproduced on 2.0.18. Under `sudo` it also rewrote the hub's broker password in root's home. |
| `app/setup_manager.py` account reload | **High** | Every new board password restarted the hub's broker, dropping every board. Now SIGHUP, with the PID unchanged (verified). |
| `nlu/device_manager.py`, `scene_manager.py` | **High** | The service and a console each saved their whole copy, so the last writer won and devices added in one vanished. A corrupt file was reset and then overwritten. Both now use the shared cross-process store (`core/locked_store.py`): corrupt files are moved aside, and unreadable files refuse writes. |
| `home_assistant/ha_flows.py` | **High** | Used HA's Python method names (`external_step`, `show_progress`) instead of what HA sends (`external`, `progress`), so every browser sign-in and pairing step failed. Selector fields were shown as free text, and a boolean answered "no" was sent as **yes**. |
| Buttons / doorbell (catalogue, automations) | **High** | Without `SetOption73 1`, a Tasmota button toggles relay 1 and reports nothing, so a doorbell press flipped the lamp and no rule could fire. Buttons are now detached and heard as SINGLE/DOUBLE/HOLD events. ⚠ Existing boards get this when they reconnect, so their physical button stops toggling relay 1. |
| `nlu/executor.py` up/down on on/off devices | Medium | "turn the fan up" switched a running fan off, and "dim the light" on an off HA light turned it on. Up now means on and down means off. "warmer/cooler" follows the device: cooler turns an AC on and a heater off, and anything unknown toggles as before. |
| `lexicon` blanket switches in questions | Medium | "is it all off" switched everything off. State questions no longer switch. Polite requests ("is it possible to turn everything off") still do. |
| `nlu/automations.py` storage | Medium | `automations.json` was fsynced on **every firing** (about 43,000 writes a day for a 2-second display rule) through a fixed `.tmp` name shared by both processes. Counts are now flushed on the history cadence and at exit. Reading rules walked every board file per MQTT message; they now come from the index. |
| `nlu/automations.py` LCD | Medium | A held line during a wake froze the panel's re-assert and bus scan forever. The hand-back ignored dropped publishes. `refresh_lcd_targets` could drop a wake stage, which re-sends `DisplayModel` and restarts the board. |
| `esp_firmware/esp_builder.py` | Medium | `_plan_notes` race between threads (per-thread now). `Gpio 255` answers split across messages were cut short. |
| `esp_firmware/esphome_catalog.py` | Medium | FriendlyName 9+ (Tasmota has 8 slots), PWM > 16, rotary > 2 and DHT > 4 were allowed and silently ignored by the firmware. The template `BASE 18` is an ESP8266 value; it is now 1. |
| `esp_firmware/ir_flows.py` | Medium | "Learned" with nothing new still rewrote the record. A remote named like an existing relay had IR codes attached to the relay. |
| Safeboot healer | Medium | Sent boards to a 404 when the hub had no image. It is now rate-limited before the image lookup, and the image is cached rather than re-read (re-cut) per message. |
| `identity/`, `voice_security/` | Medium | People journal and card store had lost writes between processes. A failed registry re-read could still write, and could resurrect a revoked phone. A quarantined voiceprint store reported enrolment success. |
| Person removal | Medium | A deleted person's phones stayed bound to them, and the phone was then stuck on "not enrolled". |
| `app/setup_manager.py` other | Medium | The unit's `CAP_SYS_TIME` leaked into the network-facing broker. `hub_account.json` was made group-readable. Unknown CLI flags started a full interactive setup. The MPNet model was missing from the asset check. |
| `app/shell.py` other | Low–Med | Argument menu actions skipped cancel/error handling. Board-picker "back" showed as failure. `menu_resume_setup` required HA for no reason. `_fire_scene` / all-on / all-off always reported success. A typo at "Who is speaking?" crashed the console. Registry writes, model inference and HA calls ran on the event loop. |
| `hub/ota_server.py`, `core/dependency_flags.py` | Low–Med | Firmware cutting and hashing on the main loop. Every voice module import loaded llama_cpp/onnxruntime just to set flags. |

**Behaviour change you asked for:** the manual-override window (`AUTOMATION_OVERRIDE_SECONDS`, 60 s) now counts from your FIRST command to a device. Repeat commands inside the window no longer extend it. A command after it closes starts a new window (`AutomationEngine.note_human`).

### Dead logic removed (examples)
- The whole `llm_describer` transformers backend (it needs torch, which the project deliberately never installs).
- About 150 `getattr(config, "X", default)` fallbacks where X always exists. Two of them had defaults that *contradicted* config (a pair-LED pin of 0 vs 27, an override of 900 s vs 60 s).
- Capability probes for store classes that can't exist, paho ReasonCode shims, macOS branches, the `ss` fallback, and the Silero VAD asset nothing used.
- About 25 parameters no caller ever passed: `room_hint`, `audio`, `everything`, `min_score`, `stale_after`, `rule_set`, `per_message`, `before`, `max_skew`…
- Unreachable fallbacks: cancel words that differed from the real ones, a copy of `ir_fallback_description`, `SECURING_ON_WORD`, and import shims for modules that always import.

### Consolidations
- **One persistence layer**, `core/locked_store.py`: cross-process lock, reload-on-change, atomic 0600 write, quarantine. It replaces 10 private copies (devices, scenes, automations, phones, pairing window, people, cards, PINs, voiceprints, board records, hub account).
- **One MQTT session helper** in `esp_builder` (it replaces three connect/wait/teardown copies), `tools/board_mqtt.py` for the CLIs, and one paho client factory.
- **One PIN loop** (`_pin_tries`) for the voice fallback, `confirm_with_pin` and phone login. Also one `choose` loop, one flow-start gate and one refusal-frame builder in ws_server, and one REST request helper in `ha_client`.
- **Shared network helpers** (`hub_net.route_address/holds_address`), a table-driven CLI, the doctor and frame router, and apostrophe folding and number words defined once in `lexicon`.

The tests now cover 34 NLU spec checks plus `tests/test_hub_regressions.py` (18 checks: drive commands, DisplayModel ordering, LCD restart discipline, text safety, buttons, warmer/cooler).

## 4c. Third pass: what the first two missed

### Bugs fixed

| Where | Severity | What was wrong |
|---|---|---|
| `esphome_catalog` shutter, garage, gate, curtain, servo | **High** | Tasmota's shutter driver is off until `SetOption80 1`, and the two relays were never interlocked. Every `ShutterPosition/Open/Close` answered "Unknown", so the motor never moved. All four now share one setup: `SetOption80 1`, `Interlock`, `ShutterRelay`. |
| `voice_security/voice_auth._record_challenge_failure` | **High** | After the first lockout expired, the old lock time was never cleared. Every later failure reset the count to 1, so the voice challenge could never lock again: unlimited guesses. |
| `core/locked_store.LockedJsonStore._load` | **High** | Any read error (too many open files, an SD-card glitch) was treated as a corrupt file. A good PIN or voiceprint store was moved aside, and every PIN was lost. A read error now keeps what is in memory, refuses writes, and retries. |
| `esp_builder.drive_commands` | High | An RGB light told "off" with a colour turned ON in that colour, e.g. an undo or a scene restoring an off colour light. Off now sets every channel to 0. |
| `nlu/automations._observe` | Medium | A rule with an ELSE re-ran the ELSE on every repeated sensor report: one door opening sent 6 notifications in 25 minutes. It now runs once. A rule held back by its condition still fires on a later report, as before. |
| `app/shell._build_and_save` | Medium | A rule the hub refused closed the phone's builder and lost the work. The builder now reopens with the blocks kept and the reason spoken. |
| `app/ws_server` busy gate | Medium | The app's automatic `check_notices` on connect made a command spoken right after connecting come back "busy". A command now waits behind that read instead. |
| `app/ws_server._broadcast` | Medium (security) | A removed phone that stayed connected but silent kept receiving readings and PIN approval requests. It is now dropped. |
| `app/ws_server._bootstrap_enrollment` | Medium | A phone bound to a person on a hub whose voiceprint store was empty was told "enroll first" on every command, then nothing happened. |
| `app/shell.run` (console) | Medium | Ctrl-D, or no input (`nohup`), looped forever printing errors (5,512 in 3 s). It now exits. |
| `esp_builder` serial provisioning | Medium | The second Backlog could land on a board that was already restarting. Both lines now go in one write. |
| `esp_builder._ensure_wifi` | Medium | Moving boards to a new network with a blank hub address sent every board to the old address. |
| `esphome_catalog.check` | Medium | A button or switch on GPIO34–39 (no pull-up) floats and reports phantom presses. It now warns. |
| `ir_flows._learn_ir_local` | Medium | Cancelling mid-way threw away buttons already announced as "Saved". |
| `home_assistant/ha_flows` | Medium | A board door strike was never marked secure by default: the check looked for a `lock` domain that no part has. |
| `nlu/automations.trigger_options` | Medium | A device that had not reported yet was offered "level rises above" rules that could never fire. |
| `voice_security/pin_auth.verify_recovery_phrase` | Medium | A timing difference told an attacker whether a user exists. |
| `hub/hub_clock.recheck` | Low–Med | On a Pi 5, "the clock went backwards" was cleared on the first tick. |
| `nlu/device_manager` | Low–Med | Every Home Assistant refresh dropped the stored unit (°C). |
| Backlog chunking | Low–Med | A serial line over Tasmota's buffer is silently dropped. Chunks are now capped at 500 characters as well as 30 commands. |
| Others | Low | Monotonic clocks for timeouts and login throttle; the `SWITCH<n>` JSON payload; `{value}` for card, button and IR rules; card and people stores now reload across processes; the intent index temp file is unique; the clock adoption runs off the event loop; a "busy" refusal names the request it refused; `update()` can't collide with a rule field. |

### Dead logic removed

Unused catalogue components (`Relay_i`, `Button_n`, `Switch_n`); `WebSocketIO._last_reply_cancelled` (never set); the `_phone_id` guards for a "legacy client" that cannot reach that code; `is None` checks on `ainput` (it returns text or raises); the unreachable bus branch in `_pick_pin`; the display-type fallback; `resolve_semantic`'s unrestricted path; unused constructor parameters in the enrolment and challenge classes; duplicate name checks. `fix_boards` now uses its `LCD_CURE` constant instead of repeating it.

### Tests added

`tests/test_hub_regressions.py` now has 25 checks: off beats colour, and ELSE runs once while a sensor stays on.

## 5. Open items (not changed: they need your decision or hardware)

1. **Buttons on existing boards.** `SetOption73 1` is re-asserted when a board reconnects, so any physical button that used to toggle relay 1 stops doing so. That is correct for "the hub hears the button", but wire an automation if you relied on the old toggle.
2. **IR raw capture does not work end to end** with the shipped firmware (`RawData` arrives as a string, and `IRsend` rejects raw arrays). Only decoded protocols can be learned, which rules out most air-conditioner remotes. It needs the IR-full firmware build plus a raw capture and replay path.
3. **Two single-channel dimmers on one board** become one colour-temperature light in Tasmota unless `SetOption68 1` is set. That option restarts the board, so it has to be handled like `DisplayModel` (sent last, never re-asserted).
4. **"down by half"** is still read as "down by 0.5". It needs a fraction flag from `slot_extractor`.
5. **`absent` rules** wait for an explicit "off", so a pulse-only sensor never triggers one.
6. **LCD restarts once per wake** (hub start and genuine returns, capped at 3 per run). The gentler `DisplayMode 0` → `1` toggle is an option; test it on one board.
7. **Other display types.** The TM1637 and MAX7219 matrix drivers are compiled in but have no catalogue entries. OLED/TFT need Tasmota's universal display driver.
8. **Broker password file** writes have no cross-process lock, because a root-owned lock file would block the hub user. Concurrent account creation can lose one entry.
9. **Challenge policy.** 2 of 3 words pass at `TEXT_MATCH_THRESHOLD=0.60`.
10. **Wi-Fi password on the `nmcli` command line** is visible in `/proc`.
11. **Next refactor:** split `InteractiveShell` (still about 5,700 code lines) into menu mixins, and trim the dated comment narratives (about 20k lines).
12. **Pushing a new build to running boards** is still manual (each board's web page). Their `OtaUrl` already points at the hub, so a "send `Upgrade 1` to every board" action would be a small addition.
13. **The voiceprint cache** in the other process is only refreshed at start-up. After someone re-enrols or is removed from the terminal, the service keeps the old voiceprint until it restarts.
14. **`auth_ok` is sent before the connection limit is checked.** A full hub says "ok" and then "no". The app now copes; moving the limit check first is cleaner.
15. **Shutter plus servo on one board.** `ShutterMode` applies to the whole board, so the two conflict. Both still need testing on hardware.
16. **Paired but not-yet-enrolled phones** still receive readings and approval notices. This is a policy decision.

## 6. How this was verified

- Every module imports from a neutral working directory, and `ruff` (F, B, E9, PLE) is clean.
- `tests/test_nlu_spec.py`: 34/34. `tests/test_hub_regressions.py`: 25/25. `test_app_nav` 223, `test_choice_match` 269, `test_pin_map` 119.
- Third pass: each fix was reproduced against the previous commit with a throwaway script, then an independent reviewer checked the whole diff for regressions (it found one, now fixed).
- The launchers run through to the setup gate. `setup_manager.py --doctor`, `--help` and unknown flags behave correctly.
- Old-vs-new differential runs, comparing the pre-pass-2 code with the current code:
  - about 150 commands through the splitter, slots and pre-filters;
  - 13,500 resolver cases and 7,128 `build_command` cases;
  - 3,000 random boards through the catalogue;
  - 6,000 `choose` sequences;
  - every menu and refusal frame.

  Every difference is one of the listed fixes.
- Real mosquitto 2.0.18 in the sandbox: `--broker` idempotency, SIGHUP reload, ACL isolation, TLS on 8883, and autostart against a Debian-style config.
- Multi-process store tests (real subprocesses): no lost writes for devices, scenes, PINs, voiceprints, phones, people and cards.
- Headless Chromium drove the test web client against a fake socket.
- Nothing was tested on a real Pi, board or Home Assistant instance. Treat the first deploy as a test, and re-run `python3 setup_manager.py --service` on existing Pis to pick up the new unit and polkit rule.

# HomeVoice hub

A Raspberry Pi hub that turns spoken and typed commands from a paired phone into actions on
Tasmota ESP32 boards (over MQTT) and Home Assistant devices, with voice and PIN security.

## Layout

```
Voice AI/
├── ws_server.py          # launcher: the hub service (systemd runs this)
├── main.py               # launcher: the console menu at the hub's keyboard
├── setup_manager.py      # launcher: installer / doctor (--check, --doctor, --broker, --service …)
├── requirements.txt
├── intent_index.npz      # generated intent index (rebuilt automatically when stale)
│
├── app/                  # the three programs
│   ├── ws_server.py      #   WebSocket + HTTPS server, phone auth, listeners
│   ├── shell.py          #   InteractiveShell: menus, commands, voice/PIN gate (was main.py)
│   └── setup_manager.py  #   dependency check, certs, mosquitto, systemd unit, pairing
│
├── core/                 # shared foundations, no app logic
│   ├── config.py         #   paths, constants, env overrides (PROJECT_ROOT lives here)
│   ├── dependency_flags.py
│   ├── hv_common.py      #   event log, cancel words, name validation, paho client factory
│   ├── locked_store.py   #   shared JSON store: cross-process lock, reload, atomic write, quarantine
│   └── tailnet.py        #   Tailscale integration
│
├── nlu/                  # text → intent → device action
│   ├── lexicon.py  slot_extractor.py  command_splitter.py  choice_match.py
│   ├── resolver.py  executor.py  device_manager.py  scene_manager.py
│   ├── model_module.py  intent_prototypes.py  llm_describer.py  app_nav.py
│   └── automations.py    #   rules engine (MQTT events, time, persist/absent, reverts)
│
├── voice_security/       # speaker verification, PIN + recovery phrase
├── identity/             # people journal, paired-phone registry + pairing window
├── home_assistant/       # HA REST/WebSocket client and guided config flows
├── hub/                  # GPIO panel, clock guard (+ root setclock wrapper), network, OTA server
├── esp_firmware/         # board flashing/provisioning, component catalogue, IR, firmware builds
│   ├── build_firmware.py        # local firmware build (a CLI)
│   ├── user_config_override.h   # the house firmware definition (stock tasmota32 + drivers)
│   └── firmware/            # EVERY image, every family: <env>.factory.bin + <env>.bin
│
├── tools/                # stand-alone CLIs (run from anywhere)
│   ├── diagnose_board.py   check_firmware.py   fix_boards.py
│   ├── board_console.py  #   plain serial console for a Tasmota board (was test_spec.py)
│   └── board_mqtt.py     #   shared MQTT helper for the tools
├── tests/
│   ├── test_nlu_spec.py        #   python3 tests/test_nlu_spec.py
│   ├── test_hub_regressions.py #   python3 tests/test_hub_regressions.py
│   ├── test_firmware_build.py  #   the proof, families, CI copy, firmware from the phone
│   └── test_app_nav.py  test_choice_match.py  test_pin_map.py
└── web/
    └── ws_test_client.html   # browser test client, served at https://<hub>:8443/
```

The root launchers keep every existing command working (`python3 setup_manager.py --service`,
`python3 ws_server.py`, the generated systemd unit). Runtime folders (`data/`, `models/`,
`certs/`, `configs/`) are created beside them and are git-ignored.

## Quick start

```
pip install -r requirements.txt
python3 setup_manager.py          # fetch models, check everything
python3 setup_manager.py --broker # MQTT broker config + accounts
python3 setup_manager.py --service  # start at boot (Pi)
python3 tests/test_nlu_spec.py    # NLU spec
python3 tests/test_hub_regressions.py
```

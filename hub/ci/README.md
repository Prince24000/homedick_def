# ci/ — the fork's build, kept with the hub

These two files belong in the **homevoice branch of the Tasmota fork**, not here. They are kept in the hub so the proof stays next to the code it copies.

| Hub file | Put it in the fork at |
|---|---|
| `ci/homevoice-build.yml` | `.github/workflows/homevoice-build.yml` |
| `ci/prove.py` | `.github/homevoice/prove.py` |
| `esp_firmware/user_config_override.h` | `tasmota/user_config_override.h` (inside `tasmota/`, not the repo root) |

The workflow runs on every push to `homevoice` and from the Actions tab (Run workflow). It builds `tasmota32`, `tasmota32s3`, `tasmota32s2`, `tasmota32c3`, `tasmota32c6` and `tasmota32c2` side by side. A family that fails does not stop the others.

For each family it:

1. builds with `pio run -e <env>`;
2. proves the linker map with `prove.py`;
3. publishes an artifact named `<env>` that holds exactly two files:
   - `<env>.factory.bin` (new boards, over the cable) and
   - `<env>.bin` (running boards, over the air).

   Both go into ONE folder on the hub: `esp_firmware/firmware/`. Unzip every
   family's artifact straight into it -- that is the whole install.

   You can also send both files from the phone: **Settings → Install firmware on hub**.

A last job lists which families passed.

**Why `prove.py` is a copy:** the fork cannot import the hub. It has to be the same proof: the same markers, the same kept-part-of-the-map rule, and the same drivers Tasmota is known to drop. So `tests/test_cloud_build.py` loads `ci/prove.py` and fails if it disagrees with `esp_firmware/build_firmware.py`. When you change the proof, change the hub first, then copy `ci/prove.py` to the fork again.

## Building from the hub (no GitHub website needed)

The hub can drive this workflow itself (`esp_firmware/cloud_build.py`):

- **Settings → GitHub builds:** set or replace the token and the fork, check they work, build the standard firmware (the `homevoice` branch, every chip), or download the latest finished build.

The hub talks to GitHub over its own internet (router or cable). The phone only has to reach the hub, at home or through Tailscale.

**The token.** github.com → Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token.
- Repository access: **only your Tasmota fork**.
- Permissions: **Contents: Read and write**, **Actions: Read and write** (Metadata: read is added automatically).

A classic token with `repo` and `workflow` also works. The hub keeps it in its secrets file (`~/.homevoice/env`, owner-only) and never shows it back. Removing it in Settings removes it from the hub. Delete it on GitHub too if you no longer need it.

**Local builds** still work without GitHub: `esp_firmware/build_firmware.py --family ...` on a machine that meets the minimum (it says what is missing).

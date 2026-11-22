"""GitHub builds: the hub's road to a build on the owner's Tasmota fork.

Everything GitHub says is faked (cloud_build._request); no network, no
token, no toolchain. Covers the settings, the run from dispatch to
installed images, the checks every image passes, the failures (no
internet, a refused token, a family GitHub could not build) and
Settings -> GitHub builds.

Run: python3 tests/test_cloud_build.py   (from anywhere)
"""
import asyncio
import base64
import contextlib
import io
import json
import os
import shutil
import stat
import struct
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("HOMEVOICE_HOME", tempfile.mkdtemp())

from core import config                                  # noqa: E402
from core.hv_common import CancelledByUser               # noqa: E402
from esp_firmware import build_firmware as bf            # noqa: E402
from esp_firmware import chips                           # noqa: E402
from esp_firmware import cloud_build as cb               # noqa: E402
from esp_firmware import esp_builder as EB               # noqa: E402
from hub import ota_server as ota                        # noqa: E402

PASSED = FAILED = 0
OVERRIDE = ROOT / "esp_firmware" / "user_config_override.h"
TOKEN = "ghp_" + "a" * 36
CLASSIC = chips.DEFAULT
C3 = chips.FAMILIES["esp32c3"]


def check(name, ok, detail=""):
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED += 1
        print(f"  [FAIL] {name}   {detail}")


# ── images a board would accept ────────────────────────────────────────

CHIP_ID = bf.IMAGE_CHIP_IDS


def app_image(marker: bytes, chip: int) -> bytes:
    """A one-segment ESP image; the extended header's chip id at 12-13."""
    seg = marker.ljust(16, b"\0")[:16]
    head = bytearray([0xE9, 1]) + bytearray(22)
    head[12:14] = chip.to_bytes(2, "little")
    app = bytes(head) + struct.pack("<II", 0x3F400020, len(seg)) + seg
    return app + b"\0" * (64 - len(app))


def factory_image(app: bytes) -> bytes:
    image = bytearray(b"\xff" * (0x10000 + 0x1000))
    entry = struct.pack("<HBBII", ota.PARTITION_MAGIC, ota.PART_TYPE_APP,
                        ota.PART_SUBTYPE_OTA_0, 0x10000, 0x1000)
    image[0x8000:0x8000 + 32] = entry + b"app0".ljust(16, b"\0") + b"\0" * 4
    image[0x10000:0x10000 + len(app)] = app
    return bytes(image)


def artifact(board, chip=None) -> bytes:
    """One family's artifact zip, as the workflow uploads it."""
    chip = CHIP_ID[board.family] if chip is None else chip
    app = app_image(board.pio_env.encode(), chip)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(board.firmware, factory_image(app))
        archive.writestr(board.ota_name, app)
    return buffer.getvalue()


# ── a fake GitHub ──────────────────────────────────────────────────────

class FakeGitHub:
    """Answers the calls cloud_build makes, remembering each."""

    def __init__(self, repo="augusta/Tasmota", workflow=None, proof=None,
                 artifacts=None, token_ok=True, online=True,
                 run_id_in_answer=True, steps=3):
        self.repo = repo
        self.workflow = (workflow if workflow is not None else
                         (ROOT / "ci" / "homevoice-build.yml").read_text())
        self.proof = (proof if proof is not None else
                      (ROOT / "ci" / "prove.py").read_text())
        self.artifacts = artifacts or {}
        self.token_ok, self.online = token_ok, online
        self.run_id_in_answer = run_id_in_answer
        self.calls, self.dispatched = [], []
        self.polls, self.steps = 0, steps

    #: What GitHub's expiry header says for this token (None: no header,
    #: a token made with no expiry date).
    expires = None

    def __call__(self, method, url, token="", body=None, accept=""):
        status, headers, raw = self._answer(method, url, token, body)
        if self.expires and url.startswith(cb.API) and status != 401:
            headers = {**headers,
                       "GitHub-Authentication-Token-Expiration": self.expires}
        return status, headers, raw

    def _answer(self, method, url, token="", body=None):
        self.calls.append((method, url, token))
        if not self.online:
            raise cb.CloudError("This hub cannot reach GitHub (no route).")
        if url.startswith("https://storage.example/"):
            name = url.rsplit("/", 1)[1]
            return 200, {}, self.artifacts[name]
        if not url.startswith(cb.API):
            return 404, {}, b""
        if not self.token_ok or token != TOKEN:
            return 401, {}, json.dumps({"message": "Bad credentials"}).encode()
        path = url[len(cb.API):]
        base = f"/repos/{self.repo}"

        def ok(obj, status=200):
            return status, {}, json.dumps(obj).encode()
        if path == "/user":
            return ok({"login": "augusta"})
        if not path.startswith(base):
            return 404, {}, b'{"message":"Not Found"}'
        path = path[len(base):]
        if path == "":
            return ok({"full_name": self.repo, "permissions": {"push": True}})
        if path == "/branches/homevoice":
            return ok({"name": "homevoice"})
        if path.startswith("/contents/"):
            text = (self.workflow if "workflows" in path else self.proof)
            return ok({"content": base64.b64encode(text.encode()).decode()})
        if path.endswith("/dispatches"):
            self.dispatched.append(body)
            if self.run_id_in_answer:
                return ok({"workflow_run_id": 77})
            return 204, {}, b""
        if "/runs?" in path and "status=completed" in path:
            branch = path.split("branch=")[1].split("&")[0]
            return ok({"workflow_runs": [self._run(branch, "completed")]})
        if "/runs?" in path:
            request = self.dispatched[-1]["inputs"]["request"]
            return ok({"workflow_runs": [
                {"id": 77, "display_title": f"HomeVoice build {request}"}]})
        if path == "/actions/runs/77":
            self.polls += 1
            status = "completed" if self.polls >= self.steps else "in_progress"
            return ok(self._run("homevoice", status))
        if path.startswith("/actions/runs/77/jobs"):
            done = self.polls >= self.steps
            return ok({"jobs": [
                {"name": f"build ({env})", "status": "completed" if done or i == 0 else "in_progress",
                 "conclusion": ("success" if env in self.artifacts else "failure")
                 if done or i == 0 else None}
                for i, env in enumerate(self._envs())]})
        if path.startswith("/actions/runs/77/artifacts"):
            return ok({"artifacts": [{"id": i, "name": name}
                                     for i, name in enumerate(self.artifacts)]})
        if path.startswith("/actions/artifacts/") and path.endswith("/zip"):
            index = int(path.split("/")[3])
            name = list(self.artifacts)[index]
            return 302, {"Location": f"https://storage.example/{name}"}, b""
        return 404, {}, b'{"message":"Not Found"}'

    def _envs(self):
        if self.dispatched:
            return json.loads(self.dispatched[-1]["inputs"]["envs"])
        return list(self.artifacts)

    def _run(self, branch, status):
        return {"id": 77, "status": status,
                "conclusion": "success" if status == "completed" else None,
                "head_branch": branch, "created_at": "2026-09-24T10:00:00Z",
                "html_url": f"https://github.com/{self.repo}/actions/runs/77"}


# ── a fake phone ───────────────────────────────────────────────────────

class Phone:
    can_send_files = True

    def __init__(self, answers):
        self.answers = list(answers)
        self.lines, self.questions, self.secrets = [], [], []

    async def write(self, text, level="info"):
        self.lines.append(text)

    async def choose(self, question, rows):
        self.questions.append((question, rows))
        answer = self.answers.pop(0)
        return answer(rows) if callable(answer) else answer

    async def ainput(self, question, patient=False, secret=False):
        answer = self.answers.pop(0)
        if secret:
            self.secrets.append(answer)
        return answer

class Shell:
    failure_note = None
    checkpoint_note = None


first = lambda rows: [rows[0]["value"]]                  # noqa: E731


@contextlib.contextmanager
def hub(records=({"slug": "old"},), github=None, can_build=False):
    """A throwaway hub: its own boards, firmware folder and secrets file;
    GitHub replaced by `github`."""
    saved_bf = {k: getattr(bf, k) for k in ("HERE", "preflight")}
    saved_cfg = {k: getattr(config, k) for k in (
        "BOARDS_DIR", "FIRMWARE_DIR", "TASMOTA_FIRMWARE_PATH",
        "TASMOTA_FAMILY_FIRMWARE_DIR", "SECRETS_FILE")}
    saved = (EB._cloud_sleep, cb._request,
             os.environ.pop(cb.TOKEN_KEY, None), os.environ.pop(cb.REPO_KEY, None))
    root = Path(tempfile.mkdtemp())
    try:
        (root / "boards").mkdir()
        config.BOARDS_DIR = str(root / "boards")
        for record in records:
            (root / "boards" / f"{record['slug']}.json").write_text(json.dumps(record))
        here = root / "esp_firmware"
        here.mkdir()
        shutil.copy(OVERRIDE, here)
        bf.HERE = here
        bf.preflight = lambda source="": [
            ("64-bit system", True, "x"), ("Memory", can_build, "2 GB"),
            ("Free disk", True, "x"), ("PlatformIO", can_build, "x"),
            ("Tasmota source", True, "x")]
        fw = root / "firmware"
        config.FIRMWARE_DIR = config.TASMOTA_FAMILY_FIRMWARE_DIR = str(fw)
        config.TASMOTA_FIRMWARE_PATH = str(fw / "tasmota32.factory.bin")
        config.SECRETS_FILE = str(root / "env")
        (root / "env").write_text("HOMEVOICE_API_KEY=keep-me\n")

        async def no_wait(_seconds):
            return None
        EB._cloud_sleep = no_wait
        if github is not None:
            cb._request = github
        yield root
    finally:
        for k, v in saved_bf.items():
            setattr(bf, k, v)
        for k, v in saved_cfg.items():
            setattr(config, k, v)
        EB._cloud_sleep, cb._request = saved[:2]
        for key, value in ((cb.TOKEN_KEY, saved[2]), (cb.REPO_KEY, saved[3])):
            os.environ.pop(key, None)
            if value is not None:
                os.environ[key] = value


def run_flow(method, answers, *args):
    builder = EB.EspBuilder.headless()
    shell = Shell()
    builder._shell = shell
    phone = Phone(answers)
    shell._io = phone
    out = asyncio.run(getattr(builder, method)(*args))
    return out, shell, phone


# ── the checks ─────────────────────────────────────────────────────────

def settings_and_shapes():
    print("\nsettings: the fork's name, the token's shape, the secrets file")
    check("owner/name, the page and the clone address all read as owner/name",
          [cb.normal_repo(x) for x in ("augusta/Tasmota",
                                       "https://github.com/augusta/Tasmota",
                                       "github.com/augusta/Tasmota.git/",
                                       "git@github.com:augusta/Tasmota.git")]
          == ["augusta/Tasmota"] * 4)
    check("...and nonsense does not",
          [cb.normal_repo(x) for x in ("Tasmota", "", "a b/c", "-x/y")] == [""] * 4)
    check("token shapes: classic, fine-grained, 40-hex; not a sentence",
          cb.looks_like_token(TOKEN) and cb.looks_like_token("github_pat_" + "A1_" * 20)
          and cb.looks_like_token("0" * 40) and not cb.looks_like_token("my password"))
    with hub() as root:
        cb.save_token(TOKEN)
        cb.save_repo("augusta/Tasmota")
        env = (root / "env").read_text()
        mode = stat.S_IMODE(os.stat(root / "env").st_mode)
        check("token and fork land in the secrets file, the rest of it kept",
              f"{cb.TOKEN_KEY}={TOKEN}" in env and "HOMEVOICE_API_KEY=keep-me" in env
              and f"{cb.REPO_KEY}=augusta/Tasmota" in env and mode == 0o600, (env, oct(mode)))
        check("...and the running hub sees them at once",
              cb.settings() == (TOKEN, "augusta/Tasmota"))
        cb.save_token(None)
        env = (root / "env").read_text()
        check("removing the token removes its line and nothing else",
              cb.TOKEN_KEY not in env and "keep-me" in env and cb.settings()[0] == "")
        try:
            config.set_secret("PATH", "x")
            refused = False
        except ValueError:
            refused = True
        check("only HOMEVOICE_ keys can be set that way", refused)


def github_checks():
    print("\nchecking the fork before anything is sent")
    fake = FakeGitHub()
    saved = cb._request
    cb._request = fake
    try:
        got = cb.GitHub(TOKEN, "augusta/Tasmota").check()
        check("token, fork, branch, workflow and proof all pass",
              [(w, ok) for w, ok, _ in got] == [
                  ("token", True), ("fork", True), ("branch", True),
                  ("workflow", True), ("proof", True)], got)
        cb._request = FakeGitHub(workflow="on: workflow_dispatch\n# old")
        got = cb.GitHub(TOKEN, "augusta/Tasmota").check()
        check("an old workflow (no family list) is named, with the fix",
              got[-1][0] == "workflow" and not got[-1][1]
              and "ci/homevoice-build.yml" in got[-1][2], got[-1])
        cb._request = FakeGitHub(token_ok=False)
        got = cb.GitHub(TOKEN, "augusta/Tasmota").check()
        check("a refused token stops at the first line, in words",
              len(got) == 1 and not got[0][1] and "refused the token" in got[0][2], got)
        leak = cb.GitHub(TOKEN, "a/b").explain(
            403, json.dumps({"message": f"no {TOKEN}"}).encode(), "/x")
        check("the token is never repeated in a message", TOKEN not in leak, leak)
    finally:
        cb._request = saved


def the_cloud_build():
    print("\nSettings -> GitHub builds -> Build the standard firmware (fake GitHub)")
    artifacts = {b.pio_env: artifact(b) for b in bf.ALL_TARGETS}
    fake = FakeGitHub(artifacts=artifacts)
    with hub(github=fake) as root:
        cb.save_token(TOKEN)
        cb.save_repo("augusta/Tasmota")
        out, shell, phone = run_flow("menu_github_builds", [["standard"], ["done"]])
        fw = root / "firmware"
        check("built on GitHub, every family installed side by side",
              sorted(p.name for p in fw.iterdir()) == sorted(
                  n for b in bf.ALL_TARGETS for n in (b.firmware, b.ota_name)),
              (shell.failure_note, phone.lines[-8:]))
        check("the workflow was asked for every family, on homevoice",
              fake.dispatched[0]["ref"] == "homevoice"
              and json.loads(fake.dispatched[0]["inputs"]["envs"])
              == [b.pio_env for b in bf.ALL_TARGETS], fake.dispatched)
        storage = [t for m, u, t in fake.calls if u.startswith("https://storage.example/")]
        check("the download's storage address never saw the token",
              storage and all(t == "" for t in storage), storage)
        check("progress was said while it ran, with the run's address",
              any("actions/runs/77" in ln for ln in phone.lines)
              and any(ln.strip().startswith("GitHub:") for ln in phone.lines),
              [ln for ln in phone.lines if "GitHub" in ln])

    print("\n  a family whose images are for the wrong chip, one GitHub could "
          "not build, and the run id found by name")
    arts = {b.pio_env: artifact(b) for b in bf.ALL_TARGETS if b is not C3}
    arts["tasmota32s3"] = artifact(chips.FAMILIES["esp32s3"], chip=0)
    fake = FakeGitHub(artifacts=arts, run_id_in_answer=False)
    with hub(github=fake) as root:
        cb.save_token(TOKEN)
        cb.save_repo("augusta/Tasmota")
        out, shell, phone = run_flow("menu_github_builds", [["standard"], ["done"]])
        fw = root / "firmware"
        check("the run is found by its request id (GitHub gave none)",
              any("/runs?" in u and "event=workflow_dispatch" in u
                  for _, u, _ in fake.calls))
        check("the classic installs; the S3 (a classic image inside) does not",
              (fw / "tasmota32.factory.bin").is_file()
              and not (fw / "tasmota32s3.factory.bin").exists()
              and any("NOT for" in ln and "ESP32-S3" in ln for ln in phone.lines),
              phone.lines[-6:])
        check("...and the C3's failure is said, with the run's page",
              any("ESP32-C3: GitHub made no images (its job: failure)" in ln
                  and "actions/runs/77" in ln for ln in phone.lines),
              phone.lines[-6:])

    print("\n  no internet on the hub")
    with hub(github=FakeGitHub(online=False)) as root:
        cb.save_token(TOKEN)
        cb.save_repo("augusta/Tasmota")
        out, shell, phone = run_flow("menu_github_builds", [["standard"], ["done"]])
        check("said in a sentence; nothing built",
              "cannot reach GitHub" in " ".join(phone.lines + [shell.failure_note or ""])
              and not (root / "firmware").exists(),
              (shell.failure_note, phone.lines[-4:]))

    print("\n  a mistyped token")
    with hub(github=FakeGitHub(token_ok=False)) as root:
        out, shell, phone = run_flow("menu_github_builds", [
            ["setup"], "augusta/Tasmota", "ghp_" + "b" * 36, ["done"]])
        env = (root / "env").read_text()
        check("refused, and the bad token is NOT kept",
              "refused the token" in " ".join(phone.lines + [shell.failure_note or ""])
              and cb.TOKEN_KEY not in env, (shell.failure_note, env))


def settings_menu():
    print("\nSettings -> GitHub builds")
    artifacts = {b.pio_env: artifact(b) for b in (CLASSIC, C3)}
    with hub(github=FakeGitHub(artifacts=artifacts)) as root:
        out, shell, phone = run_flow("menu_github_builds", [
            ["setup"], "augusta/Tasmota", TOKEN, ["done"]])
        check("Set it up: the fork, then the token, checked and kept",
              out is True and cb.settings() == (TOKEN, "augusta/Tasmota")
              and any("Ready" in ln for ln in phone.lines), phone.lines[-4:])
        rows = [r["value"] for r in phone.questions[0][1]]
        check("before setup it offers setting up, not building", "setup" in rows
              and "standard" not in rows, rows)
        out, shell, phone = run_flow("menu_github_builds", [
            ["latest"], first, ["done"]])
        fw = root / "firmware"
        check("Download the latest build installs what that run made",
              (fw / "tasmota32.factory.bin").is_file()
              and (fw / "tasmota32c3.bin").is_file()
              and any("Installed for" in ln for ln in phone.lines), phone.lines[-5:])
        rows = [r["value"] for r in phone.questions[0][1]]
        check("once set up: check, build, download, change, remove",
              rows == ["check", "standard", "latest", "token", "fork", "forget", "done"], rows)
        out, shell, phone = run_flow("menu_github_builds", [["forget"], ["done"]])
        check("Remove the token", cb.settings()[0] == ""
              and cb.TOKEN_KEY not in (root / "env").read_text())

    print("\nthe token's expiry date, as GitHub gives it")
    from datetime import datetime, timezone
    fake = FakeGitHub(artifacts=artifacts)
    fake.expires = "2026-11-10 00:00:00 UTC"
    with hub(github=fake) as root:
        out, shell, phone = run_flow("menu_github_builds", [
            ["setup"], "augusta/Tasmota", TOKEN, ["done"]])
        check("the date is read from GitHub's answer and kept -- nobody "
              "types it", cb.token_expiry() == "2026-11-10 00:00:00 UTC"
              and any("expires 10 Nov 2026" in ln for ln in phone.lines),
              phone.lines)
        out, shell, phone = run_flow("menu_github_builds", [["done"]])
        check("GitHub builds says when it expires, next to the token",
              any("Token: saved on this hub -- expires 10 Nov 2026" in ln
                  for ln in phone.lines), phone.lines[:3])
        out, shell, phone = run_flow("menu_github_builds", [["forget"], ["done"]])
        check("removing the token forgets its date", cb.token_expiry() == "")
    fake = FakeGitHub()
    with hub(github=fake):
        run_flow("menu_github_builds", [["setup"], "augusta/Tasmota", TOKEN,
                                        ["done"]])
        check("a token made with no expiry says so", cb.token_expiry() == "never"
              and cb.expiry_words("never") == "never expires")
    now = datetime(2026, 10, 11, tzinfo=timezone.utc)
    check("a week's warning, then expired",
          cb.days_left("2026-10-15 00:00:00 UTC", now) == 4
          and cb.expiry_words("2026-10-12 09:00:00 UTC", now)
          == "expires 12 Oct 2026 (tomorrow)"
          and cb.expiry_words("2026-10-01 00:00:00 UTC", now)
          == "EXPIRED on 1 Oct 2026"
          and cb.days_left("never", now) is None)
    from app.shell import InteractiveShell
    shell = InteractiveShell.__new__(InteractiveShell)
    import os
    os.environ[cb.TOKEN_KEY] = TOKEN
    try:
        os.environ[cb.EXPIRY_KEY] = "2026-11-10 00:00:00 UTC"
        far = shell._github_token_notice(now)
        os.environ[cb.EXPIRY_KEY] = "2026-10-15 00:00:00 UTC"
        soon = shell._github_token_notice(now)
        os.environ[cb.EXPIRY_KEY] = "2026-10-01 00:00:00 UTC"
        gone = shell._github_token_notice(now)
    finally:
        os.environ.pop(cb.TOKEN_KEY, None)
        os.environ.pop(cb.EXPIRY_KEY, None)
    check("no notice while a week or more is left", far is None)
    check("a week before: 'expires ... (in 4 days)', Renew the token",
          soon and soon["title"] == "Your GitHub token expires 15 Oct 2026 "
          "(in 4 days)" and soon["action_value"] == "github_builds"
          and not soon["now"], soon)
    check("after: 'has expired', asked about at once",
          gone and gone["title"] == "Your GitHub token has expired"
          and "expired on 1 Oct 2026" in gone["body"] and gone["now"], gone)


def cancel_is_cancel():
    print("\ncancelling the fork question cancels")
    with hub(github=FakeGitHub()):
        try:
            run_flow("menu_github_builds", [["setup"], "cancel"])
            cancelled = False
        except CancelledByUser:
            cancelled = True
        check("'cancel' at the fork question ends the flow", cancelled)


if __name__ == "__main__":
    for section in (settings_and_shapes, github_checks, the_cloud_build, settings_menu, cancel_is_cancel):
        section()
    print(f"\n  {PASSED} passed, {FAILED} failed")
    sys.exit(1 if FAILED else 0)

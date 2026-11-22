"""Building HomeVoice's standard firmware on GitHub, driven by the hub.

The other road to the firmware (the first is build_firmware.py on this
machine). The hub starts the owner's Tasmota fork's HomeVoice workflow
(ci/homevoice-build.yml) on its `homevoice` branch, waits, downloads the
proven images and installs them into esp_firmware/firmware/ -- the same
place and the same checks as every other way firmware arrives. Nothing
is pushed: the fork's branch is built as it is.

WHO TALKS TO GITHUB: the hub, over its own internet. The phone only has to
reach the hub (house Wi-Fi, or Tailscale from anywhere); a hub with no
internet is told so in a sentence.

THE TOKEN. A fine-grained personal access token limited to the fork, with
Contents and Actions read & write (or a classic token with `repo` and
`workflow`). It lives in the hub's secrets file (config.set_secret,
owner-only), never in a log, never echoed back to any phone. The fork's
name is kept beside it -- not a secret, but one file for one setting.

Every HTTP call goes through the module-level `_request`, so the tests
fake GitHub instead of needing it. Standard library only.
"""

from __future__ import annotations

import base64
import io
import json
import re
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

API = "https://api.github.com"
WORKFLOW = "homevoice-build.yml"
BASE_BRANCH = "homevoice"
TOKEN_KEY = "HOMEVOICE_GITHUB_TOKEN"
#: When the token stops working, AS GITHUB SAYS: every API answer to a
#: token that expires carries it in this header, so nobody types the date.
#: "never" for a token made without one; absent until GitHub has been asked.
EXPIRY_KEY = "HOMEVOICE_GITHUB_TOKEN_EXPIRES"
_EXPIRY_HEADER = "github-authentication-token-expiration"
REPO_KEY = "HOMEVOICE_GITHUB_REPO"

#: One image. A factory image is ~2-4 MB; anything this size is not one.
MAX_IMAGE_BYTES = 16 * 2**20

#: How to make the token, said wherever one is asked for.
TOKEN_HELP = (
    "On github.com: Settings -> Developer settings -> Personal access "
    "tokens -> Fine-grained tokens -> Generate new token. Repository "
    "access: only your Tasmota fork. Permissions: Contents = Read and "
    "write, Actions = Read and write. Copy it when GitHub shows it -- it "
    "is shown once.")


class CloudError(Exception):
    """A sentence for a person: what GitHub said, and what to do."""


# ── the one door to the network ────────────────────────────────────────

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Artifact downloads answer 302 to a storage URL that must NOT get the
    token (it has its own signature, and a stray Authorization header is
    refused there). So redirects come back to the caller, who follows them
    bare."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _request(method: str, url: str, token: str = "",
             body: Any = None, accept: str = "application/vnd.github+json"
             ) -> Tuple[int, Dict[str, str], bytes]:
    """(status, headers, body). Raises CloudError only when GitHub cannot
    be reached at all; every HTTP answer, error or not, is returned."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", accept)
    req.add_header("User-Agent", "HomeVoice-hub")
    if url.startswith(API):
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=120) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read()
        except OSError:
            raw = b""
        return exc.code, dict(exc.headers or {}), raw
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise CloudError(
            f"This hub cannot reach GitHub ({reason}). It needs its own "
            f"internet for this -- through the router, or a cable. The "
            f"driver is saved; nothing was sent.") from None


# ── settings ───────────────────────────────────────────────────────────

def settings() -> Tuple[str, str]:
    """(token, repo) as the hub holds them now; "" for either missing."""
    import os
    return (os.environ.get(TOKEN_KEY, "").strip(),
            os.environ.get(REPO_KEY, "").strip())


def normal_repo(text: str) -> str:
    """"owner/name" out of what a person pastes: owner/name, the page URL,
    or the clone URL. "" when it is none of those."""
    text = (text or "").strip()
    m = re.match(r"^(?:https?://|git@)?(?:www\.)?github\.com[/:]", text)
    if m:
        text = text[m.end():]
    text = re.sub(r"\.git$", "", text.strip("/"))
    parts = text.split("/")
    if len(parts) < 2:
        return ""
    owner, name = parts[0], parts[1]
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})", owner):
        return ""
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", name) or name in (".", ".."):
        return ""
    return f"{owner}/{name}"


def looks_like_token(text: str) -> bool:
    """A GitHub token's shape (ghp_/github_pat_/gho_... or a 40-hex classic).
    Only to catch a paste of the wrong thing; GitHub has the last word."""
    text = (text or "").strip()
    return bool(re.fullmatch(r"(gh[pousr]_[A-Za-z0-9]{20,255}"
                             r"|github_pat_[A-Za-z0-9_]{20,255}"
                             r"|[0-9a-f]{40})", text))


def save_token(token: Optional[str]) -> None:
    from core import config
    config.set_secret(TOKEN_KEY, token.strip() if token else None)


def save_repo(repo: Optional[str]) -> None:
    from core import config
    config.set_secret(REPO_KEY, repo or None)


def token_expiry() -> str:
    """GitHub's own words for when the saved token expires ("2026-11-10
    00:00:00 UTC"), "never", or "" when GitHub has not been asked yet."""
    import os
    return os.environ.get(EXPIRY_KEY, "").strip()


def save_expiry(text: Optional[str]) -> None:
    from core import config
    config.set_secret(EXPIRY_KEY, text or None)


def _expiry_moment(text: str):
    from datetime import datetime, timezone
    try:
        return datetime.strptime(text.strip()[:19], "%Y-%m-%d %H:%M:%S") \
            .replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def days_left(text: str, now=None) -> Optional[int]:
    """Whole days until the token expires (negative once it has); None when
    it never does or the date is not known."""
    from datetime import datetime, timezone
    moment = _expiry_moment(text)
    if moment is None:
        return None
    now = now or datetime.now(timezone.utc)
    return int((moment - now).total_seconds() // 86400)


def expiry_words(text: str, now=None) -> str:
    """"expires 10 Nov 2026 (in 29 days)", "never expires", "EXPIRED on
    10 Nov 2026", or "" when not known."""
    if text == "never":
        return "never expires"
    moment = _expiry_moment(text)
    if moment is None:
        return ""
    date = f"{moment.day} {moment.strftime('%b %Y')}"
    left = days_left(text, now)
    if left is not None and left < 0:
        return f"EXPIRED on {date}"
    soon = ("today" if left == 0 else "tomorrow" if left == 1
            else f"in {left} days")
    return f"expires {date} ({soon})"


# ── the API, as sentences ──────────────────────────────────────────────

class GitHub:
    """One fork, one token."""

    def __init__(self, token: str, repo: str):
        self.token, self.repo = token, repo
        #: GitHub's expiry header from the last answer that carried one.
        self.expires: Optional[str] = None

    @property
    def base(self) -> str:
        return f"{API}/repos/{self.repo}"

    def call(self, method: str, path: str, body: Any = None,
             ok: Sequence[int] = (200, 201, 204)) -> Any:
        url = path if path.startswith("http") else (
            API + path if path.startswith("/user") else self.base + path)
        status, headers, raw = _request(method, url, self.token, body)
        said = {str(k).lower(): v for k, v in (headers or {}).items()}
        if said.get(_EXPIRY_HEADER):
            self.expires = str(said[_EXPIRY_HEADER]).strip()
        if status in ok:
            if not raw:
                return {}
            try:
                return json.loads(raw)
            except ValueError:
                return {}
        raise CloudError(self.explain(status, raw, path))

    def explain(self, status: int, raw: bytes, path: str) -> str:
        try:
            said = str(json.loads(raw).get("message", "")).strip()
        except (ValueError, AttributeError):
            said = ""
        said = said.replace(self.token, "***") if self.token else said
        if status == 401:
            return ("GitHub refused the token -- it is mistyped, expired or "
                    "was deleted. Set a new one: Settings -> GitHub builds.")
        if status == 403:
            return (f"GitHub says the token may not do this on {self.repo}"
                    f"{f' ({said})' if said else ''}. It needs Contents and "
                    f"Actions, both Read and write, on that repository.")
        if status == 404:
            return (f"GitHub found nothing for {path or '/'} on {self.repo}. "
                    f"Check the fork's name -- and note a token that is not "
                    f"allowed to see a repository gets the same answer.")
        return (f"GitHub answered {status}"
                f"{f': {said}' if said else ''} ({path}).")

    # ── checks ──
    def check(self) -> List[Tuple[str, bool, str]]:
        """(what, ok, detail) for everything a build needs, in order, the
        first failure stopping the rest (each later check needs the one
        before). For Settings -> GitHub builds -> Check, and first use."""
        out: List[Tuple[str, bool, str]] = []
        try:
            who = self.call("GET", "/user")
            if self.expires is None:
                self.expires = "never"      # no header: made without a date
            when = expiry_words(self.expires)
            out.append(("token", True, f"signed in as {who.get('login', '?')}"
                        + (f"; it {when}" if when else "")))
        except CloudError as exc:
            out.append(("token", False, str(exc)))
            return out
        try:
            info = self.call("GET", "")
            perms = info.get("permissions") or {}
            if perms and not perms.get("push", False):
                out.append(("fork", False, f"{self.repo}: this account cannot "
                            f"push there -- use YOUR fork."))
                return out
            out.append(("fork", True, info.get("full_name", self.repo)))
        except CloudError as exc:
            out.append(("fork", False, str(exc)))
            return out
        try:
            self.call("GET", f"/branches/{BASE_BRANCH}")
            out.append(("branch", True, BASE_BRANCH))
        except CloudError:
            out.append(("branch", False, f"{self.repo} has no '{BASE_BRANCH}' "
                        f"branch -- the one holding tasmota/"
                        f"user_config_override.h and the workflow."))
            return out
        for label, path in (("workflow", f".github/workflows/{WORKFLOW}"),
                            ("proof", ".github/homevoice/prove.py")):
            try:
                got = self.call("GET", f"/contents/{path}?ref={BASE_BRANCH}")
            except CloudError:
                out.append((label, False, f"{path} is missing on "
                            f"'{BASE_BRANCH}' -- copy it from the hub's ci/ "
                            f"folder (ci/README.md says where)."))
                return out
            if label == "workflow":
                text = _content_text(got)
                if "envs" not in text or "fromJSON" not in text:
                    out.append((label, False, f"{path} is an older version "
                                f"that cannot be told which families to "
                                f"build -- replace it with the hub's "
                                f"ci/homevoice-build.yml."))
                    return out
            out.append((label, True, path))
        return out

    # ── runs ──
    def dispatch(self, branch: str, envs: Sequence[str], request: str) -> Optional[int]:
        """Start the workflow on `branch` for `envs`. The run's id when
        GitHub says it (it does when asked, since 2025), else None and
        find_run() looks for the run by its request id."""
        body = {"ref": branch,
                "inputs": {"envs": json.dumps(list(envs)), "request": request},
                "return_run_details": True}
        status, _h, raw = _request(
            "POST", f"{self.base}/actions/workflows/{WORKFLOW}/dispatches",
            self.token, body)
        if status == 422 and b"return_run_details" in raw:
            del body["return_run_details"]
            status, _h, raw = _request(
                "POST", f"{self.base}/actions/workflows/{WORKFLOW}/dispatches",
                self.token, body)
        if status not in (200, 201, 204):
            raise CloudError(self.explain(status, raw, "/actions (start the build)"))
        try:
            got = json.loads(raw) if raw else {}
        except ValueError:
            got = {}
        run_id = got.get("workflow_run_id") or got.get("id")
        return int(run_id) if run_id else None

    def find_run(self, branch: str, request: str) -> Optional[Dict[str, Any]]:
        """The run whose name carries `request` (the workflow's run-name)."""
        got = self.call(
            "GET", f"/actions/workflows/{WORKFLOW}/runs?branch={branch}"
                   f"&event=workflow_dispatch&per_page=20")
        for run in got.get("workflow_runs", []):
            title = f"{run.get('display_title', '')} {run.get('name', '')}"
            if request and request in title:
                return run
        return None

    def latest_run(self, branch: str) -> Optional[Dict[str, Any]]:
        """The newest SUCCESSFUL-or-partly-successful finished run on
        `branch` -- one with artifacts to fetch."""
        got = self.call("GET", f"/actions/workflows/{WORKFLOW}/runs"
                               f"?branch={branch}&status=completed&per_page=10")
        for run in got.get("workflow_runs", []):
            if run.get("conclusion") in ("success", "failure"):
                return run
        return None

    def run(self, run_id: int) -> Dict[str, Any]:
        return self.call("GET", f"/actions/runs/{run_id}")

    def jobs(self, run_id: int) -> List[Dict[str, Any]]:
        return self.call("GET", f"/actions/runs/{run_id}/jobs?per_page=50").get("jobs", [])

    def artifacts(self, run_id: int) -> List[Dict[str, Any]]:
        return self.call("GET", f"/actions/runs/{run_id}/artifacts?per_page=100"
                         ).get("artifacts", [])

    def download(self, artifact_id: int) -> bytes:
        status, headers, raw = _request(
            "GET", f"{self.base}/actions/artifacts/{artifact_id}/zip", self.token)
        if status in (301, 302, 303, 307, 308):
            where = headers.get("Location") or headers.get("location") or ""
            if not where:
                raise CloudError("GitHub sent the artifact elsewhere without "
                                 "saying where.")
            status, headers, raw = _request("GET", where, "", accept="*/*")
        if status != 200:
            raise CloudError(self.explain(status, raw, "/actions/artifacts (download)"))
        return raw


def _content_text(got: Any) -> str:
    try:
        return base64.b64decode(got.get("content", "")).decode("utf-8", "replace")
    except (ValueError, AttributeError):
        return ""


def unpack(blob: bytes, board) -> Dict[str, bytes]:
    """{"factory": ..., "app": ...} out of one family's artifact zip --
    exactly <env>.factory.bin and <env>.bin, nothing else trusted."""
    wanted = {board.firmware: "factory", board.ota_name: "app"}
    out: Dict[str, bytes] = {}
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            for info in archive.infolist():
                name = Path(info.filename).name
                kind = wanted.get(name)
                if kind is None or info.is_dir():
                    continue
                if info.file_size > MAX_IMAGE_BYTES:
                    raise CloudError(f"{name} in the download is "
                                     f"{info.file_size // 2**20} MB -- not "
                                     f"a firmware image.")
                out[kind] = archive.read(info)
    except zipfile.BadZipFile:
        raise CloudError(f"The {board.pio_env} download is not a zip.") from None
    missing = [n for n, k in wanted.items() if k not in out]
    if missing:
        raise CloudError(f"The {board.pio_env} download has no "
                         + " or ".join(missing) + ".")
    return out


def request_id() -> str:
    """A short id the run's name carries, so this hub finds ITS run."""
    import secrets
    return "hv-" + secrets.token_hex(4)


def job_family(job: Dict[str, Any], envs: Iterable[str]) -> str:
    """The env a matrix job built ("build (tasmota32c3)" / "tasmota32c3")."""
    name = str(job.get("name", ""))
    for env in sorted(envs, key=len, reverse=True):
        if re.search(rf"(?<![a-z0-9]){re.escape(env)}(?![a-z0-9])", name):
            return env
    return ""


def wait_seconds(started: float, now: Optional[float] = None) -> float:
    """How long to sleep between looks: 20 s at first, then 45 s."""
    now = time.time() if now is None else now
    return 20.0 if now - started < 300 else 45.0

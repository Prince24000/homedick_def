#!/usr/bin/env python3
"""
ha_client.py — talks to Home Assistant's REST API. Nothing more than that:
fetch the live entity list, call a service. device_manager.py owns the
decision of what to DO with that data (matching against our own semantic
sidecar, resolving a command); this module only speaks HA's wire protocol.

Deliberately synchronous (plain `requests`), not the WebSocket API HA's own
docs now recommend for new integrations. The REST API is explicitly stated
by HA's own maintainers as not deprecated, just not getting new features --
for this project's actual usage pattern (occasional fetch-and-call, not a
persistent real-time subscription), REST is simpler and sufficient. Revisit
if a future need calls for push-based updates instead of polling.

Every method fails loud (prints exactly what went wrong) but returns a safe
empty/None value rather than raising -- HA being unreachable should degrade
the caller's behavior, not crash the whole voice assistant. Per the earlier
design discussion: HA is local, genuinely rare to be down, but "rare" isn't
"never" (container restart timing, etc.), so this is treated as a real,
expected failure mode, not an afterthought.

WHAT IS NOT IN HERE, ON PURPOSE: driving a config flow. That is a
conversation -- asking, waiting, explaining, and coping with somebody
backing out -- and it lives in ha_flows.py. If it lived here, this file
would have to import a cancellation exception, know what a form is, and know
that a person might change their mind. It would stop being a client.
"""

from typing import Optional, List, Dict, Any
from urllib.parse import quote

import json
import re
import requests

from core import config

# From config, so a slow HA can't stall a voice command for an unpredictable
# length of time depending on which method happened to be called.
TIMEOUT_SECONDS = config.HA_TIMEOUT_SECONDS
LIVENESS_TIMEOUT_SECONDS = config.HA_LIVENESS_TIMEOUT_SECONDS

# Shapes of the identifiers interpolated into URL paths. Quoting alone keeps
# a "/" from escaping its segment, but "." and ".." survive quoting and are
# collapsed by urllib3's dot-segment removal -- /api/states/.. is /api/.
# HA itself lower-cases entity ids on lookup, so case is not enforced.
_ENTITY_ID_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$", re.IGNORECASE)
_SLUG_RE = re.compile(r"^[a-z0-9_]+$", re.IGNORECASE)
_OPAQUE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

#: websockets refuses frames above 1 MiB by default; the entity registry of a
#: large house and manifest/list for every integration can exceed that.
_WS_MAX_MESSAGE_BYTES = 16 * 1024 * 1024


def _valid(rx: "re.Pattern[str]", value: Any) -> bool:
    return isinstance(value, str) and bool(rx.match(value))


class HAClient:
    def __init__(self, base_url: Optional[str] = None, token: Optional[str] = None):
        self.base_url = (base_url or config.HA_BASE_URL).rstrip("/")
        self.token = token or config.require_ha_token()
        # A Session, not bare module-level calls. Every request used to open a
        # fresh TCP (and, over https, TLS) connection; a single voice command
        # can make several. Keep-alive turns that into one.
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        })
        #: Did the LAST is_reachable() get a 401? Declared here so a caller
        #: may read it without first having probed. See is_reachable.
        self.token_rejected = False

    def close(self) -> None:
        """Release the pooled connection. Optional -- the process exiting does
        the same thing -- but explicit when a client is discarded and
        recreated, as _try_connect_ha does on a reconnect.

        AND IT IS ACTUALLY CALLED NOW (2026-09-14). This method's own
        sentence named `_try_connect_ha` as its caller and that call did
        not exist anywhere in the tree -- `_try_connect_ha` built a client
        (which opens a Session in __init__), found it unreachable, and
        simply returned None, abandoning the pool. With HA stopped,
        `reconnect_ha_if_needed` did that once a minute for as long as HA
        stayed down. The method written to prevent exactly that was dead
        code whose docstring asserted a caller it never had.
        """
        try:
            self._session.close()
        except Exception:                          # noqa: BLE001
            pass          # releasing a pool must never be the thing that raises

    @staticmethod
    def _entity_path(entity_id: str) -> str:
        """URL-safe entity id.

        An id is interpolated straight into a path, and ids reach us from a
        person typing during registration as well as from HA itself. Without
        quoting, a stray slash silently addresses a DIFFERENT endpoint rather
        than failing."""
        return quote(str(entity_id), safe="")

    def _request(self, method: str, url: str, *, what: str,
                 timeout: float = TIMEOUT_SECONDS,
                 **kwargs) -> "tuple[bool, Any]":
        """One HTTP round trip: (answered 2xx, parsed JSON body or None).
        Never raises. The ONE copy of the status handling every REST call
        here shares -- call_service and call_service_with_response each
        used to carry their own, and they had drifted apart.

        Catches ValueError alongside RequestException. This module's docstring
        promises every method "returns a safe empty/None value rather than
        raising" -- but a 200 response carrying malformed JSON raises out of
        .json(), and a JSON decode error is not a RequestException on every
        version of requests. The one promise the module makes was the one
        thing it didn't cover. A 2xx whose body is not JSON still counts as
        answered: the request DID succeed, only its body is unusable."""
        try:
            resp = self._session.request(method, url, timeout=timeout, **kwargs)
            if resp.status_code == 401:
                # A wrong or expired token is a configuration fault, not a
                # transient one, and it is worth saying so plainly instead of
                # letting it read as "HA is down".
                print(f"[HA] Unauthorised ({what}) -- check HOMEVOICE_HA_TOKEN.")
                return False, None
            if resp.status_code == 404:
                print(f"[HA] Not found: {what}")
                return False, None
            if resp.status_code == 400:
                print(f"[HA] {what} was refused: {resp.text[:200]}")
                return False, None
            resp.raise_for_status()
        except requests.exceptions.RequestException as e:
            print(f"[HA] {what} failed: {e}")
            return False, None
        if not resp.content:
            return True, None
        try:
            return True, resp.json()
        except ValueError as e:
            print(f"[HA] {what} returned a body that isn't JSON: {e}")
            return True, None

    def _request_json(self, method: str, url: str, *, what: str,
                      **kwargs) -> Any:
        """The parsed JSON body, or None on ANY failure. See _request."""
        return self._request(method, url, what=what, **kwargs)[1]

    def _flow_url(self, flow_id: str) -> str:
        return (f"{self.base_url}/api/config/config_entries/flow/"
                f"{quote(flow_id, safe='')}")

    def reachable_base_url(self, fallback_hosts: Optional[List[str]] = None
                           ) -> str:
        """An address for HA that a PHONE can use, as opposed to the one
        THIS process uses. base_url is normally http://localhost:8123 --
        HA lives on the hub -- and a phone handed that would connect to
        its own loopback (camera review, 2026-09-03). HA's own idea comes
        first (/api/config internal_url, then external_url); failing that
        a loopback host is swapped for the first of `fallback_hosts` (the
        hub's own bindings), keeping the port. Never raises; worst case
        returns base_url unchanged."""
        from urllib.parse import urlsplit, urlunsplit
        cfg = self._request_json("GET", f"{self.base_url}/api/config",
                                 what="HA config")
        if isinstance(cfg, dict):
            for key in ("internal_url", "external_url"):
                url = cfg.get(key)
                if isinstance(url, str) and url.startswith(("http://", "https://")):
                    host = urlsplit(url).hostname or ""
                    if host and host not in ("localhost", "127.0.0.1", "::1"):
                        return url.rstrip("/")
        parts = urlsplit(self.base_url)
        host = parts.hostname or ""
        if host in ("localhost", "127.0.0.1", "::1") and fallback_hosts:
            swap = next((h for h in fallback_hosts if h), "")
            if swap:
                if ":" in swap and not swap.startswith("["):
                    swap = f"[{swap}]"          # an IPv6 literal in a netloc
                port = f":{parts.port}" if parts.port else ""
                return urlunsplit((parts.scheme, f"{swap}{port}", parts.path,
                                   "", "")).rstrip("/")
        return self.base_url

    def get_states(self) -> Optional[List[Dict[str, Any]]]:
        """Fetch every entity's current state from HA, live -- no caching.
        Returns None when HA did not answer (or answered nonsense), so a
        poller can tell "nothing changed" from "could not ask"; callers that
        only iterate write `get_states() or []`."""
        data = self._request_json("GET", f"{self.base_url}/api/states",
                                  what="fetching states")
        # Shape-checked, not trusted. The declared return type was a list of
        # dicts and the code returned whatever .json() produced -- so a proxy
        # error page or an API change handed callers something they would
        # iterate into an AttributeError somewhere far away from here.
        if not isinstance(data, list):
            if data is not None:
                print("[HA] /api/states did not return a list -- ignoring the response.")
            return None
        # Every caller indexes e["entity_id"]; an item without a string id
        # would be a KeyError/TypeError far from here.
        return [e for e in data
                if isinstance(e, dict) and isinstance(e.get("entity_id"), str)]

    # day_stats() is gone (owner's decision, 2026-09-02): statistics are kept
    # locally by the automation engine. Home Assistant is used only to add
    # and control integration devices.

    def get_state(self, entity_id: str) -> Optional[Dict[str, Any]]:
        """Fetch a single entity's current state. None on failure or if the
        entity doesn't exist (HA returns 404 for that case)."""
        if not _valid(_ENTITY_ID_RE, entity_id):
            print(f"[HA] Not an entity id: {entity_id!r}")
            return None
        data = self._request_json(
            "GET", f"{self.base_url}/api/states/{self._entity_path(entity_id)}",
            what=f"state for {entity_id}")
        if data is None:
            return None
        if not isinstance(data, dict):
            print(f"[HA] state for {entity_id} was not an object -- ignoring.")
            return None
        return data

    # ── Config entries and flows ─────────────────────────────────────────
    # What Home Assistant's own "Devices & Services" page is built on. These
    # are NOT services -- they are the config API the frontend calls, and they
    # are the only way to set a device up without opening HA's UI.
    #
    # The distinction that matters: an "entry" is an integration already
    # CONFIGURED, a "flow" is one being set up. HA auto-discovers devices over
    # mDNS and parks them as flows awaiting confirmation, which is exactly the
    # "a new ESP32 appeared" case.

    @staticmethod
    def _as_step(body: Any, what: str) -> Optional[Dict[str, Any]]:
        """A config-flow step, or None if the answer was not one.

        Checked rather than trusted. The caller branches on `type` to decide
        whether to render a form or declare success, and a body that is not an
        object would fail on the first .get() with a message about the wrong
        thing entirely.
        """
        if body is not None and not isinstance(body, dict):
            print(f"[HA] {what}: expected a flow step, got "
                  f"{type(body).__name__} -- ignoring.")
            return None
        return body

    def list_config_entries(self) -> List[Dict[str, Any]]:
        """Every configured integration. [] on failure, never None."""
        data = self._request_json("GET",
                                  f"{self.base_url}/api/config/config_entries/entry",
                                  what="listing config entries")
        if not isinstance(data, list):
            if data is not None:
                print("[HA] config_entries did not return a list -- ignoring.")
            return []
        return [e for e in data if isinstance(e, dict)]

    def _ws_dicts(self, command: Dict[str, Any],
                  what: str) -> List[Dict[str, Any]]:
        """The command's result as a list of objects; [] on failure or on
        any other shape -- see _ws_command_ok for the form that can tell a
        null result from a refusal."""
        data = self._ws_command_ok(command, what)[1]
        if not isinstance(data, list):
            return []
        return [d for d in data if isinstance(d, dict)]

    def _ws_command_ok(self, command: Dict[str, Any],
                       what: str) -> "tuple[bool, Optional[Any]]":
        """One WebSocket command, over a connection opened and closed for it.

        THIS EXISTS BECAUSE SOME THINGS ARE NOT IN THE REST API AT ALL. The
        list of discovered-but-unconfigured devices is one: `GET` on
        /api/config/config_entries/flow answers 405, because that path only
        accepts POST to START a flow. The list itself is a WebSocket command,
        config_entries/flow/progress, and there is no REST equivalent.

        A connection per command, rather than a pooled one. This is called
        once when somebody opens "Add device", not in a loop, and a persistent
        socket would mean reconnect logic, a background reader and a lifecycle
        to own -- all of it to save a handshake on an interaction that already
        involves a person reading a screen.

        Synchronous, matching the rest of this class. Callers wrap it in
        asyncio.to_thread exactly as they do every other method here.

        Returns the command's result, or None on any failure. Never raises.
        """
        try:
            from websockets.sync.client import connect
        except ImportError:
            print("[HA] websockets has no sync client -- cannot reach the "
                  "WebSocket API. Upgrade it, or use the by-address route.")
            return False, None

        ws_url = self.base_url.replace("https://", "wss://", 1) \
                              .replace("http://", "ws://", 1) + "/api/websocket"
        try:
            with connect(ws_url, open_timeout=TIMEOUT_SECONDS,
                         close_timeout=2,
                         max_size=_WS_MAX_MESSAGE_BYTES) as ws:
                # HA speaks first: auth_required, then it wants the token, then
                # it answers auth_ok. Only after that will it take commands.
                hello = json.loads(ws.recv(timeout=TIMEOUT_SECONDS))
                if hello.get("type") != "auth_required":
                    print(f"[HA] {what}: expected auth_required, got "
                          f"{hello.get('type')!r}")
                    return False, None
                ws.send(json.dumps({"type": "auth", "access_token": self.token}))
                ack = json.loads(ws.recv(timeout=TIMEOUT_SECONDS))
                if ack.get("type") != "auth_ok":
                    print(f"[HA] {what}: authentication refused "
                          f"({ack.get('message', ack.get('type'))}). Check "
                          f"HOMEVOICE_HA_TOKEN.")
                    return False, None

                ws.send(json.dumps({"id": 1, **command}))
                # HA may interleave other messages; take the one answering us.
                for _ in range(10):
                    msg = json.loads(ws.recv(timeout=TIMEOUT_SECONDS))
                    if msg.get("id") != 1 or msg.get("type") != "result":
                        continue
                    if not msg.get("success"):
                        err = msg.get("error") or {}
                        print(f"[HA] {what}: {err.get('message', 'refused')}")
                        return False, None
                    return True, msg.get("result")
                print(f"[HA] {what}: no answer to the command.")
                return False, None
        except Exception as e:
            print(f"[HA] {what} failed: {type(e).__name__}: {e}")
            return False, None

    def list_pending_flows(self) -> List[Dict[str, Any]]:
        """Set-ups WAITING to be completed -- mostly auto-discovered devices.

        This is the list HA shows as "Discovered" on its integrations page. A
        freshly flashed ESPHome node announces itself over mDNS and lands here
        within seconds, needing only a confirmation and perhaps an encryption
        key.

        WebSocket, not REST. There is no REST endpoint for this -- GET on the
        flow path returns 405 Method Not Allowed, because that path exists
        only to POST a new flow into being.
        """
        return self._ws_dicts({"type": "config_entries/flow/progress"},
                              what="listing discovered devices")

    def entity_registry(self) -> List[Dict[str, Any]]:
        """Every registered entity, and WHICH CONFIG ENTRY OWNS IT.

        The link that /api/states does not carry. A state tells you a light
        exists; only the registry tells you that light came from a particular
        ESPHome node, which is the question removal has to answer before it
        can say whether deleting an entry would take anything else with it.

        WebSocket, like list_pending_flows -- the registry has no REST
        endpoint. ONE call returns the lot, so callers should fetch it once
        and index it rather than asking per entity: every call here is a
        fresh connection and handshake.
        """
        return self._ws_dicts({"type": "config/entity_registry/list"},
                              what="reading the entity registry")

    def device_registry(self) -> List[Dict[str, Any]]:
        """Every device Home Assistant knows, with names and owning entries.

        THIS is where a Tasmota board lives in Home Assistant. The Tasmota
        integration is ONE config entry for every board -- so config-entry
        titles can never identify a single board, and code that matches them
        (as removal used to) silently matches nothing. A board is a DEVICE:
        an entry here, carrying the name the hub provisioned, an id its
        entities point back to, and the config entry that owns it.
        """
        return self._ws_dicts({"type": "config/device_registry/list"},
                              what="reading the device registry")

    def remove_device_from_entry(self, device_id: str,
                                 config_entry_id: str) -> bool:
        """Detach one device from its integration -- HA deletes the device
        and its entities when no entry is left holding it.

        The Tasmota-shaped counterpart to delete_config_entry: deleting the
        Tasmota ENTRY would take every board with it, so removal has to act
        on the one device. Same caveat as delete_config_entry, sharpened: a
        board that is still powered keeps announcing itself over MQTT and
        WILL be rediscovered. Callers should say so.
        """
        if not device_id or not config_entry_id:
            return False
        # THE COMMAND NAME WAS INVENTED. This used to send
        # "remove_config_entry_from_device", which no Home Assistant
        # version has ever known -- every board retirement got "Unknown
        # command" back and printed a shrug (found 2026-08-24, retiring
        # bedroom). The real name is "config/device_registry/remove"
        # since core 2026.8 (device_id only); older cores know it as
        # ".../remove_config_entry" and want the entry id too. Current
        # name first, old name as fallback -- the second attempt is a
        # harmless no-op when the first failed for any other reason.
        # SUCCESS IS THE FLAG, NOT THE BODY: HA answers a removal that
        # deleted the device outright with success=true and result=null,
        # which used to read as failure and print "would not remove it".
        ok, _ = self._ws_command_ok(
            {"type": "config/device_registry/remove",
             "device_id": device_id},
            what="removing a device from Home Assistant")
        if ok:
            return True
        ok, _ = self._ws_command_ok(
            {"type": "config/device_registry/remove_config_entry",
             "device_id": device_id,
             "config_entry_id": config_entry_id},
            what="removing a device from Home Assistant (older API)")
        return ok

    def entity_owners(self) -> Dict[str, str]:
        """entity_id -> config_entry_id, for every registered entity.

        The shape callers actually want. Entities with no owning entry (a
        helper, a template) are omitted rather than mapped to None, so a
        lookup miss means one thing only.
        """
        out: Dict[str, str] = {}
        for entry in self.entity_registry():
            entity_id = entry.get("entity_id")
            owner = entry.get("config_entry_id")
            if isinstance(entity_id, str) and isinstance(owner, str):
                out[entity_id] = owner
        return out

    def delete_config_entry(self, entry_id: str) -> bool:
        """Remove an integration entry AND everything it owns.

        Home Assistant deletes the entry's devices and entities with it, so
        this is the real "stop knowing about this thing" -- there is no
        separate entity delete to follow up with.

        What it does NOT do is stop the hardware. A flashed ESP32 keeps
        running, keeps joining Wi-Fi and keeps announcing itself, so HA will
        rediscover it and it will reappear as a pending flow. Callers should
        say so; a person who deletes something and watches it come back
        assumes the delete failed.
        """
        if not _valid(_OPAQUE_ID_RE, entry_id):
            return False
        return self._request(
            "DELETE",
            f"{self.base_url}/api/config/config_entries/entry/"
            f"{quote(entry_id, safe='')}",
            what=f"removing config entry {entry_id}")[0]

    def list_flow_handlers(self) -> List[str]:
        """Every integration that can be set up, by domain. [] on failure.

        This is what Home Assistant's own "Add Integration" list is built
        from -- upwards of a thousand entries, maintained by them. Asking for
        it is the alternative to keeping a copy here, which would be a copy
        that goes stale the first time HA ships a release.
        """
        data = self._request_json(
            "GET", f"{self.base_url}/api/config/config_entries/flow_handlers",
            what="listing integrations")
        if not isinstance(data, list):
            return []
        return sorted(d for d in data if isinstance(d, str))

    def get_integration_manifests(self, domains: List[str]
                                  ) -> Dict[str, Dict[str, Any]]:
        """Manifests by domain, for the friendly name and the iot_class.

        BEST EFFORT. The handler list above is what matters; this only makes
        the list nicer -- a real name instead of a domain, and whether the
        integration talks to a device directly or through somebody's cloud.
        An empty dict is a perfectly workable answer and callers must treat it
        as one.

        `iot_class` is HA's own classification, declared in every integration's
        manifest: local_polling / local_push talk to the device, cloud_polling
        / cloud_push need an account and the internet. It is the distinction
        that decides whether a set-up can finish here or has to hand over to a
        browser for a sign-in.

        WEBSOCKET, NOT REST. This used to GET /api/manifest/list, a path no
        Home Assistant answers (404 on 2026.2.3; the frontend has always
        fetched manifests over the WebSocket command `manifest/list`), so
        every call returned {} and the integration list showed bare domains
        with no local/cloud split -- found against a live HA, 2026-09-06.
        Without `integrations` the command returns only the LOADED
        integrations' manifests; passing the flow-handler list (or `domains`)
        makes HA load and return every one of them -- 802 in 0.3 s.
        """
        if not domains:
            return {}
        data = self._ws_dicts({"type": "manifest/list",
                               "integrations": list(domains)},
                              what="listing integration manifests")
        return {m["domain"]: m for m in data
                if isinstance(m.get("domain"), str)}

    def start_config_flow(self, handler: str) -> Optional[Dict[str, Any]]:
        """Begin setting up an integration by name. Returns the FIRST step.

        The response is either a form to fill (`type: "form"` with a schema),
        a finished entry (`type: "create_entry"`), or an abort. Callers must
        branch on `type` rather than assuming a form -- some integrations
        complete in one hop.
        """
        return self._as_step(self._request_json(
            "POST", f"{self.base_url}/api/config/config_entries/flow",
            what=f"starting {handler} setup",
            json={"handler": handler, "show_advanced_options": False}),
            what=f"starting {handler}")

    def submit_config_flow(self, flow_id: str,
                           answers: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Answer the current step. Returns the NEXT step, or the result."""
        if not _valid(_OPAQUE_ID_RE, flow_id):
            return None
        return self._as_step(self._request_json(
            "POST", self._flow_url(flow_id),
            what=f"submitting step for flow {flow_id}",
            json=answers),
            what=f"flow {flow_id}")

    def get_config_flow(self, flow_id: str) -> Optional[Dict[str, Any]]:
        """The CURRENT state of an in-progress flow, without answering it.

        The counterpart to submit_config_flow, and the reason it has to exist
        separately: some steps have nothing to answer. A browser sign-in
        (`external_step`) finishes when the site redirects back to HA; a
        Zigbee pairing (`show_progress`) finishes when the bulb joins. In both
        cases HOME ASSISTANT advances the flow, not us -- submitting to one
        would push it somewhere it is not ready to go. All a caller may do is
        look, repeatedly, until it has moved.

        None means the flow is gone, which is itself an answer: somebody
        finished or abandoned it somewhere else. _request_json already turns
        HA's 404 into None, so that case arrives here as an ordinary result
        rather than an exception.
        """
        if not _valid(_OPAQUE_ID_RE, flow_id):
            return None
        return self._as_step(self._request_json(
            "GET", self._flow_url(flow_id), what=f"reading flow {flow_id}"),
            what=f"flow {flow_id}")

    def abort_config_flow(self, flow_id: str) -> bool:
        """Cancel a half-finished set-up.

        Worth calling on cancellation: an abandoned flow stays in HA's pending
        list, and the next person to look sees a set-up apparently in progress
        that nobody is driving.
        """
        if not _valid(_OPAQUE_ID_RE, flow_id):
            return False
        return self._request("DELETE", self._flow_url(flow_id),
                             what=f"aborting flow {flow_id}")[0]

    def call_service_with_response(self, domain: str, service: str,
                                   http_wait: Optional[int] = None,
                                   **service_data: Any) -> Optional[Dict[str, Any]]:
        """Call a service that RETURNS DATA, and hand that data back.

        A separate method from call_service, which returns a bool and discards
        the body. Most HA services have nothing to say -- turn_on either
        happened or it did not -- and collapsing them to a bool is right. A few
        answer a question, and for those the body IS the point.

        This exists for OpenIRBlaster's learn_start, which arms the IR receiver
        and BLOCKS until a code arrives or the timeout expires, then reports
        {"status": "received"|"timeout"|"cancelled", ...}. Driving that through
        a boolean would mean firing the call, ignoring the answer, and then
        polling the entity registry hoping a button appeared -- inference
        instead of confirmation, with no way to tell a missed capture from a
        garbled one.

        NO entity_id, unlike call_service: a service like learn_start targets
        an integration entry, not an entity. Everything goes through
        `service_data`.

        `http_wait` overrides the HTTP read timeout, and must when the SERVICE
        itself blocks: learn_start with timeout=30 holds the connection open
        for those 30 seconds, and the default 5s HTTP timeout would abandon it
        at second five -- reported as a network failure while the device was
        still waiting for the remote.

        NOT called `timeout`. Service data goes through **kwargs, and
        learn_start's own field is also called `timeout` -- so one name would
        have to mean both, and Python raises "got multiple values for keyword
        argument" the moment a caller tries to set each. A distinct name is
        the only way both can be expressed.

        Returns the response body, or None on any failure. Never raises.
        """
        # HA only returns a body when asked, and only for services that
        # support it. A service that does NOT support it returns 400 with this
        # flag set -- which is why call_service does not simply always pass it.
        url = self._service_url(domain, service)
        if url is None:
            return None
        # +10: the service's own deadline plus room for it to answer afterwards.
        # Equal values would race, and the HTTP side losing looks like a
        # network failure rather than a timed-out capture.
        http_timeout = TIMEOUT_SECONDS if http_wait is None else http_wait + 10
        # A 400 here usually means the service cannot return data, or the
        # data sent was rejected; `what` says so, since _request prints it.
        body = self._request_json(
            "POST", url + "?return_response=true", json=dict(service_data),
            timeout=http_timeout,
            what=f"{domain}.{service} with a response (it may not support "
                 f"returning data)")
        if body is None:
            return None
        if not isinstance(body, dict):
            print(f"[HA] {domain}.{service} returned {type(body).__name__}, "
                  f"expected an object.")
            return None
        # HA wraps it: {"changed_states": [...], "service_response": {...}}.
        # The service_response is what the caller asked for; returning the
        # envelope would make every caller unwrap it themselves.
        inner = body.get("service_response")
        if isinstance(inner, dict):
            return inner
        return body

    def call_service(self, domain: str, service: str, entity_id: str,
                     **extra_data: Any) -> bool:
        """Call an HA service (e.g. domain='light', service='turn_on') on a
        specific entity. Returns True/False for success -- never raises.

        Service data goes in as KEYWORD arguments:

            call_service("light", "turn_on", "light.kitchen", brightness_pct=50)

        Spelled out because a caller passing the data as a positional dict --
        call_service(domain, service, entity_id, {"brightness_pct": 50}) --
        raises TypeError, and did: device_manager's numeric write path was
        built that way and crashed on every value command to an HA device.
        """
        url = self._service_url(domain, service)
        if url is None:
            return False
        if not _valid(_ENTITY_ID_RE, entity_id):
            print(f"[HA] Not an entity id: {entity_id!r}")
            return False
        return self._request(
            "POST", url, json={"entity_id": entity_id, **extra_data},
            what=f"service call {domain}.{service} on {entity_id}")[0]

    def _service_url(self, domain: str, service: str) -> Optional[str]:
        """/api/services/<domain>/<service>, or None (said) for a name that
        is not one."""
        if not (_valid(_SLUG_RE, domain) and _valid(_SLUG_RE, service)):
            print(f"[HA] Not a service name: {domain!r}.{service!r}")
            return None
        return (f"{self.base_url}/api/services/{quote(domain, safe='')}/"
                f"{quote(service, safe='')}")

    def is_reachable(self) -> bool:
        """Cheap liveness check -- HA's own root API endpoint just confirms
        it's up, without the cost of fetching every entity's state.

        A 401 answers the question truthfully: something is there, and it is
        HA. Returning False for it made a bad token indistinguishable from a
        dead server, and main.py duly reported "not reachable right now" for a
        configuration mistake that would never fix itself.

        THAT PARAGRAPH DESCRIBED A FIX THAT WAS NEVER MADE. The 401 branch
        below still returned a bare False, so the docstring and the code
        under it said opposite things and the code won -- a revoked token
        printed "check HOMEVOICE_HA_TOKEN" and was then immediately
        contradicted by main.py's "not reachable right now ... nothing needs
        restarting when HA comes back", and re-probed every sixty seconds
        forever for a fault that never clears on its own (2026-09-14).

        It still returns False, because the answer to "can this client be
        used" really is no. What is new is `token_rejected`, set beside it,
        so the caller can tell the two failures apart. See
        main._try_connect_ha, which now says the true sentence for each.
        """
        self.token_rejected = False
        try:
            resp = self._session.get(f"{self.base_url}/api/",
                                     timeout=LIVENESS_TIMEOUT_SECONDS)
        except requests.exceptions.RequestException:
            return False
        if resp.status_code == 401:
            print("[HA] Reachable, but the token was rejected -- "
                  "check HOMEVOICE_HA_TOKEN.")
            self.token_rejected = True
            return False
        return resp.status_code == 200

    #: From config, so adding a domain is a setting change rather than a code
    #: edit. Excludes the read-only informational domains (weather, calendar,
    #: update, zone, person, sun) that HA's raw /api/states includes but that
    #: voice commands never target -- see config.CONTROLLABLE_DOMAINS for why
    #: it is an allowlist.
    CONTROLLABLE_DOMAINS = config.CONTROLLABLE_DOMAINS

    def list_controllable_entities(self) -> List[Dict[str, Any]]:
        """The clean view: just entity_id, domain, friendly name, and current
        state, filtered to domains we'd actually let voice control -- strips
        every bit of noise the raw /api/states response carries (timestamps,
        context IDs, camera/media tokens, etc). This is the shape
        device_manager.py will want when it lets a person pick a real HA
        entity during registration."""
        clean = []
        for entity in self.get_states() or []:
            entity_id = entity["entity_id"]       # a str: get_states checks
            domain = entity_id.split(".", 1)[0] if "." in entity_id else ""
            if domain not in self.CONTROLLABLE_DOMAINS:
                continue
            attrs = entity.get("attributes")
            if not isinstance(attrs, dict):
                attrs = {}
            # A SENSOR EARNS ITS PLACE ONLY IF IT MEASURES SOMETHING.
            #
            # "sensor" is allowlisted so a power meter can be registered and
            # read -- that is its documented reason in config. But a Tasmota
            # board reports its own health through the same domain: restart
            # reason, Wi-Fi connect count, SSID, last boot time. One board
            # put five of those in the picker around its one real switch, so
            # the person adding "led light" scrolled the board's diary to
            # find it.
            #
            # The line between the two is the unit. A meter measures IN
            # something -- W, kWh, degrees, percent. Telemetry has no unit:
            # a restart reason is text, a connect count is a bare tally, an
            # SSID is a name. No unit_of_measurement, not offered. The
            # board's actual devices live in switch/light and are untouched.
            if domain == "sensor" and not attrs.get("unit_of_measurement"):
                continue
            clean.append({
                "entity_id": entity_id,
                "domain": domain,
                # `or entity_id`: HA sends friendly_name: null for entities
                # that have none, and .get(key, default) does not fall back on
                # an explicit null -- so the name came through as None and was
                # formatted into the picker as "None".
                "name": attrs.get("friendly_name") or entity_id,
                "state": entity.get("state", "unknown"),
            })
        return clean
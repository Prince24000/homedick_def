"""
scene_manager.py — Scene registry and embedding-based trigger resolution.

A scene maps a trigger phrase to a batch of device state updates.
Trigger matching is semantic: the command embedding is dotted against each
scene's stored embedding; if the top score clears the threshold and beats the
runner-up by the margin, the scene fires.

WHAT IS EMBEDDED. The field is called trigger_embedding, but register()
embeds the DESCRIPTION when one is given and only falls back to the trigger
phrase otherwise -- and main.menu_add_scene always passes a generated
description. So the same scene ends up with a completely different vector
depending on whether the LLM happened to be available at registration time.
That is worth knowing when a scene stops matching: it may simply have been
registered on a day the model was missing.
"""

from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
import numpy as np

from core.config import (SCENES_JSON_PATH, SCENE_MATCH_THRESHOLD,
                    SCENE_AMBIGUITY_MARGIN, SCENE_EMBED_DIM)
from core.hv_common import event
# The same cross-process discipline devices.json has: the service and a
# terminal shell each hold a SceneManager over the one file.
from core.locked_store import JsonRegistry

SCENES_PATH = Path(SCENES_JSON_PATH)


@dataclass(frozen=True)
class SceneAction:
    device_id: str
    state: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {"device_id": self.device_id, "state": dict(self.state)}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SceneAction":
        if not isinstance(data, dict):
            raise ValueError(f"Action must be a dict, got {type(data).__name__}")
        did = data.get("device_id")
        if not did or not isinstance(did, str):
            raise ValueError(f"Action missing valid device_id: {data!r}")
        st = data.get("state")
        if not isinstance(st, dict):
            st = {}
        return cls(device_id=did, state=dict(st))


@dataclass(frozen=True, eq=False)
class Scene:
    """eq=False on purpose.

    A frozen dataclass generates __eq__ and __hash__, and this one holds a
    numpy array: comparing two Scenes returns an array and then raises "truth
    value of an array is ambiguous", while hashing raises outright. Nothing
    does either today, but `sorted(scored)` without a key would -- and that is
    one keystroke away in resolve(). Identity comparison is what a registry
    entry actually wants.
    """
    name: str
    trigger: str
    trigger_embedding: np.ndarray
    actions: Tuple["SceneAction", ...] = field(default_factory=tuple)
    created_at: str = ""
    #: WHO MADE IT, and who last changed it (2026-10-11). Empty for a scene
    #: made before anyone was recorded: it belongs to the house.
    created_by: str = ""
    changed_by: str = ""
    changed_at: str = ""
    #: "Just mine": only its maker sees it, and only their voice fires it.
    private: bool = False
    #: What it is for, in the words kept for it ("" when none were given).
    description: str = ""

    def visible_to(self, user: Optional[str]) -> bool:
        return not self.private or (bool(user) and self.created_by == user)

    def matches(self, command_emb: np.ndarray) -> float:
        """Cosine similarity. Both embeddings must be L2-normalized."""
        return float(np.dot(self.trigger_embedding, command_emb))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "trigger": self.trigger,
            "trigger_embedding": self.trigger_embedding.tolist(),
            "actions": [a.to_dict() for a in self.actions],
            "created_at": self.created_at,
            "created_by": self.created_by,
            "changed_by": self.changed_by,
            "changed_at": self.changed_at,
            "private": self.private,
            "description": self.description,
        }


class SceneManager(JsonRegistry):
    _TAG = "SCENE"
    _WHAT = "scenes"
    _DUMP_KW = {"indent": 2}

    def __init__(self, embed_fn: Callable[[str], np.ndarray],
                 scenes_path: Path = SCENES_PATH,
                 emb_dim: int = SCENE_EMBED_DIM,
                 is_secure: Optional[Callable[[str], bool]] = None):
        """emb_dim: the width `embed_fn` emits. PASS IT.

        is_secure: device_id -> bool, so resolve() can say whether a scene
        reaches a secure device (main passes
        `lambda did: bool(dm.devices.get(did, {}).get("secure"))`). Without
        it a scene could unlock a door on the passive voice check alone.

        It defaulted to a hardcoded 768 and main.py never supplied one, while
        model_module deliberately probes the real width because "an index
        built at a different width would otherwise fail later, inside a
        matmul, instead of here where the cause is obvious". On any model of
        another width every stored scene failed _load's length check and
        vanished behind a WARNING, and register() could never create one.
        model_module.embedding_dim() is the answer to pass.
        """
        self.embed = embed_fn
        self.emb_dim = emb_dim
        self.is_secure = is_secure
        self.scenes_path = Path(scenes_path)
        self.scenes_path.parent.mkdir(parents=True, exist_ok=True)
        self.scenes: Dict[str, Scene] = {}
        #: Records _load could not use (another model's embedding width),
        #: written back by _save so a run under the wrong model does not
        #: delete every scene the moment one is registered or removed.
        self._unloadable: Dict[str, Any] = {}
        self._init_registry(self.scenes_path)

    # ── Persistence (JsonRegistry hooks) ──────────────────────

    def _clear(self) -> None:
        self.scenes, self._unloadable = {}, {}

    def _install(self, raw: Dict[str, Any]) -> None:
        self._clear()
        for name, data in raw.items():
            if not isinstance(data, dict):
                event("SCENE", f"Skipping scene {name!r}: not a dict")
                self._unloadable[name] = data
                continue
            emb = data.get("trigger_embedding")
            if not isinstance(emb, list) or len(emb) != self.emb_dim:
                width = len(emb) if isinstance(emb, list) else "not a list"
                event("SCENE", f"Skipping scene {name!r}: embedding is {width} "
                      f"wide, this model emits {self.emb_dim}. It was created "
                      f"under a different model -- kept on disk, not matched.")
                self._unloadable[name] = data
                continue
            try:
                actions = tuple(SceneAction.from_dict(a) for a in data.get("actions", []))
            except (ValueError, TypeError) as exc:   # TypeError: "actions": null
                # KEPT ON DISK, like the two skips above it (2026-09-14).
                # This was the only one of the three branches that did
                # not record the raw scene in `_unloadable`, and `_save`
                # writes `_unloadable + scenes` -- so a scene skipped
                # here was in NEITHER collection and was ERASED the next
                # time anything saved.
                #
                # One malformed action -- a lost device_id -- and the
                # whole scene went: its trigger phrase, its embedding
                # and every valid action in it, destroyed by the
                # unrelated act of registering some other scene.
                # Meanwhile the wrong-model case `_unloadable` was
                # actually built for survived perfectly.
                event("SCENE", f"Skipping scene {name!r}: invalid action "
                      f"({exc}) -- kept on disk, not matched")
                self._unloadable[name] = data
                continue
            self.scenes[name] = Scene(
                name=name,
                trigger=data.get("trigger", ""),
                trigger_embedding=np.array(emb, dtype=np.float32),
                actions=actions,
                created_at=data.get("created_at", ""),
                created_by=str(data.get("created_by") or ""),
                changed_by=str(data.get("changed_by") or ""),
                changed_at=str(data.get("changed_at") or ""),
                private=data.get("private") is True,
                description=str(data.get("description") or ""),
            )

    def _document(self) -> Dict[str, Any]:
        """What _save writes (a UNIQUE temp file via atomic_write_json, not a
        fixed `scenes.tmp`: with one shared temp name, two processes' saves
        truncated and wrote the same file and os.replace published the mix)."""
        out = {name: data for name, data in self._unloadable.items()
               if name not in self.scenes}
        out.update({name: s.to_dict() for name, s in self.scenes.items()})
        return out

    @staticmethod
    def words_for(trigger: str, description: Optional[str]) -> str:
        """What is embedded: the description, led by the scene's name when
        the description does not say it; the name alone without one."""
        words = (description or "").strip()
        name = (trigger or "").strip()
        if not words:
            return name
        if name and name.lower() not in words.lower():
            return f"{name}. {words}"
        return words

    def _embed_normalised(self, text: str) -> np.ndarray:
        """The L2-normalised embedding of `text`, or ValueError."""
        emb = self.embed(text)
        if not isinstance(emb, np.ndarray) or emb.shape != (self.emb_dim,):
            raise ValueError(f"embed_fn must return a {self.emb_dim}-dim numpy array")
        emb = emb.astype(np.float32)
        norm = np.linalg.norm(emb)
        if norm == 0:
            raise ValueError("embed_fn returned a zero vector")
        return emb / norm

    # ── Registration ──────────────────────────────────────────

    def register(self, name: str, trigger: str, actions: List[Dict[str, Any]],
                 description: Optional[str] = None, *, by: str = "",
                 private: bool = False) -> Scene:
        key = name.strip().lower()
        if not key:
            raise ValueError("Scene name is empty")
        # Validated (from_dict raises ValueError) and embedded BEFORE the
        # cross-process lock: the model call is the slow part.
        new_actions = tuple(SceneAction.from_dict(a) for a in actions)
        emb = self._embed_normalised(self.words_for(trigger, description))
        with self._writing():
            if key in self.scenes:
                raise ValueError(self._taken(key, by))

            scene = Scene(
                name=key,
                trigger=trigger.strip(),
                trigger_embedding=emb,
                actions=new_actions,
                created_at=datetime.now(timezone.utc).isoformat(),
                created_by=by or "",
                private=bool(private and by),
                description=(description or "").strip(),
            )
            self.scenes[key] = scene
            self._save()
            return scene

    def update(self, name: str, *, new_name: Optional[str] = None,
               trigger: Optional[str] = None,
               description: Optional[str] = None,
               actions: Optional[List[Dict[str, Any]]] = None,
               by: Optional[str] = None,
               private: Optional[bool] = None) -> Scene:
        """Edit an existing scene in place. Only the fields passed change.

        Re-embeds ONLY when description (or, failing that, trigger) is
        given -- an unchanged embedding is kept byte-for-byte, so updating a
        scene's actions can never silently change which utterances fire it.
        Renaming moves the registry key and refuses a collision. created_at
        is preserved: this is the same scene, edited, not a new one.
        """
        key = name.strip().lower()
        new_actions = (None if actions is None
                       else tuple(SceneAction.from_dict(a) for a in actions))
        emb = (self._embed_normalised(self.words_for(
                   trigger or new_name or name, description))
               if description or trigger else None)
        with self._writing():
            old = self.scenes.get(key)
            if old is None:
                raise ValueError(f"Scene {key!r} does not exist")

            target = (new_name.strip().lower() if new_name else key)
            if not target:
                raise ValueError("Scene name is empty")
            if target != key and target in self.scenes:
                raise ValueError(self._taken(target, by))
            if new_actions is None:
                new_actions = old.actions
            if emb is None:
                emb = old.trigger_embedding

            scene = Scene(
                name=target,
                trigger=(trigger.strip() if trigger else old.trigger),
                trigger_embedding=emb,
                actions=new_actions,
                created_at=old.created_at
                or datetime.now(timezone.utc).isoformat(),
                created_by=old.created_by,
                changed_by=(by if by is not None else old.changed_by),
                changed_at=(datetime.now(timezone.utc).isoformat()
                            if by is not None else old.changed_at),
                private=(old.private if private is None
                         else bool(private and old.created_by)),
                description=(old.description if description is None
                             else description.strip()),
            )
            if target != key:
                del self.scenes[key]
            self.scenes[target] = scene
            self._save()
            return scene

    def remove(self, name: str) -> bool:
        key = name.strip().lower()
        with self._writing():
            if key not in self.scenes:
                return False
            del self.scenes[key]
            self._save()
            return True

    def _taken(self, key: str, by: Optional[str]) -> str:
        """Why a name cannot be used. Somebody else's "just mine" scene is
        not shown to you, so it is named as theirs rather than as a scene
        you cannot find."""
        other = self.scenes.get(key)
        if other is not None and other.private and other.created_by \
                and other.created_by != by:
            return (f"{other.created_by} already has a scene of their own "
                    f"called {key!r} -- pick another name")
        return f"Scene {key!r} already exists"

    def get(self, name: str, user: Any = ...) -> Optional[Scene]:
        """The scene by name. With `user`, only one that person may see
        (another person's "just mine" scene is not theirs to find)."""
        with self._lock:
            self._reload_if_changed()
            scene = self.scenes.get(name.strip().lower())
        if scene is not None and user is not ... and not scene.visible_to(user):
            return None
        return scene

    def list_scenes(self, user: Any = ...) -> List[Scene]:
        """Every scene; with `user`, the ones that person may see."""
        with self._lock:
            self._reload_if_changed()
            scenes = list(self.scenes.values())
        if user is ...:
            return scenes
        return [s for s in scenes if s.visible_to(user)]

    # ── Owners ────────────────────────────────────────────────

    def owned_by(self, user: str) -> List[Scene]:
        with self._lock:
            self._reload_if_changed()
            return [s for s in self.scenes.values() if user and s.created_by == user]

    def rename_owner(self, old: str, new: str) -> int:
        """A person was renamed: their scenes, and their changes, follow."""
        if not old or not new or old == new:
            return 0
        moved = 0
        with self._writing():
            for key, sc in list(self.scenes.items()):
                made, changed = sc.created_by == old, sc.changed_by == old
                if made or changed:
                    self.scenes[key] = Scene(
                        name=sc.name, trigger=sc.trigger,
                        trigger_embedding=sc.trigger_embedding,
                        actions=sc.actions, created_at=sc.created_at,
                        created_by=new if made else sc.created_by,
                        changed_by=new if changed else sc.changed_by,
                        changed_at=sc.changed_at, private=sc.private,
                        description=sc.description)
                    moved += 1
            if moved:
                self._save()
        return moved

    def hand_over(self, name: str, to: str) -> bool:
        """A shared scene of someone who left becomes `to`'s."""
        key = name.strip().lower()
        with self._writing():
            sc = self.scenes.get(key)
            if sc is None:
                return False
            self.scenes[key] = Scene(
                name=sc.name, trigger=sc.trigger,
                trigger_embedding=sc.trigger_embedding, actions=sc.actions,
                created_at=sc.created_at, created_by=to,
                changed_by=sc.changed_by, changed_at=sc.changed_at,
                private=False, description=sc.description)
            self._save()
            return True

    # ── Resolution ────────────────────────────────────────────

    def resolve(self, command_emb: np.ndarray,
                speaker: Any = ...) -> Optional[Dict[str, Any]]:
        """`speaker`: whose voice this is. Another person's "just mine"
        scene is never matched, and the speaker's OWN scene wins a near tie
        with a shared one -- it is the one they meant."""
        with self._lock:
            self._reload_if_changed()
            pool = [s for s in self.scenes.values()
                    if speaker is ... or s.visible_to(speaker)]
            if not pool:
                return None

            scored = [(s, s.matches(command_emb)) for s in pool]
            scored.sort(key=lambda x: x[1], reverse=True)
            decided = False
            if speaker is not ... and len(scored) > 1 \
                    and scored[0][1] - scored[1][1] < SCENE_AMBIGUITY_MARGIN:
                mine = [i for i, (sc, score) in enumerate(scored[:2])
                        if sc.private and sc.created_by == speaker
                        and score >= SCENE_MATCH_THRESHOLD]
                if len(mine) == 1:
                    # Decided: theirs. The runner-up no longer makes a tie.
                    scored.insert(0, scored.pop(mine[0]))
                    decided = True

            top, top_score = scored[0]
            second_score = scored[1][1] if len(scored) > 1 else 0.0

            if top_score < SCENE_MATCH_THRESHOLD:
                return None

            def touches_secure(*scenes: Scene) -> bool:
                if self.is_secure is None:
                    return False
                return any(bool(self.is_secure(a.device_id))
                           for s in scenes for a in s.actions)

            margin = top_score - second_score
            if not decided and margin < SCENE_AMBIGUITY_MARGIN \
                    and second_score >= SCENE_MATCH_THRESHOLD:
                return {
                    "status": "ambiguous",
                    # Any of the candidates reaching a secure device.
                    "secure": touches_secure(*(s for s, _ in scored[:3])),
                    "top": {"name": top.name, "trigger": top.trigger, "score": float(top_score)},
                    "candidates": [
                        {"name": s.name, "trigger": s.trigger, "score": float(sc)}
                        for s, sc in scored[:3]
                    ],
                }

            # to_dict() would serialise the whole embedding -- hundreds of
            # floats -- on every resolution, and the caller (main._fire_scene)
            # reads only the name and the actions.
            return {
                "status": "fired",
                # True when any action targets a secure device: the caller
                # must run the ACTIVE voice challenge before firing it.
                "secure": touches_secure(top),
                "scene": {"name": top.name,
                          "trigger": top.trigger,
                          "actions": [a.to_dict() for a in top.actions]},
                "score": float(top_score),
                "margin": float(margin),
            }
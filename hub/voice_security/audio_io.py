"""
audio_io.py — the single audio-decode chokepoint.

EVERY path that turns audio into the float32 array SV/ASR see goes through
here: a file on disk (enrollment, CLI testing) via decode_file(), and raw
bytes off the network (the phone) via decode_bytes(). Routing both through
one place is the point -- enrollment and verification can never again decode
or resample differently, which was the asymmetry at the root of the whole
speaker-verification saga. Output is ALWAYS 16kHz mono float32 in [-1, 1],
or None.

ffmpeg-first
------------
If the ffmpeg binary is on PATH, it decodes EVERYTHING -- AAC/M4A, WhatsApp
Opus, MP3, WAV, FLAC -- and resamples to 16k mono in one deterministic step.
This is the reason the module exists: WhatsApp/AAC test files that stdlib
`wave` and `soundfile` cannot read decode cleanly through ffmpeg, and every
clip lands in the exact same 16k-mono shape regardless of what the phone
sent. ffmpeg is a system binary, not a pip package -- setup_manager checks
for it and says so loudly if it's missing (see BINARY_ASSETS there).

Fallback (no ffmpeg)
--------------------
stdlib `wave` for PCM WAV, `soundfile` for OGG/FLAC, then a numpy resample to
16k. Zero binary dependency, covers the common case, so the system still runs
without ffmpeg -- it just can't decode AAC/Opus until ffmpeg is installed.

Fast path (already-16k-mono PCM)
-------------------------------
"ffmpeg decodes EVERYTHING" was costing a process launch per command to
convert audio that was ALREADY in the target format -- the browser client
resamples to 16k mono s16 before it sends, so ffmpeg was reading 16k mono s16
and writing back the identical bytes. Measured at 70-900ms per spawn on
Linux and worse on Windows, against ~0.5ms for the stdlib parse.

_fast_pcm_wav() short-circuits that ONE case and nothing else. The guard is
deliberately strict -- exactly 16000 Hz, exactly 1 channel, exactly 16-bit --
because those are the conditions under which the two decoders are provably
bit-identical: no resampling, no channel mixing, the same int16/32768.0
arithmetic on both sides. At any other rate or width the paths would diverge
(ffmpeg's polyphase resampler vs _to_16k_mono's linear interpolation), so
anything that isn't an exact match falls through to ffmpeg untouched.

It is wired into BOTH decode_file and decode_bytes, deliberately. Applying it
to only one would reintroduce the exact asymmetry this module exists to
prevent: enrollment decoded one way, verification another.
"""

import io
import os
import shutil
import subprocess
import wave
from typing import Optional

import numpy as np

from core.config import MIC_SR, FFMPEG_TIMEOUT_SECONDS
from core.dependency_flags import sf, _SOUNDFILE

# THE SAME NUMBER AS THE MICROPHONE, not a second opinion about it. This was
# a local 16000 sitting beside config.MIC_SR = 16000: two definitions of the
# rate everything decodes to. Had they ever diverged, enrollment and
# verification would have resampled differently -- precisely the asymmetry
# this module's docstring says it exists to make impossible.
TARGET_SR = MIC_SR

# Resolved once at import. None if ffmpeg isn't on PATH -> fallback decoders.
_FFMPEG = shutil.which("ffmpeg")


def _pcm16_to_float(raw: bytes) -> Optional[np.ndarray]:
    """s16le bytes -> float32 in [-1, 1], or None if the buffer is malformed.

    np.frombuffer raises ValueError on a buffer whose length isn't a multiple
    of the element size -- a truncated ffmpeg pipe, a WAV header that lies
    about its sample width. Every frombuffer in this module sat OUTSIDE its
    function's try/except, so that ValueError escaped a module whose entire
    contract is "16kHz mono float32, or None". The one promise it makes was
    the one case it did not cover."""
    try:
        return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    except ValueError:
        return None


# ── Shared normalization ────────────────────────────────────────────────────

def _to_16k_mono(audio: np.ndarray, sr) -> np.ndarray:
    """Force to 16k mono float32. Used by the fallback decoders; ffmpeg already
    emits 16k mono so its output skips this. Linear resample -- good enough for
    speech at gentle ratios (44.1k/48k -> 16k); TitaNet doesn't perceive the
    difference vs a filtered resampler."""
    audio = np.asarray(audio, dtype=np.float32)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)  # mix to mono
    if sr and sr != TARGET_SR and len(audio) > 1:
        new_len = int(round(len(audio) * TARGET_SR / sr))
        if new_len > 1:
            idx = np.linspace(0, len(audio) - 1, new_len)
            audio = np.interp(idx, np.arange(len(audio)), audio).astype(np.float32)
    return audio


# ── Fast path: input already in the target format ───────────────────────────

def _fast_pcm_wav(src) -> Optional[np.ndarray]:
    """Return float32 audio IFF src is already exactly 16kHz mono 16-bit PCM
    WAV; otherwise None so the caller falls through to ffmpeg.

    src: a path or a file-like object. Only the header is trusted -- if any
    of the three conditions misses, we do not attempt a partial conversion
    here, because that is precisely where the two decode paths would start
    producing different arrays for the same input.
    """
    try:
        with wave.open(src, "rb") as wf:
            if (wf.getframerate() != TARGET_SR
                    or wf.getnchannels() != 1
                    or wf.getsampwidth() != 2):
                return None
            frames = wf.readframes(wf.getnframes())
    except Exception:
        return None
    # Same arithmetic as the ffmpeg branch: s16le -> float32 / 32768.
    return _pcm16_to_float(frames)


# ── ffmpeg path ─────────────────────────────────────────────────────────────

#: fmt_hint -> explicit ffmpeg input demuxer. Pinning the demuxer stops
#: ffmpeg PROBING attacker-supplied bytes, where a playlist (hls/concat)
#: could name local files or URLs for it to open.
_FFMPEG_INPUT_FORMATS = {
    "wav": ["-f", "wav"], "wave": ["-f", "wav"], "pcm": ["-f", "wav"],
    "ogg": ["-f", "ogg"], "opus": ["-f", "ogg"], "oga": ["-f", "ogg"],
    "webm": ["-f", "matroska"], "mkv": ["-f", "matroska"],
    "raw": ["-f", "s16le", "-ar", str(TARGET_SR), "-ac", "1"],
    "s16le": ["-f", "s16le", "-ar", str(TARGET_SR), "-ac", "1"],
}


def _ffmpeg_to_16k(input_source: str, input_bytes: Optional[bytes],
                   fmt_hint: str = "") -> Optional[np.ndarray]:
    """Decode ANY input to 16k mono via ffmpeg, returned as float32.

    input_source is 'pipe:0' with input_bytes set (network case), or a file
    path with input_bytes=None (file case). Output is raw s16le PCM on stdout,
    which we convert to float32 ourselves -- no temp files, one subprocess.
    """
    if _FFMPEG is None:
        return None
    cmd = [
        _FFMPEG, "-hide_banner", "-loglevel", "error",
        # Whatever the container, ffmpeg may only read the input it was
        # handed -- never a file or a URL a playlist inside it names.
        "-protocol_whitelist",
        "pipe,data" if input_bytes is not None else "file,pipe,data",
        *_FFMPEG_INPUT_FORMATS.get((fmt_hint or "").lower(), []),
        "-i", input_source,
        "-ac", "1",                 # mono
        "-ar", str(TARGET_SR),      # 16 kHz
        "-f", "s16le",              # raw 16-bit little-endian PCM
        "-acodec", "pcm_s16le",
        "pipe:1",
    ]
    try:
        proc = subprocess.run(
            cmd,
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            # For file input we take no stdin at all (prevents any hang); for
            # pipe input, subprocess wires stdin to feed input_bytes.
            stdin=subprocess.DEVNULL if input_bytes is None else None,
            timeout=FFMPEG_TIMEOUT_SECONDS,
        )
    except Exception:
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    return _pcm16_to_float(proc.stdout)


# ── Fallback decoders (no ffmpeg) ───────────────────────────────────────────

#: sample width -> (dtype, offset, full scale) for PCM WAV.
_WAV_SAMPLE_FORMATS = {1: (np.uint8, 128, 128.0), 2: (np.int16, 0, 32768.0),
                       4: (np.int32, 0, 2147483648.0)}


def _decode_wave(src) -> Optional[np.ndarray]:
    """src: a path or a file-like object. PCM WAV only (stdlib)."""
    try:
        with wave.open(src, "rb") as wf:
            n_channels = wf.getnchannels()
            sw = wf.getsampwidth()
            sr = wf.getframerate()
            frames = wf.readframes(wf.getnframes())
    except Exception:
        return None
    if sw not in _WAV_SAMPLE_FORMATS:
        return None
    dtype, offset, scale = _WAV_SAMPLE_FORMATS[sw]
    try:
        audio = (np.frombuffer(frames, dtype=dtype).astype(np.float32) - offset) / scale
        # Inside the try: a truncated data chunk can end mid-frame, and
        # reshape then raises ValueError just like frombuffer does.
        # _to_16k_mono mixes the channels.
        audio = audio.reshape(-1, n_channels)
    except ValueError:
        # Frame count and declared sample width disagree -- a malformed file.
        return None
    return _to_16k_mono(audio, sr)


def _decode_soundfile(src) -> Optional[np.ndarray]:
    """src: a path or a file-like object. OGG/FLAC/etc via soundfile."""
    if not _SOUNDFILE:
        return None
    try:
        data, sr = sf.read(src, dtype="float32")
    except Exception:
        return None
    return _to_16k_mono(data, sr)


# ── Public API ──────────────────────────────────────────────────────────────

def _first_decoded(*decoders) -> Optional[np.ndarray]:
    """The first decoder, in order, that yields audio; None if none does.

    Zero samples is not audio: every caller in the system asks `if audio is
    None`, so a length-0 array (a WAV carrying an empty data chunk) would
    sail through as valid audio -- _voice_gate skipping its no-audio branch
    and handing nothing to TitaNet, enrollment counting a silent capture as
    a recorded phrase. One shape of "no audio", checked in one place."""
    for decode in decoders:
        audio = decode()
        if audio is not None and len(audio) > 0:
            return audio
    return None


def decode_file(path: str) -> Optional[np.ndarray]:
    """Decode an audio FILE to 16k mono float32, or None. Used by enrollment /
    CLI (main.py._load_wav)."""
    if not path:
        return None
    path = path.strip().strip('"').strip("'")  # tolerate quoted paths from Explorer
    if not os.path.exists(path):
        return None
    ext = os.path.splitext(path)[1].lower()
    return _first_decoded(
        # Already 16k mono s16? Skip the subprocess -- provably identical output.
        lambda: _fast_pcm_wav(path),
        lambda: _ffmpeg_to_16k(path, None, ext.lstrip(".")),
        # ffmpeg unavailable or failed -- fall back.
        lambda: _decode_wave(path) if ext == ".wav" else None,
        lambda: _decode_soundfile(path))


def decode_bytes(raw: bytes, fmt_hint: str = "wav") -> Optional[np.ndarray]:
    """Decode raw audio BYTES to 16k mono float32, or None. Used by the network
    path (ws_server.decode_audio_bytes). fmt_hint picks the fast path, pins
    ffmpeg's input demuxer (an unknown hint falls back to probing, under a
    protocol whitelist), and guides the FALLBACK decoders."""
    if not raw:
        return None
    # The browser client already sends 16k mono s16 PCM, so this is the hot
    # path for every voice command -- and it never needed ffmpeg at all.
    # Case-insensitive: a client sending "WAV" would otherwise miss the fast
    # path and pay for an ffmpeg process to produce identical bytes.
    wav_hint = (fmt_hint or "").lower() in ("wav", "pcm", "wave")
    return _first_decoded(
        lambda: _fast_pcm_wav(io.BytesIO(raw)) if wav_hint else None,
        lambda: _ffmpeg_to_16k("pipe:0", raw, fmt_hint),
        # Fallback: try WAV via stdlib, then soundfile for the rest.
        lambda: _decode_wave(io.BytesIO(raw)),
        lambda: _decode_soundfile(io.BytesIO(raw)))
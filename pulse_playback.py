"""PulseAudio playback for headerless PCM, fed chunk by chunk.

Split out of ``run_observer.py`` so that file reads as what it is meant
to demonstrate — attaching to a cascade and consuming its events through
the SDK — rather than as audio plumbing with a subscription buried in
it.  Nothing here knows about jaato: it takes a mime type and bytes.

The observer stays the SDK example; this is the speaker driver.
"""
import shutil
import subprocess


def _pcm_params(mime_type: str) -> dict[str, str] | None:
    """Read rate/channels/encoding out of a mime type, or None.

    The framework tags streamed speech as e.g.
    ``audio/pcm;rate=24000;channels=1;encoding=s16le``.  Those parameters
    are spelled out precisely because the payload is HEADERLESS — nothing
    in the bytes says how to play them — so they are parsed here rather
    than assumed.  A mime type this player cannot satisfy returns None
    and is reported instead of being guessed at.
    """
    base, _, params = mime_type.partition(";")
    if base.strip().lower() != "audio/pcm":
        return None
    parsed = {}
    for item in params.split(";"):
        key, _, value = item.partition("=")
        if key.strip():
            parsed[key.strip().lower()] = value.strip()
    if not {"rate", "channels", "encoding"} <= parsed.keys():
        return None
    return parsed


class PulsePlayer:
    """Plays model speech through PulseAudio as chunks arrive.

    One `paplay` per stream_id, fed on stdin, so audio starts before the
    turn ends — the point of observing a run live rather than opening a
    file afterwards.

    Playback failure never stops the trace: this is a read-only observer,
    and losing sound is not a reason to stop reporting events.  It says
    so once, loudly, rather than failing silently.
    """

    def __init__(self, enabled: bool = True) -> None:
        self._procs: dict[str, subprocess.Popen] = {}
        self._enabled = enabled and shutil.which("paplay") is not None
        self._complained = False
        if enabled and not self._enabled:
            print("  (paplay not on PATH — tracing without sound)")

    def feed(self, stream_id: str, mime_type: str, payload: bytes) -> None:
        """Push one chunk into this stream's player, starting it if needed."""
        if not self._enabled:
            return
        proc = self._procs.get(stream_id)
        if proc is None:
            params = _pcm_params(mime_type)
            if params is None:
                self._complain(f"cannot play {mime_type} — not headerless PCM")
                return
            try:
                proc = subprocess.Popen(
                    ["paplay", "--raw",
                     f"--format={params['encoding']}",
                     f"--rate={params['rate']}",
                     f"--channels={params['channels']}"],
                    stdin=subprocess.PIPE)
            except OSError as exc:
                self._complain(f"could not start paplay: {exc}")
                return
            self._procs[stream_id] = proc
        try:
            proc.stdin.write(payload)
            proc.stdin.flush()
        except (BrokenPipeError, ValueError) as exc:
            self._complain(f"playback stream died: {exc}")

    def finish(self, stream_id: str) -> None:
        """Close one stream so paplay drains and exits."""
        proc = self._procs.pop(stream_id, None)
        if proc is None:
            return
        try:
            proc.stdin.close()
        except (BrokenPipeError, ValueError):
            pass
        proc.wait(timeout=30)

    def finish_all(self) -> None:
        for stream_id in list(self._procs):
            self.finish(stream_id)

    def _complain(self, message: str) -> None:
        if not self._complained:
            print(f"  AUDIO: {message}")
            self._complained = True

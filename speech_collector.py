"""Reassemble model speech into a playable WAV.

Split out of ``run_cascade.py`` so that file reads as what it is meant
to demonstrate -- driving a cascade through the SDK -- rather than as
audio assembly with a session buried in it.  The companion to
``pulse_playback.py``: that one turns chunks into sound, this one turns
them into a file, and neither knows anything about jaato.

It takes ``(stream_id, sequence, bytes)``.  Unpacking those out of a
``ToolOutputEvent`` is the DRIVER's job, because that is the part which
is about the framework.
"""
import wave
from pathlib import Path

#: OpenAI streams audio as headerless pcm16 -- 24 kHz mono signed
#: 16-bit little-endian.  Headerless means these cannot be recovered
#: from the payload, so writing a playable WAV means supplying them.
PCM_RATE, PCM_CHANNELS, PCM_WIDTH = 24000, 1, 2


class SpeechCollector:
    """Reassembles model-generated audio from its chunks.

    Chunks are keyed by ``stream_id`` because one session may produce
    several utterances, and ordered by ``sequence`` rather than by
    arrival: the framework's per-client queue may evict media under
    backpressure, so a gap is possible, and sorting surfaces it instead
    of silently splicing the audio.
    """

    def __init__(self) -> None:
        self._chunks: dict[str, list[tuple[int, bytes]]] = {}

    def add(self, stream_id: str, sequence, data: bytes) -> None:
        """Take one decoded chunk of speech.

        Bytes, not an event: this module has no opinion about where they
        came from, which is what lets it be read without knowing the
        SDK.  ``sequence`` may be absent -- a whole-blob delivery is a
        one-chunk stream -- and is treated as 0 there.
        """
        self._chunks.setdefault(stream_id, []).append(
            (sequence if sequence is not None else 0, data))

    def write_wav(self, path: Path) -> tuple[int, float, list[int]]:
        """Write every stream to one WAV; return (bytes, seconds, gaps)."""
        raw = b""
        gaps: list[int] = []
        for stream_id in sorted(self._chunks):
            ordered = sorted(self._chunks[stream_id])
            got = [s for s, _ in ordered]
            expected = list(range(len(ordered)))
            if got != expected:
                gaps.extend(sorted(set(expected) - set(got)))
            raw += b"".join(data for _, data in ordered)
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as w:
            w.setnchannels(PCM_CHANNELS)
            w.setsampwidth(PCM_WIDTH)
            w.setframerate(PCM_RATE)
            w.writeframes(raw)
        seconds = len(raw) / float(PCM_RATE * PCM_CHANNELS * PCM_WIDTH)
        return len(raw), seconds, gaps

    def __bool__(self) -> bool:
        return bool(self._chunks)

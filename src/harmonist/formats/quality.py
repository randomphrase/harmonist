"""What the audio *stream* is, as distinct from the tags written on it (#130).

`ALAC` alone doesn't say whether a download is the quality that was paid for,
and it doesn't say whether two copies of an album are really the same file. The
sample rate, bit depth and bitrate do.

Every number here comes off the `info` object mutagen builds when a file is
opened, so reading it costs nothing extra: the scan already opens each file once
for its tags and `info` is available from the same handle.

**Which numbers are meaningful is codec knowledge**, and it is applied here
rather than at the point of display:

- **Bit depth exists only for lossless.** mutagen offers `bits_per_sample` on
  `MP4Info` for AAC as well as ALAC, but "16 bit" against a lossy stream
  describes the decoder's output, not the file — it would be wrong, not merely
  noisy. Lossless formats report depth and no bitrate; lossy ones report bitrate
  and no depth, because a lossless bitrate is an artifact of how well the audio
  happened to compress.
- **Opus has no sample rate at all.** Opus always decodes at 48 kHz whatever
  went in, so the container records none and mutagen exposes none. The clause is
  omitted rather than shown as unknown.
- **Only MP3 reports a bitrate mode.** For a VBR file it matters more than the
  number beside it, which is an average.
"""

from __future__ import annotations

from typing import Any, NamedTuple

from mutagen.mp3 import BitrateMode


class AudioQuality(NamedTuple):
    """One file's stream properties. Every field is absent when its format
    doesn't carry it — see the module docstring for which, and why."""

    #: Hz. None for Opus, which records none.
    sample_rate: int | None = None
    #: Bits per sample. Lossless only.
    bit_depth: int | None = None
    #: Bits per second. Lossy only.
    bitrate: int | None = None
    #: "CBR" / "VBR" / "ABR". MP3 only, and only when mutagen could tell.
    bitrate_mode: str | None = None

    @property
    def label(self) -> str | None:
        """Human summary — "44.1 kHz · 16 bit", "44.1 kHz · 320 kbps CBR" — or
        None when the format gave us nothing to say."""
        parts: list[str] = []
        if self.sample_rate:
            parts.append(f"{_trim(self.sample_rate / 1000)} kHz")
        if self.bit_depth:
            parts.append(f"{self.bit_depth} bit")
        elif self.bitrate:
            rate = f"{round(self.bitrate / 1000)} kbps"
            parts.append(f"{rate} {self.bitrate_mode}" if self.bitrate_mode else rate)
        return " · ".join(parts) or None


def _trim(khz: float) -> str:
    """48000 Hz reads as "48 kHz", 44100 as "44.1 kHz" — a trailing ".0" on a
    round rate looks like precision that isn't there."""
    return f"{khz:.1f}".removesuffix(".0")


def read(info: Any, *, lossless: bool) -> AudioQuality:
    """Pull the quality fields off a mutagen `info` object.

    `getattr` throughout because the whole point is that the four mutagen info
    classes carry different subsets of these — `OggOpusInfo` has no
    `sample_rate`, and only `MPEGInfo` has a `bitrate_mode`.
    """
    return AudioQuality(
        sample_rate=getattr(info, "sample_rate", None),
        bit_depth=getattr(info, "bits_per_sample", None) if lossless else None,
        bitrate=None if lossless else getattr(info, "bitrate", None),
        bitrate_mode=_mode(getattr(info, "bitrate_mode", None)),
    )


#: mutagen's `BitrateMode` is NOT an `enum.Enum` — it is a plain int subclass
#: built by mutagen's own decorator, so its members have no `.name` and reading
#: one off gives None rather than "CBR". Spelled out here instead.
#:
#: `BitrateMode.UNKNOWN` is absent on purpose: it is the answer for any MP3
#: without a Xing/VBRI header, and "UNKNOWN" beside the bitrate says less than
#: nothing at all.
_MODES = {
    BitrateMode.CBR: "CBR",
    BitrateMode.VBR: "VBR",
    BitrateMode.ABR: "ABR",
}


def _mode(mode: Any) -> str | None:
    """One of "CBR" / "VBR" / "ABR", or None when the format doesn't say."""
    return _MODES.get(mode) if mode is not None else None

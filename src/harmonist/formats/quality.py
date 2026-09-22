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
- **Some bitrates are an average, not a setting** — MP3 VBR/ABR, Opus, AAC.
  Two tracks from one encode report different numbers, so files are compared
  on `AudioQuality.key`, which leaves that number out (#582).
"""

from __future__ import annotations

from collections.abc import Sequence
from statistics import fmean
from typing import Any, NamedTuple

from mutagen.mp3 import BitrateMode
from mutagen.mp4 import MP4Info
from mutagen.oggopus import OggOpusInfo


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
    #: Whether `bitrate` is an average of what this file's audio needed rather
    #: than a setting the encode shared (#582) — see `key`.
    bitrate_varies: bool = False

    @property
    def label(self) -> str | None:
        """Human summary — "44.1 kHz · 16 bit", "44.1 kHz · 320 kbps CBR" — or
        None when the format gave us nothing to say."""
        return self._describe(exact=True)

    @property
    def key(self) -> str | None:
        """What two files are compared on (#582): the label, except that a
        bitrate which varies track to track is left out — "44.1 kHz · VBR".

        A CBR bitrate is a setting, so two tracks from one encode share it and a
        different number is a different encode. A VBR bitrate is an average of
        what that track's audio needed, so two tracks from one V0 encode almost
        never share one, and comparing it reports every VBR album as mixed. The
        mode is kept, because CBR beside VBR IS a different encode. A file with
        no mode gets a placeholder in its place instead, so an Opus file, which
        has no sample rate either, still has a key and doesn't count as a file
        we couldn't read. Keys are only compared, never shown.
        """
        return self._describe(exact=not self.bitrate_varies)

    def _describe(self, *, exact: bool) -> str | None:
        parts: list[str] = []
        if self.sample_rate:
            parts.append(f"{_trim(self.sample_rate / 1000)} kHz")
        if self.bit_depth:
            parts.append(f"{self.bit_depth} bit")
        elif self.bitrate and not exact:
            parts.append(self.bitrate_mode or "varying bitrate")
        elif self.bitrate:
            rate = f"{round(self.bitrate / 1000)} kbps"
            parts.append(f"{rate} {self.bitrate_mode}" if self.bitrate_mode else rate)
        return " · ".join(parts) or None


def average(qualities: Sequence[AudioQuality]) -> AudioQuality:
    """One quality standing for several files that share a `key`: the first,
    with its bitrate replaced by the mean of all of theirs.

    Where the bitrate is a setting, every file has the same one and this
    changes nothing. Where it varies, the mean is what tells a V0 album from a
    V2 one, and `key` had to leave it out. A plain mean over tracks, not weighted by duration: it's a
    summary for display, and a long track nudging it by a few kbps changes
    nothing a reader would act on.
    """
    first = qualities[0]
    rates = [q.bitrate for q in qualities if q.bitrate]
    return first._replace(bitrate=round(fmean(rates))) if rates else first


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
    mode = _mode(getattr(info, "bitrate_mode", None))
    return AudioQuality(
        sample_rate=getattr(info, "sample_rate", None),
        bit_depth=getattr(info, "bits_per_sample", None) if lossless else None,
        bitrate=None if lossless else getattr(info, "bitrate", None),
        bitrate_mode=mode,
        bitrate_varies=mode in _VARYING or isinstance(info, _AVERAGED),
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


#: The MP3 modes whose bitrate is an outcome of the audio rather than a setting.
_VARYING = frozenset({"VBR", "ABR"})

#: The formats whose bitrate mutagen reports as the file's own average, with no
#: mode to say whether it was a setting. Opus divides the file's size by its
#: length, and MP4 reads the encoder's average from the `esds` box, so two
#: tracks from one VBR encode report different numbers. Treating them as
#: varying means a CBR AAC album with one odd track at another bitrate isn't
#: flagged, because nothing in the file says it was CBR (#582).
#:
#: Vorbis is NOT here: mutagen reports the nominal bitrate from its header,
#: which the quality setting fixes, so tracks from one encode agree on it.
_AVERAGED = (MP4Info, OggOpusInfo)


def _mode(mode: Any) -> str | None:
    """One of "CBR" / "VBR" / "ABR", or None when the format doesn't say."""
    return _MODES.get(mode) if mode is not None else None

"""Your own drum kit: drum kit 13 (setting 337 = 13), fork firmware 113 on. Up to thirteen of the player's
own one-shots, built into one image by minicontrol (or anything else that follows the contract), sent to
the minichord over SysEx and kept in its flash, between the firmware and the presets, 256 kB of sound at
most. The contract is the fork's firmware/generator/user_kit.md: the image and its checks, the messages
and the .mckit file. user_kit.py beside this is the fork's generator/user_kit.py, its Python reference, as
it is (sync.py copies it; edit it there, then sync), so the checks and the messages here are the very ones
the firmware and minicontrol are tested against, and tests/kits holds the reference's test vectors.

This adds what minictl needs around them: a kit file read and checked before anything goes to the
minichord (a file that fails is never sent), what it holds in words, and the last kit sent from this
computer kept in ~/.cache/minictl/last-kit.mckit, to send again when a bigger firmware has moved where the
sound starts (INFO's reason 4) or the kit was cleared. The sending itself is device.py's (kit_upload)."""
import datetime, os, re

from . import user_kit as U

KIT_VERSION = 113            # the fork's firmware with the kit slot; stock and older fork firmware don't answer
KIT_SETTING = 337            # drum kit
OWN_KIT = 13                 # its "your own"
STAND_IN_KIT = 4             # what kit 13 plays while no kit is stored: the studio kit
DRUMS, HARP_DRUMS = 334, 343 # what plays a kit: the drums with the rhythm, and the harp's pads
ROOM = U.FILE_CAPACITY       # the most sound a minichord has room for (and what a file is checked against)


def plain(text, most=60):
    """a string from a .mckit's JSON, safe to print: no control characters, not too long"""
    text = re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", str(text)).strip()
    return text if len(text) <= most else text[:most - 1] + "…"


def voice_names(mask):
    """the voices with a hit, by name, from a filled-voices mask (bit v for voice v)"""
    return [U.VOICE_NAMES[v] for v in range(U.VOICES) if mask >> v & 1]


class Kit:
    """A kit file: a .mckit (the JSON about it, then the image verbatim), or a bare image as
    user_kit.py's vectors are (a file that starts MCK1). Nothing is checked on reading; problem() says
    whether it may be sent, reason() what check_image says of the image."""

    def __init__(self, image, meta=None, data=None, path=None):
        self.image = bytes(image)
        self.meta = meta
        self.data = data              # the .mckit's bytes as they were read, kept as they are
        self.path = path

    @classmethod
    def load(cls, path):
        with open(path, "rb") as f:
            data = f.read()
        if data[:4] != U.MCKIT_MAGIC:
            return cls(data, path=path)
        try:
            meta, image = U.read_mckit(data)
        except ValueError as e:          # a bad JSON or UTF-8 is a ValueError too
            raise ValueError(f"{path}: {e}")
        return cls(image, meta, data, path)

    def reason(self, capacity=ROOM):
        """check_image's reason, 0 when it passes: in a file data_base is 0, and the room a file's"""
        return U.check_image(self.image, 0, capacity)

    def problem(self):
        """None, or why this file can't be sent, in words: a file that is neither a .mckit nor an image
        (one starting MCK1) isn't taken for a broken kit"""
        if self.meta is None and self.image[:4] != U.MAGIC:
            return "not a kit file (.mckit): make one on minicontrol's kit page"
        r = self.reason()
        if not r:
            return None
        return f"it fails its check, reason {r}: {U.REASONS[r]}"

    @property
    def info(self):
        """what INFO would say of it once stored: size, crc, base_kit, filled and name (the image valid)"""
        i = U.image_info(self.image)
        return dict(i, name=i["name"].rstrip(b"\0").decode("ascii", "replace"))

    @property
    def sound(self):
        return len(self.image) - U.HEADER_BYTES

    @property
    def name(self):
        return self.info["name"]

    def describe(self, kit_names=None):
        """a few lines on what it holds, for before it's sent"""
        i = self.info
        base = kit_names.get(i["base_kit"]) if kit_names else None
        filled = voice_names(i["filled"])
        lines = [f"{i['name'] or '(no name)'}: {len(filled)} of {U.VOICES} voices, {kb(self.sound)} of sound"
                 + f", the rest from kit {i['base_kit']}" + (f" ({base})" if base else "")]
        lines.append("  " + ", ".join(filled) if filled else "  (no voices of its own)")
        m = self.meta or {}
        said = [f"{word} {plain(m[k])}" for k, word in (("author", "by"), ("licence", "licence"), ("made", "made"))
                if isinstance(m.get(k), str) and m.get(k)]
        if said:
            lines.append("  " + ", ".join(said))
        return lines

    def frames(self):
        """every message of its upload, BEGIN, the DATA pieces from offset 0, COMMIT"""
        return U.frames(self.image)

    def sources(self):
        """a .mckit's sources for an image that came without one, from its header: by voice, null for an
        empty voice, the voice's name for a filled one, and the earlier voice's again for a shared hit"""
        entries = U.read_header(self.image)["entries"]
        out = []
        for v, e in enumerate(entries):
            shared = next((u for u in range(v) if entries[u]["length"]
                           and (entries[u]["offset"], entries[u]["length"]) == (e["offset"], e["length"])), None)
            out.append(None if not e["length"] else out[shared] if shared is not None else U.VOICE_NAMES[v])
        return out

    def mckit(self):
        """the kit as a .mckit: the file's own bytes when it was one, or else one made around the image
        with what its header tells (the name, the voices filled)"""
        if self.data is not None:
            return self.data
        meta = self.meta or {"name": self.name, "author": "", "licence": "unknown", "sources": self.sources(),
                             "made": datetime.date.today().isoformat()}
        return U.write_mckit(meta, self.image)


def kb(n):
    return f"{n / 1024:.1f} kB" if n < 10240 else f"{n // 1024} kB"


# ---- the last kit sent -------------------------------------------------------------------------

def cache_path():
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "minictl", "last-kit.mckit")


def remember(kit):
    """keeps the kit just sent as the last one; its path, or None when it can't be written (that's no
    reason for the upload to fail)"""
    path = cache_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".part", "wb") as f:
            f.write(kit.mckit())
        os.replace(path + ".part", path)
    except OSError:
        return None
    return path


def last():
    """the last kit sent from this computer, or None: None too for a file there that fails its checks
    (cut short, or put there by something else), so what's said of it is always a kit that can be sent"""
    try:
        k = Kit.load(cache_path())
    except (OSError, ValueError, RecursionError):   # RecursionError: JSON nested past what Python reads
        return None
    return None if k.problem() else k

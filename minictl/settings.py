"""The minichord's settings by name, for the firmware a minichord runs: Ben Poilvé's stock firmware, or
the minichord fork's, which has every stock setting at the same address and a few hundred more.

Names, the describer and the instruction grammar always work in the fork's terms, the larger of the
two; for a stock minichord, fit() then leaves out what stock firmware doesn't have and keeps what's
left within stock's ranges, and says so. Preset codes, checks and clamping use the firmware's own
settings."""
import json, os, re, unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")

import sys                                             # noqa: E402
from .describer import preset_maker as pm               # noqa: E402
sys.modules.setdefault("preset_maker", pm)              # the describer imports it by its plain name
from .describer import interpret                        # noqa: E402
sys.modules.setdefault("interpret", interpret)

# the describer reads its files from the fork's checkout; here they travel with minictl
pm.PARAMETERS_JSON = os.path.join(DATA, "fork", "parameters.json")
pm.SHARED_PRESETS_JSON = os.path.join(DATA, "fork", "shared_presets.json")
pm.MAIN_CPP = os.path.join(DATA, "missing")
interpret.LOOKUP_H = os.path.join(DATA, "fork", "parameter_lookup.h")

with open(os.path.join(DATA, "firmware.json")) as _f:
    FIRMWARE = json.load(_f)
STOCK_VERSION = FIRMWARE["stock"]["version"]
PROFILES = ("fork", "stock")

BANKS = 12
NAME_FIRST, NAME_SLOTS = 290, 12
RHYTHM_FIRST, RHYTHM_LAST = 220, 235
TARGETS = set(pm.KNOB_TARGETS) | set(pm.TAP_TARGETS)
# Stock firmware drops a write whose address's low byte is 0x7E or 0x7F, taking it for a universal
# SysEx message, so these can't be set over USB there (the fork tells them apart, fbb887f)
STOCK_UNWRITABLE = {126, 127, 254, 255}

# Names for the controls, shorter than parameters.json's (the generated name stays an alias)
RENAMES = {10: "chord-knob-alt", 11: "chord-knob-alt-range", 12: "harp-knob-alt", 13: "harp-knob-alt-range",
           14: "mod-knob", 15: "mod-knob-range", 16: "mod-knob-alt", 17: "mod-knob-alt-range",
           249: "hover", 250: "hover-value", 251: "hover-reach", 325: "hover-alt", 326: "hover-alt-value",
           200: "double-tap", 201: "double-tap-value", 209: "double-tap-2", 210: "double-tap-value-2",
           211: "double-tap-3", 212: "double-tap-value-3", 4: "chord-knob-memory", 5: "harp-knob-memory",
           6: "mod-knob-memory"}
ALIASES = {"key": 35, "volume-chords": 3, "volume-harp": 2, "bpm": None, "tempo": None}


def profile_for(version, has_page1):
    """Which firmware a minichord runs, from the version it reports (address 7) and whether it answered
    for page 1. Stock reports 9 and below; the fork reports 10 and up, with page 1 from 31. A stock
    firmware past 30 would answer no page 1, so it reads as stock too."""
    if version <= STOCK_VERSION:
        return "stock"
    if version >= 31 and not has_page1:
        return "stock"
    return "fork"


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower().replace("#", " sharp ")).strip("-")


def load_params(path):
    """parameters.json as preset_maker reads it: by address, with its section and controls"""
    with open(path) as f:
        d = json.load(f)
    params = {}
    for key, section in pm.SECTIONS.items():
        for p in d.get(key, []):
            p = dict(p, section=section)
            p["controls"] = p.get("controls") or "all"
            params[p["sysex_adress"]] = p
    return params


class Settings:
    def __init__(self, profile="fork"):
        if profile not in PROFILES:
            raise ValueError(f"firmware is stock or fork, not {profile!r}")
        self.profile = profile
        self.fork = load_params(os.path.join(DATA, "fork", "parameters.json"))
        self.params = self.fork if profile == "fork" else load_params(os.path.join(DATA, "stock", "parameters.json"))
        self.version = FIRMWARE[profile]["version"]
        self._it = interpret.Interpreter(self.fork)
        with open(os.path.join(DATA, "options.json")) as f:
            options = {int(a): {int(v): label for v, label in o.items()} for a, o in json.load(f).items()}
        # a stock setting offers the choices its own range reaches
        self.options = {a: {v: label for v, label in o.items() if a not in self.params or v <= self.params[a]["max_value"]}
                        for a, o in options.items()}
        self.names, self.aliases = {}, {}
        for a in sorted(self.fork):
            name = slug(self._it.setting_name(a))
            if RHYTHM_FIRST <= a <= RHYTHM_LAST:
                name = f"rhythm-step-{a - RHYTHM_FIRST + 1}"
            self.names[a] = RENAMES.get(a, name)
            self.aliases[name] = a
        self.by_name = {n: a for a, n in self.names.items()}
        for n, a in ALIASES.items():
            if a is not None:
                self.aliases[n] = a
        tempo = [a for a, p in self.fork.items() if p["name"].strip().lower() in ("bpm", "tempo", "rhythm bpm")]
        if len(tempo) == 1:
            self.aliases["bpm"] = self.aliases["tempo"] = tempo[0]

    @property
    def stock(self):
        return self.profile == "stock"

    def has(self, a, version):
        """whether a minichord on this firmware version has setting a"""
        p = self.params.get(a)
        return p is not None and (self.stock or p.get("introduction_version", 0) <= version)

    # ---- naming ----

    def title(self, a):
        """the long, readable name: section / group / name"""
        p = self.params.get(a) or self.fork.get(a)
        return pm.label(p) if p else f"address {a}"

    def find(self, word):
        """A setting from what was typed: an address, a name (reverb-size, "reverb size"), or the start of
        one if only one name starts so. Returns the address, or raises ValueError listing the candidates."""
        a = self._find(word)
        if a not in self.params and a in self.fork:
            raise ValueError(f"{self.names[a]} is the minichord fork's; stock firmware {STOCK_VERSION} doesn't have it")
        return a

    def writable(self, a):
        """why setting a can't be written over USB on this firmware, or None"""
        if self.stock and a in STOCK_UNWRITABLE:
            return (f"stock firmware can't take {self.names.get(a, a)} over USB: it reads a write to {a} as a "
                    f"universal SysEx message and drops it (the fork fixed this)")
        return None

    def _find(self, word):
        w = word.strip()
        if re.fullmatch(r"\d+", w):
            a = int(w)
            if not 0 <= a < pm.PARAMETER_SIZE:
                raise ValueError(f"{a} isn't an address (0-{pm.PARAMETER_SIZE - 1})")
            return a
        s = slug(w)
        if s in self.by_name:
            return self.by_name[s]
        if s in self.aliases:
            return self.aliases[s]
        # the start of a name, then whole words of one ("key" is chord-key-signature, not a keytrack),
        # then any part of one; this firmware's own settings first
        mine = {n: a for n, a in self.by_name.items() if a in self.params}
        for pool in (mine, self.by_name):
            hits = [n for n in pool if n.startswith(s)] or \
                   [n for n in pool if f"-{s}-" in f"-{n}-"] or [n for n in pool if s in n]
            if len(hits) == 1:
                return pool[hits[0]]
            if hits:
                shown = ", ".join(sorted(hits)[:12]) + (" ..." if len(hits) > 12 else "")
                raise ValueError(f"{word!r} could be: {shown}")
        raise ValueError(f"no setting called {word!r} (`minictl list {s.split('-')[0]}` searches the names)")

    def search(self, words):
        """the settings whose names hold every word, then those whose help does"""
        ws = [slug(w) for w in words]
        named, helped = [], []
        for a in sorted(self.params):
            n, p = self.names.get(a, str(a)), self.params[a]
            if all(w in f"{n} {slug(p['group'])}" for w in ws):
                named.append(a)
            elif all(w in slug(p["tooltip"]) for w in ws):
                helped.append(a)
        return named + helped

    # ---- values ----

    def shown(self, a, stored, values=None):
        """a stored value in the units a person reads: Hz as Hz, floats as floats, choices by name"""
        p = self.params.get(a)
        if p is None:
            return str(stored)
        if a in TARGETS:
            return "unused" if stored == 0 or stored not in self.fork else self.names[stored]
        if p.get("follows_target") is not None and values is not None:
            t = self.params.get(values[p["follows_target"]])
            if t is not None:
                return self.shown(t["sysex_adress"], stored) if t["sysex_adress"] not in TARGETS else str(stored)
        if a in self.options and stored in self.options[a]:
            label = self.options[a][stored]
            return label if label == str(stored) else f"{stored} ({label})"
        h = pm.to_human(p, stored)
        return f"{h:g}" if isinstance(h, float) else str(h)

    def parse(self, a, text, values):
        """What was typed for setting a, in its own units, as the number preset_maker's checks take: a
        choice by its name, a target setting by its name, a float as a float."""
        t = text.strip()
        if a in TARGETS:
            if t.lower() in ("0", "none", "unused", "off", "nothing"):
                return 0
            return self.find(t)
        p = self.params.get(a)
        if p is not None and p.get("follows_target") is not None:
            target = values[p["follows_target"]] if values else None
            if target in self.options:
                a = target
        if self.options.get(a) and not re.fullmatch(r"-?\d+(\.\d+)?", t):
            low = t.lower()
            hits = [v for v, label in self.options[a].items() if label.lower() == low] or \
                   [v for v, label in self.options[a].items() if label.lower().startswith(low)]
            if len(hits) == 1:
                return hits[0]
            choices = "; ".join(f"{v} {label}" for v, label in self.options[a].items())
            raise ValueError(f"{self.names.get(a, a)} takes one of: {choices}")
        if t.lower() in ("on", "yes", "true"):
            return 1
        if t.lower() in ("off", "no", "false"):
            return 0
        try:
            v = float(t)
            return int(v) if v.is_integer() else v
        except ValueError:
            raise ValueError(f"{t!r} isn't a value for {self.names.get(a, a)}")

    def describe_range(self, a):
        p = self.params[a]
        if a in TARGETS:
            return "the name of a setting, or none"
        if self.options.get(a):
            return "; ".join(f"{v} {label}" for v, label in self.options[a].items())
        unit = "" if p["data_type"] == "int" else " (decimal)"
        return f"{p['min_value']:g} to {p['max_value']:g}{unit}, default {p['default_value']:g}"

    # ---- stock firmware: the fork's terms fitted to it ----

    def full(self, values):
        """a preset's values as the fork's 512, filling what this firmware doesn't keep with the fork's
        defaults, for the describer and the grammar to work over"""
        out = list(values) + [0] * (pm.PARAMETER_SIZE - len(values))
        for a, p in self.fork.items():
            if a >= len(values) or (self.stock and a not in self.params):
                out[a] = pm.stored_default(p)
        return out

    def fit(self, before, after):
        """For stock firmware: the changes from before to after that it can take, within its ranges.
        Returns (values, notes); the fork takes everything."""
        if not self.stock:
            return after, []
        out, left, notes = list(after), [], []
        for a in range(len(after)):
            if after[a] == before[a]:
                continue
            p = self.params.get(a)
            if p is None or a in STOCK_UNWRITABLE:
                if a in self.fork and self.fork[a]["group"] != "hidden":
                    left.append(self.names[a])
                out[a] = before[a]
                continue
            if a in TARGETS:
                if after[a] and (after[a] not in self.params or after[a] > p["max_value"]):
                    notes.append(f"{self.names[a]} can't go to {self.names.get(after[a], after[a])} on stock firmware")
                    out[a] = before[a]
                continue
            lo, hi = pm.stored_bounds(p)
            if not lo <= after[a] <= hi:
                out[a] = max(lo, min(hi, after[a]))
                notes.append(f"{self.names[a]} goes to {self.shown(a, out[a])} at most on stock firmware")
        if left:
            notes.insert(0, f"left out, as stock firmware {STOCK_VERSION} doesn't have them or can't take them "
                            f"over USB: " + ", ".join(left))
        return out, notes

    def clamp(self, values):
        """a whole preset within this firmware's ranges: a control aimed at a setting it doesn't have
        does nothing"""
        out = list(values)
        if not self.stock:
            return out
        for a, p in self.params.items():
            if a >= len(out) or out[a] is None or a in (0, 1, 7):
                continue
            if a in TARGETS:
                if out[a] and (out[a] not in self.params or out[a] > p["max_value"]):
                    out[a] = 0
                continue
            lo, hi = pm.stored_bounds(p)
            out[a] = max(lo, min(hi, out[a]))
        return out

    # ---- names kept in a preset (fork firmware 50 and on) ----

    @staticmethod
    def preset_name(values):
        out = ""
        for k in range(NAME_SLOTS):
            v = values[NAME_FIRST + k] if NAME_FIRST + k < len(values) else 0
            for c in (v & 127, (v >> 7) & 127):
                if c < 32:
                    return out.strip()
                out += chr(c)
        return out.strip()

    @staticmethod
    def set_preset_name(values, name):
        s = unicodedata.normalize("NFD", name)
        s = "".join(c for c in s if 32 <= ord(c) < 127)
        s = re.sub(r"\s+", " ", s).strip()[:2 * NAME_SLOTS]
        for k in range(NAME_SLOTS):
            first = ord(s[2 * k]) if 2 * k < len(s) else 0
            second = ord(s[2 * k + 1]) if 2 * k + 1 < len(s) else 0
            values[NAME_FIRST + k] = first + 128 * second if first else 0

    # ---- codes and the describer ----

    def decode(self, code):
        return pm.decode(code, self.params)

    def encode(self, values):
        return pm.encode(values, self.params)

    def defaults(self):
        return pm.default_values(self.params, self.version)

    def apply(self, values, changes):
        """[(address, value in its units)] onto a copy of values, checked and clamped as preset_maker does.
        Returns (new values, [(address, old, new)], notes)."""
        return pm.apply_changes(values, [{"address": a, "value": v} for a, v in changes], self.params)

    def conflicts(self, values):
        """the setting conflicts that hold in a preset, from the describer's CONFLICTS table"""
        full = self.full(values)
        # in the description's own units, as the CONFLICTS rows are written (describe.js's conflicts() too)
        state = {a: pm.to_human(self.fork[a], full[a]) if a in self.fork else full[a] for a in range(len(full))}
        return interpret.conflict_notes(self.fork, self._it.selectors, {}, state, self._it.setting_name)

    def shared_presets(self):
        with open(os.path.join(DATA, self.profile, "shared_presets.json")) as f:
            return json.load(f)["shared_presets"]

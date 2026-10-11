"""Tunings: a Scala file into the fork's own temperament (firmware 67, settings 359-370), and any of
the minichord's temperaments out as one. Kept with minicontrol's tuning.js, which reads Scala files the
same way and holds the same table of the firmware's temperaments (include/temperament_profiles.h)."""
import math, re

OWN_TEMPERAMENT = 13
OWN_FIRST = 359          # own temperament C; B is 370, in tenths of a cent from 1000
OWN_VERSION = 67
NAMES = ["C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"]
# offsets from equal temperament for C, C# ... B, in cents; 10-12 divide the octave instead
TEMPERAMENT_OFFSETS = [
    [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    [10, -14, 3, 21, -3, 14, -10, 7, -17, 0, 17, -7],
    [16, 27, 20, 31, 2, 14, 6, 18, 29, 0, 12, 4],
    [-6, 8, -2, -12, 2, -8, 6, -4, 10, 0, -10, 4],
    [12, 2, 4, 6, 2, 10, 0, 8, 4, 0, 8, 4],
    [10, 0, 3, 4, -3, 8, 0, 7, 2, 0, 6, -1],
    [6, 0, 2, 4, -2, 8, -2, 4, 2, 0, 6, -4],
    [6, -4, 2, 0, -2, 4, -6, 4, -2, 0, 2, -4],
    [8, -2, 3, 2, -3, 6, -4, 5, 0, 0, 4, -1],
    [5, -7, 2, 10, -2, 7, -5, 3, -8, 0, 8, -3],
]
DIVISIONS = {10: 19, 11: 24, 12: 31}
TEMPERAMENT_NAMES = ["equal", "quarter-comma meantone", "five-limit just (C major)", "pythagorean",
                     "werckmeister III", "kirnberger III", "vallotti", "young no. 2", "kellner",
                     "sixth-comma meantone", "19-EDO", "24-EDO (quarter tones)", "31-EDO", "your own"]


def js_round(x):
    return math.floor(x + 0.5)


def parse_scala(text):
    """A .scl: comment lines start with "!", then a description, the number of notes, and each note
    above the first, in cents (with a point) or as a ratio, the last the period. Returns (name, the
    twelve notes' offsets from equal temperament in cents, C first)."""
    lines = [l for l in re.split(r"\r?\n", text) if not l.strip().startswith("!")]
    try:
        count = int(lines[1].strip().split()[0]) if len(lines) > 1 and lines[1].strip() else None
    except ValueError:
        count = None
    if count != 12:
        raise ValueError(f"it has {'no' if count is None else count} notes to the octave; your own temperament takes "
                         f"twelve (for 19, 24 and 31 choose them as the temperament)")
    cents = []
    for i in range(12):
        line = lines[2 + i] if 2 + i < len(lines) else ""
        token = line.strip().split()[0] if line.strip() else ""
        if not token:
            raise ValueError("it ends before its twelfth note")
        try:
            if "." in token:
                c = float(token)
            elif "/" in token:
                a, b = (float(x) for x in token.split("/"))
                c = 1200 * math.log2(a / b)
            else:
                c = 1200 * math.log2(float(token))
        except (ValueError, ZeroDivisionError):
            raise ValueError(f'"{token}" isn\'t a pitch')
        if not math.isfinite(c):
            raise ValueError(f'"{token}" isn\'t a pitch')
        cents.append(c)
    if abs(cents[11] - 1200) > 0.5:
        raise ValueError(f"its period is {cents[11]:.1f} cents, not an octave")
    return lines[0].strip() if lines else "", [c - 100 * k for k, c in enumerate([0] + cents[:11])]


def check(offsets):
    """the offsets the minichord can hold, or why not"""
    for k, c in enumerate(offsets):
        if abs(c) > 100:
            raise ValueError(f"its {NAMES[k]} is {c:.1f} cents from equal, past the 100 the minichord holds: "
                             f"is its first note C?")


def stored(offsets):
    """the twelve offsets as settings 359-370 keep them"""
    return {OWN_FIRST + k: js_round(c * 10) + 1000 for k, c in enumerate(offsets)}


def own_offsets(values):
    return [(values[OWN_FIRST + k] - 1000) / 10 for k in range(12)]


def offsets_of(values):
    """the live temperament's twelve offsets from equal, or None for a divided octave"""
    t = values[237] if len(values) > 237 else 0
    if t == OWN_TEMPERAMENT and len(values) > OWN_FIRST + 11:
        return own_offsets(values)
    if 0 <= t < len(TEMPERAMENT_OFFSETS):
        return [float(c) for c in TEMPERAMENT_OFFSETS[t]]
    return None


def to_scala(name, values):
    """the live tuning as a .scl"""
    t = values[237] if len(values) > 237 else 0
    lines = [f"! {re.sub(r'[^A-Za-z0-9._-]+', '-', name).strip('-') or 'minichord'}.scl", "!",
             f"{name}"]
    if t in DIVISIONS:
        n = DIVISIONS[t]
        lines += [f" {n}", "!"] + [f" {1200 * k / n:.5f}" for k in range(1, n + 1)]
    else:
        off = offsets_of(values)
        lines += [" 12", "!"] + [f" {100 * k + off[k]:.5f}" for k in range(1, 12)] + [" 2/1"]
    return "\n".join(lines) + "\n"

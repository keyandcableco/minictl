"""minictl: the minichord from the command line. `minictl help` lists the commands."""
import argparse, datetime, json, os, re, sys, time

from . import device as dev, kit
from .settings import (Settings, BANKS, RHYTHM_FIRST, RHYTHM_LAST, pm, interpret, profile_for, FIRMWARE,
                       STOCK_VERSION, STOCK_UNWRITABLE)

S = {}            # Settings by firmware, loaded once each
PROFILE = None    # "stock" or "fork": from --firmware or MINICTL_FIRMWARE, or else the minichord says
FORCED = False


def settings():
    """the settings of the firmware in play: the minichord's once one is open, the fork's before"""
    p = PROFILE or "fork"
    if p not in S:
        S[p] = Settings(p)
    return S[p]


def needs_fork(what, d, version=0):
    if PROFILE == "stock":
        raise RuntimeError(f"{what} needs the minichord fork's firmware; this one runs stock firmware {d.version}")
    if d.version < version:
        raise RuntimeError(f"{what} needs firmware {version} or later; this one has {d.version}")


# ---- helpers ---------------------------------------------------------------------------------------

def confirm(question, args):
    """a yes from the person, or --yes; refuses when nobody is there to ask"""
    if args.yes:
        return True
    if not sys.stdin.isatty():
        raise RuntimeError(f"{question} Pass --yes to go ahead without asking.")
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


def bank_arg(text):
    try:
        b = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} isn't a bank number")
    if not 1 <= b <= BANKS:
        raise argparse.ArgumentTypeError(f"banks go from 1 to {BANKS}")
    return b - 1


def banks_arg(text):
    out = []
    for part in text.split(","):
        if "-" in part:
            a, b = part.split("-", 1)
            out += range(bank_arg(a), bank_arg(b) + 1)
        else:
            out.append(bank_arg(part))
    return sorted(set(out))


def wait_for_other_upload(d):
    """another program sending the minichord a kit (INFO's state 2, or its replies heard): waited out, and
    INFO's reply after (said on stderr, so --json's output stays JSON)"""
    print("another program is sending the minichord a kit (an upload is open): waiting for it to end",
          file=sys.stderr, flush=True)
    d.kit_wait_others()
    info = d.kit_info()
    if info is None or info["state"] == 2:
        raise RuntimeError("another program's kit upload is still open on the minichord: try again once it ends")
    return info


def upload_waited_out(d):
    """A minichord taking another program's kit (fork firmware 113) answers nothing but the kit's messages
    until that ends, so when it doesn't answer for its settings INFO is asked, and an upload open waited
    out. Whether to ask for the settings again: not when INFO goes unanswered too (no kit slot there)."""
    info = d.kit_info()
    if info is None:
        return False
    if info["state"] == 2:
        wait_for_other_upload(d)
    return True


def open_device(args, unanswered=upload_waited_out):
    """the minichord, open and read once, so which firmware it runs is known; unanswered(d), when it
    doesn't answer, says whether to ask again (by default: once another program's kit upload has been
    waited out)"""
    global PROFILE
    d = dev.Minichord(args.port)
    # another program's upload heard later, in the middle of the command: what minictl sends waits it out
    d.on_others_wait = lambda: print("another program is sending the minichord a kit: waiting for it to end, "
                                     "then going on", file=sys.stderr, flush=True)
    try:
        try:
            d.read()
        except RuntimeError:
            if not unanswered or not unanswered(d):
                raise
            d.read()
    except Exception:
        d.close()
        raise
    if not FORCED:
        PROFILE = profile_for(d.version, d.has_page1)
    return d


def page1_defaults():
    s = settings()
    return {a: pm.stored_default(p) for a, p in s.params.items() if a >= dev.PAGE}


def print_json(x):
    json.dump(x, sys.stdout, indent=1)
    print()


def preset_code_from(text):
    """a code typed, or a file holding one, or a shared preset's name"""
    s = settings()
    if text == "defaults":
        return s.defaults(), "a plain starting point"
    for p in s.shared_presets():
        if p["name"].lower() == text.lower():
            return s.decode(p["value"]), p["name"]
    if os.path.isfile(text):
        with open(text) as f:
            text = f.read()
    return s.decode(text), "the code given"


def walk_banks(d, which, show=None):
    """each bank's stored settings, loading them in turn, then back to the bank it was on"""
    if PROFILE == "stock" and d.version < 9:
        raise RuntimeError(f"reading banks needs stock firmware 9 or later (it loads them by SysEx); this one has {d.version}")
    start = d.read()[1]
    out = {}
    try:
        for b in which:
            out[b] = d.load_bank(b)
            if show:
                show(b, out[b])
    finally:
        d.load_bank(start)
    return out


NOTE = re.compile(r"([A-Ga-g])([#b]?)(-?\d)?$")


def note_number(text):
    if re.fullmatch(r"\d+", text):
        return int(text)
    m = NOTE.match(text)
    if not m:
        raise ValueError(f"{text!r} isn't a note (C4, Eb3, F#5 or a MIDI number)")
    n = "C D EF G A B".index(m.group(1).upper())
    n += {"#": 1, "b": -1, "": 0}[m.group(2)]
    return 12 * (int(m.group(3) or 4) + 1) + n


# ---- the commands ----------------------------------------------------------------------------------

def cmd_ports(args):
    outs, ins = dev.list_ports()
    print("MIDI out:", *(f"  {n}" for n in outs or ["(none)"]), sep="\n")
    print("MIDI in:", *(f"  {n}" for n in ins or ["(none)"]), sep="\n")
    i = dev.pick_port(outs, 1, args.port)
    print(f"\nminictl would use: {outs[i] if i is not None else 'nothing (no minichord found)'}")
    try:
        sp = dev.serial_port()
        print(f"serial: {sp or 'none'}")
    except RuntimeError as e:
        print(f"serial: {e}")


def cmd_info(args):
    with open_device(args) as d:
        v = d.read()
    s = settings()
    info = {"port": d.port_name, "firmware": v[7], "family": s.profile, "bank": v[1] + 1,
            "name": s.preset_name(v) if s.profile == "fork" else "",
            "settings": sum(1 for a in s.params if s.has(a, v[7])),
            "minictl_knows": s.version}
    if args.json:
        return print_json(info)
    print(f"minichord on {d.port_name}")
    if s.stock:
        print(f"stock firmware {v[7]}, {len(s.params)} settings" +
              (f" (minictl knows stock up to {s.version})" if v[7] > s.version else ""))
    else:
        print(f"the minichord fork's firmware {v[7]}, {sum(1 for a in s.params if s.has(a, v[7]))} settings" +
              (f" (minictl knows up to {s.version}: settings past that have no name here)" if v[7] > s.version else ""))
    print(f"bank {v[1] + 1}" + (f": {info['name']}" if info["name"] else ""))


def cmd_list(args):
    s = settings()
    hits = s.search(args.words) if args.words else sorted(s.params)
    if not args.all:
        hits = [a for a in hits if s.params[a]["group"] != "hidden" and a not in (0, 1, 7)]
    if args.json:
        return print_json([{"address": a, "name": s.names[a], "title": s.title(a), "range": s.describe_range(a),
                            "help": s.params[a]["tooltip"].strip()} for a in hits])
    for a in hits:
        print(f"{a:4} {s.names[a]:40} {s.describe_range(a)}")
        if args.verbose:
            print(f"     {s.params[a]['tooltip'].strip()}\n")
    if not hits:
        print("nothing matches")


def cmd_get(args):
    if args.code:
        values, _ = preset_code_from(args.code)
    elif args.bank is not None:
        with open_device(args) as d:
            values = walk_banks(d, [args.bank])[args.bank]
    else:
        with open_device(args) as d:
            values = d.read()
    s = settings()
    if args.settings:
        which, missing = [], []
        for w in args.settings:
            try:
                which.append(s.find(w))
            except ValueError as e:
                missing.append(str(e))
        if not which:
            raise ValueError("; ".join(missing))
        for m in missing:
            print(f"! {m}", file=sys.stderr)
    else:
        which = [a for a in sorted(s.params) if a < len(values) and s.params[a]["group"] != "hidden" and s.has(a, values[7])
                 and (args.all or values[a] != pm.stored_default(s.params[a]))]
        if args.all:
            which = sorted(s.params)
    which = [a for a in which if a < len(values)]
    if args.json:
        return print_json({s.names.get(a, str(a)): {"address": a, "stored": values[a],
                                                     "value": s.shown(a, values[a], values)} for a in which})
    if args.raw:
        for a in which:
            print(values[a])
        return
    for a in which:
        print(f"{a:4} {s.names.get(a, '?'):40} {s.shown(a, values[a], values)}")
    if not args.settings and not args.all:
        print(f"({len(which)} settings away from their defaults; --all shows every one)")


def cmd_set(args):
    if len(args.pairs) % 2:
        raise RuntimeError("set takes a setting and a value, as many pairs as you like")
    with open_device(args) as d:
        values = d.read()
        s = settings()
        changes, raw = [], []
        for name, text in zip(args.pairs[::2], args.pairs[1::2]):
            a = s.find(name)
            if s.writable(a):
                raise RuntimeError(s.writable(a))
            if args.raw:
                raw.append((a, int(text)))
            else:
                changes.append((a, s.parse(a, text, values)))
        after, made, notes = s.apply(values, changes)
        for a, v in raw:
            if a in dev.RESERVED or a == 0:
                raise RuntimeError(f"{a} can't be written")
            if a >= len(values):
                raise RuntimeError(f"this firmware has no setting {a}")
            made.append((a, after[a], v))
            after[a] = v
        for n in notes:
            print(f"! {n}")
        for a, old, new in made:
            d.set(a, new)
            print(f"{s.names.get(a, a)}: {s.shown(a, old, values)} -> {s.shown(a, new, after)}")
        if not made:
            print("nothing changed")
        known = set(s.conflicts(values)) if made else set()
        for n in s.conflicts(after) if made else []:
            if n not in known:
                print(f"  ! {n}")
        if args.save and made:
            b = values[1]
            if confirm(f"Save to bank {b + 1}, replacing what is stored there?", args):
                d.save(b)
                print(f"saved to bank {b + 1}")
        elif made:
            print("(live only: `minictl save` keeps it)")


def cmd_bank(args):
    with open_device(args) as d:
        v = d.load_bank(args.bank) if args.bank is not None else d.read()
    s = settings()
    name = s.preset_name(v)
    print(f"bank {v[1] + 1}" + (f": {name}" if name else ""))


def cmd_banks(args):
    rows = []

    def show(b, v):
        s = settings()
        rows.append({"bank": b + 1, "name": s.preset_name(v), "code": s.encode(v)})
        if not args.json:
            line = f"{b + 1:3}  {rows[-1]['name'] or '(no name)'}"
            print(line + (f"\n     {rows[-1]['code']}" if args.codes else ""), flush=True)

    with open_device(args) as d:
        walk_banks(d, range(BANKS), show)
    if args.json:
        print_json(rows)


def cmd_save(args):
    with open_device(args) as d:
        v = d.read()
        b = v[1] if args.bank is None else args.bank
        if confirm(f"Save the live settings to bank {b + 1}, replacing what is stored there?", args):
            d.save(b)
            print(f"saved to bank {b + 1}")


def cmd_reset(args):
    with open_device(args) as d:
        if confirm(f"Put bank {args.bank + 1} back to its factory preset? What is stored there is lost.", args):
            d.reset(args.bank)
            print(f"bank {args.bank + 1} is its factory preset again, and playing")


def cmd_name(args):
    with open_device(args) as d:
        v = d.read()
        s = settings()
        needs_fork("a preset name", d, 50)
        if not args.name:
            print(s.preset_name(v) or "(no name)")
            return
        after = list(v)
        s.set_preset_name(after, " ".join(args.name))
        for a in range(290, 302):
            if after[a] != v[a]:
                d.set(a, after[a])
        print(f"named {s.preset_name(after)!r} (live: `minictl save` keeps it)")


def cmd_backup(args):
    with open_device(args) as d:
        s = settings()

        def show(b, v):
            print(f"bank {b + 1}: {s.preset_name(v)}", file=sys.stderr, flush=True)
        got = walk_banks(d, range(BANKS), show)
    banks = [{"bank": b, "name": s.preset_name(v) if not s.stock else "", "values": v} for b, v in sorted(got.items())]
    data = {"minichord_backup": 1, "created": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "firmware_version": banks[0]["values"][7], "parameter_size": len(banks[0]["values"]),
            "address_names": {str(a): s.title(a) for a in sorted(s.params)}, "banks": banks}
    path = args.file or f"minichord-backup-{datetime.date.today()}.json"
    with open(path, "w") as f:
        json.dump(data, f, indent=1)
    print(f"{len(banks)} banks -> {path}")


def backup_problem(data, size):
    """minicontrol's checks: a damaged file is refused before anything is written"""
    if not isinstance(data, dict) or not data.get("minichord_backup"):
        return "that isn't a minichord backup file"
    banks = data.get("banks")
    if not isinstance(banks, list) or not banks or len(banks) > BANKS:
        return "that backup has no banks in it"
    seen = set()
    for e in banks:
        b = e.get("bank") if isinstance(e, dict) else None
        if not isinstance(b, int) or not 0 <= b < BANKS or b in seen:
            return "that backup's bank numbers don't make sense"
        seen.add(b)
        vals = e.get("values")
        if not isinstance(vals, list) or len(vals) > size or \
                not all(v is None or (isinstance(v, int) and 0 <= v < 16384) for v in vals):
            return f"bank {b + 1} in that backup is damaged"
    return None


def cmd_restore(args):
    with open(args.file) as f:
        data = json.load(f)
    with open_device(args) as d:
        live = d.read()
        s = settings()
        problem = backup_problem(data, pm.PARAMETER_SIZE)
        if problem:
            raise RuntimeError(f"{problem}")
        entries = [e for e in data["banks"] if args.banks is None or e["bank"] in args.banks]
        if not entries:
            raise RuntimeError("none of those banks are in the file")
        fw = data.get("firmware_version")
        print(f"{args.file}: {len(entries)} banks" + (f", from firmware {fw}" if fw else "") +
              f"; the minichord has firmware {live[7]}")
        for e in entries:
            print(f"  bank {e['bank'] + 1}: {e.get('name') or ''}")
        if not confirm(f"Write these {len(entries)} banks, replacing what each holds?", args):
            return
        start = live[1]

        def write(e):
            values = list(e["values"])
            # Sound Lab test firmware kept the master tuning at 255, not 109
            if len(values) > 255 and not values[109] and values[255] and 4320 <= values[255] <= 4460:
                values[109] = values[255]
            # a backup from before names were kept in the bank brings its name along
            if live[7] >= 50 and e.get("name") and len(values) > 301 and not s.preset_name(values):
                s.set_preset_name(values, e["name"])
            if s.stock:     # a backup from the fork's firmware: what stock keeps, within its ranges
                values = s.clamp(values)
            d.load_bank(e["bank"])
            time.sleep(0.15)
            d.apply(values, page1_defaults())
            time.sleep(0.04)
            d.save(e["bank"])
            print(f"bank {e['bank'] + 1} written", flush=True)

        def differs(e):
            got = d.load_bank(e["bank"])
            want = s.clamp(e["values"]) if s.stock else e["values"]
            return [a for a, v in enumerate(want) if a not in (0, 1, 7) and a not in dev.RESERVED and not s.writable(a)
                    and not d.leaves_alone(a) and v is not None and a < len(got) and got[a] != v
                    and not (a == 109 and not v) and not (290 <= a < 302 and not v)]

        try:
            # Another program's kit upload heard meanwhile was waited out, but what reached the minichord in
            # the moment before it was heard was dropped: the banks that then differ go again
            todo, diffs = entries, {}
            for _round in range(3):
                seen = d.kit_others_seen
                for e in todo:
                    write(e)
                diffs.update((e["bank"], differs(e)) for e in todo)
                todo = [e for e in todo if diffs[e["bank"]]]
                if not todo or d.kit_others_seen == seen:
                    break
                print(f"another program sent the minichord a kit while the banks were written, and the minichord "
                      f"dropped some of what was sent: writing {'bank' if len(todo) == 1 else 'banks'} "
                      f"{', '.join(str(e['bank'] + 1) for e in todo)} again", flush=True)
            bad = 0
            for e in entries:
                diff = diffs[e["bank"]]
                if diff:
                    bad += 1
                    print(f"bank {e['bank'] + 1}: {len(diff)} settings differ from the file, first "
                          + ", ".join(s.names.get(a, str(a)) for a in diff[:6]))
            print("every bank matches the file" if not bad else f"{bad} banks differ")
            left = sorted(a for a in (d.locks or set()) if a not in dev.INSTRUMENT)
            if left:
                print(f"(left as the minichord's own, being locked: {', '.join(s.names.get(a, str(a)) for a in left)})")
            if s.stock:
                print(f"(not checked: {', '.join(s.names.get(a, str(a)) for a in sorted(STOCK_UNWRITABLE))}, which stock "
                      f"firmware can't take over USB)")
        finally:
            d.load_bank(start)


def cmd_code(args):
    if args.code:
        with open_device(args) as d:
            live = d.read()
            s = settings()
            values, what = preset_code_from(args.code)
            if s.stock:
                values, notes = s.fit(s.full(live), s.full(s.clamp(values)))
                for n in notes:
                    print(f"! {n}")
            d.apply(values, page1_defaults())
            d.command(0)
            print(f"playing {what} on the minichord (live: `minictl save` keeps it)")
        return
    with open_device(args) as d:
        v = walk_banks(d, [args.bank])[args.bank] if args.bank is not None else d.read()
    print(settings().encode(v))


def cmd_describe(args):
    d = None
    try:
        if args.base in (None, "live"):
            try:
                d = open_device(args)
                values, base_name = d.read(), "the minichord's live preset"
            except dev.NotFound:
                if args.base == "live":
                    raise
                values, base_name = settings().defaults(), "a plain starting point"
        elif args.base == "defaults":
            values, base_name = settings().defaults(), "a plain starting point"
        else:
            values, base_name = preset_code_from(args.base)
        s = settings()
        presets = s.shared_presets()
        names = [p["name"] for p in presets]
        # the describer works in the fork's terms; stock firmware takes what it has of the result
        values = s.full(values)
        values[7] = FIRMWARE["fork"]["version"]
        text = " ".join(args.text)
        result = interpret.interpret(text, values, s.fork, names, first=True)
        if result.base:
            base = next(p for p in presets if p["name"] == result.base)
            values, base_name = s.full(s.decode(base["value"])), result.base
            values[7] = FIRMWARE["fork"]["version"]
            result = interpret.interpret(text, values, s.fork, names, first=True)
        after, _, notes = pm.apply_changes(values, result.changes, s.fork)
        after, fitted = s.fit(values, after)
        notes += fitted
        values[7] = after[7] = s.version
        made = [(a, values[a], after[a]) for a in range(len(after)) if after[a] != values[a] and a != 7]
        if args.json:
            return print_json({"base": base_name, "understood": result.understood, "unknown": result.unknown,
                               "heard_as": result.heard_as, "notes": result.notes + notes,
                               "changes": [{"name": s.names.get(a), "address": a, "from": o, "to": n}
                                           for a, o, n in made], "code": s.encode(after)})
        print(f"Starting from {base_name}.")
        print("\nUnderstood:" if result.understood else "\nNothing in that matched a word I know.")
        for line in result.understood:
            print(f"  {line}")
        for heard, taken in result.heard_as:
            print(f"  (took \"{heard}\" as \"{taken}\")")
        if result.unknown:
            print(f"Not understood: {', '.join(result.unknown)}  (`minictl describe --words` lists what I know)")
        for n in result.notes + notes:
            print(f"  ! {n}")
        if not made:
            return
        print(f"\n{len(made)} settings changed:")
        for a, old, new in made:
            print(f"  {s.names.get(a, a)}: {s.shown(a, old, values)} -> {s.shown(a, new, after)}")
        print(f"\nPreset code:\n{s.encode(after)}")
        if args.send or args.save is not None:
            if d is None:
                d = open_device(args)
                d.read()
            d.apply(after, page1_defaults())
            d.command(0)
            print("\nPlaying on the minichord (live).")
            if args.save is not None and confirm(f"Save it to bank {args.save + 1}, replacing what is there?", args):
                d.save(args.save)
                print(f"saved to bank {args.save + 1}")
    finally:
        if d:
            d.close()


def cmd_do(args):
    """An instruction in words, minicontrol's instruction box on the command line"""
    from . import commands as C
    from .settings import DATA
    with open(os.path.join(DATA, "options.json")) as f:
        C.setup(json.load(f))           # the grammar reads in the fork's terms, its choices too
    text = " ".join(args.text)
    d = None
    try:
        if args.code:
            live, _ = preset_code_from(args.code)
            current, read = -1, {"live": list(live)}
            names = []
        else:
            d = open_device(args)
            live = d.read()
            current = live[1]
            read = {"live": list(live)}
            names = []
            walked = False
            # a bank called by its name ("my Drone bank") needs every bank's name, so every bank read
            marked, _ = C.mark_scopes(C.normalise(text), current, [])
            if re.search(r"\b(?:bank|slot)s?\b", re.sub(r"«\d+»", "", marked)) and PROFILE == "fork" and live[7] >= 50:
                got = walk_banks(d, range(BANKS))
                walked = True
                names = [Settings.preset_name(got[b]) for b in range(BANKS)]
                read.update({f"b{b}": v for b, v in got.items()})
            want = [k for k in C.places_named(text, current, names) if k not in read]
            if want:
                walked = True
                got = walk_banks(d, [int(k[1:]) for k in want])
                read.update({f"b{b}": v for b, v in got.items()})
        s = settings()
        presets = s.shared_presets()
        size = {k: len(v) for k, v in read.items()}
        work = {k: s.full(v) for k, v in read.items()}
        before = {k: list(v) for k, v in work.items()}

        def values(k):
            if k not in work:
                raise RuntimeError(f"{C.scope_name([k])} wasn't read")
            return work[k]

        ctx = C.Context(values, current, [p["name"] for p in presets], presets, names)
        out = C.plan(text, ctx)
        for q in list(out["questions"]):
            print(f'"{q.phrase}" could be:')
            for i, ch in enumerate(q.choices, 1):
                print(f"  {i}. {ch['name']}")
            if args.yes or not sys.stdin.isatty():
                print("  (left out: say which in the words)")
                continue
            answer = input("which? (number, Enter to leave it out) ").strip()
            if answer.isdigit() and 1 <= int(answer) <= len(q.choices):
                more = C.plan(q.clause(q.choices[int(answer) - 1]["addr"]), C.Context(values, current, [], presets, names))
                for k in ("understood", "notes", "unknown"):
                    out[k] += [x for x in more[k] if x not in out[k]]
        for k in work:                      # stock firmware: what it has of the result
            work[k], notes = s.fit(before[k], work[k])
            out["notes"] += [n for n in notes if n not in out["notes"]]
        if args.json:
            print_json({"understood": out["understood"], "notes": out["notes"], "unknown": out["unknown"],
                        "changes": {k: {s.names.get(a, str(a)): {"from": before[k][a], "to": work[k][a]}
                                        for a in range(len(work[k])) if work[k][a] != before[k][a]} for k in work}})
        else:
            for line in out["understood"]:
                print(f"  {line}")
            if out["unknown"]:
                print(f"Not understood: {', '.join(out['unknown'])}")
            for n in out["notes"]:
                print(f"  ! {n}")
        changed = {k: [a for a in range(len(work[k])) if work[k][a] != before[k][a] and a < max(size[k], dev.PAGE)]
                   for k in work}
        changed = {k: v for k, v in changed.items() if v}
        if not changed:
            if not args.json:
                print("nothing changes")
            return
        if not args.json:
            for k in sorted(changed, key=lambda k: (k != "live", int(k[1:]) if k != "live" else 0)):
                where = "live" if k == "live" else C.scope_name([k])
                print(f"\n{where}:")
                for a in changed[k]:
                    print(f"  {s.names.get(a, a)}: {s.shown(a, before[k][a], before[k])} -> {s.shown(a, work[k][a], work[k])}")
        if args.code:
            print(f"\nPreset code:\n{s.encode(work['live'])}")
            return
        if args.dry_run:
            print("\n(dry run: nothing sent)")
            return
        banks = sorted(int(k[1:]) for k in changed if k != "live")
        if banks:
            if not confirm(f"Write {C.scope_name([f'b{b}' for b in banks])}, replacing what "
                           f"{'it holds' if len(banks) == 1 else 'they hold'}?", args):
                print("banks left as they were")
                banks = []
            for b in banks:
                d.load_bank(b)
                time.sleep(0.1)
                for a in changed[f"b{b}"]:
                    if a not in dev.RESERVED and a != 7:
                        d.set(a, work[f"b{b}"][a])
                time.sleep(0.04)
                d.save(b)
                print(f"bank {b + 1} written")
            walked = walked or bool(banks)
        # Loading a bank on the way (to read or write one) dropped what was live and unsaved, so then
        # the whole live sound goes back, with the changes; otherwise only the changes go
        if walked:
            now = d.load_bank(current) if banks else d.read()
            send = [a for a in range(2, size["live"]) if work["live"][a] != now[a]]
        else:
            send = changed.get("live", [])
        for a in send:
            if a not in dev.RESERVED and a != 7:
                d.set(a, work["live"][a])
        if "live" in changed:
            print("live: playing (`minictl save` keeps it)")
    finally:
        if d:
            d.close()


def cmd_tuning(args):
    """firmware 67's own temperament from a Scala file, the live tuning shown, or written out as one"""
    from . import tuning as T
    with open_device(args) as d:
        v = d.read()
        s = settings()
        name = T.TEMPERAMENT_NAMES[v[237]] if v[237] < len(T.TEMPERAMENT_NAMES) else str(v[237])
        if args.export:
            with open(args.export, "w") as f:
                f.write(T.to_scala(f"minichord {name}", v))
            print(f"{name} -> {args.export}")
            return
        if not args.file and not args.equal:
            off = T.offsets_of(v)
            print(f"temperament {v[237]}: {name}" + (f", master tuning {v[109] / 10:g} Hz" if v[109] else ""))
            if off is None:
                print(f"  the octave in {T.DIVISIONS[v[237]]} equal steps")
            else:
                print("  " + "  ".join(f"{n} {c:+g}" for n, c in zip(T.NAMES, off)) + "  (cents from equal)")
            return
        needs_fork("your own temperament", d, T.OWN_VERSION)
        if args.equal:
            offsets, what = [0.0] * 12, "equal"
        else:
            with open(args.file, encoding="utf-8", errors="replace") as f:
                title, offsets = T.parse_scala(f.read())
            if args.keep_a:
                offsets = [c - offsets[9] for c in offsets]
            T.check(offsets)
            what = title or os.path.basename(args.file)
        changes = T.stored(offsets)
        if not args.equal:
            changes[237] = T.OWN_TEMPERAMENT
        for a, value in changes.items():
            d.set(a, value)
        print(f"your own temperament: {what}")
        print("  " + "  ".join(f"{n} {c:+.1f}" for n, c in zip(T.NAMES, offsets)) + "  (cents from equal)")
        if not args.equal:
            print("temperament set to your own (live: `minictl save` keeps it)")


def cmd_words(args):
    interpret.print_vocabulary()


def cmd_conflicts(args):
    if args.code:
        values, what = preset_code_from(args.code)
    else:
        with open_device(args) as d:
            values = walk_banks(d, [args.bank])[args.bank] if args.bank is not None else d.read()
        what = f"bank {values[1] + 1}" + ("" if args.bank is not None else ", live")
    notes = settings().conflicts(values)
    print(f"{what}: " + ("no settings undo one another" if not notes else f"{len(notes)} conflict" + ("s" if len(notes) > 1 else "")))
    for n in notes:
        print(f"  ! {n}")
    return 1 if notes and args.strict else 0


def cmd_play(args):
    notes = [note_number(n) for n in args.notes]
    with open_device(args) as d:
        v = d.read()
        needs_fork("playing notes sent to it", d, 29)
        if not v[8]:
            print("! MIDI in plays (setting 8) is off in this preset, so these notes are silent: "
                  "`minictl set midi-in-plays on`")
        cable = 2 if args.harp else 1
        for n in notes:
            d.note(True, n, args.velocity, args.channel, cable)
            if args.arp:
                time.sleep(args.arp)
        time.sleep(args.length)
        for n in notes:
            d.note(False, n, 0, args.channel, cable)


def cmd_panic(args):
    with open_device(args) as d:
        d.panic()
    print("all notes off, on every channel of both ports")


def cmd_rhythm(args):
    with open_device(args) as d:
        needs_fork("rhythm mode by SysEx", d)
        d.command(8, {"off": 0, "on": 1, "toggle": 2}[args.state])
    print(f"rhythm mode {args.state}")


def cmd_snapshot(args):
    with open_device(args) as d:
        needs_fork("a snapshot", d)
        d.command(5, 1)
    print("the live settings are remembered: `minictl revert` puts them back (until another bank loads)")


def cmd_revert(args):
    with open_device(args) as d:
        needs_fork("revert", d)
        d.command(6)
    print("back to the snapshot (if there was one)")


def cmd_locks(args):
    with open_device(args) as d:
        needs_fork("the locks", d, dev.LOCKS_VERSION)
        s = settings()
        if args.defaults:
            locks = d.reset_locks(True)
        elif args.none:
            locks = d.reset_locks(False)
        else:
            locks = d.read_locks()
            for word in args.lock:
                a = s.find(word)
                locks = d.set_lock(a, True)
                if a not in locks:
                    print(f"{s.names.get(a, a)} can't be locked" + (": it is the instrument's own already" if a in dev.INSTRUMENT else
                          ": an action, a name or a control's own storage"))
            for word in args.unlock:
                a = s.find(word)
                locks = d.set_lock(a, False)
                if a in locks:
                    print(f"{s.names.get(a, a)} stays locked: it is the instrument's own always")
    if getattr(args, "json", False):
        print_json(sorted(locks))
        return
    print("locked, the minichord's own whatever preset is loaded:" if locks else "nothing is locked")
    for a in sorted(locks):
        print(f"  {a:3}  {s.title(a)}" + ("  (always)" if a in dev.INSTRUMENT else ""))


# ---- your own drum kit ---------------------------------------------------------------------------

def kit_names():
    """drum kit 337's choices by number, as minicontrol names them"""
    return dict(settings().options.get(kit.KIT_SETTING, {}))


def open_kit(args, what):
    """the minichord, open, and its kit slot's INFO: fork firmware 113 that answers it, with another
    program's upload waited out (open_device's, when the settings go unanswered, and here)"""
    d = open_device(args)
    try:
        needs_fork(what, d, kit.KIT_VERSION)
        info = d.kit_info()
        if info is None:
            raise RuntimeError(f"this firmware has no kit slot: the minichord didn't answer the kit's INFO "
                               f"(firmware {d.version}; {what} needs the fork's firmware {kit.KIT_VERSION} or later)")
        if info["state"] == 2 or d.kit_others():
            info = wait_for_other_upload(d)
    except Exception:
        d.close()
        raise
    return d, info


def kit_fallback():
    """what drum kit 13 plays with no kit stored"""
    n = kit.STAND_IN_KIT
    name = kit_names().get(n)
    return f"the {name} kit (drum kit {n})" if name else f"drum kit {n}"


def kit_state_lines(info, last=None):
    """INFO in words"""
    names = kit_names()
    state, reason = info["state"], info["reason"]
    room = f"{kit.kb(info['capacity'])} of room, the sound from 0x{info['data_base']:08X}"
    if state == 1:
        filled = kit.voice_names(info["filled"])
        base = names.get(info["base_kit"])
        lines = [f"your own drum kit: {info['name'] or '(no name)'}, {len(filled)} of 13 voices, "
                 f"{kit.kb(info['size'] - 256)} of sound, the rest from kit {info['base_kit']}" + (f" ({base})" if base else ""),
                 "  " + (", ".join(filled) or "(no voices of its own)"),
                 f"  {info['size']} bytes, CRC {info['crc']:08X}; {room}"]
        if last and last.info["crc"] == info["crc"] and len(last.image) == info["size"]:
            lines.append("  (the last kit sent from this computer)")
        return lines
    if state == 0:
        lines = [f"no kit stored: drum kit 13 plays {kit_fallback()} until one is sent", f"  {room}"]
    elif state == 3:
        lines = [f"the stored kit is damaged, reason {reason}: {kit.U.REASONS[reason] if reason < len(kit.U.REASONS) else '?'}"
                 + (". A bigger firmware moved where the sound starts" if reason == kit.U.REASON_DATA_BASE else ""),
                 f"  drum kit 13 plays {kit_fallback()} until a kit is sent again", f"  {room}"]
    elif state == 4:
        lines = ["the kit slot is disabled: this firmware leaves less than 64 kB of flash for it"]
    else:
        lines = [f"the kit slot is {kit.U.STATES[state] if state < len(kit.U.STATES) else state}"]
    if last and state in (0, 3):
        lines.append(f"  the last kit sent from this computer: {last.name or '(no name)'} "
                     f"(`minictl kit send {kit.cache_path()}`)")
    return lines


def cmd_kit(args):
    args.kit_parser.print_help()


def cmd_kit_info(args):
    d, info = open_kit(args, "your own drum kit")
    d.close()
    if args.json:
        return print_json(dict(info, state_name=kit.U.STATES[info["state"]] if info["state"] < len(kit.U.STATES) else None,
                               reason_text=kit.U.REASONS[info["reason"]] if info["reason"] < len(kit.U.REASONS) else None,
                               voices=kit.voice_names(info["filled"])))
    for line in kit_state_lines(info, kit.last()):
        print(line)


def kit_upload(d, k):
    """the upload, with its progress on a terminal; if BEGIN finds the rhythm or the looper running, both
    are stopped, that said, and BEGIN sent once more (user_kit.md section 5)"""
    show = sys.stderr.isatty()

    def progress(done, size):
        if show:
            print(f"\rsending: {100 * done // size:3d}%", end="", file=sys.stderr, flush=True)

    try:
        try:
            return d.kit_upload(k.image, progress)
        except dev.KitBusy:
            pass
        print("! the minichord is busy: its rhythm or its looper is running, and it can't write a kit while "
              "either plays")
        d.command(8, 0)
        d.set(dev.LOOPER_ADDRESS, 3)
        time.sleep(0.3)
        print("rhythm and looper stopped, to send the kit")
        try:
            return d.kit_upload(k.image, progress)
        except dev.KitBusy:
            raise RuntimeError("the minichord is still busy: another program may be sending it a kit")
    finally:
        if show:
            print("\r" + " " * 16 + "\r", end="", file=sys.stderr, flush=True)


def port_check(d, force):
    """Linux: who else has the minichord's MIDI port. A browser (minicontrol's tab, maybe) stops the send
    unless forced; anything else (PipeWire's or JACK's bridge, Midi Through) is only noted."""
    others = d.others_on_port()
    if others is None:
        print("! couldn't tell whether a browser has the minichord's MIDI port (aconnect -l didn't run)")
        return

    def named(peers):
        return ", ".join(dict.fromkeys(name + (f" ({program})" if program and program != name else "")
                                       for name, program in peers))
    browsers = [p for p in others if dev.web_midi_browser(*p)]
    rest = [p for p in others if p not in browsers]
    if rest:
        print(f"(also connected to the minichord's MIDI port: {named(rest)}; what they send it is dropped while "
              f"the kit is written)")
    if browsers:
        print(f"! a browser is connected to the minichord's MIDI port: {named(browsers)}")
        print("  close minicontrol's tab: its messages are dropped while the kit is written, and unplugging with "
              "Web MIDI open can hang USB on some Linux kernels")
        if not force:
            raise RuntimeError("not sending while a browser has the minichord's port (--force sends anyway)")


def cmd_kit_send(args):
    k = kit.Kit.load(args.file)
    problem = k.problem()
    if problem:                        # checked before anything goes to the minichord
        raise RuntimeError(f"{args.file}: {problem}. Nothing was sent")
    names = kit_names()
    for line in k.describe(names):
        print(line)
    d, info = open_kit(args, "your own drum kit")
    with d:
        port_check(d, args.force)
        if info["state"] == 4:
            raise RuntimeError("the minichord's kit slot is disabled: its firmware leaves less than 64 kB of flash for one")
        if k.sound > info["capacity"]:
            raise RuntimeError(f"{args.file} has {kit.kb(k.sound)} of sound, and this minichord has room for "
                               f"{kit.kb(info['capacity'])}: nothing was sent")
        if info["state"] == 1 and info["crc"] == k.info["crc"] and info["size"] == len(k.image):
            print("(the minichord holds this kit already; it goes again)")
        if not confirm("Send it? The minichord goes quiet while the kit is written, usually under 20 seconds; "
                       "don't unplug it.", args):
            return
        t0 = time.monotonic()
        info = kit_upload(d, k)
        print(f"sent in {time.monotonic() - t0:.1f} s")
        path = kit.remember(k)
        if info:
            for line in kit_state_lines(info):
                print(line)
        if path:
            print(f"(kept as the last kit sent: {path})")
        if args.select:
            v = d.read()
            d.set(kit.KIT_SETTING, kit.OWN_KIT)
            print("drum kit 13, your own, playing (live: `minictl save` keeps it)")
            if not v[kit.DRUMS] and not v[kit.HARP_DRUMS]:
                print(f"  ! the drums and harp drums are both off in this preset, so it plays nothing yet: "
                      f"`minictl set {settings().names[kit.DRUMS]} on`")


def cmd_kit_clear(args):
    d, info = open_kit(args, "clearing your own drum kit")
    with d:
        if info["state"] == 0:
            print("no kit stored: nothing to clear")
            return
        if info["state"] == 4:
            print("the kit slot is disabled (this firmware leaves less than 64 kB of flash for it): nothing to clear")
            return
        what = f"the kit {info['name']!r}" if info["state"] == 1 and info["name"] else "the stored kit"
        if not confirm(f"Erase {what} from the minichord? Drum kit 13 then plays {kit_fallback()} until another "
                       f"is sent.", args):
            return
        info = d.kit_clear()
        for line in kit_state_lines(info):
            print(line)


def cmd_monitor(args):
    show_midi = args.midi or not args.serial
    show_serial = args.serial or not args.midi
    d = ser = None
    try:
        if show_midi:
            d = open_device(args)

            def heard(msg):
                if msg[0] == 0xF0:
                    print(f"{time.strftime('%H:%M:%S')} sysex {len(msg)} bytes", flush=True)
                elif msg[0] & 0xF0 in (0x80, 0x90, 0xB0, 0xE0, 0xD0, 0xA0, 0xC0):
                    kind = {0x80: "off", 0x90: "on", 0xA0: "aftertouch", 0xB0: "cc", 0xC0: "program",
                            0xD0: "pressure", 0xE0: "bend"}[msg[0] & 0xF0]
                    print(f"{time.strftime('%H:%M:%S')} ch{(msg[0] & 15) + 1:<2} {kind:10} {' '.join(map(str, msg[1:]))}",
                          flush=True)
            d.listeners.append(heard)
        if show_serial:
            import serial
            port = dev.serial_port()
            if port is None:
                print("(no serial port from the minichord)", file=sys.stderr)
            else:
                ser = serial.Serial(port, 115200, timeout=0.2)
        print("listening; Ctrl-C stops", file=sys.stderr)
        while True:
            if ser:
                line = ser.readline().decode(errors="replace").rstrip()
                if line:
                    print(f"{time.strftime('%H:%M:%S')} | {line}", flush=True)
            else:
                time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        if d:
            d.close()
        if ser:
            ser.close()


def cmd_record(args):
    used = dev.record(args.file, args.seconds, args.device)
    print(f"{args.seconds:g} s from {used} -> {args.file}")


def cmd_flash(args):
    if not os.path.isfile(args.hex):
        raise RuntimeError(f"no file {args.hex}")
    problems = dev.flash_hazards()
    for p in problems:
        print(f"! {p}")
    if problems and not args.force:
        raise RuntimeError("not flashing (--force flashes anyway)")
    before = None
    try:
        with open_device(args) as d:
            before = d.read()[7]
    except (dev.NotFound, RuntimeError):
        pass
    print(f"{args.hex}: {os.path.getsize(args.hex) // 1024} kB" +
          (f"; the minichord has firmware {before}" if before is not None else "; no minichord answering"))
    print("Close every program using the minichord's MIDI (minicontrol, a DAW) first. A firmware that changes "
          "the preset storage wipes the banks: `minictl backup` keeps them.")
    if not confirm("Flash it?", args):
        return
    took = dev.flash(args.hex)
    with open_device(args) as d:
        after = d.read()[7]
    print(f"back after {took:.0f} s, on firmware {after}")


# ---- the parser ------------------------------------------------------------------------------------

def parser():
    def common(p, default):
        kw = {} if default else {"default": argparse.SUPPRESS}
        p.add_argument("--port", help="the minichord's MIDI port, by a piece of its name (or MINICTL_PORT)", **kw)
        p.add_argument("-y", "--yes", action="store_true", help="don't ask before saving, restoring, flashing or "
                                                                "writing a kit", **kw)
        p.add_argument("--json", action="store_true", help="JSON output, for scripts", **kw)

    ap = argparse.ArgumentParser(prog="minictl", description="The minichord from the command line.")
    common(ap, True)
    ap.add_argument("--firmware", choices=["stock", "fork"],
                    help="which firmware's settings to use (or MINICTL_FIRMWARE); a plugged-in minichord says by itself")
    shared = argparse.ArgumentParser(add_help=False)
    common(shared, False)
    sub = ap.add_subparsers(dest="command", metavar="command")

    def add(name, fn, help, aliases=()):
        p = sub.add_parser(name, help=help, description=help, aliases=list(aliases), parents=[shared])
        p.set_defaults(fn=fn)
        return p

    add("info", cmd_info, "the minichord's firmware, bank and preset name")
    add("ports", cmd_ports, "the MIDI and serial ports, and which one minictl takes")
    p = add("list", cmd_list, "the settings, by name, with their ranges (words narrow it down)", ["ls"])
    p.add_argument("words", nargs="*")
    p.add_argument("-a", "--all", action="store_true", help="hidden settings too")
    p.add_argument("-v", "--verbose", action="store_true", help="what each one does")

    p = add("get", cmd_get, "read settings: those named, or every one away from its default")
    p.add_argument("settings", nargs="*", help="names or addresses")
    p.add_argument("-a", "--all", action="store_true", help="every setting")
    p.add_argument("-b", "--bank", type=bank_arg, help="a stored bank instead of the live settings (loads it)")
    p.add_argument("-c", "--code", help="a preset code, file or shared preset name instead of the minichord")
    p.add_argument("--raw", action="store_true", help="stored values only, one a line")

    p = add("set", cmd_set, "change live settings: set reverb-size 80 chord-attack 200")
    p.add_argument("pairs", nargs="+", metavar="setting value")
    p.add_argument("-s", "--save", action="store_true", help="then save to the current bank (asks)")
    p.add_argument("--raw", action="store_true", help="stored values to any address, unchecked")

    p = add("bank", cmd_bank, "which bank is playing, or load one: bank 3")
    p.add_argument("bank", nargs="?", type=bank_arg)
    p = add("banks", cmd_banks, "every bank's name (walks through them, then back)")
    p.add_argument("--codes", action="store_true", help="and each bank's preset code")
    p = add("save", cmd_save, "save the live settings to the current bank, or the one given (asks)")
    p.add_argument("bank", nargs="?", type=bank_arg)
    p = add("reset", cmd_reset, "put a bank back to its factory preset (asks)")
    p.add_argument("bank", type=bank_arg)
    p = add("name", cmd_name, "the live preset's name, or name it (firmware 50 and on)")
    p.add_argument("name", nargs="*")
    p = add("backup", cmd_backup, "every bank to a file, minicontrol's backup format")
    p.add_argument("file", nargs="?")
    p = add("restore", cmd_restore, "write banks from a backup file, then read them back (asks)")
    p.add_argument("file")
    p.add_argument("--banks", type=banks_arg, help="only these: 1,3,5-7")
    p = add("code", cmd_code, "the live preset as a share code, or play a code (or shared preset's name)")
    p.add_argument("code", nargs="?")
    p.add_argument("-b", "--bank", type=bank_arg, help="a stored bank's code (loads it)")

    p = add("describe", cmd_describe, "a preset from words, by preset_maker's rules: describe \"darker, more reverb\"")
    p.add_argument("text", nargs="+")
    p.add_argument("--base", help="start from: live (the default when a minichord is plugged in), defaults, "
                                  "a code, a file or a shared preset's name")
    p.add_argument("--send", action="store_true", help="play the result on the minichord (live)")
    p.add_argument("--save", type=bank_arg, metavar="BANK", help="play it and save it to a bank (asks)")
    add("words", cmd_words, "every word and phrase describe understands")
    p = add("tuning", cmd_tuning, "the tuning playing; tuning FILE.scl makes a Scala file your own temperament "
                                  "(fork firmware 67)")
    p.add_argument("file", nargs="?", help="a Scala .scl of twelve notes to the octave")
    p.add_argument("--keep-a", action="store_true", help="shift it so A stays where it is (C stays otherwise)")
    p.add_argument("--equal", action="store_true", help="your own temperament back to equal")
    p.add_argument("--export", metavar="FILE", help="write the tuning playing as a .scl")
    p = add("do", cmd_do, "an instruction in words, for the live sound or any banks: "
                          "do \"turn off hover on banks 2 and 4, raise the reverb\"")
    p.add_argument("text", nargs="+")
    p.add_argument("-n", "--dry-run", action="store_true", help="show what would change, send nothing")
    p.add_argument("-c", "--code", help="work on a preset code instead of the minichord, and print the new code")
    p = add("conflicts", cmd_conflicts, "settings that undo one another, in the live preset or a code")
    p.add_argument("code", nargs="?")
    p.add_argument("-b", "--bank", type=bank_arg)
    p.add_argument("--strict", action="store_true", help="exit 1 when there are any")

    p = add("play", cmd_play, "play notes through MIDI in: play C4 E4 G4")
    p.add_argument("notes", nargs="+")
    p.add_argument("--harp", action="store_true", help="on the harp's port (cable 2)")
    p.add_argument("-l", "--length", type=float, default=1.0, help="seconds held")
    p.add_argument("-v", "--velocity", type=int, default=100)
    p.add_argument("-c", "--channel", type=int, default=1)
    p.add_argument("--arp", type=float, default=0, help="seconds between the notes' starts")
    add("panic", cmd_panic, "all notes off")
    p = add("rhythm", cmd_rhythm, "rhythm mode on, off or toggle")
    p.add_argument("state", choices=["on", "off", "toggle"])
    add("snapshot", cmd_snapshot, "remember the live settings, to revert to")
    add("revert", cmd_revert, "back to the snapshot")
    p = add("locks", cmd_locks, "the settings the minichord keeps as its own, whatever preset is loaded (fork firmware 76)")
    p.add_argument("--lock", action="append", default=[], metavar="SETTING", help="lock a setting, at its present value")
    p.add_argument("--unlock", action="append", default=[], metavar="SETTING", help="unlock a setting, so presets set it again")
    p.add_argument("--defaults", action="store_true", help="lock the default ones again (the harp plate, the touch thresholds, the master tuning), nothing else")
    p.add_argument("--none", action="store_true", help="unlock everything")
    p = add("kit", cmd_kit, "your own drum kit, drum kit 13 (fork firmware 113): kit info, kit send FILE.mckit, kit clear")
    p.set_defaults(kit_parser=p)
    kits = p.add_subparsers(dest="kit_command", metavar="info|send|clear")

    def add_kit(name, fn, help):
        q = kits.add_parser(name, help=help, description=help, parents=[shared])
        q.set_defaults(fn=fn)
        return q

    add_kit("info", cmd_kit_info, "what the minichord's kit slot holds")
    q = add_kit("send", cmd_kit_send, "send a kit (a .mckit, from minicontrol) into the minichord's flash (asks)")
    q.add_argument("file", help="the .mckit (or a bare image), checked before anything is sent")
    q.add_argument("--select", action="store_true", help="then play it: drum kit 13, live")
    q.add_argument("--force", action="store_true", help="send even while a browser has the minichord's port open")
    add_kit("clear", cmd_kit_clear, "erase the kit from the minichord's flash (asks)")
    p = add("monitor", cmd_monitor, "print what the minichord sends: MIDI and its serial log")
    p.add_argument("--midi", action="store_true", help="MIDI only")
    p.add_argument("--serial", action="store_true", help="serial only")
    p = add("record", cmd_record, "record the minichord's USB audio to a wav")
    p.add_argument("file")
    p.add_argument("-s", "--seconds", type=float, default=10)
    p.add_argument("--device", help="the audio input (or MINICTL_AUDIO)")
    p = add("flash", cmd_flash, "put a firmware .hex on the minichord (asks)")
    p.add_argument("hex")
    p.add_argument("--force", action="store_true", help="flash despite a warning")
    sub.add_parser("help", help="this list").set_defaults(fn=None)
    return ap


def main(argv=None):
    global PROFILE, FORCED
    ap = parser()
    args = ap.parse_args(argv)
    PROFILE = args.firmware or os.environ.get("MINICTL_FIRMWARE") or None
    FORCED = PROFILE is not None
    if not getattr(args, "fn", None):
        ap.print_help()
        return 0
    try:
        return args.fn(args) or 0
    except KeyboardInterrupt:
        print()
        return 130
    except BrokenPipeError:            # piped into head, which stopped reading
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 141
    except (RuntimeError, ValueError, OSError) as e:
        print(f"minictl: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

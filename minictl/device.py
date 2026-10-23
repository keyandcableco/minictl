"""Talking to a minichord over USB: its two MIDI ports (SysEx settings and notes), its serial port, its
USB audio and its flashing, on Linux, macOS and Windows. The protocol is minicontrol's:

    F0 <addr lo> <addr hi> <value lo> <value hi> F7    set a live setting (7 bits each)
    F0 00 00 <command> <parameter> F7                  a control command (address 0)

Commands: 0 report page 0 (512 bytes, no header), 7 report page <parameter> (7D 6D <page> first),
1 wipe memory, 2 save to bank, 3 reset a bank to its factory preset, 4 load a bank (it reports itself),
5 push the live settings, 6 pop them back, 8 rhythm mode (0 off, 1 on, 2 the other way), 9 report the
locks (7D 6C, a count, then two bytes an address; firmware 76), 10 unlock everything (0) or lock the
default settings again (1).

Your own drum kit (fork firmware 113) has messages of its own, F0 7D 6B <op> ... F7, at least 7 bytes
so the settings path never takes one, answered 9 bytes (a status) or 49 (INFO): the fork's
generator/user_kit.md, user_kit.py here, kit.py. Its replies are kept apart from the other SysEx, so a
page report (514 bytes, or 517 with 7D 6D <page>) is never looked for among them, nor they among those."""
import collections, glob, os, re, subprocess, sys, threading, time

from . import user_kit as U

PAGE = 256
PAGE1_VERSION = 31         # firmware 31 grew the settings to two pages
RESERVED = {382, 383, 510, 511}
VERSION_ADDRESS = 7
LOCKS_VERSION = 76         # firmware 76 keeps locks: settings that are the instrument's, whatever preset loads
LOCK_ADDRESS = 385         # writing an address locks it, 1024 + the address unlocks it
INSTRUMENT = {244, 384}    # USB audio and upside down: the instrument's own, left alone by a whole preset
PJRC_VID = 0x16C0
KIT = (0xF0, 0x7D, 0x6B)    # the user drum kit's messages: the fork's own ID (7D), then "k"
LOOPER_ADDRESS = 256        # the looper: writing 3 stops it
KIT_OTHERS_QUIET = 6.0      # another program's upload is over when nothing of it is heard for this long
KIT_OWN_STRAYS = 1.0        # BEGIN and DATA replies this soon after minictl's own upload are late ones to it
# the kit's status codes minictl acts on (user_kit.md section 3; U.STATUSES says them all in words)
KIT_BUSY, KIT_BAD_MESSAGE, KIT_NO_UPLOAD, KIT_PIECE_CRC, KIT_READ_BACK = 1, 3, 5, 7, 8


class NotFound(RuntimeError):
    pass


class KitBusy(RuntimeError):
    """BEGIN answered busy: the rhythm or the looper is running, or another upload is open"""


def _rtmidi():
    try:
        import rtmidi
        return rtmidi
    except ImportError:
        raise RuntimeError("minictl needs python-rtmidi: pip install python-rtmidi")


SECOND = re.compile(r"(MIDI|Port)\s*2\b|MIDI(IN|OUT)2", re.I)
FIRST = re.compile(r"(MIDI|Port)\s*1\b", re.I)
ALSA_ADDRESS = re.compile(r"\s(\d+):(\d+)$")     # ALSA's client:port, at the end of a port's name on Linux


def pick_port(names, cable=1, want=None):
    """The index of the minichord's port for cable 1 (chords, and every SysEx) or cable 2 (the harp).
    ALSA calls them "minichord MIDI 1/2", macOS "minichord Port 1/2", Windows "minichord" and
    "MIDIIN2 (minichord)". `want` (or MINICTL_PORT) picks by a piece of the name instead."""
    want = want or os.environ.get("MINICTL_PORT")
    if want and cable == 1:
        hits = [i for i, n in enumerate(names) if want.lower() in n.lower()]
        hits = [i for i in hits if not SECOND.search(names[i])] or hits
        return hits[0] if hits else None
    if want:      # the second port of a chosen device: its name with 2 where the first has 1
        first = next((n for n in names if want.lower() in n.lower()), "")
        stem = FIRST.split(first)[0].strip() if FIRST.search(first) else None
        hits = [i for i, n in enumerate(names) if stem and n.startswith(stem) and SECOND.search(n)]
        return hits[0] if hits else None
    ours = [(i, n) for i, n in enumerate(names) if "minichord" in n.lower()]
    if cable == 1:
        hits = [i for i, n in ours if FIRST.search(n)] or [i for i, n in ours if not SECOND.search(n)]
    else:
        hits = [i for i, n in ours if SECOND.search(n)]
    return hits[0] if hits else None


def list_ports():
    rtmidi = _rtmidi()
    o, i = rtmidi.MidiOut(), rtmidi.MidiIn()
    try:
        return o.get_ports(), i.get_ports()
    finally:
        o.delete()
        i.delete()


class Minichord:
    """One minichord, open on both its MIDI ports. Values are stored values (what the SysEx carries)."""

    def __init__(self, port=None):
        rtmidi = _rtmidi()
        self.out, self.inp, self.out2 = rtmidi.MidiOut(), rtmidi.MidiIn(), rtmidi.MidiOut()
        names = self.out.get_ports()
        i = pick_port(names, 1, port)
        j = pick_port(self.inp.get_ports(), 1, port)
        if i is None or j is None:
            seen = ", ".join(names) or "none"
            raise NotFound(f"no minichord found (MIDI ports: {seen}). Plug it in, or name its port with --port")
        try:
            self.out.open_port(i, "minictl")
            self.inp.open_port(j, "minictl")
        except Exception as e:
            raise RuntimeError(f"couldn't open the minichord's port ({e}). Another program may hold it: "
                               f"minicontrol in a browser, or a DAW (Windows lets only one program at a time)")
        self.port_name = names[i]
        k = pick_port(names, 2, port)
        if k is not None:
            try:
                self.out2.open_port(k, "minictl harp")
            except Exception:
                pass
        self.inp.ignore_types(sysex=False, timing=True, active_sense=True)
        # ALSA's client numbers of the minichord's ports ("... 28:0" ends each name on Linux), to see who
        # else is connected to them
        self.alsa_clients = {int(m.group(1)) for n in (names[i], self.inp.get_ports()[j], names[k] if k is not None else "")
                             for m in [ALSA_ADDRESS.search(n)] if m}
        self.lock = threading.Lock()
        self.sysex = []
        self.kit_heard = collections.deque()   # (number, when, message): the kit's replies, apart
        self.kit_arrived = threading.Condition(self.lock)
        self.kit_count = 0
        self.kit_mine = False                  # an upload of minictl's own is under way
        self.kit_mine_until = 0.0              # and the moment after it, when its late replies can still come
        self.kit_others_at = None              # when another program's upload was last heard, until it ends
        self.kit_others_seen = 0               # how many of them have been heard begin
        self.on_others_wait = None             # called when a message starts to wait one out, to say so
        self.kit_quiet = threading.Event()     # set while no upload is: every other message waits for it
        self.kit_quiet.set()
        self.kit_thread = None
        self.listeners = []
        self.inp.set_callback(self._on_midi)
        self.version = None
        self.bank = None
        self.page1 = None          # whether it keeps a second page of settings, once read
        self.locks = None          # the locked addresses, once asked for (firmware 76 on)
        self.sent = False

    def close(self):
        if self.sent:
            time.sleep(0.05)      # let the last messages out before the port goes: ALSA delivers after send returns
        try:
            self.inp.cancel_callback()
        except Exception:
            pass
        for p in (self.out, self.inp, self.out2):
            try:
                p.close_port()
                p.delete()          # the ALSA client goes now, not whenever the collector gets to it
            except Exception:
                pass
        self.listeners = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def _on_midi(self, ev, _):
        msg, _dt = ev
        if msg and msg[0] == 0xF0:
            with self.lock:
                if tuple(msg[:3]) == KIT:
                    self._kit_heard(msg)
                else:
                    self.sysex.append(msg)
        for f in self.listeners:
            f(msg)

    def _kit_heard(self, msg):
        """a kit reply, kept for _kit_take; one to a BEGIN or DATA minictl didn't send is another
        program's upload, going on until a COMMIT, ABORT or CLEAR reply ends it (under self.lock). For a
        second after minictl's own upload ends, those are late answers to its own last piece, not another's."""
        now = time.monotonic()
        self.kit_count += 1
        self.kit_heard.append((self.kit_count, now, msg))
        while len(self.kit_heard) > 64 or (self.kit_heard and now - self.kit_heard[0][1] > 10):
            self.kit_heard.popleft()
        self.kit_arrived.notify_all()
        op = msg[3] if len(msg) > 4 else 0
        if not self.kit_mine and now >= self.kit_mine_until and op in (0x40 | U.BEGIN, 0x40 | U.DATA):
            if self.kit_others_at is None:
                self.kit_others_seen += 1
            self.kit_others_at = now
        elif op in (0x40 | U.COMMIT, 0x40 | U.ABORT, 0x40 | U.CLEAR):
            self.kit_others_at = None

    def _others_wait(self, most=120.0, say=True):
        """Holds while another program's upload is heard: until a COMMIT, ABORT or CLEAR reply, or 6 s with
        nothing of it heard (the minichord ends an upload left silent for 5). say: on_others_wait() is
        called if it has to wait. RuntimeError after `most` seconds of it."""
        t0 = time.monotonic()
        while True:
            with self.lock:
                at = self.kit_others_at
                if at is not None and time.monotonic() - at >= KIT_OTHERS_QUIET:
                    self.kit_others_at = at = None
            if at is None:
                return
            if say and self.on_others_wait:
                self.on_others_wait()
            say = False
            if time.monotonic() - t0 >= most:
                raise RuntimeError(f"another program has been sending the minichord a kit for {most:.0f} s")
            time.sleep(0.05)

    # ---- sending ----

    def _hold(self):
        """An upload under way holds every other message until it ends, as the minichord drops what isn't
        the kit's meanwhile: minictl's own on another thread, or another program's once it is heard"""
        if threading.get_ident() == self.kit_thread:
            return
        if not self.kit_quiet.is_set():
            self.kit_quiet.wait(60)
        self._others_wait()

    def set(self, address, value):
        value = int(value) & 0x3FFF
        self._hold()
        self.sent = True
        self.out.send_message([0xF0, address % 128, address // 128, value % 128, value // 128, 0xF7])

    def command(self, cmd, param=0):
        self._hold()
        self.sent = True
        self.out.send_message([0xF0, 0, 0, cmd, param, 0xF7])

    def send_sysex(self, msg):
        """one SysEx message as it is, F0, 7-bit bytes, F7"""
        msg = list(msg)
        if len(msg) < 2 or msg[0] != 0xF0 or msg[-1] != 0xF7 or any(b > 0x7F for b in msg[1:-1]):
            raise ValueError("a SysEx message is F0, then 7-bit bytes, then F7")
        self.sent = True
        self.out.send_message(msg)

    # ---- reading ----

    def _take(self, want, timeout, nudge=()):
        """the first SysEx reply that `want` accepts, waiting up to timeout; nudge: (seconds, fn) to call
        while waiting, the asking itself among them, as minicontrol asks again in case a report was missed.
        Another program's upload heard meanwhile (the minichord drops the asking then) is waited out, and
        the asking and the timeout start again after it"""
        asks = sorted(nudge, key=lambda x: x[0])
        seen = None
        while True:
            self._others_wait()
            if seen != self.kit_others_seen:          # the start, or another program's upload since
                seen, t0, nudge = self.kit_others_seen, time.monotonic(), list(asks)
            elif time.monotonic() - t0 >= timeout:
                return None
            with self.lock:
                for k, m in enumerate(self.sysex):
                    r = want(m[1:-1])
                    if r is not None:
                        del self.sysex[:k + 1]
                        return r
            while nudge and time.monotonic() - t0 >= nudge[0][0]:
                nudge.pop(0)[1]()
            time.sleep(0.005)

    @staticmethod
    def _page0(body):
        if len(body) == 2 * PAGE:
            return [body[2 * i] + 128 * body[2 * i + 1] for i in range(PAGE)]
        return None

    @staticmethod
    def _page(page):
        def want(body):
            if len(body) == 3 + 2 * PAGE and body[:3] == [0x7D, 0x6D, page]:
                b = body[3:]
                return [b[2 * i] + 128 * b[2 * i + 1] for i in range(PAGE)]
            return None
        return want

    def read(self, timeout=3.0, bank=None, load=False):
        """Every live setting, page 0 and (firmware 31 on) page 1. With bank, only a report of that bank
        counts: a report from the bank before can still be on its way. load: that bank is loaded first
        (it reports itself), the load asked again with the report if another program's upload held it."""
        with self.lock:
            self.sysex.clear()

        def want0(body):
            v = self._page0(body)
            return v if v is not None and (bank is None or v[1] == bank) else None

        asks = [(0.0, lambda: self.command(0)), (0.4, lambda: self.command(0)),
                (0.9, lambda: self.command(0))] if bank is None else \
            [(0.08, lambda: self.command(0)), (0.4, lambda: self.command(0)), (0.9, lambda: self.command(0))]
        if load:
            asks.insert(0, (0.0, lambda: self.command(4, bank)))
        p0 = self._take(want0, timeout, asks)
        if p0 is None:
            raise RuntimeError("the minichord didn't answer" + (f" for bank {bank + 1}" if bank is not None else ""))
        self.version, self.bank = p0[VERSION_ADDRESS], p0[1]
        if self.version < PAGE1_VERSION or self.page1 is False:
            self.page1 = False
            return p0
        p1 = self._take(self._page(1), 1.0 if self.page1 is None else 3.0, [(0.0, lambda: self.command(7, 1))])
        if p1 is None:
            if self.page1:
                raise RuntimeError("the minichord sent page 0 but not page 1")
            self.page1 = False         # firmware past 30 that keeps one page: stock
            return p0
        self.page1 = True
        return p0 + p1

    @property
    def has_page1(self):
        return bool(self.page1)

    def load_bank(self, bank, timeout=3.0):
        """load bank 0-11 and return its settings as stored (the load drops unsaved live edits)"""
        return self.read(timeout, bank=bank, load=True)

    def read_locks(self, timeout=1.5):
        """The locked addresses (firmware 76 on): settings the minichord keeps as its own, whatever preset
        is loaded. Command 9 answers 7D 6C, a count, then two bytes an address, the instrument's two
        (USB audio, upside down) first. Older firmware: none."""
        if self.version is None or self.version < LOCKS_VERSION:
            self.locks = set()
            return self.locks
        with self.lock:
            self.sysex.clear()

        def want(body):
            if len(body) >= 3 and body[0] == 0x7D and body[1] == 0x6C:
                return {body[3 + 2 * i] + 128 * body[4 + 2 * i] for i in range(body[2]) if 4 + 2 * i < len(body)}
            return None

        r = self._take(want, timeout, [(0.0, lambda: self.command(9)), (0.6, lambda: self.command(9))])
        if r is None:
            raise RuntimeError("the minichord didn't answer for its locks")
        self.locks = r
        return r

    def leaves_alone(self, a):
        """what a whole preset sent leaves as it is: the instrument's own settings, and its locks"""
        return a in INSTRUMENT or (self.locks is not None and a in self.locks)

    def set_lock(self, a, on):
        """lock a setting at its present value, or unlock it; the locks as they are after"""
        self.set(LOCK_ADDRESS, a if on else 1024 + a)
        time.sleep(0.1)
        return self.read_locks()

    def reset_locks(self, defaults):
        """unlock everything, or lock the default settings again; the locks as they are after"""
        with self.lock:
            self.sysex.clear()
        self.command(10, 1 if defaults else 0)
        time.sleep(0.1)
        return self.read_locks()

    def apply(self, values, page1_defaults=None):
        """Sends a whole preset to the live settings, as minicontrol's load does; nothing is saved. A
        page-0 preset gets page 1's defaults rather than keeping what the last preset had there. The
        instrument's own settings and its locks are left as they are, as minicontrol leaves them."""
        if self.locks is None:
            self.read_locks()
        n = min(len(values), 2 * PAGE if self.has_page1 else PAGE)
        for a in range(2, n):
            if a == VERSION_ADDRESS or a in RESERVED or values[a] is None or self.leaves_alone(a):
                continue
            self.set(a, values[a])
            if a % 32 == 0:
                time.sleep(0.002)
        if len(values) <= PAGE and self.has_page1 and page1_defaults:
            for a, v in page1_defaults.items():
                if a not in RESERVED and not self.leaves_alone(a):
                    self.set(a, v)
        time.sleep(0.02)

    def save(self, bank):
        self.command(2, bank)
        time.sleep(0.3)                  # a flash write

    def reset(self, bank):
        self.command(3, bank)
        time.sleep(0.4)

    # ---- notes ----

    def port_for(self, cable):
        return self.out2 if cable == 2 and self.out2.is_port_open() else self.out

    def note(self, on, note, vel=100, channel=1, cable=1):
        st = (0x90 if on else 0x80) | ((channel - 1) & 15)
        self._hold()
        self.sent = True
        self.port_for(cable).send_message([st, note, vel if on else 0])

    def panic(self):
        self._hold()
        self.sent = True
        for port in {id(self.out): self.out, id(self.out2): self.out2}.values():
            if not port.is_port_open():
                continue
            for ch in range(16):
                port.send_message([0xB0 | ch, 64, 0])     # sustain up
                port.send_message([0xB0 | ch, 123, 0])    # all notes off
                port.send_message([0xB0 | ch, 120, 0])    # all sound off

    # ---- your own drum kit (fork firmware 113; kit.py, and the fork's generator/user_kit.md) ----
    #
    # Stop and wait, with the contract's timers (its section 5): BEGIN waits 3 s for its reply; each DATA
    # piece 1 s, then goes again, three tries in all before an ABORT; COMMIT 3 s, then INFO is asked
    # whether the kit landed all the same; INFO 1 s, and no reply means no kit slot. A piece's reply is
    # taken only when its `next` is the end of that piece, so a late reply to a piece sent twice is never
    # taken for the next one's.

    def _kit_mark(self):
        with self.lock:
            return self.kit_count

    def _kit_take(self, want, timeout, since):
        """the first kit reply heard after mark `since` that want() accepts, parsed (user_kit's
        parse_reply), or None after timeout seconds"""
        end = time.monotonic() + timeout
        with self.kit_arrived:
            while True:
                for k, (n, _t, msg) in enumerate(self.kit_heard):
                    if n <= since:
                        continue
                    try:
                        r = U.parse_reply(msg)
                    except ValueError:
                        r = None
                    if r is not None and want(r):
                        del self.kit_heard[k]
                        return r
                left = end - time.monotonic()
                if left <= 0:
                    return None
                self.kit_arrived.wait(left)

    def _kit_ask(self, msg, want, timeout):
        since = self._kit_mark()
        self.send_sysex(msg)
        return self._kit_take(want, timeout, since)

    @staticmethod
    def _status_of(op):
        return lambda r: r["kind"] == "status" and r["op"] == op

    def kit_info(self, timeout=1.0):
        """INFO's reply (state, reason, capacity, data_base, size, crc, base_kit, filled, name...), or None:
        no reply within a second means this firmware has no kit slot"""
        return self._kit_ask(U.info_message(), lambda r: r["kind"] == "info", timeout)

    def kit_others(self):
        """whether another program's upload has been heard going on (replies to a BEGIN or DATA that
        minictl didn't send)"""
        with self.lock:
            return self.kit_others_at is not None

    def kit_wait_others(self, most=120.0):
        """Waits while another program sends the minichord a kit: until a COMMIT, ABORT or CLEAR reply,
        or 6 s with nothing of it heard (the minichord ends an upload left silent for 5). Called when
        INFO says an upload is open, the 6 s count from now if none of it was heard yet."""
        with self.lock:
            if self.kit_others_at is None:
                self.kit_others_seen += 1
                self.kit_others_at = time.monotonic()
        self._others_wait(most, say=False)

    def kit_abort(self):
        """ABORT: an open upload ended with no kit; the reply's status, or None without one"""
        r = self._kit_ask(U.abort_message(), self._status_of(U.ABORT), 1.0)
        return r and r["status"]

    def kit_clear(self):
        """CLEAR: the stored kit erased (and an upload, if one was open, ended); INFO's reply after"""
        r = self._kit_ask(U.clear_message(), self._status_of(U.CLEAR), 3.0)
        if r is None:
            raise RuntimeError("the minichord didn't answer CLEAR within 3 s")
        if r["status"]:
            raise RuntimeError(f"the minichord didn't clear its kit: {kit_status(r['status'])}")
        info = self.kit_info()
        if info is None:
            raise RuntimeError("the minichord cleared its kit but didn't answer INFO after")
        return info

    def kit_upload(self, image, progress=None):
        """Sends an image (checked first: kit.py's Kit.problem) to the minichord's kit slot, BEGIN, each DATA
        piece in turn and COMMIT, and returns the INFO reply that follows COMMIT's. Nothing else goes out
        meanwhile (set, command and the notes wait), as the minichord drops it. progress(done, size) is
        called after each piece. KitBusy when BEGIN is answered busy; RuntimeError when the minichord
        refuses the kit or stops answering, after an ABORT if the upload may be open (the minichord also
        ends one left silent for 5 s), so no kit is left half sent. From BEGIN sent to COMMIT answered,
        anything that stops it, Ctrl-C too, sends ABORT on the way out."""
        image = bytes(image)
        size, crc = len(image), U.crc32(image)
        frames = U.frames(image)
        pending = False                    # BEGIN sent and COMMIT not answered: the upload may be open

        def fail(text, ended=False):
            """given up: ABORT first, unless the minichord has ended the upload itself (ended). An ABORT
            that can't go out (the port gone) doesn't hide why: the minichord ends it after 5 s anyway"""
            nonlocal pending
            if pending and not ended:
                pending = False
                try:
                    self.kit_abort()
                except Exception:
                    pass
            pending = False
            return RuntimeError(text)

        self.kit_quiet.clear()
        self.kit_thread = threading.get_ident()
        with self.lock:
            self.kit_mine = True
        try:
            pending = True
            r = self._kit_ask(frames[0], self._status_of(U.BEGIN), 3.0)
            if r is None:                  # ABORT, in case it opened and only the reply went missing
                raise fail("the minichord didn't answer BEGIN within 3 s")
            if r["status"]:
                pending = False            # refused, nothing opened: an ABORT could end another program's upload
                if r["status"] == KIT_BUSY:
                    raise KitBusy("the minichord is busy: its rhythm or its looper is running, or another upload is open")
                raise RuntimeError(f"the minichord refused the kit: {kit_status(r['status'])}")
            for k, msg in enumerate(frames[1:-1]):
                end = min((k + 1) * U.PIECE, size)
                want = (lambda end: lambda r: r["kind"] == "status" and r["op"] == U.DATA
                        and (r["status"] or r["next"] == end))(end)
                last = None
                for _try in range(3):
                    r = self._kit_ask(msg, want, 1.0)
                    if r is None or r["status"] in (KIT_BAD_MESSAGE, KIT_PIECE_CRC):
                        last = r or last
                        continue           # lost, or damaged on the way: the same piece again
                    if r["status"]:        # after 5 and 8 the minichord has ended it itself
                        raise fail(f"the minichord stopped the upload at byte {k * U.PIECE} of {size}: "
                                   f"{kit_status(r['status'])}", r["status"] in (KIT_NO_UPLOAD, KIT_READ_BACK))
                    break
                else:
                    why = f"it says {kit_status(last['status'])}" if last else "no reply"
                    raise fail(f"the minichord didn't take the piece at byte {k * U.PIECE} of {size} in three tries "
                               f"({why}): the upload is called off")
                if progress:
                    progress(end, size)
            since = self._kit_mark()
            r = self._kit_ask(frames[-1], self._status_of(U.COMMIT), 3.0)
            if r is None:
                info = self.kit_info()
                if info and info["state"] == 1 and info["size"] == size and info["crc"] == crc:
                    pending = False
                    return info            # COMMIT's reply lost on the way, and the kit stored all the same
                # COMMIT itself lost (the upload still open), or INFO unanswered too: ABORT. Any other state
                # is a COMMIT refused with only its reply lost: the upload is over
                raise fail("the minichord didn't answer COMMIT within 3 s" +
                           (f", and says its slot is {U.STATES[info['state']]}" if info and info["state"] < len(U.STATES) else ""),
                           info is not None and info["state"] != 2)
            if r["status"]:                # 5: nothing open; anything else ABORT, as a COMMIT damaged on the way (3) leaves it open
                raise fail(f"the minichord didn't keep the kit: {kit_status(r['status'])}", r["status"] == KIT_NO_UPLOAD)
            pending = False
            return self._kit_take(lambda r: r["kind"] == "info", 1.0, since) or self.kit_info()
        except BaseException:
            if pending:                    # Ctrl-C, or anything else, between BEGIN sent and COMMIT answered
                pending = False
                try:
                    self.kit_abort()
                except Exception:
                    pass                   # if it can: the minichord ends the upload after 5 s of silence anyway
            raise
        finally:
            with self.lock:
                self.kit_mine = False
                self.kit_mine_until = time.monotonic() + KIT_OWN_STRAYS
            self.kit_thread = None
            self.kit_quiet.set()

    def others_on_port(self):
        """Linux: the other programs connected to the minichord's MIDI ports, [(ALSA client name, program)],
        from `aconnect -l`; None when that can't be read, and [] elsewhere (macOS shares a port freely,
        Windows lets one program at a time have it)"""
        if not sys.platform.startswith("linux") or not self.alsa_clients:
            return []
        try:
            listing = subprocess.run(["aconnect", "-l"], capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.TimeoutExpired):
            return None
        out = []
        for _c, name, pid in alsa_peers(listing, self.alsa_clients, os.getpid(), seq_client_names()):
            program = None
            if pid:
                try:
                    with open(f"/proc/{pid}/comm") as f:
                        program = f.read().strip()
                except OSError:
                    pass
            out.append((name, program))
        return out


# the browsers that have Web MIDI, as whole words of their process's name (/proc's comm: "chrome",
# "chromium-browse", "firefox-esr", "msedge") or of their ALSA client's, where Firefox's says "WebMIDI"
WEB_MIDI_BROWSERS = {"chrome", "chromium", "firefox", "msedge", "edge", "opera", "brave", "vivaldi"}
WEB_MIDI_CLIENTS = WEB_MIDI_BROWSERS | {"webmidi"}
WORD = re.compile(r"[a-z0-9]+")


def web_midi_browser(name, program=None):
    """whether a program connected to the minichord's port is a browser, minicontrol's tab maybe; others
    (PipeWire's or JACK's MIDI bridges, Midi Through) only pass messages on. By the process's name first,
    then the client's, each as whole words, so a client called "Operator" isn't Opera"""
    words = lambda text: set(WORD.findall((text or "").lower()))
    return bool(words(program) & WEB_MIDI_BROWSERS or words(name) & WEB_MIDI_CLIENTS)


def kit_status(status):
    """a kit status reply's code in words"""
    return U.STATUSES[status] if status < len(U.STATUSES) else f"status {status}"


def seq_client_names(path="/proc/asound/seq/clients"):
    """every ALSA sequencer client's name, from the kernel's own list. `aconnect -l` leaves out a client
    whose ports can't be subscribed to, and that is how Chrome's Web MIDI shows ("Chrome (input)" and
    "Chrome (output)", connected to every port), so without this a browser on the port has no name."""
    try:
        with open(path) as f:
            text = f.read()
    except OSError:
        return {}
    return {int(c): n for c, n in re.findall(r'^Client\s+(\d+) : "(.*)"', text, re.M)}


def alsa_peers(listing, clients, me=None, known=None):
    """Who else is connected to the ALSA sequencer clients given (the minichord's), from `aconnect -l`'s
    listing: [(client, its name, its process or None)], leaving out process me's own clients (minictl's,
    and a pretend minichord's in the same process). A client the listing doesn't show takes its name from
    known (seq_client_names), else "?"."""
    names, pids, peers, client = {}, {}, set(), None
    for line in listing.splitlines():
        m = re.match(r"client (\d+): '(.*)' \[(.*)\]", line)
        if m:
            client = int(m.group(1))
            names[client] = m.group(2)
            p = re.search(r"pid=(\d+)", m.group(3))
            pids[client] = int(p.group(1)) if p else None
            continue
        m = re.match(r"\s+(?:Connecting To|Connected From): (.*)", line)
        if m and client in clients:
            peers |= {int(c) for c, _p in re.findall(r"(\d+):(\d+)", m.group(1))}
    known = known or {}
    return [(c, names.get(c) or known.get(c, "?"), pids.get(c)) for c in sorted(peers - set(clients))
            if me is None or pids.get(c) != me]


# ---- serial ----------------------------------------------------------------------------------------

def serial_port():
    """the minichord's serial port (Teensy's vendor id), or None"""
    try:
        from serial.tools import list_ports
    except ImportError:
        raise RuntimeError("the serial log needs pyserial: pip install pyserial")
    ports = [p for p in list_ports.comports() if p.vid == PJRC_VID]
    ours = [p for p in ports if "minichord" in f"{p.product} {p.description}".lower()]
    pick = (ours or ports or [None])[0]
    return pick.device if pick else None


# ---- USB audio -------------------------------------------------------------------------------------

def audio_input(want=None):
    """The minichord's USB audio input for sounddevice: a device index, or "pulse" with PULSE_SOURCE
    set when the desktop's sound server holds the card (Linux, USB audio mode 1)."""
    try:
        import sounddevice as sd
    except ImportError:
        raise RuntimeError("recording needs sounddevice: pip install sounddevice")
    want = want or os.environ.get("MINICTL_AUDIO")
    if want:
        return int(want) if want.isdigit() else want
    devs = sd.query_devices()
    hits = [i for i, d in enumerate(devs) if "minichord" in d["name"].lower() and d["max_input_channels"] >= 2]
    if sys.platform.startswith("linux"):
        try:
            src = subprocess.run(["pactl", "list", "short", "sources"], capture_output=True, text=True,
                                 timeout=5).stdout
            m = re.search(r"^\d+\s+(\S*minichord\S*analog\S*)", src, re.M | re.I)
        except (OSError, subprocess.TimeoutExpired):
            m = None
        if m and any(d["name"] == "pulse" for d in devs):
            os.environ["PULSE_SOURCE"] = m.group(1)
            return "pulse"
    if hits:
        return hits[0]
    raise NotFound("no minichord audio input found (is USB audio on? `minictl get usb-audio`); "
                   "name one with --device (python -m sounddevice lists them)")


def record(path, seconds, device=None, rate=44100):
    import sounddevice as sd, wave
    dev = audio_input(device)
    frames = []
    with sd.RawInputStream(device=dev, samplerate=rate, channels=2, dtype="int16") as s:
        left = int(seconds * rate)
        while left > 0:
            data, _ = s.read(min(left, 4096))
            frames.append(bytes(data))
            left -= min(left, 4096)
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(frames))
    return dev


# ---- flashing --------------------------------------------------------------------------------------

def teensy_tools():
    """the folder holding PJRC's teensy_post_compile, from PlatformIO or the Arduino IDE"""
    exe = "teensy_post_compile" + (".exe" if os.name == "nt" else "")
    env = os.environ.get("MINICTL_TEENSY_TOOLS")
    home = os.path.expanduser("~")
    places = [env] if env else []
    places += [os.path.join(home, ".platformio", "packages", "tool-teensy")]
    for arduino in (os.path.join(home, ".arduino15"), os.path.join(home, "Library", "Arduino15"),
                    os.path.join(os.environ.get("LOCALAPPDATA", home), "Arduino15")):
        places += sorted(glob.glob(os.path.join(arduino, "packages", "teensy", "tools", "teensy-tools", "*")),
                         reverse=True)
    for p in places:
        if p and os.path.exists(os.path.join(p, exe)):
            return p, os.path.join(p, exe)
    raise NotFound("can't find PJRC's teensy tools (teensy_post_compile). Install PlatformIO or the Arduino IDE "
                   "with Teensyduino, or point MINICTL_TEENSY_TOOLS at the folder")


def flash_hazards():
    """reasons not to flash now, on Linux: the kernel with the MIDI unplug hang, another upload running,
    or a USB stack already stuck"""
    if not sys.platform.startswith("linux"):
        return []
    out = []
    k = os.uname().release
    if k.startswith("6.1.0-53"):
        out.append(f"kernel {k} (6.1.187) hangs USB when a MIDI device drops off; boot 6.1.0-50 first")
    busy = subprocess.run(["pgrep", "-f", "teensy_post_compile|teensy_loader_cli|pio run -t upload|pio run --target upload"],
                          capture_output=True, text=True).stdout.split()
    mine, pid = set(), os.getpid()            # minictl and the shells that started it, whose command
    while pid > 1 and pid not in mine:         # lines can mention the same words
        mine.add(pid)
        try:
            with open(f"/proc/{pid}/stat") as f:
                pid = int(f.read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
    busy = [p for p in busy if int(p) not in mine]
    if busy:
        out.append(f"another upload is running (pids {', '.join(busy)})")
    d = subprocess.run("ps -eLo stat,wchan:40 | awk '$1 ~ /D/' | grep -c snd_use_lock", shell=True,
                       capture_output=True, text=True).stdout.strip()
    if d not in ("", "0"):
        out.append("threads are stuck in snd_use_lock: the USB stack is already hung")
    return out


def flash(hexfile, wait=60, log=print):
    """Puts a .hex on the minichord through PJRC's loader (it presses the program button by itself),
    then waits for the minichord's MIDI port to come back. Close every MIDI program first."""
    tools, post = teensy_tools()
    hexfile = os.path.abspath(hexfile)
    folder, name = os.path.split(hexfile)
    stem = name[:-4] if name.lower().endswith(".hex") else name
    r = subprocess.run([post, f"-file={stem}", f"-path={folder}", f"-tools={tools}", "-board=TEENSY40", "-reboot"],
                       capture_output=True, text=True, timeout=120)
    said = (r.stdout + r.stderr).strip()
    if r.returncode != 0:
        raise RuntimeError(f"the loader failed ({r.returncode}): {said[-300:]}")
    t = time.monotonic()
    time.sleep(3)
    while time.monotonic() - t < wait:
        outs, _ = list_ports()
        if pick_port(outs) is not None:
            time.sleep(2)
            return time.monotonic() - t
        time.sleep(0.5)
    raise RuntimeError(f"flashed, but the minichord's MIDI port didn't come back within {wait} s")

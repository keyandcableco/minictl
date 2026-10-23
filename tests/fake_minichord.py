"""A pretend minichord on virtual MIDI ports, answering minictl's SysEx the way the firmware does
(control_command in src/main.cpp), so the device commands can be tested without the instrument.
Virtual ports exist on Linux (ALSA) and macOS (CoreMIDI), not on Windows.

    python tests/fake_minichord.py [stock]   # run one; then minictl --port fakechord info

From firmware 113 it has the user drum kit's slot too (FakeKitSlot, below), on a pretend flash, so a kit
can be sent to it from minictl, or from minicontrol in Chrome, which sees the virtual ports as it sees the
instrument's. minicontrol looks for "minichord" in a port's name, so give it one with that in it:

    python tests/fake_minichord.py --name "minichord fake"      # then minicontrol's kit page, or
    minictl --port "minichord fake" kit send tests/kits/synthetic.mckit

It prints what its kit slot does as it happens. --room sets the slot's room (256, 192, 128, 64 kB, or 0
for a firmware too big to leave one), --kit starts it with a kit stored, --rhythm with the rhythm running
(so BEGIN is answered busy until rhythm mode goes off), and --lose-every N loses the reply to every Nth
DATA piece, so the sender has to send it again.
"""
import argparse, os, sys, threading, time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from minictl.settings import Settings, BANKS, pm   # noqa: E402
from minictl import user_kit as U                  # noqa: E402

PAGE = 256
LOCKS_VERSION = 76            # the locks (LOCKS in src/main.cpp): command 9 reports them, 10 resets them, 385 sets one
LOCK_ADDRESS = 385
FIXED = (244, 384)            # USB audio and upside down: the instrument's by their own code, reported first
LOOPER_ADDRESS = 256          # the looper: 1 record, 2 play, 3 stop, 4 clear, an action and never kept
KIT_VERSION = 113             # the user drum kit's slot (include/user_kit.h)

# Where the kit lives (user_kit.md section 2): the header in a 4 kB sector of its own at KIT_HEADER, the
# sound from data_base up to KIT_TOP, data_base the first 64 kB boundary after the firmware and never
# below KIT_FLOOR. The pretend flash is KIT_FLOOR to the end of the header's sector.
KIT_HEADER = KIT_TOP = 0x601E0000
KIT_FLOOR = 0x601A0000
FLASH_END = KIT_HEADER + 4096
SECTOR, FLASH_PAGE = 4096, 256        # the flash's erase and program sizes
MIN_ROOM = 65536                      # less than this and the slot is disabled
SILENCE = 5.0                         # an upload with no DATA, COMMIT, ABORT or CLEAR for this long is ended


class FakeKitSlot:
    """The minichord's side of the user drum kit, as user_kit.md's section 4 has it and the firmware's
    include/user_kit_core.h does it, step for step, on a pretend NOR flash: erasing a 4 kB sector sets it
    to FF, programming a page (256 bytes, never across one) ANDs the bytes in, and what's read back is
    what the flash holds. The header is held in RAM through an upload and written last, with data_base
    filled in and its CRC made again; each sector of the sound is erased the first time the sound reaches
    it, even one that reads all FF (an erase cut short can read FF and not hold a page well), each page
    programmed when full and read back. Only the slot's own flash is ever read, erased or programmed: the
    sound's from data_base up to KIT_TOP, and the header's sector. A header sector that doesn't start MCK1
    holds no kit (empty, reason 1), blank or not. The reference's own pieces (unpacking, CRCs, check_image,
    the replies) come from minictl's user_kit.py.

    The firmware notes a BEGIN when it comes in and opens it from its loop, a moment later; this opens it
    at the end of the message (the loop's next pass) unless deferred is set, when a test runs the pass
    itself (loop_pass). Meanwhile the BEGIN is waiting: ABORT calls it off (0), CLEAR calls it off and
    clears, a second BEGIN is busy (1), DATA and COMMIT find no upload (5) and INFO tells the kit stored.
    Its 5 s of silence are looked at (poll) whenever anything comes in, which is all anyone can see of
    them, and every half second when the fake runs on its own; only a DATA starts them again (COMMIT,
    ABORT and CLEAR end the upload), never INFO, BEGIN or a message that can't be read. For tests: log is
    every kit message heard, replies every reply sent, lose_reply() loses the next replies to an op, and
    clock is the time in seconds the silence is counted in (time.monotonic, or a test's own)."""

    def __init__(self, send, data_base=KIT_FLOOR, busy=lambda: False, say=None, clock=time.monotonic):
        self.send_out = send
        self.busy = busy              # the rhythm running, or the looper recording or playing
        self.say = say                # what happens, in words, for the fake run on its own
        self.clock = clock
        self.flash = bytearray(b"\xff" * (FLASH_END - KIT_FLOOR))
        self.erases, self.programs = [], []      # the sectors erased and the pages programmed, in order
        self.log, self.replies = [], []
        self.lose, self.losing, self.lose_every, self.data_seen = [], False, 0, 0
        self.open = self.muted = False
        self.deferred = self.begin_waiting = False
        self.begin_message = None
        self.prev = None
        self.last_heard = 0.0
        self.move(data_base)

    # ---- the flash ----

    def inside(self, addr, n):
        """whether n bytes from addr are the slot's own flash: the sound's room, or the header's sector"""
        return self.data_base <= addr and addr + n <= KIT_TOP or KIT_HEADER <= addr and addr + n <= FLASH_END

    def read(self, addr, n):
        assert self.inside(addr, n), f"read outside the kit's flash at {addr:08X}"
        i = addr - KIT_FLOOR
        return bytes(self.flash[i:i + n])

    def erase(self, addr):
        assert addr % SECTOR == 0 and self.inside(addr, SECTOR), f"erase at {addr:08X}"
        i = addr - KIT_FLOOR
        self.flash[i:i + SECTOR] = b"\xff" * SECTOR
        self.erases.append(addr)

    def program(self, addr, data):
        assert data and addr // FLASH_PAGE == (addr + len(data) - 1) // FLASH_PAGE, "a program crosses a page"
        assert self.inside(addr, len(data)), f"program outside the kit's flash at {addr:08X}"
        i = addr - KIT_FLOOR
        for j, b in enumerate(data):
            self.flash[i + j] &= b        # NOR flash: programming only clears bits
        self.programs.append(addr)

    def program_checked(self, addr, data):
        self.program(addr, data)
        return self.read(addr, len(data)) == bytes(data)

    # ---- the stored kit ----

    def move(self, data_base):
        """where the sound starts, as a firmware of another size puts it: the stored kit is checked again"""
        self.data_base = data_base
        self.capacity = max(0, KIT_TOP - data_base)
        self.refresh()

    def refresh(self):
        """the stored kit's state from the flash, as at boot: disabled, empty, damaged or valid"""
        self.state, self.reason, self.stored = 0, 0, None
        if self.capacity < MIN_ROOM:
            self.state, self.reason = 4, 7
            return
        h = self.read(KIT_HEADER, U.HEADER_BYTES)
        if h[:4] != U.MAGIC:
            # no kit's header, blank or not: an old filesystem's bytes (the sector was LittleFS's before
            # firmware 77) or a header write cut short aren't a damaged kit
            self.state, self.reason = 0, 1
            return
        # in flash the size is the header's own, so step 1 has nothing to say: the sound is read as far as
        # data_bytes, never past the room, and step 5 finds data_bytes out of its rule
        data_bytes = int.from_bytes(h[12:16], "little")
        image = h + self.read(self.data_base, min(data_bytes, self.capacity) & ~3)
        why = U.check_image(image, self.data_base, self.capacity)
        if why:
            self.state, self.reason = 3, why
            return
        self.state, self.stored = 1, image

    def store(self, image):
        """an image put in the slot as a good upload leaves it (a test's, or --kit's), checked first"""
        assert U.check_image(image) == 0 and len(image) - U.HEADER_BYTES <= self.capacity
        self.erase(KIT_HEADER)
        sound = image[U.HEADER_BYTES:]
        for at in range(0, len(sound), SECTOR):
            self.erase(self.data_base + at)
        for at in range(0, len(sound), FLASH_PAGE):
            self.program(self.data_base + at, sound[at:at + FLASH_PAGE])
        self.program(KIT_HEADER, U.set_field(image[:U.HEADER_BYTES], "data_base", self.data_base))
        self.refresh()
        assert self.state == 1

    # ---- replies ----

    def reply(self, msg):
        if self.losing:
            return
        self.replies.append(bytes(msg))
        self.send_out(list(msg))

    def status(self, op, status):
        self.reply(U.status_reply(op, status, self.next if self.open else 0))

    def info(self):
        if self.open:
            self.reply(U.info_reply(2, 0, self.capacity, self.data_base))
        elif self.state == 1:
            self.reply(U.info_reply(1, 0, self.capacity, self.data_base, **U.image_info(self.stored)))
        else:
            self.reply(U.info_reply(self.state, self.reason, self.capacity, self.data_base))

    def lose_reply(self, op, skip=0):
        """the replies to the next message of op, after skip more of them, go unsent: lost on the way"""
        self.lose.append([op, skip])

    def tell(self, text):
        if self.say:
            self.say(text)

    # ---- the messages ----

    def receive(self, m):
        """every SysEx heard, whole; True when it was a kit message (F0 7D 6B ...), answered or not"""
        m = bytes(m)
        if len(m) < 4 or m[:3] != U.SYSEX:
            return False
        self.poll()
        self.log.append(m)
        op = m[3]
        if op >= 0x40:
            return True                   # a reply's shape (or an F7 where the op should be): never answered
        self.losing = False
        for entry in self.lose:
            if entry[0] == op:
                if entry[1]:
                    entry[1] -= 1
                else:
                    self.lose.remove(entry)
                    self.losing = True
                break
        if op == U.DATA and self.lose_every:
            self.data_seen += 1
            self.losing = self.losing or self.data_seen % self.lose_every == 0
        formed = len(m) >= 7 and m[-1] == 0xF7 and all(b < 0x80 for b in m[1:-1])
        malformed = len(m) != 7 or m[4] or m[5]          # COMMIT's and ABORT's shape: 00 00 after the op
        if not formed:
            self.status(op, 3)
        elif op == U.BEGIN:
            if len(m) != 14:
                self.status(op, 3)
            elif self.open or self.begin_waiting:
                self.begin(m, True)       # refused: one open, or one noted and waiting (or a reason before)
            else:
                self.begin_waiting, self.begin_message = True, m
        elif op == U.DATA:
            self.data(m)                  # 5 with no upload open (a BEGIN still waiting too), before its shape
        # COMMIT and ABORT: 5 with no upload, before their shape; a BEGIN waiting has no upload for COMMIT,
        # and ABORT calls it off: it never opens, and the kit stored is left as it is
        elif op == U.COMMIT:
            if not self.open:
                self.status(op, 5)
            elif malformed:
                self.status(op, 3)
            else:
                self.commit()
        elif op == U.ABORT:
            if not self.open and not self.begin_waiting:
                self.status(op, 5)
            elif malformed:
                self.status(op, 3)
            elif not self.open:
                self.begin_waiting = False
                self.status(op, 0)
                self.tell("ABORT: the BEGIN waiting to open called off")
            else:
                self.finish(False)
                self.status(op, 0)
                self.tell("ABORT: upload ended, no kit")
        elif op == U.INFO:
            self.info() if len(m) == 7 else self.status(op, 3)
        elif op == U.CLEAR:
            if m[4:-1] != b"\x00CL":
                self.status(op, 3)
            elif self.capacity < MIN_ROOM:
                self.status(op, 11)       # no slot, so no kit to erase
            else:
                self.clear()
        else:
            self.status(op, 3)
        if not self.deferred:
            self.loop_pass()
        self.losing = False
        return True

    def loop_pass(self):
        """the firmware's loop, a moment after a message: a BEGIN noted is opened, or refused (the rhythm or
        the looper running then is busy)"""
        if self.begin_waiting:
            self.begin_waiting = False
            self.begin(self.begin_message, self.busy())

    def poll(self):
        """5 s with no DATA heard ends an upload, with no kit and no reply"""
        if self.open and self.clock() - self.last_heard >= SILENCE:
            self.finish(False)
            self.tell(f"upload ended: {SILENCE:g} s with no DATA, COMMIT, ABORT or CLEAR, no kit")

    def heard(self):
        """the silence starts again: a DATA heard, whether taken, a repeat, out of order or with a bad CRC
        (a computer that is there, sending), but not one that couldn't be read (status 3)"""
        self.last_heard = self.clock()

    def begin(self, m, busy):
        """a well formed BEGIN (14 bytes): refused, in the contract's order, or (not busy) the upload opened"""
        size, crc = U.decode_n(m[5:8]), U.decode_n(m[8:13])
        if crc > 0xFFFFFFFF:
            return self.status(U.BEGIN, 3)
        status = 0
        if m[4] != U.PROTO:
            status = 4
        elif self.begin_waiting:
            status = 1                    # another BEGIN noted, still to open
        elif self.capacity < MIN_ROOM:
            status = 11
        elif size < U.HEADER_BYTES or size - U.HEADER_BYTES > self.capacity or size % 4:
            status = 2
        elif self.open or busy:
            status = 1
        if status:
            self.tell(f"BEGIN refused: {U.STATUSES[status]}")
            return self.status(U.BEGIN, status)
        # muted as a preset save is, the kit given up before its flash changes, then its header erased
        self.muted = True
        self.state, self.reason, self.stored = 0, 1, None
        self.erase(KIT_HEADER)
        self.open = True
        self.up_size, self.up_crc, self.next, self.prev = size, crc, 0, None
        self.header = bytearray(b"\xff" * U.HEADER_BYTES)
        self.page = bytearray(FLASH_PAGE)
        self.last_heard = self.clock()
        self.tell(f"BEGIN: an upload of {size} bytes opened")
        self.status(U.BEGIN, 0)

    def data(self, m):
        if not self.open:
            return self.status(U.DATA, 5)
        if len(m) < 13:
            return self.status(U.DATA, 3)
        offset, crc = U.decode_n(m[4:7]), U.decode_n(m[7:12])
        if crc > 0xFFFFFFFF:
            return self.status(U.DATA, 3)
        if offset != self.next or self.next >= self.up_size:
            self.heard()
            # the previous piece again, its reply lost on the way: answered, not written again
            return self.status(U.DATA, 0 if self.prev == (offset, crc) else 6)
        want = min(U.PIECE, self.up_size - self.next)
        packed = m[12:-1]
        try:
            raw = U.unpack87(packed) if len(packed) == U.packed_length(want) else None
        except ValueError:
            raw = None
        if raw is None or len(raw) != want:
            return self.status(U.DATA, 3)
        self.heard()
        if U.crc32(raw) != crc:
            return self.status(U.DATA, 7)
        for j, b in enumerate(raw):
            i = offset + j
            if i < U.HEADER_BYTES:
                self.header[i] = b
                continue
            rel = i - U.HEADER_BYTES
            if rel % SECTOR == 0:
                self.erase(self.data_base + rel)   # the first byte to reach it (pieces come in order), FF or not
            self.page[rel % FLASH_PAGE] = b
            if rel % FLASH_PAGE == FLASH_PAGE - 1 and not self.program_checked(self.data_base + rel - (FLASH_PAGE - 1), self.page):
                self.finish(False)
                return self.status(U.DATA, 8)
        self.prev = (offset, crc)
        self.next += len(raw)
        self.status(U.DATA, 0)

    def commit_failed(self, status, header_written):
        if header_written:
            self.erase(KIT_HEADER)
        self.finish(False)
        self.tell(f"COMMIT failed: {U.STATUSES[status]}, no kit")
        self.status(U.COMMIT, status)

    def commit(self):
        if not self.open:
            return self.status(U.COMMIT, 5)
        fill = (self.next - U.HEADER_BYTES) % FLASH_PAGE if self.next > U.HEADER_BYTES else 0
        if fill and not self.program_checked(self.data_base + (self.next - U.HEADER_BYTES) - fill, self.page[:fill]):
            return self.commit_failed(8, False)
        if self.next != self.up_size:
            return self.commit_failed(3, False)
        image = bytes(self.header) + self.read(self.data_base, self.up_size - U.HEADER_BYTES)
        if U.crc32(image) != self.up_crc:
            return self.commit_failed(9, False)
        # the image as it was sent, header and sound, through all of check_image: data_base 0 on the wire
        if U.check_image(image, 0, self.capacity):
            return self.commit_failed(10, False)
        header = U.set_field(image[:U.HEADER_BYTES], "data_base", self.data_base)
        if not self.program_checked(KIT_HEADER, header):
            return self.commit_failed(8, True)
        self.refresh()
        if self.state != 1:
            return self.commit_failed(10, True)
        self.finish(True)
        self.tell(f"COMMIT: kit {self.stored[20:40].rstrip(bytes(1)).decode()!r} stored, {self.up_size} bytes")
        self.status(U.COMMIT, 0)
        self.info()

    def clear(self):
        """CLEAR (a well formed one, the slot not disabled): a BEGIN waiting called off, any upload ended,
        and the header erased"""
        self.begin_waiting = False
        if self.open:
            self.erase(KIT_HEADER)
            self.finish(False)
        else:
            self.muted = True
            self.erase(KIT_HEADER)
            self.refresh()
            self.muted = False
        self.tell("CLEAR: no kit")
        self.status(U.CLEAR, 0)

    def finish(self, kit):
        """upload mode left, however: the DAC back on, and without a kit the state what the flash says"""
        self.open = False
        self.prev = None
        if not kit:
            self.refresh()
        self.muted = False


class FakeMinichord:
    def __init__(self, name="fakechord", version=None, firmware="fork", kit_base=KIT_FLOOR, say=None):
        import rtmidi
        s = Settings(firmware)
        self.firmware = firmware
        self.version = version or s.version
        self.defaults = s.defaults()[:PAGE if firmware == "stock" else None]
        self.defaults[7] = self.version
        self.banks = []
        for b in range(BANKS):
            v = list(self.defaults)
            v[20] = 30 * b                    # a bank color each, to tell them apart
            v[1] = b
            if firmware == "fork":
                Settings.set_preset_name(v, f"Bank {b + 1}")
            self.banks.append(v)
        self.live = list(self.banks[0])
        self.lock_kinds = {a: p.get("lock") for a, p in s.params.items()}
        self.locks = {}               # address: the instrument's value
        if self.has_locks():
            self.lock_defaults()      # the first boot locks the default ones at the first preset's values
        self.pushed = None
        self.rhythm = False
        self.looper = "empty"
        self.saves, self.notes, self.log = [], [], []
        self.last = time.monotonic()
        self.lock = threading.Lock()
        self.inp = rtmidi.MidiIn(name=name)
        self.out = rtmidi.MidiOut(name=name)
        self.inp2 = rtmidi.MidiIn(name=name)
        self.inp.ignore_types(sysex=False)
        self.inp.open_virtual_port(f"{name} MIDI 1")
        self.out.open_virtual_port(f"{name} MIDI 1")
        self.inp2.open_virtual_port(f"{name} MIDI 2")
        # the user drum kit's slot, from firmware 113; kit_answers False makes it as silent as older firmware
        self.kit = None
        if firmware == "fork" and self.version >= KIT_VERSION:
            self.new_kit_slot(kit_base, say)
        self.kit_answers = True
        self.inp.set_callback(lambda ev, _: self._heard(ev[0], 1))
        self.inp2.set_callback(lambda ev, _: self._heard(ev[0], 2))

    def close(self):
        for p in (self.inp, self.out, self.inp2):
            p.close_port()

    def new_kit_slot(self, kit_base=KIT_FLOOR, say=None):
        """the kit slot as a new instrument has it: its flash erased, nothing heard yet"""
        with self.lock:
            self.kit = FakeKitSlot(self.out.send_message, kit_base, say=say,
                                   busy=lambda: self.rhythm or self.looper in ("recording", "playing"))
        return self.kit

    def settle(self, quiet=0.3, most=20):
        """wait until nothing has come for a moment: a slow machine can still be working through a test's
        hundreds of messages when the next one starts"""
        t0 = time.monotonic()
        while time.monotonic() - self.last < quiet and time.monotonic() - t0 < most:
            time.sleep(0.05)

    def _heard(self, msg, cable):
        self.last = time.monotonic()
        with self.lock:
            if self.kit:
                self.kit.poll()
            if self.kit and self.kit_answers and msg[0] == 0xF0 and self.kit.receive(msg):
                return
            if self.kit and self.kit.open:
                return                        # the upload loop drops every other message
            if msg[0] != 0xF0:
                self.notes.append((cable, msg))
                return
            body = msg[1:-1]
            if len(body) != 4:
                return
            if self.firmware == "stock" and body[0] in (0x7E, 0x7F):
                return                        # stock takes these for universal SysEx and drops them
            address, value = body[0] + 128 * body[1], body[2] + 128 * body[3]
            self.log.append((address, value))
            if address == 0:
                if self.firmware == "stock" and body[2] > 4:
                    return                    # stock firmware knows commands 0 to 4
                self.command(body[2], body[3])
            elif address < len(self.live) and (self.firmware == "stock" or (address not in (1, 7) and address not in pm.RESERVED)):
                if address >= PAGE and self.version < 31:
                    return
                if address == LOCK_ADDRESS and self.has_locks():
                    self.lock_action(value)
                    return
                if address == LOOPER_ADDRESS and self.firmware == "fork":
                    self.looper = {1: "recording", 2: "playing", 3: "stopped", 4: "empty"}.get(value, self.looper)
                    return
                self.live[address] = value
                if address in self.locks:
                    self.locks[address] = value   # a locked setting changed live is the instrument's new value

    def has_locks(self):
        return self.firmware == "fork" and self.version >= LOCKS_VERSION

    def lockable(self, a):
        return 0 < a < len(self.live) and self.lock_kinds.get(a, "never") not in ("never", "fixed")

    def lock_defaults(self):
        for a in sorted(self.lock_kinds):
            if self.lock_kinds[a] == "default" and a not in self.locks:
                self.locks[a] = self.live[a]

    def lock_action(self, value):
        """lock (385): an address locks it at its present value, 1024 + an address unlocks it"""
        self.live[LOCK_ADDRESS] = 0           # an action, never a value to keep
        a = value if value < 1024 else value - 1024
        if value <= 0 or not self.lockable(a):
            return
        if value < 1024:
            self.locks.setdefault(a, self.live[a])
        else:
            self.locks.pop(a, None)

    def report_locks(self):
        """command 9: 7D 6C, a count, then two bytes an address, the fixed ones first"""
        addresses = list(FIXED) + list(self.locks)
        data = [b for a in addresses for b in (a % 128, a // 128)]
        self.out.send_message([0xF0, 0x7D, 0x6C, len(addresses)] + data + [0xF7])

    def load(self, values):
        """a preset load: the instrument's own settings and the locked ones stay as the instrument has them"""
        before = self.live
        self.live = list(values)
        if self.has_locks():
            for a in FIXED:
                self.live[a] = before[a]
            for a, v in self.locks.items():
                self.live[a] = v

    def report(self, page):
        vals = self.live[page * PAGE:(page + 1) * PAGE]
        data = [b for v in vals for b in (v % 128, v // 128)]
        self.out.send_message([0xF0] + ([0x7D, 0x6D, page] if page else []) + data + [0xF7])

    def command(self, cmd, param):
        if cmd == 0:
            self.report(0)
        elif cmd == 7:
            if self.firmware == "fork" and self.version >= 31 and param == 1:
                self.report(1)
        elif cmd == 2:
            self.saves.append(param)
            self.banks[param] = list(self.live)
            self.banks[param][1] = param
        elif cmd == 3:
            self.banks[param] = list(self.defaults)
            self.banks[param][1] = param
            self.load(self.banks[param])
        elif cmd == 4 and param < BANKS:
            self.load(self.banks[param])
            self.live[1] = param
            self.pushed = None
            self.report(0)
        elif cmd == 5:
            if self.pushed is None or param == 1:
                self.pushed = list(self.live)
        elif cmd == 6 and self.pushed:
            self.live = list(self.pushed)
            self.pushed = None
            self.report(0)
        elif cmd == 8:
            self.rhythm = (not self.rhythm) if param == 2 else param == 1
        elif cmd == 9 and self.has_locks():
            self.report_locks()
        elif cmd == 10 and self.has_locks():
            self.locks = {}
            if param == 1:
                self.lock_defaults()
            self.report_locks()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="A pretend minichord on virtual MIDI ports")
    ap.add_argument("firmware", nargs="?", choices=["fork", "stock"], default="fork")
    ap.add_argument("--name", default="fakechord", help='its ports\' name; minicontrol wants "minichord" in it')
    ap.add_argument("--version", type=int, help="the firmware version it reports")
    ap.add_argument("--room", type=int, default=256, choices=[256, 192, 128, 64, 0],
                    help="the kit slot's room in kB (0: a firmware too big to leave any, the slot disabled)")
    ap.add_argument("--kit", help="a .mckit (or bare image) stored in the slot to start with")
    ap.add_argument("--rhythm", action="store_true", help="start with the rhythm running: BEGIN is busy until it stops")
    ap.add_argument("--lose-every", type=int, default=0, metavar="N", help="lose the reply to every Nth DATA piece")
    a = ap.parse_args()
    f = FakeMinichord(a.name, a.version, a.firmware, kit_base=KIT_TOP - 1024 * a.room,
                      say=lambda text: print(f"{time.strftime('%H:%M:%S')} kit: {text}", flush=True))
    f.rhythm = a.rhythm
    if f.kit:
        f.kit.lose_every = a.lose_every
        if a.kit:
            from minictl.kit import Kit
            f.kit.store(Kit.load(a.kit).image)
    slot = (f"; its kit slot {a.room} kB, {U.STATES[f.kit.state]}" if f.kit else "; no kit slot")
    print(f"a fake minichord, {a.firmware} firmware {f.version}, on '{a.name} MIDI 1/2'{slot}; Ctrl-C ends it")
    try:
        while True:
            time.sleep(0.5)
            if f.kit:
                with f.lock:
                    f.kit.poll()
    except KeyboardInterrupt:
        f.close()

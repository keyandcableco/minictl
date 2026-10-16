"""A pretend minichord on virtual MIDI ports, answering minictl's SysEx the way the firmware does
(control_command in src/main.cpp), so the device commands can be tested without the instrument.
Virtual ports exist on Linux (ALSA) and macOS (CoreMIDI), not on Windows.

    python tests/fake_minichord.py [stock]   # run one; then minictl --port fakechord info
"""
import os, sys, threading, time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from minictl.settings import Settings, BANKS, pm   # noqa: E402

PAGE = 256
LOCKS_VERSION = 76            # the locks (LOCKS in src/main.cpp): command 9 reports them, 10 resets them, 385 sets one
LOCK_ADDRESS = 385
FIXED = (244, 384)            # USB audio and upside down: the instrument's by their own code, reported first


class FakeMinichord:
    def __init__(self, name="fakechord", version=None, firmware="fork"):
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
        self.inp.set_callback(lambda ev, _: self._heard(ev[0], 1))
        self.inp2.set_callback(lambda ev, _: self._heard(ev[0], 2))

    def close(self):
        for p in (self.inp, self.out, self.inp2):
            p.close_port()

    def settle(self, quiet=0.3, most=20):
        """wait until nothing has come for a moment: a slow machine can still be working through a test's
        hundreds of messages when the next one starts"""
        t0 = time.monotonic()
        while time.monotonic() - self.last < quiet and time.monotonic() - t0 < most:
            time.sleep(0.05)

    def _heard(self, msg, cable):
        self.last = time.monotonic()
        with self.lock:
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
    firmware = sys.argv[1] if len(sys.argv) > 1 else "fork"
    f = FakeMinichord(firmware=firmware)
    print(f"a fake minichord, {firmware} firmware {f.version}, on 'fakechord MIDI 1/2'; Ctrl-C ends it")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        f.close()

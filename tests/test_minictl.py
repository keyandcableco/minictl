"""minictl's tests: names, values and codes offline, then every device command against a fake minichord
on virtual MIDI ports (Linux and macOS). Nothing here talks to a real minichord.

    python tests/test_minictl.py
"""
import contextlib, io, json, os, shutil, subprocess, sys, tempfile, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
from minictl import cli, device, kit                # noqa: E402
from minictl import user_kit as U                  # noqa: E402
from minictl.settings import Settings, pm          # noqa: E402

S = Settings()
# the user drum kit's test vectors, the fork's firmware/test/kits as sync.py brings them
KITS = os.path.join(HERE, "kits")
SYNTHETIC = os.path.join(KITS, "synthetic.mckit")


def read_json(path):
    with open(path) as f:
        return json.load(f)


def write_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f)


FAKES = []        # the pretend minichords running, to wait for after each command


def run(*argv):
    """minictl's output and exit code for these arguments"""
    cli.PROFILE, cli.FORCED = None, False
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(argv))
    for f in FAKES:
        f.settle()
    return code, out.getvalue() + err.getvalue()


class Offline(unittest.TestCase):
    def test_names_are_unique_and_found(self):
        self.assertEqual(len(set(S.names.values())), len(S.names))
        for a, n in S.names.items():
            self.assertEqual(S.find(n), a, n)

    def test_find(self):
        self.assertEqual(S.find("mod-knob"), 14)
        self.assertEqual(S.find("reverb size"), 24)
        self.assertEqual(S.find("key"), 35)
        self.assertEqual(S.find("237"), 237)
        self.assertEqual(S.find("mod-main-control"), 14)       # the generated name stays an alias
        with self.assertRaises(ValueError):
            S.find("attack")                                     # chord or harp, filter or not: says which
        with self.assertRaises(ValueError):
            S.find("no-such-thing")

    def test_parse_and_show(self):
        v = S.defaults()
        self.assertEqual(S.parse(237, "werck", v), 4)
        self.assertEqual(S.parse(265, "choir", v), 3)
        self.assertEqual(S.parse(14, "reverb-size", v), 24)
        self.assertEqual(S.parse(14, "none", v), 0)
        self.assertEqual(S.parse(8, "on", v), 1)
        self.assertEqual(S.shown(237, 4), "4 (Werckmeister III)")
        self.assertEqual(S.shown(14, 24), "reverb-size")
        self.assertEqual(S.shown(24, 50), "0.5")
        with self.assertRaises(ValueError):
            S.parse(265, "banjo", v)

    def test_preset_names(self):
        v = [0] * 512
        Settings.set_preset_name(v, "Café night")
        self.assertEqual(Settings.preset_name(v), "Cafe night")
        Settings.set_preset_name(v, "x" * 40)
        self.assertEqual(len(Settings.preset_name(v)), 24)

    def test_code_round_trip(self):
        for p in S.shared_presets()[:10]:
            v = S.decode(p["value"])
            self.assertEqual(S.decode(S.encode(v)), v, p["name"])

    def test_get_from_code(self):
        code, out = run("get", "-c", "Jazzy Rhodes", "chord-voice", "temperament")
        self.assertEqual(code, 0)
        self.assertIn("(Equal)", out)

    def test_describe_offline(self):
        code, out = run("--json", "describe", "--base", "defaults", "warm pad chords, the mod knob opens the filter")
        self.assertEqual(code, 0)
        d = json.loads(out)
        self.assertIn("mod-knob", [c["name"] for c in d["changes"]])
        self.assertTrue(S.decode(d["code"]))

    def test_conflicts(self):
        _, out = run("--json", "describe", "--base", "Jazzy Rhodes", "drums only")
        code, out = run("conflicts", "--strict", json.loads(out)["code"])
        self.assertEqual(code, 1)
        self.assertIn("drums only", out)

    def test_ports_are_picked_by_name(self):
        linux = ["Midi Through:Midi Through Port-0 14:0", "minichord:minichord MIDI 1 24:0",
                 "minichord:minichord MIDI 2 24:1"]
        windows = ["Microsoft GS Wavetable Synth 0", "minichord 1", "MIDIOUT2 (minichord) 2"]
        mac = ["IAC Driver Bus 1", "minichord Port 2", "minichord Port 1"]
        for names in (linux, windows, mac):
            self.assertIn("1", names[device.pick_port(names, 1)].replace("MIDI 1", "1"))
            self.assertRegex(names[device.pick_port(names, 2)], r"2")
        self.assertIsNone(device.pick_port(["IAC Driver Bus 1"], 1))

    def test_scala(self):
        from minictl import tuning as T
        just = "! just.scl\n!\nFive-limit just\n 12\n!\n16/15\n9/8\n6/5\n5/4\n4/3\n45/32\n3/2\n8/5\n5/3\n9/5\n15/8\n2/1\n"
        name, off = T.parse_scala(just)
        self.assertEqual(name, "Five-limit just")
        self.assertAlmostEqual(off[4], -13.686, places=2)          # 5/4 is 13.7 cents flat
        self.assertEqual(T.stored(off)[363], 863)
        v = [0] * 512
        v[237] = 13
        v.__setitem__(slice(359, 371), [T.stored(off)[a] for a in range(359, 371)])
        self.assertEqual([round(c, 1) for c in T.parse_scala(T.to_scala("x", v))[1]], [round(c, 1) for c in off])
        for bad, why in (("x\n 19\n", "19 notes"), ("x\n 12\n100.0\n", "twelfth"),
                         ("x\n 12\n" + "100.0\n" * 11 + "1100.0\n", "not an octave")):
            with self.assertRaises(ValueError) as e:
                T.parse_scala(bad)
            self.assertIn(why, str(e.exception))
        with self.assertRaises(ValueError):
            T.check([0, 120] + [0] * 10)

    def test_notes(self):
        self.assertEqual(cli.note_number("C4"), 60)
        self.assertEqual(cli.note_number("Eb3"), 51)
        self.assertEqual(cli.note_number("F#5"), 78)
        self.assertEqual(cli.note_number("64"), 64)


def frames_file():
    with open(os.path.join(KITS, "synthetic.frames")) as f:
        return [bytes.fromhex(line) for line in f.read().split()]


class KitOffline(unittest.TestCase):
    """the user drum kit's file side against the reference's vectors, no minichord needed"""

    def test_reference_selftest(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ok = U.selftest()               # minictl's copy of user_kit.py, without the fork's test/kits beside it
        self.assertTrue(ok, out.getvalue())

    def test_broken_images_fail_with_their_reasons(self):
        # every broken image there is (sync.py brings the fork's as they are, however many), with its
        # reason; one that doesn't start MCK1 isn't taken for a kit at all
        expected = read_json(os.path.join(KITS, "broken", "expected.json"))
        self.assertEqual(set(expected), {n for n in os.listdir(os.path.join(KITS, "broken")) if n.endswith(".image")})
        for name, reason in expected.items():
            k = kit.Kit.load(os.path.join(KITS, "broken", name))
            self.assertEqual(k.reason(), reason, name)
            self.assertIn(f"reason {reason}" if k.image[:4] == b"MCK1" else "not a kit file", k.problem(), name)
        self.assertEqual(kit.Kit.load(os.path.join(KITS, "small.image")).reason(), 0)
        self.assertIsNone(kit.Kit.load(os.path.join(KITS, "small.image")).problem())

    def test_synthetic_kit(self):
        k = kit.Kit.load(SYNTHETIC)
        self.assertIsNone(k.problem())
        self.assertEqual((len(k.image), U.crc32(k.image), k.info["filled"], k.name),
                         (66492, 0x19A640C5, 4091, "synthetic test kit"))
        self.assertEqual(k.meta["licence"], "CC0")
        self.assertEqual(k.frames(), frames_file())
        with open(SYNTHETIC, "rb") as f:
            self.assertEqual(k.mckit(), f.read())         # a .mckit is kept as it was read
        bare = kit.Kit(k.image)
        meta, image = U.read_mckit(bare.mckit())          # a bare image gets a .mckit around it
        self.assertEqual((image, meta["name"], meta["author"], meta["licence"]), (k.image, "synthetic test kit", "", "unknown"))
        # its sources from the header: null for an empty voice (2 and 12), the earlier voice's again for a
        # shared hit (8 plays 7's)
        names = list(U.VOICE_NAMES)
        names[2] = names[12] = None
        names[8] = names[7]
        self.assertEqual(meta["sources"], names)
        self.assertEqual(sorted(meta), ["author", "licence", "made", "name", "sources"])
        self.assertIn("11 of 13 voices", k.describe({1: "808"})[0])
        self.assertIn("(808)", k.describe({1: "808"})[0])

    def test_kit_names(self):
        # drum kit's choices as minicontrol names them (options.json, by sync.py), the thirteenth its own
        names = S.options[kit.KIT_SETTING]
        self.assertEqual(sorted(names), list(range(14)))
        self.assertEqual((names[kit.STAND_IN_KIT], names[kit.OWN_KIT]), ("studio", "your own"))
        self.assertEqual(S.params[kit.KIT_SETTING]["max_value"], 13)

    def test_not_a_kit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "x.mckit")
            with open(path, "wb") as f:
                f.write(b"MCKF\x02\0\0\0" + bytes(8))
            with self.assertRaises(ValueError):
                kit.Kit.load(path)
            # neither a .mckit nor an image: said so, not taken for a broken kit
            for data in (b"RIFF....WAVEfmt " + bytes(240), b"RIFF\x24\0\0\0WAVE", b""):
                with open(path, "wb") as f:
                    f.write(data)
                code, out = run("-y", "kit", "send", path)    # refused before any minichord is looked for
                self.assertEqual(code, 1)
                self.assertIn("not a kit file (.mckit): make one on minicontrol's kit page. Nothing was sent", out)
                self.assertNotIn("reason", out)
            self.assertEqual(kit.Kit.load(path).reason(), U.REASON_RULE)   # check_image's own say is kept
            # a .mckit whose image is broken is a kit file all the same, refused for its reason
            with open(path, "wb") as f:
                f.write(U.write_mckit({"name": "x"}, b"MCK2" + bytes(252)))
            self.assertIn("reason 2", kit.Kit.load(path).problem())

    def test_last_kit_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["XDG_CACHE_HOME"] = tmp
            try:
                self.assertIsNone(kit.last())
                path = kit.remember(kit.Kit.load(SYNTHETIC))
                self.assertEqual(path, os.path.join(tmp, "minictl", "last-kit.mckit"))
                self.assertEqual(kit.last().image, kit.Kit.load(SYNTHETIC).image)
                # a bare image is kept as a .mckit around it
                path = kit.remember(kit.Kit.load(os.path.join(KITS, "small.image")))
                self.assertEqual(kit.last().meta["sources"][:3], ["kick", "snare", None])
                # what fails its checks there (cut short, or something else's) is no last kit, and nothing
                # said of it can fail
                for data in (b"", b"MCK1", b"MCK1" + bytes(100), b"MCKF\1\0\0\0", b"MCKF\1\0\0\0\5\0\0\0[1,2",
                             b"MCKF\1\0\0\0\x02\0\0\0{}MCK1", b"not a kit at all",
                             U.write_mckit({}, kit.Kit.load(os.path.join(KITS, "broken", "data_crc.image")).image),
                             b"MCKF\1\0\0\0\x10\x27\0\0" + b"[" * 10000):
                    with open(path, "wb") as f:
                        f.write(data)
                    self.assertIsNone(kit.last(), data[:16])
            finally:
                os.environ.pop("XDG_CACHE_HOME")

    def test_alsa_peers(self):
        listing = """client 0: 'System' [type=kernel]
    0 'Timer           '
client 28: 'minichord' [type=kernel,card=2]
    0 'minichord MIDI 1'
\tConnecting To: 129:0, 140:0
\tConnected From: 128:0, 141:0[real:0]
    1 'minichord MIDI 2'
client 128: 'RtMidiOut Client' [type=user,pid=4242]
    0 'minictl         '
\tConnecting To: 28:0
client 129: 'RtMidiIn Client' [type=user,pid=4242]
    0 'minictl         '
\tConnected From: 28:0
client 140: 'Chrome' [type=user,pid=777]
    0 'Chrome input    '
\tConnected From: 28:0
client 141: 'Chrome' [type=user,pid=777]
    0 'Chrome output   '
\tConnecting To: 28:0[real:0]
"""
        self.assertEqual(device.alsa_peers(listing, {28}, 4242), [(140, "Chrome", 777), (141, "Chrome", 777)])
        self.assertEqual([c for c, _, _ in device.alsa_peers(listing, {28})], [128, 129, 140, 141])
        self.assertEqual(device.alsa_peers(listing, {99}, 4242), [])
        # Chrome's Web MIDI clients can't be subscribed to, so `aconnect -l` lists only their connections:
        # their names come from the kernel's own list, and the guard knows them for a browser
        hidden = """client 28: 'minichord' [type=kernel,card=3]
    0 'minichord MIDI 1'
\tConnecting To: 128:0
\tConnected From: 129:1
"""
        with tempfile.NamedTemporaryFile("w", suffix="clients", delete=False) as f:
            f.write('Client info\n  cur  clients : 5\nClient  28 : "minichord" [Kernel]\n  Port   0 : "minichord MIDI 1" (RWeX)\n'
                    'Client 128 : "Chrome (input)" [User]\n  Port   0 : "port-0" (-w--)\n'
                    'Client 129 : "Chrome (output)" [User]\n  Port   1 : "port-1" (r---)\n')
        try:
            known = device.seq_client_names(f.name)
        finally:
            os.unlink(f.name)
        self.assertEqual(known, {28: "minichord", 128: "Chrome (input)", 129: "Chrome (output)"})
        self.assertEqual(device.alsa_peers(hidden, {28}), [(128, "?", None), (129, "?", None)])
        peers = device.alsa_peers(hidden, {28}, 4242, known)
        self.assertEqual(peers, [(128, "Chrome (input)", None), (129, "Chrome (output)", None)])
        self.assertTrue(all(device.web_midi_browser(name, program) for _c, name, program in peers))
        self.assertEqual(device.seq_client_names("/nonexistent/clients"), {})
        # a browser, by its client's name or its program's, refuses a send; a bridge only passes messages on
        for name, program in (("Chrome", "chrome"), ("Chromium", None), ("Midi Through", "chromium"),
                              ("Firefox", "firefox-bin"), ("RtMidi Output Client", "firefox"),
                              ("Client-130", "chromium-browse"), ("Client-131", "firefox-esr"), ("Client-132", "msedge"),
                              ("Microsoft Edge", None), ("Client-133", "opera"), ("Client-134", "brave"),
                              ("WebMIDI input", "MainThread"), ("WebMIDI output", None)):
            self.assertTrue(device.web_midi_browser(name, program), (name, program))
        # as whole words only: a synth called Operator isn't Opera, nor a program whose name holds "edge"
        for name, program in (("PipeWire-RT-Event", "pipewire"), ("PipeWire-System", "pipewire"),
                              ("a2jmidid", "a2jmidid"), ("Midi Through", None), ("RtMidiOut Client", "python3"),
                              ("Operator", "operator"), ("Operator", None), ("Client-135", "knowledged"),
                              ("Chromatic Tuner", "chromatic"), ("Bravely", "bravely")):
            self.assertFalse(device.web_midi_browser(name, program), (name, program))


class FakeKitSlotRules(unittest.TestCase):
    """The fake's kit slot itself, held to user_kit.md section 4 message by message, on a clock of the
    test's own, so what minictl (and minicontrol) are tested against does what the firmware does. No MIDI
    ports: its replies are kept in a list."""

    def setUp(self):
        from fake_minichord import FakeKitSlot
        self.FakeKitSlot = FakeKitSlot
        self.t = 0.0
        self.slot = self.new_slot()
        self.small = kit.Kit.load(os.path.join(KITS, "small.image")).frames()   # BEGIN, two pieces, COMMIT

    def new_slot(self):
        self.out = []
        return self.FakeKitSlot(lambda m: self.out.append(bytes(m)), clock=lambda: self.t)

    def replies(self, msg, at=None):
        """the replies to one message (heard at time at), parsed"""
        if at is not None:
            self.t = at
        self.out.clear()
        self.slot.receive(msg)
        return [U.parse_reply(m) for m in self.out]

    def status(self, msg, at=None):
        r = self.replies(msg, at)
        self.assertEqual([x["kind"] for x in r], ["status"], msg.hex())
        return r[0]["status"]

    def state(self, at=None):
        r = self.replies(U.info_message(), at)
        self.assertEqual([x["kind"] for x in r], ["info"])
        return r[0]["state"]

    def test_commit_runs_all_of_check_image(self):
        # every broken image there is, sent as it is (BEGIN's CRC its own): what BEGIN can't refuse for its
        # size, COMMIT refuses with 10, the header never written, so no kit after
        refused, expected = {}, read_json(os.path.join(KITS, "broken", "expected.json"))
        for name in expected:
            image = kit.Kit.load(os.path.join(KITS, "broken", name)).image
            self.slot = self.new_slot()
            frames = U.frames(image)
            if len(image) < 256 or len(image) % 4 or len(image) - 256 > self.slot.capacity:
                self.assertEqual(self.status(frames[0]), 2, name)
                continue
            for msg in frames[:-1]:
                self.assertEqual(self.status(msg), 0, name)
            refused[name] = self.status(frames[-1])
            self.assertNotIn(0x601E0000, self.slot.programs, name)
            self.assertEqual(self.slot.read(0x601E0000, 4096), b"\xff" * 4096, name)
            self.assertEqual((self.slot.state, self.slot.reason, self.slot.open, self.slot.muted), (0, 1, False, False), name)
        self.assertEqual(set(refused.values()), {10})
        # among them a data_base that isn't 0 (as a copy from a minichord's flash has) and a bad sound CRC
        for name in ("data_base_nonzero.image", "order_data_base_before_rules.image", "data_crc.image", "data_crc_gap.image"):
            self.assertIn(name, refused)
        # all of them but those BEGIN refused for their size, which fail check_image's step 1 or 5 there
        self.assertTrue(all(expected[n] == U.REASON_RULE for n in set(expected) - set(refused)))
        # and a good one kept, at the slot's own data_base
        self.slot = self.new_slot()
        for msg in self.small[:-1]:
            self.assertEqual(self.status(msg), 0)
        r = self.replies(self.small[-1])
        self.assertEqual([(x["kind"], x.get("status")) for x in r], [("status", 0), ("info", None)])
        self.assertEqual((self.slot.state, U.read_header(self.slot.read(0x601E0000, 256))["data_base"]), (1, 0x601A0000))

    def test_only_data_commit_abort_and_clear_count_against_the_silence(self):
        import fake_minichord
        self.assertEqual(fake_minichord.SILENCE, 5.0)
        # INFO asked again and again, BEGIN refused (one is open), a DATA too short to read and a COMMIT of
        # the wrong length (both 3, the upload left open): none of them holds an upload open past 5 s
        short_data = self.small[1][:-3] + b"\xf7"
        long_commit = U.message(U.COMMIT, b"\0\0\0")
        for what, msg, want in (("INFO", U.info_message(), None), ("BEGIN", self.small[0], 1),
                                ("short DATA", short_data, 3), ("long COMMIT", long_commit, 3)):
            self.slot = self.new_slot()
            self.assertEqual(self.status(self.small[0], at=100.0), 0)
            for at in (101.0, 102.0, 103.0, 104.0, 104.9):
                if want is None:
                    self.assertEqual(self.state(at), 2, what)
                else:
                    self.assertEqual(self.status(msg, at), want, what)
            self.assertTrue(self.slot.open, what)
            self.assertEqual(self.state(105.0), 0, what)              # 5 s after BEGIN: ended, no kit
            self.assertFalse(self.slot.open or self.slot.muted, what)
        # a DATA read, whether taken (0), a repeat (0), out of order (6) or with a bad CRC (7), starts the
        # 5 s again
        bad_crc = bytearray(self.small[2])
        bad_crc[20] ^= 1
        out_of_order = self.small[1][:4] + U.n3(448) + self.small[1][7:]
        self.slot = self.new_slot()
        self.assertEqual(self.status(self.small[0], at=200.0), 0)
        for at, msg, want in ((204.9, self.small[1], 0), (209.8, self.small[1], 0), (214.7, out_of_order, 6),
                              (219.6, bytes(bad_crc), 7)):
            self.assertEqual(self.status(msg, at), want)
            self.assertEqual(self.state(at + 4.9), 2)
        self.assertEqual(self.status(self.small[2], at=224.5), 0)       # still open, and the kit goes on
        r = self.replies(self.small[3], at=229.4)
        self.assertEqual((r[0]["status"], self.slot.state), (0, 1))
        # and 5 s after the last DATA the upload is over
        self.slot = self.new_slot()
        self.assertEqual(self.status(self.small[0], at=300.0), 0)
        self.assertEqual(self.status(self.small[1], at=301.0), 0)
        self.assertEqual(self.state(305.9), 2)
        self.assertEqual(self.state(306.0), 0)
        self.assertEqual(self.status(self.small[2], at=306.1), 5)       # no upload begun
        # ABORT and CLEAR end one at once (and COMMIT, above)
        for msg in (U.abort_message(), U.clear_message()):
            self.slot = self.new_slot()
            self.assertEqual(self.status(self.small[0], at=400.0), 0)
            self.assertEqual(self.status(msg, at=401.0), 0)
            self.assertFalse(self.slot.open or self.slot.muted)
            self.assertEqual(self.state(), 0)

    def test_every_sector_erased_on_first_touch(self):
        # each 4 kB sector of the sound erased as the sound first reaches it, though it reads all FF, at
        # whatever data_base the firmware leaves; nothing below data_base touched
        synthetic, small = kit.Kit.load(SYNTHETIC).image, kit.Kit.load(os.path.join(KITS, "small.image")).image
        for base, image in ((0x601A0000, synthetic), (0x601B0000, synthetic), (0x601D0000, small)):
            sectors = -(-(len(image) - 256) // 4096)
            for old in (b"\xff", b"\0"):
                self.slot = self.new_slot()
                self.slot.move(base)
                self.slot.flash[:] = old * len(self.slot.flash)
                for msg in U.frames(image):
                    self.assertEqual(self.status(msg) if msg[3] != U.COMMIT else self.replies(msg)[0]["status"], 0)
                self.assertEqual(self.slot.erases, [0x601E0000] + [base + 4096 * k for k in range(sectors)], hex(base))
                self.assertTrue(all(base <= a < 0x601E1000 for a in self.slot.programs))
                self.assertEqual((self.slot.state, U.read_header(self.slot.read(0x601E0000, 256))["data_base"]), (1, base))
                if base > 0x601A0000:
                    self.assertEqual(self.slot.flash[:base - 0x601A0000], old * (base - 0x601A0000))
        with self.assertRaises(AssertionError):
            self.slot.read(0x601C0000, 4)                                    # below data_base: not the slot's

    def test_foreign_header_is_no_kit(self):
        # a header sector that doesn't start MCK1 (an old filesystem's bytes, a header write cut short) holds
        # no kit: empty, reason 1, as blank. One that starts MCK1 and fails is damaged, with its reason
        for start, want in ((b"\x03\0\0\0littlefs", (0, 1)), (b"\0" * 256, (0, 1)), (b"MCK", (0, 1)),
                            (b"mck1", (0, 1)), (b"MCK1", (3, 2)), (b"MCK1\1\0\x0d\x05", (3, 3))):
            self.slot = self.new_slot()
            self.slot.program(0x601E0000, start)
            self.slot.refresh()
            self.assertEqual((self.slot.state, self.slot.reason), want, start)
            r = self.replies(U.info_message())[0]
            self.assertEqual((r["state"], r["reason"], r["size"], r["crc"]), want + (0, 0), start)

    def test_clear_on_a_disabled_slot(self):
        self.slot.move(0x601E0000)                                          # no room: disabled
        self.assertEqual(self.status(bytes.fromhex("f07d6b060043f7")), 3)  # its shape first
        self.assertEqual(self.status(U.clear_message()), 11)
        self.assertEqual((self.slot.erases, self.slot.programs, self.slot.muted), ([], [], False))
        self.assertEqual((self.slot.state, self.slot.reason), (4, 7))

    def test_commit_and_abort_with_no_upload_are_5_first(self):
        for msg in (U.message(U.COMMIT, b"\0\x01"), U.message(U.COMMIT, b"\0\0\0"), U.message(U.ABORT, b"\x01\0"),
                    U.message(U.ABORT, b"\0\0\0\0"), U.commit_message(), U.abort_message()):
            self.assertEqual(self.status(msg), 5, msg.hex())
        # shorter than any message the computer sends, or with a byte past 7 bits: 3 still
        for msg in (bytes.fromhex("f07d6b04f7"), bytes.fromhex("f07d6b0300f7"), bytes.fromhex("f07d6b048000f7")):
            self.assertEqual(self.status(msg), 3, msg.hex())
        # open, their shape is looked at: 3, the upload left open
        self.assertEqual(self.status(self.small[0]), 0)
        for msg in (U.message(U.COMMIT, b"\0\x01"), U.message(U.ABORT, b"\0\0\0")):
            self.assertEqual(self.status(msg), 3, msg.hex())
            self.assertTrue(self.slot.open)

    def test_a_begin_waiting_to_open(self):
        # the firmware opens a BEGIN from its loop: until then it's waiting, and what comes meanwhile is
        # answered as the firmware does (the fake's deferred mode, the loop's pass run by hand)
        synthetic = kit.Kit.load(SYNTHETIC)
        frames = synthetic.frames()

        def fresh():
            self.slot = self.new_slot()
            self.slot.store(synthetic.image)
            self.slot.deferred = True
            self.slot.erases.clear()
            self.assertEqual(self.replies(frames[0]), [])              # noted, not answered yet
            self.assertTrue(self.slot.begin_waiting)

        def untouched():
            self.assertEqual((self.slot.open, self.slot.muted, self.slot.erases, self.slot.state), (False, False, [], 1))

        # ABORT calls it off (a malformed one doesn't); it never opens, the kit stored left as it is
        fresh()
        self.assertEqual((self.state(), self.replies(U.info_message())[0]["crc"]), (1, 0x19A640C5))
        self.assertEqual(self.status(U.message(U.ABORT, b"\x01\0")), 3)
        self.assertTrue(self.slot.begin_waiting)
        self.assertEqual(self.status(U.abort_message()), 0)
        self.slot.loop_pass()
        self.assertEqual(self.slot.replies[-1][3], 0x40 | U.ABORT)        # nothing more said at the pass
        untouched()
        self.assertEqual(self.status(U.abort_message()), 5)               # nothing open or waiting now
        # CLEAR calls it off and clears
        fresh()
        self.assertEqual(self.status(U.clear_message()), 0)
        self.slot.loop_pass()
        self.assertEqual((self.slot.open, self.slot.muted, self.slot.state, self.slot.erases), (False, False, 0, [0x601E0000]))
        # DATA and COMMIT find no upload; a second BEGIN is busy, before too big, after proto and its CRC;
        # INFO tells the kit stored. Then the pass opens the first, and the upload goes by its size and CRC
        fresh()
        self.assertEqual(self.status(frames[1]), 5)
        self.assertEqual(self.status(U.commit_message()), 5)
        self.assertEqual(self.status(self.small[0]), 1)
        self.assertEqual(self.status(U.begin_message(256 + 262148, 0)), 1)
        self.assertEqual(self.status(U.message(U.BEGIN, bytes([2]) + U.n3(444) + U.n5(0))), 4)
        self.assertEqual(self.status(U.message(U.BEGIN, bytes([1]) + U.n3(444) + U.n5(1 << 32))), 3)
        self.assertEqual(self.state(), 1)
        untouched()
        self.out.clear()
        self.slot.loop_pass()
        self.assertEqual([U.parse_reply(m)["status"] for m in self.out], [0])
        self.assertEqual((self.slot.open, self.slot.muted, self.slot.up_size), (True, True, len(synthetic.image)))
        for msg in frames[1:-1]:
            self.assertEqual(self.status(msg), 0)
        r = self.replies(frames[-1])
        self.assertEqual([(x["kind"], x.get("status")) for x in r], [("status", 0), ("info", None)])
        # refused at the pass: the rhythm running then, or a CRC over 32 bits; nothing touched
        fresh()
        self.slot.busy = lambda: True
        self.out.clear()
        self.slot.loop_pass()
        self.assertEqual([U.parse_reply(m)["status"] for m in self.out], [1])
        untouched()
        self.slot.busy = lambda: False
        self.assertEqual(self.replies(U.message(U.BEGIN, bytes([1]) + U.n3(len(synthetic.image)) + U.n5(synthetic.info["crc"] | 1 << 32))), [])
        self.out.clear()
        self.slot.loop_pass()
        self.assertEqual([U.parse_reply(m)["status"] for m in self.out], [3])
        untouched()
        self.assertFalse(self.slot.begin_waiting)

    def test_reply_shapes_never_answered_other_unknown_ops_3(self):
        for op in (0x40, 0x41, 0x42, 0x43, 0x45, 0x46, 0x47, 0x4A, 0x6B, 0x7F):
            for msg in (U.status_reply(op & 0x3F, 0, 0), U.message(op, b"\0\0"), U.message(op, b""),
                        bytes([0xF0, 0x7D, 0x6B, op]), U.message(op, b"\x80\0")):
                self.assertEqual(self.replies(msg), [], msg.hex())
        self.assertEqual(self.replies(U.info_reply(1, 0, 262144, 0x601A0000)), [])   # an INFO reply echoed back
        self.assertEqual(self.replies(bytes.fromhex("f07d6bf7")), [])               # an F7 where the op should be
        for op in (0x00, 0x07, 0x08, 0x10, 0x20, 0x3F):
            r = self.replies(U.message(op, b"\0\0"))
            self.assertEqual([(x["kind"], x["op"], x["status"]) for x in r], [("status", op, 3)], hex(op))
        # during an upload too, and neither holds it open
        self.assertEqual(self.status(self.small[0], at=10.0), 0)
        self.assertEqual(self.replies(U.status_reply(U.DATA, 0, 224), at=12.0), [])
        self.assertEqual(self.status(U.message(0x09, b"\0\0"), at=14.0), 3)
        self.assertEqual(self.state(15.0), 0)


@unittest.skipIf(os.name == "nt", "virtual MIDI ports don't exist on Windows")
class WithFakeMinichord(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fake_minichord import FakeMinichord
        try:
            cls.fake = FakeMinichord()
            FAKES.append(cls.fake)
        except Exception as e:          # no virtual MIDI here (a CI machine without a sound system)
            raise unittest.SkipTest(f"no virtual MIDI ports: {e}")
        os.environ["MINICTL_PORT"] = "fakechord"

    @classmethod
    def tearDownClass(cls):
        FAKES.remove(cls.fake)
        cls.fake.close()
        os.environ.pop("MINICTL_PORT", None)

    def setUp(self):
        self.fake.settle()
        self.fake.live = list(self.fake.banks[0])
        self.fake.saves.clear()

    def test_info(self):
        code, out = run("--json", "info")
        self.assertEqual(code, 0, out)
        d = json.loads(out)
        self.assertEqual((d["firmware"], d["family"], d["bank"], d["name"]), (S.version, "fork", 1, "Bank 1"))

    def test_set_and_get(self):
        code, out = run("set", "reverb-size", "0.8", "temperament", "kellner", "mod-knob", "chord-attack")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.fake.live[24], 80)
        self.assertEqual(self.fake.live[237], 8)
        self.assertEqual(self.fake.live[14], S.find("chord-attack"))
        self.assertIn("live only", out)
        self.assertEqual(self.fake.saves, [])
        code, out = run("get", "reverb-size", "temperament")
        self.assertIn("0.8", out)
        self.assertIn("Kellner", out)

    def test_set_clamps_and_refuses(self):
        code, out = run("set", "reverb-size", "7")
        self.assertIn("!", out)
        self.assertEqual(self.fake.live[24], 100)
        code, out = run("set", "usb-audio", "1")             # the instrument's own setup isn't a preset's
        self.assertIn("isn't a setting a preset can change", out)

    def test_save_asks(self):
        run("set", "reverb-size", "0.2")
        code, out = run("save")                               # no terminal to ask: refused
        self.assertEqual(self.fake.saves, [])
        code, out = run("--yes", "save", "3")
        self.assertEqual(self.fake.saves, [2])
        self.assertEqual(self.fake.banks[2][24], 20)

    def test_banks_walk_and_return(self):
        self.fake.command(4, 4)
        code, out = run("banks")
        self.assertEqual(code, 0, out)
        self.assertIn("12  Bank 12", out)
        self.assertEqual(self.fake.live[1], 4)                # back on the bank it was on

    def test_bank_load(self):
        run("bank", "6")
        self.assertEqual(self.fake.live[1], 5)
        self.assertEqual(self.fake.live[20], 150)
        run("bank", "1")

    def test_backup_and_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "b.json")
            code, out = run("backup", path)
            self.assertEqual(code, 0, out)
            data = read_json(path)
            self.assertEqual(len(data["banks"]), 12)
            self.assertEqual(data["banks"][3]["name"], "Bank 4")
            data["banks"][3]["values"][24] = 33
            data["banks"][3]["name"] = "Changed"
            write_json(path, data)
            code, out = run("--yes", "restore", path, "--banks", "4")
            self.assertEqual(code, 0, out)
            self.assertIn("every bank matches", out)
            self.assertEqual(self.fake.banks[3][24], 33)
            self.assertEqual(self.fake.saves, [3])
            write_json(path, {"minichord_backup": 1, "banks": [{"bank": 40, "values": []}]})
            code, out = run("--yes", "restore", path)
            self.assertEqual(code, 1)
            self.assertIn("don't make sense", out)

    def test_locks(self):
        default = {a for a, p in S.params.items() if p.get("lock") in ("default", "fixed")}
        code, out = run("--json", "locks")
        self.assertEqual(code, 0, out)
        self.assertEqual(set(json.loads(out)), default)
        stored = self.fake.banks[2][24]
        try:
            code, out = run("--json", "locks", "--lock", "reverb-size", "--unlock", "harp-plate")
            self.assertEqual(code, 0, out)
            self.assertEqual(set(json.loads(out)), (default - {243}) | {24})
            held = self.fake.live[24]
            self.fake.banks[2][24] = held + 10
            self.fake.command(4, 2)                      # a preset load leaves a locked setting as the instrument has it
            self.assertEqual(self.fake.live[24], held)
            code, out = run("--json", "locks", "--none")
            self.assertEqual(set(json.loads(out)), {244, 384})
        finally:
            self.fake.banks[2][24] = stored
            code, out = run("--json", "locks", "--defaults")
        self.assertEqual(set(json.loads(out)), default)

    def test_code_both_ways(self):
        code, out = run("code", "Raver")
        self.assertEqual(code, 0, out)
        raver = S.decode(next(p["value"] for p in S.shared_presets() if p["name"] == "Raver"))
        self.assertEqual([self.fake.live[a] for a in range(8, 256)], raver[8:256])
        code, out = run("code")
        self.assertEqual(S.decode(out.strip())[8:256], raver[8:256])

    def test_describe_live_and_send(self):
        code, out = run("describe", "--send", "lots of reverb on the chords")
        self.assertEqual(code, 0, out)
        self.assertIn("live preset", out)
        self.assertGreater(self.fake.live[184], self.fake.banks[0][184])

    def test_do_live_and_banks(self):
        self.fake.banks[1][249] = self.fake.banks[3][249] = S.find("chord-attack")
        self.fake.live[24] = 77                               # unsaved, live: must survive the walk
        code, out = run("--yes", "do", "turn off hover on banks 2 and 4, set the chord attack to 200 ms")
        self.assertEqual(code, 0, out)
        self.assertEqual((self.fake.banks[1][249], self.fake.banks[3][249]), (0, 0))
        self.assertEqual(sorted(self.fake.saves), [1, 3])
        self.assertEqual(self.fake.live[1], 0)                # back on bank 1
        self.assertEqual(self.fake.live[137], 200)
        self.assertEqual(self.fake.live[24], 77)
        self.assertEqual(self.fake.banks[0][137], S.defaults()[137])   # live, not saved

    def test_do_dry_run_and_refusal(self):
        code, out = run("do", "-n", "set the chord attack to 300")
        self.assertIn("dry run", out)
        self.assertNotEqual(self.fake.live[137], 300)
        code, out = run("do", "darker on bank 5")              # no one to ask: the bank isn't written
        self.assertEqual(self.fake.saves, [])

    def test_do_bank_by_name(self):
        code, out = run("--yes", "do", "set the chord attack to 50 on the Bank 7 bank")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.fake.banks[6][137], 50)

    def test_do_code_offline(self):
        os.environ.pop("MINICTL_PORT")
        try:
            code, out = run("--port", "no-such-port", "do", "-c", "defaults", "mod knob to the reverb size")
        finally:
            os.environ["MINICTL_PORT"] = "fakechord"
        self.assertEqual(code, 0, out)
        self.assertEqual(S.decode(out.strip().splitlines()[-1])[14], 24)

    def test_tuning(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "q.scl")
            with open(path, "w") as f:
                f.write("! q\n!\nquarter-comma meantone\n 12\n!\n" + "".join(
                    f" {100 * k + c:.3f}\n" for k, c in enumerate([-24, -7, 10, -14, 3, -21, -3, -17, 0, 7, -10], 1)) + " 2/1\n")
            code, out = run("tuning", path)
            self.assertEqual(code, 0, out)
            self.assertEqual(self.fake.live[237], 13)
            self.assertEqual(self.fake.live[363], 860)          # E, 14 cents flat
            code, out = run("tuning")
            self.assertIn("your own", out)
            self.assertIn("E -14", out)
            back = os.path.join(tmp, "back.scl")
            run("tuning", "--export", back)
            from minictl import tuning as T
            with open(back) as f:
                self.assertEqual(round(T.parse_scala(f.read())[1][4]), -14)

    def test_name(self):
        run("name", "Night", "drive")
        self.assertEqual(Settings.preset_name(self.fake.live), "Night drive")

    def test_play_rhythm_snapshot(self):
        self.fake.notes.clear()
        run("set", "midi-in-plays", "on")
        run("play", "C4", "E4", "-l", "0.05")
        run("play", "--harp", "G4", "-l", "0.05")
        import time
        time.sleep(0.1)
        cables = [(c, m[0] & 0xF0, m[1]) for c, m in self.fake.notes]
        self.assertIn((1, 0x90, 60), cables)
        self.assertIn((2, 0x90, 67), cables)
        run("rhythm", "on")
        self.assertTrue(self.fake.rhythm)
        run("snapshot")
        run("set", "reverb-size", "0.1")
        run("revert")
        self.assertEqual(self.fake.live[24], self.fake.banks[0][24])


@unittest.skipIf(os.name == "nt", "virtual MIDI ports don't exist on Windows")
class WithFakeStockMinichord(unittest.TestCase):
    """Ben's stock firmware 9: one page of settings, control commands 0 to 4"""

    @classmethod
    def setUpClass(cls):
        from fake_minichord import FakeMinichord
        try:
            cls.fake = FakeMinichord(name="fakestock", firmware="stock")
            FAKES.append(cls.fake)
        except Exception as e:          # no virtual MIDI here (a CI machine without a sound system)
            raise unittest.SkipTest(f"no virtual MIDI ports: {e}")
        os.environ["MINICTL_PORT"] = "fakestock"
        cls.stock = Settings("stock")

    @classmethod
    def tearDownClass(cls):
        FAKES.remove(cls.fake)
        cls.fake.close()
        os.environ.pop("MINICTL_PORT", None)

    def setUp(self):
        self.fake.settle()
        self.fake.live = list(self.fake.banks[0])
        self.fake.saves.clear()

    def test_info_says_stock(self):
        code, out = run("--json", "info")
        self.assertEqual(code, 0, out)
        d = json.loads(out)
        self.assertEqual((d["firmware"], d["family"], d["settings"]), (9, "stock", len(self.stock.params)))

    def test_set_within_stock(self):
        code, out = run("set", "reverb-size", "0.8", "chord-attack", "250")
        self.assertEqual(code, 0, out)
        self.assertEqual((self.fake.live[24], self.fake.live[137]), (80, 250))
        code, out = run("set", "chord-voice", "choir")
        self.assertEqual(code, 1)
        self.assertIn("fork", out)
        code, out = run("set", "chord-alternate-layout-alt-layout-maj", "25")   # the fork goes to 29, stock to 18
        self.assertEqual(self.fake.live[202], 18)

    def test_stock_cannot_take_126_and_127(self):
        code, out = run("set", "chord-amplitude-3", "0.4")
        self.assertEqual(code, 1)
        self.assertIn("universal SysEx", out)
        code, out = run("--yes", "do", "set chord amplitude 3 to 0.4")
        self.assertIn("left out", out)

    def test_list_is_stock_only(self):
        code, out = run("--firmware", "stock", "--json", "list")
        addrs = {x["address"] for x in json.loads(out)}
        self.assertTrue(addrs <= set(self.stock.params))
        self.assertNotIn(265, addrs)

    def test_describe_leaves_out_what_stock_lacks(self):
        code, out = run("describe", "--send", "choir chords, darker, lots of reverb")
        self.assertEqual(code, 0, out)
        self.assertIn("left out", out)
        self.assertIn("chord-voice", out)
        self.assertEqual(len(self.fake.live), 256)
        self.assertGreater(self.fake.live[184], self.fake.banks[0][184])
        writes = [a for a, _ in self.fake.log if a]
        self.assertTrue(all(a < 256 for a in writes))

    def test_do_on_stock(self):
        code, out = run("--yes", "do", "on bank 3 set the chord attack to 200 ms, set the chord voice to choir")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.fake.banks[2][137], 200)
        self.assertEqual(self.fake.saves, [2])
        self.assertIn("left out", out)                        # the chord voice is the fork's
        self.assertEqual(self.fake.live[1], 0)

    def test_banks_and_code(self):
        code, out = run("banks")
        self.assertEqual(code, 0, out)
        self.assertIn(" 12", out)
        code, out = run("code")
        fields = __import__("base64").b64decode(out.strip()).decode().split(";")
        self.assertLessEqual(len(fields), 256)

    def test_fork_only_commands_refuse(self):
        for argv in (["play", "C4"], ["snapshot"], ["rhythm", "on"], ["name", "x"], ["tuning", "--equal"], ["kit", "info"]):
            code, out = run(*argv)
            self.assertEqual(code, 1, argv)
            self.assertIn("fork", out)

    def test_restore_fork_backup_into_stock(self):
        v = Settings("fork").defaults()
        v[202] = 25        # past stock's range
        v[14] = 265        # the mod knob on a setting stock doesn't have
        v[24] = 30
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "b.json")
            write_json(path, {"minichord_backup": 1, "banks": [{"bank": 4, "name": "x", "values": v}]})
            code, out = run("--yes", "restore", path)
        self.assertEqual(code, 0, out)
        self.assertIn("every bank matches", out)
        self.assertEqual((self.fake.banks[4][202], self.fake.banks[4][14], self.fake.banks[4][24]), (18, 0, 30))


@unittest.skipIf(os.name == "nt", "virtual MIDI ports don't exist on Windows")
class WithFakeKitSlot(unittest.TestCase):
    """Your own drum kit: minictl's kit commands against the fake's kit slot (FakeKitSlot), on its
    pretend flash, the vectors the fork's reference wrote"""

    @classmethod
    def setUpClass(cls):
        from fake_minichord import FakeMinichord
        try:
            cls.fake = FakeMinichord(name="fakekit")
            FAKES.append(cls.fake)
        except Exception as e:          # no virtual MIDI here (a CI machine without a sound system)
            raise unittest.SkipTest(f"no virtual MIDI ports: {e}")
        os.environ["MINICTL_PORT"] = "fakekit"
        # only this test's own clients count on the fake's port: a browser with Web MIDI open on this
        # machine (minicontrol in Chrome) joins every new port, the fake's too, and would refuse every send
        cls.others = device.Minichord.others_on_port
        ours = lambda peers: None if peers is None else [(n, p) for n, p in peers if (p or "").startswith("python")]
        device.Minichord.others_on_port = lambda d: ours(cls.others(d))
        cls.cache = tempfile.TemporaryDirectory()
        os.environ["XDG_CACHE_HOME"] = cls.cache.name
        cls.synthetic = kit.Kit.load(SYNTHETIC)

    @classmethod
    def tearDownClass(cls):
        device.Minichord.others_on_port = cls.others
        FAKES.remove(cls.fake)
        cls.fake.close()
        os.environ.pop("MINICTL_PORT", None)
        os.environ.pop("XDG_CACHE_HOME", None)
        cls.cache.cleanup()

    def setUp(self):
        self.fake.settle()
        self.fake.live = list(self.fake.banks[0])
        self.fake.rhythm, self.fake.looper, self.fake.kit_answers = False, "empty", True
        self.slot = self.fake.new_kit_slot()
        self.fake.log.clear()

    def info(self):
        code, out = run("--json", "kit", "info")
        self.assertEqual(code, 0, out)
        return json.loads(out)

    def sent(self):
        """the kit messages the fake heard after minictl's first INFO"""
        return [m for m in self.slot.log if m != U.info_message()]

    def test_info_empty(self):
        d = self.info()
        self.assertEqual((d["state"], d["reason"], d["capacity"], d["data_base"]), (0, 1, 262144, 0x601A0000))
        self.assertEqual(d["reason_text"], "no kit header (blank, or other bytes)")
        code, out = run("kit", "info")
        self.assertIn("no kit stored: drum kit 13 plays the studio kit (drum kit 4) until one is sent", out)
        self.assertEqual(self.slot.log, [U.info_message()] * 2)
        # a header sector holding other bytes (an old filesystem's) is no kit stored too, not a damaged one
        self.slot.program(0x601E0000, b"\x03\0\0\0littlefs")
        self.slot.refresh()
        code, out = run("kit", "info")
        self.assertIn("no kit stored: drum kit 13 plays", out)
        self.assertNotIn("damaged", out)

    def test_info_with_a_broken_cache(self):
        # a last-kit.mckit cut short, or not a kit at all: kit info says the slot's state, and nothing of it
        os.makedirs(os.path.dirname(kit.cache_path()), exist_ok=True)
        try:
            for data in (b"MCK1", b"MCKF\1\0\0\0", b"RIFF....WAVEfmt " + bytes(240)):
                with open(kit.cache_path(), "wb") as f:
                    f.write(data)
                code, out = run("kit", "info")
                self.assertEqual(code, 0, out)
                self.assertIn("no kit stored", out)
                self.assertNotIn("last kit sent", out)
        finally:
            os.remove(kit.cache_path())

    def test_send_synthetic(self):
        code, out = run("-y", "kit", "send", SYNTHETIC, "--select")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.slot.log, [U.info_message()] + frames_file())    # byte for byte, nothing sent twice
        d = self.info()
        self.assertEqual((d["state"], d["reason"], d["size"], d["crc"], d["filled"], d["name"], d["base_kit"]),
                         (1, 0, 66492, 0x19A640C5, 4091, "synthetic test kit", 1))
        # in its flash: the header last, with the minichord's data_base, and the sound from there
        header = self.slot.read(0x601E0000, 256)
        self.assertEqual(U.read_header(header)["data_base"], 0x601A0000)
        stored = header + self.slot.read(0x601A0000, len(self.synthetic.image) - 256)
        self.assertEqual(U.check_image(stored, 0x601A0000), 0)
        self.assertEqual(U.image_info(stored)["crc"], 0x19A640C5)
        # the old header gone, and each sector of the sound erased as the sound first reaches it, though
        # all FF already
        self.assertEqual(self.slot.erases, [0x601E0000] + [0x601A0000 + 4096 * k for k in range(17)])
        self.assertEqual(self.slot.programs[-1], 0x601E0000)  # and the header written last, once
        self.assertEqual(self.slot.programs[:-1], [0x601A0000 + 256 * k for k in range(259)])
        self.assertFalse(self.slot.open or self.slot.muted)
        with open(os.path.join(self.cache.name, "minictl", "last-kit.mckit"), "rb") as f, open(SYNTHETIC, "rb") as g:
            self.assertEqual(f.read(), g.read())
        self.assertEqual(self.fake.live[337], 13)                             # --select: drum kit 13, live
        self.assertIn("drum kit 13", out)
        self.assertIn("`minictl set chord-drums on`", out)                    # the drums are off in bank 1
        code, out = run("kit", "info")
        self.assertIn("the last kit sent from this computer", out)
        # again, over it: the same
        self.slot.erases.clear()
        code, out = run("-y", "kit", "send", SYNTHETIC)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.slot.erases, [0x601E0000] + [0x601A0000 + 4096 * k for k in range(17)])
        self.assertIn("holds this kit already", out)
        self.assertEqual(self.info()["crc"], 0x19A640C5)

    def test_asks_first(self):
        code, out = run("kit", "send", SYNTHETIC)                  # no one to ask: refused, the silence named
        self.assertEqual(code, 1)
        self.assertIn("the minichord goes quiet while the kit is written, usually under 20 seconds; don't unplug it",
                      out.replace("The minichord", "the minichord"))
        self.assertEqual(self.sent(), [])
        self.slot.store(self.synthetic.image)
        code, out = run("kit", "clear")
        self.assertEqual(code, 1)
        self.assertEqual(self.slot.state, 1)

    def test_clear(self):
        self.slot.store(self.synthetic.image)
        self.assertEqual(self.info()["state"], 1)
        code, out = run("-y", "kit", "clear")
        self.assertEqual(code, 0, out)
        self.assertIn("no kit stored", out)
        self.assertEqual(self.slot.read(0x601E0000, 4096), b"\xff" * 4096)
        d = self.info()
        self.assertEqual((d["state"], d["reason"], d["size"], d["crc"], d["name"]), (0, 1, 0, 0, ""))
        code, out = run("-y", "kit", "clear")
        self.assertIn("nothing to clear", out)
        # a disabled slot holds nothing either: no CLEAR sent
        self.slot.move(0x601E0000)
        self.slot.log.clear()
        code, out = run("-y", "kit", "clear")
        self.assertEqual(code, 0, out)
        self.assertIn("the kit slot is disabled (this firmware leaves less than 64 kB of flash for it): nothing to clear", out)
        self.assertEqual(self.sent(), [])

    def test_broken_refused_before_anything_is_sent(self):
        for name, reason in read_json(os.path.join(KITS, "broken", "expected.json")).items():
            path = os.path.join(KITS, "broken", name)
            code, out = run("-y", "kit", "send", path)
            self.assertEqual(code, 1, name)
            with open(path, "rb") as f:
                self.assertIn(f"reason {reason}" if f.read(4) == b"MCK1" else "not a kit file", out, name)
            self.assertIn("Nothing was sent", out)
        self.assertEqual(self.slot.log, [])
        self.assertEqual(self.fake.log, [])                         # not a setting nor a command either

    def test_lost_reply_sent_again(self):
        self.slot.lose_reply(U.DATA, skip=11)                     # the twelfth piece's reply lost on the way
        t0 = time.monotonic()
        code, out = run("-y", "kit", "send", SYNTHETIC)
        self.assertEqual(code, 0, out)
        self.assertGreaterEqual(time.monotonic() - t0, 1.0)        # a second's wait, then the same piece again
        want = frames_file()
        self.assertEqual(self.sent(), want[:13] + [want[12]] + want[13:])
        d = self.info()
        self.assertEqual((d["state"], d["crc"]), (1, 0x19A640C5))

    def test_lost_commit_reply_asks_info(self):
        self.slot.lose_reply(U.COMMIT)                            # COMMIT's reply and its INFO lost
        code, out = run("-y", "kit", "send", SYNTHETIC)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.slot.log[-1], U.info_message())     # asked after 3 s: the kit is there
        self.assertEqual(self.slot.state, 1)

    def test_busy_rhythm_stopped(self):
        self.fake.rhythm, self.fake.looper = True, "playing"
        code, out = run("-y", "kit", "send", SYNTHETIC)
        self.assertEqual(code, 0, out)
        self.assertIn("busy", out)
        self.assertIn("rhythm and looper stopped", out)
        self.assertEqual((self.fake.rhythm, self.fake.looper), (False, "stopped"))
        self.assertEqual(self.slot.state, 1)
        self.assertEqual([m[3] for m in self.sent()].count(U.BEGIN), 2)   # refused, then taken

    def test_busy_reported(self):
        self.slot.busy = lambda: True                             # busy whatever is stopped
        code, out = run("-y", "kit", "send", SYNTHETIC)
        self.assertEqual(code, 1)
        self.assertIn("busy", out)
        self.assertNotIn(U.DATA, [m[3] for m in self.sent()])
        self.assertNotIn(U.ABORT, [m[3] for m in self.sent()])  # refused, nothing opened: no ABORT
        self.assertEqual(self.slot.state, 0)

    def test_begin_busy_with_another_upload_sends_no_abort(self):
        # another program's upload open when minictl's BEGIN comes: busy, and no ABORT, which would end theirs
        small = kit.Kit.load(os.path.join(KITS, "small.image")).frames()
        with self.fake.lock:
            self.slot.receive(small[0])
        d = device.Minichord("fakekit")
        try:
            with self.assertRaises(device.KitBusy):
                d.kit_upload(self.synthetic.image)
        finally:
            d.close()
        self.assertEqual([m[3] for m in self.slot.log], [U.BEGIN, U.BEGIN])
        self.assertTrue(self.slot.open)

    def test_other_commands_wait_out_another_programs_upload(self):
        import fake_minichord
        # not only the kit's: a command that finds the minichord deaf to its settings asks INFO, and an
        # upload open is waited out (the timers shortened as below)
        quiet, silence = device.KIT_OTHERS_QUIET, fake_minichord.SILENCE
        device.KIT_OTHERS_QUIET, fake_minichord.SILENCE = 4.0, 3.5
        try:
            small = kit.Kit.load(os.path.join(KITS, "small.image")).frames()
            with self.fake.lock:
                self.slot.receive(small[0])
            code, out = run("--json", "info")
        finally:
            device.KIT_OTHERS_QUIET, fake_minichord.SILENCE = quiet, silence
        self.assertEqual(code, 0, out)
        self.assertIn("another program is sending the minichord a kit", out)
        self.assertEqual(json.JSONDecoder().raw_decode(out)[0]["firmware"], self.fake.version)   # stdout stays JSON
        self.assertFalse(self.slot.open)

    def another_upload(self, when, hold, reply_late=0.0):
        """another program's upload of small.image, on a thread: BEGIN once when() is true, its reply
        reply_late seconds after the upload opens (the real minichord erases a sector first, and drops what
        comes meanwhile), then its pieces and COMMIT hold seconds after that"""
        import threading
        small = kit.Kit.load(os.path.join(KITS, "small.image")).frames()

        def go():
            t0 = time.monotonic()
            while not when() and time.monotonic() - t0 < 30:
                time.sleep(0.002)
            held = []
            with self.fake.lock:
                send, self.slot.send_out = self.slot.send_out, (held.append if reply_late else self.slot.send_out)
                self.slot.receive(small[0])
                self.slot.send_out = send
            time.sleep(reply_late)
            for m in held:
                send(m)
            time.sleep(hold)
            for m in small[1:]:
                with self.fake.lock:
                    self.slot.receive(m)
        t = threading.Thread(target=go)
        t.start()
        return t

    def loads(self):
        """where in the fake's log each bank load (command 4) is"""
        return [i for i, (a, v) in enumerate(list(self.fake.log)) if a == 0 and v % 128 == 4]

    def test_bank_walk_waits_out_another_programs_upload(self):
        # another program's upload begins in the middle of a backup's walk through the banks: the load it
        # drops is asked again once it ends, longer than the 3 s a report is waited for
        t = self.another_upload(lambda: len(self.loads()) >= 4, hold=3.5)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "b.json")
            code, out = run("backup", path)
            t.join()
            self.assertEqual(code, 0, out)
            self.assertIn("another program is sending the minichord a kit: waiting for it to end", out)
            banks = read_json(path)["banks"]
        self.assertEqual([(e["bank"], e["name"], e["values"][20]) for e in banks],
                         [(b, f"Bank {b + 1}", 30 * b) for b in range(12)])
        self.assertEqual(self.slot.state, 1)                      # theirs landed

    def test_restore_waits_out_another_programs_upload(self):
        # another program's upload opens in the middle of a bank's settings, and the minichord drops what
        # comes until minictl hears it (0.4 s, a slow erase): the rest waits it out, and the bank that
        # then differs from the file is written again
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "b.json")
            code, out = run("backup", path)
            self.assertEqual(code, 0, out)
            data = read_json(path)
            plain = [a for a in range(130, 240) if a in S.params and S.params[a].get("lock") not in ("default", "fixed")]
            for e in data["banks"]:
                for a in plain:
                    e["values"][a] += 1
            write_json(path, data)
            self.fake.log.clear()
            mid = lambda: len(self.loads()) >= 3 and len(self.fake.log) - self.loads()[2] > 50
            t = self.another_upload(mid, hold=1.0, reply_late=0.4)
            code, out = run("--yes", "restore", path, "--banks", "2-5")
            t.join()
        self.assertEqual(code, 0, out)
        self.assertIn("another program is sending the minichord a kit: waiting for it to end", out)
        self.assertIn("dropped some of what was sent: writing bank 4 again", out)
        self.assertIn("every bank matches the file", out)
        for b in range(1, 5):
            self.assertEqual([self.fake.banks[b][a] for a in plain], [data["banks"][b]["values"][a] for a in plain], b)
        self.assertEqual(self.slot.state, 1)

    def test_another_programs_upload_waited_out(self):
        import fake_minichord
        # it opened a moment ago and goes silent: the minichord, in its upload, drops minictl's request for
        # its settings, so minictl asks INFO (state 2), waits for it to end and asks again. The minichord's
        # 5 s and minictl's 6 s shortened, the 5 s still longer than minictl's 3 s wait for the settings and
        # the 6 s longer than the 5 (counted from the other program's last DATA: minictl's INFO doesn't
        # start them again)
        quiet, silence = device.KIT_OTHERS_QUIET, fake_minichord.SILENCE
        device.KIT_OTHERS_QUIET, fake_minichord.SILENCE = 4.0, 3.5
        try:
            small = kit.Kit.load(os.path.join(KITS, "small.image")).frames()
            with self.fake.lock:
                self.slot.receive(small[0])                       # another program's BEGIN, then nothing more
            self.assertTrue(self.slot.open)
            code, out = run("-y", "kit", "send", SYNTHETIC)
        finally:
            device.KIT_OTHERS_QUIET, fake_minichord.SILENCE = quiet, silence
        self.assertEqual(code, 0, out)
        self.assertIn("another program is sending the minichord a kit", out)
        self.assertEqual((self.slot.state, U.crc32(self.slot.stored) != 0), (1, True))
        self.assertEqual(self.info()["crc"], 0x19A640C5)

    # ---- the sender's timers and answers, user_kit.md section 5 ----

    def answer_piece(self, offset, statuses):
        """the piece at offset answered with these statuses in turn, then taken as usual; 5 and 8 end the
        upload first, as the minichord's do"""
        slot, data, left = self.slot, self.slot.data, list(statuses)

        def answered(m):
            if left and U.decode_n(m[4:7]) == offset:
                status = left.pop(0)
                if status in (5, 8):
                    slot.finish(False)
                return slot.status(U.DATA, status)
            return data(m)
        slot.data = answered

    def test_data_statuses(self):
        piece = U.data_message(self.synthetic.image, 448)
        d = device.Minichord("fakekit")
        try:
            # 3 and 7 (damaged on the way): the same piece again, each counted among its three tries
            self.answer_piece(448, [7, 3])
            self.assertEqual(d.kit_upload(self.synthetic.image)["state"], 1)
            self.assertEqual(self.slot.log.count(piece), 3)
            # three tries not taken, or any other status, end it with ABORT; not 5 or 8, where the minichord
            # has ended it itself
            for statuses, aborted in (([7, 3, 7], True), ([6], True), ([2], True), ([1], True), ([5], False), ([8], False)):
                self.slot = self.fake.new_kit_slot()
                self.answer_piece(448, statuses)
                with self.assertRaises(RuntimeError) as e:
                    d.kit_upload(self.synthetic.image)
                ops = [m[3] for m in self.slot.log]
                self.assertEqual(self.slot.log.count(piece), len(statuses), statuses)
                self.assertEqual(ops[-1] == U.ABORT, aborted, statuses)
                self.assertNotIn(U.COMMIT, ops)
                self.assertIn("three tries" if len(statuses) == 3 else U.STATUSES[statuses[0]], str(e.exception))
                self.assertFalse(self.slot.open or self.slot.muted, statuses)
        finally:
            d.close()

    def test_begin_unanswered_aborts(self):
        self.slot.lose_reply(U.BEGIN)                             # it opened, and only the reply went missing
        d = device.Minichord("fakekit")
        try:
            t0 = time.monotonic()
            with self.assertRaises(RuntimeError) as e:
                d.kit_upload(self.synthetic.image)
            self.assertGreaterEqual(time.monotonic() - t0, 3.0)
            self.assertIn("didn't answer BEGIN", str(e.exception))
        finally:
            d.close()
        self.assertEqual([m[3] for m in self.slot.log], [U.BEGIN, U.ABORT])
        self.assertFalse(self.slot.open or self.slot.muted)       # ended then, not 5 s later

    def test_commit_unanswered(self):
        d = device.Minichord("fakekit")
        try:
            # COMMIT itself lost: INFO says the upload is still open (receiving), so ABORT
            commit = self.slot.commit
            self.slot.commit = lambda: setattr(self.slot, "commit", commit)
            with self.assertRaises(RuntimeError) as e:
                d.kit_upload(self.synthetic.image)
            self.assertIn("didn't answer COMMIT", str(e.exception))
            self.assertEqual([m[3] for m in self.slot.log[-3:]], [U.COMMIT, U.INFO, U.ABORT])
            self.assertEqual((self.slot.open, self.slot.muted, self.slot.state), (False, False, 0))
            # its reply lost, and INFO says neither landed nor open (the image failed at COMMIT): given up,
            # no ABORT
            self.slot = self.fake.new_kit_slot()
            self.slot.lose_reply(U.COMMIT)
            with self.assertRaises(RuntimeError) as e:
                d.kit_upload(kit.Kit.load(os.path.join(KITS, "broken", "data_crc.image")).image)
            self.assertIn("says its slot is empty", str(e.exception))
            self.assertEqual([m[3] for m in self.slot.log[-2:]], [U.COMMIT, U.INFO])
            # COMMIT lost, and INFO unanswered too: ABORT, in case the upload is open
            self.slot = self.fake.new_kit_slot()
            commit = self.slot.commit
            self.slot.commit = lambda: setattr(self.slot, "commit", commit)
            self.slot.lose_reply(U.INFO)
            with self.assertRaises(RuntimeError) as e:
                d.kit_upload(self.synthetic.image)
            self.assertIn("didn't answer COMMIT", str(e.exception))
            self.assertEqual([m[3] for m in self.slot.log[-3:]], [U.COMMIT, U.INFO, U.ABORT])
            self.assertFalse(self.slot.open or self.slot.muted)
        finally:
            d.close()

    def test_commit_refused_is_followed_by_abort(self):
        # a COMMIT damaged on the way is answered 3 and leaves the upload open: ABORT ends it at once
        d = device.Minichord("fakekit")
        try:
            self.slot.commit = lambda: self.slot.status(U.COMMIT, 3)
            with self.assertRaises(RuntimeError) as e:
                d.kit_upload(self.synthetic.image)
            self.assertIn(U.STATUSES[3], str(e.exception))
            self.assertEqual([m[3] for m in self.slot.log[-2:]], [U.COMMIT, U.ABORT])
            self.assertEqual((self.slot.open, self.slot.muted, self.slot.state), (False, False, 0))
            # 5: nothing was open, so nothing to ABORT
            self.slot = self.fake.new_kit_slot()
            self.slot.commit = lambda: (self.slot.finish(False), self.slot.status(U.COMMIT, 5))
            with self.assertRaises(RuntimeError):
                d.kit_upload(self.synthetic.image)
            self.assertEqual(self.slot.log[-1][3], U.COMMIT)
        finally:
            d.close()

    def test_stopped_after_begin_sends_abort(self):
        # Ctrl-C (or anything) between BEGIN sent and COMMIT answered: ABORT on the way out. While BEGIN's
        # reply is awaited, then after the fifth piece
        d = device.Minichord("fakekit")
        try:
            take = d._kit_take

            def interrupted(*a):
                d._kit_take = take
                raise KeyboardInterrupt
            d._kit_take = interrupted
            with self.assertRaises(KeyboardInterrupt):
                d.kit_upload(self.synthetic.image)
            self.fake.settle()
            self.assertEqual([m[3] for m in self.slot.log], [U.BEGIN, U.ABORT])
            self.assertFalse(self.slot.open or self.slot.muted)

            def progress(done, size):
                if done >= 5 * U.PIECE:
                    raise KeyboardInterrupt
            self.slot = self.fake.new_kit_slot()
            with self.assertRaises(KeyboardInterrupt):
                d.kit_upload(self.synthetic.image, progress)
            self.fake.settle()
            self.assertEqual([m[3] for m in self.slot.log], [U.BEGIN] + [U.DATA] * 5 + [U.ABORT])
            self.assertFalse(self.slot.open or self.slot.muted)
            # an ABORT that can't go out doesn't hide what stopped it
            self.slot = self.fake.new_kit_slot()

            def unsendable():
                raise OSError("the port went away")
            d.kit_abort = unsendable
            with self.assertRaises(KeyboardInterrupt):
                d.kit_upload(self.synthetic.image, progress)
            # nor what made it give up: the RuntimeError with the minichord's reason comes through
            self.slot = self.fake.new_kit_slot()
            self.answer_piece(448, [6])
            with self.assertRaises(RuntimeError) as e:
                d.kit_upload(self.synthetic.image)
            self.assertIn(U.STATUSES[6], str(e.exception))
            self.assertTrue(self.slot.open)                       # no ABORT went: the minichord's 5 s end it
            with self.fake.lock:
                self.slot.finish(False)
        finally:
            d.close()

    def test_late_replies_to_its_own_upload_arent_another_programs(self):
        d = device.Minichord("fakekit")
        try:
            self.assertEqual(d.kit_upload(self.synthetic.image)["state"], 1)
            late = list(U.status_reply(U.DATA, 0, 224))
            d._on_midi((late, 0.0), None)                          # just after: a late answer to its own piece
            self.assertFalse(d.kit_others())
            d.kit_mine_until = 0.0                                 # a second on: another program's
            d._on_midi((late, 0.0), None)
            self.assertTrue(d.kit_others())
        finally:
            d.close()

    def test_other_traffic_waits_for_the_upload(self):
        import threading
        self.slot.lose_reply(U.DATA, skip=3)                      # an upload a second longer
        d = device.Minichord("fakekit")
        try:
            t = threading.Thread(target=d.kit_upload, args=(self.synthetic.image,))
            t.start()
            time.sleep(0.3)
            self.assertTrue(self.slot.open)
            d.set(24, 55)                                         # held until the upload ends, not dropped
            self.assertFalse(self.slot.open)
            t.join()
        finally:
            d.close()
        self.fake.settle()
        self.assertEqual(self.fake.live[24], 55)
        self.assertEqual(self.slot.state, 1)

    def test_fake_slot_answers_as_the_contract_says(self):
        small = kit.Kit.load(os.path.join(KITS, "small.image")).frames()   # BEGIN, two pieces, COMMIT
        d = device.Minichord("fakekit")

        def ask(msg, op=None):
            r = d._kit_ask(msg, lambda r: True, 1.0)
            self.assertIsNotNone(r, msg.hex())
            return (r["status"], r["next"]) if r["kind"] == "status" else r

        try:
            self.assertEqual(ask(small[1]), (5, 0))                        # DATA with no upload begun
            self.assertEqual(ask(U.abort_message()), (5, 0))
            self.assertEqual(ask(U.begin_message(256 + 262148, 0)), (2, 0))   # bigger than the room
            self.assertEqual(ask(U.begin_message(258, 0)), (2, 0))         # not a multiple of 4
            self.assertEqual(ask(U.message(U.BEGIN, bytes([2]) + U.n3(444) + U.n5(0))), (4, 0))   # proto 2
            self.assertEqual(ask(bytes.fromhex("f07d6b060043f7")), (3, 0))  # CLEAR without its "CL"
            self.assertEqual(ask(bytes.fromhex("f07d6b09000000f7")), (3, 0))  # no such op
            self.assertEqual(ask(small[0]), (0, 0))
            self.assertEqual(ask(small[0]), (1, 0))                        # one is open already
            self.assertEqual(ask(U.info_message())["state"], 2)
            self.assertEqual(ask(small[2]), (6, 0))                        # out of order: 0 is expected
            self.assertEqual(ask(small[1]), (0, 224))
            programs = len(self.slot.programs)
            self.assertEqual(ask(small[1]), (0, 224))                      # again: answered, not written again
            self.assertEqual(len(self.slot.programs), programs)
            bad = bytearray(small[2])
            bad[20] ^= 1                                                   # a payload byte changed on the way
            self.assertEqual(ask(bytes(bad)), (7, 224))
            self.assertEqual(ask(small[2][:-3] + b"\xf7"), (3, 224))       # cut short
            self.assertEqual(ask(small[2]), (0, 444))
            self.assertEqual(ask(U.commit_message()), (0, 0))
            self.assertEqual(d._kit_take(lambda r: r["kind"] == "info", 1.0, 0)["crc"], 0xC675026D)
            self.assertEqual(ask(U.abort_message()), (5, 0))
            self.assertEqual(ask(small[0]), (0, 0))                        # over it: the kit given up at once
            self.assertEqual(ask(U.info_message())["state"], 2)
            self.assertEqual(ask(U.abort_message()), (0, 0))
            self.assertEqual((ask(U.info_message())["state"], ask(U.info_message())["reason"]), (0, 1))
        finally:
            d.close()
        self.assertFalse(self.slot.open or self.slot.muted)

    def test_image_failing_at_commit(self):
        # past minictl's own check, as another sender might: a header CRC, a data_base that isn't 0 (a copy
        # from a minichord's flash) and the sound's CRC are each found at COMMIT, 10
        d = device.Minichord("fakekit")
        try:
            for name in ("header_crc.image", "data_base_nonzero.image", "data_crc.image"):
                image = kit.Kit.load(os.path.join(KITS, "broken", name)).image
                with self.assertRaises(RuntimeError) as e:
                    d.kit_upload(image)
                self.assertIn(U.STATUSES[10], str(e.exception), name)
                self.assertEqual(d.kit_info()["state"], 0, name)
                # ended by the minichord; the ABORT after any refused COMMIT but 5 finds nothing open
                self.assertEqual([m[3] for m in self.slot.log[-3:]], [U.COMMIT, U.ABORT, U.INFO], name)
                self.assertEqual(U.parse_reply(self.slot.replies[-2])["status"], 5, name)
                self.assertFalse(self.slot.muted)
        finally:
            d.close()

    def test_no_kit_slot(self):
        self.fake.kit_answers = False                             # as fork firmware before 113, or stock
        code, out = run("kit", "info")
        self.assertEqual(code, 1)
        self.assertIn("this firmware has no kit slot", out)
        self.fake.live[7] = 112
        code, out = run("kit", "info")
        self.assertIn("needs firmware 113 or later", out)

    def test_too_big_and_disabled(self):
        self.slot.move(0x601D0000)                                # a firmware that leaves 64 kB
        code, out = run("-y", "kit", "send", SYNTHETIC)
        self.assertEqual(code, 1)
        self.assertIn("room for 64 kB", out)
        self.assertEqual(self.sent(), [])
        self.slot.move(0x601E0000)                                # none
        self.assertEqual((self.info()["state"], self.info()["reason"]), (4, 7))
        code, out = run("-y", "kit", "send", SYNTHETIC)
        self.assertIn("disabled", out)
        self.assertEqual(self.sent(), [])

    def test_data_base_moved(self):
        self.slot.store(self.synthetic.image)
        kit.remember(self.synthetic)
        self.slot.move(0x601B0000)                                # a bigger firmware: the sound starts higher
        d = self.info()
        self.assertEqual((d["state"], d["reason"]), (3, 4))
        code, out = run("kit", "info")
        self.assertIn("moved where the sound starts", out)
        self.assertIn("minictl kit send", out)
        code, out = run("-y", "kit", "send", kit.cache_path())
        self.assertEqual(code, 0, out)
        self.assertEqual((self.info()["state"], self.info()["data_base"]), (1, 0x601B0000))

    @unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("aconnect"), "ALSA's aconnect")
    def test_other_program_on_the_port(self):
        chrome = subprocess.Popen([sys.executable, "-c", (
            "import rtmidi, sys, time\n"
            "o = rtmidi.MidiOut(name='Chrome')\n"
            "o.open_port([i for i, n in enumerate(o.get_ports()) if 'fakekit MIDI 1' in n][0], 'Chrome output')\n"
            "print('open', flush=True)\n"
            "sys.stdin.read()\n")], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(chrome.stdout.readline().strip(), "open")
            code, out = run("-y", "kit", "send", SYNTHETIC)
            self.assertEqual(code, 1)
            self.assertIn("a browser is connected to the minichord's MIDI port: Chrome", out)
            self.assertIn("close minicontrol's tab: its messages are dropped while the kit is written, and unplugging "
                          "with Web MIDI open can hang USB on some Linux kernels", out)
            self.assertNotIn("this machine", out)
            self.assertIn("--force", out)
            self.assertEqual(self.sent(), [])
            code, out = run("-y", "kit", "send", SYNTHETIC, "--force")
            self.assertEqual(code, 0, out)
            self.assertEqual(self.slot.state, 1)
        finally:
            chrome.stdin.close()
            chrome.wait(10)
            chrome.stdout.close()

    @unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("aconnect"), "ALSA's aconnect")
    def test_bridge_on_the_port_is_only_named(self):
        # PipeWire's ALSA bridge (or a2jmidid, Midi Through) subscribes to every port: named, and the kit sent
        bridge = subprocess.Popen([sys.executable, "-c", (
            "import rtmidi, sys, time\n"
            "i = rtmidi.MidiIn(name='PipeWire-RT-Event')\n"
            "i.open_port([k for k, n in enumerate(i.get_ports()) if 'fakekit MIDI 1' in n][0], 'input')\n"
            "print('open', flush=True)\n"
            "sys.stdin.read()\n")], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(bridge.stdout.readline().strip(), "open")
            code, out = run("-y", "kit", "send", SYNTHETIC)
            self.assertEqual(code, 0, out)
            self.assertIn("also connected to the minichord's MIDI port: PipeWire-RT-Event", out)
            self.assertNotIn("browser", out)
            self.assertEqual(self.slot.state, 1)
        finally:
            bridge.stdin.close()
            bridge.wait(10)
            bridge.stdout.close()


if __name__ == "__main__":
    unittest.main(verbosity=1)

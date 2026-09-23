import copy
from itertools import combinations
import json
import random
import re
import unittest

from model.chronos.capture_metadata import COUNTERS
from model.chronos.capture_session import decode_capture
from model.chronos.controller import COMMANDS, STATES
from model.chronos.events import Observation
from model.chronos.predicates import KINDS, keeper, matcher
from model.chronos.raw_decode import decode_page
from model.chronos.raw_encode import _TYPES
from model.chronos.registers import LANES, MAP, MAP_PATH, RegisterFile, check, pack, unpack
from model.chronos.retention import CODECS
from scripts.config import ROOT, read_json

FULL = 0xFFFFFFFF


def user(value):
    return Observation("USER_EVENT", dict(value=value))


def request(value):
    return Observation("BUS_REQ", dict(transaction=value, address=0x8000 + value, write=True, data=value, mask=15))


class RegisterMapTests(unittest.TestCase):
    def setUp(self):
        self.doc = json.loads(MAP_PATH.read_text())

    def test_map_is_valid_and_covers_every_interface_group(self):
        self.assertEqual(len(MAP["registers"]), 49)
        self.assertEqual({register["group"] for register in MAP["registers"].values()},
                         {"identity", "configuration", "commands", "triggers", "status", "accounting", "readout"})
        offsets = sorted(register["offset"] for register in MAP["registers"].values())
        self.assertEqual(len(offsets), len(set(offsets)))
        self.assertLess(offsets[-1], MAP["window_bytes"])

    def test_malformed_maps_are_rejected(self):
        def mutated(change):
            doc = copy.deepcopy(self.doc)
            change(doc)
            return doc
        registers = lambda doc: {register["name"]: register for register in doc["registers"]}
        cases = {
            "misaligned": lambda doc: registers(doc)["STATUS"].update(offset="0x051"),
            "duplicate offset": lambda doc: registers(doc)["STATUS"].update(offset="0x054"),
            "outside window": lambda doc: registers(doc)["STATUS"].update(offset="0x200"),
            "decimal offset": lambda doc: registers(doc)["STATUS"].update(offset=80),
            "overlap": lambda doc: registers(doc)["STATUS"]["fields"][1].update(lsb=2),
            "outside word": lambda doc: registers(doc)["CFG_SPLIT"]["fields"][1].update(width=17),
            "enum too wide": lambda doc: registers(doc)["STATUS"]["fields"][0].update(width=2),
            "unknown enum": lambda doc: registers(doc)["STATUS"]["fields"][0].update(enum="nope"),
            "reset too wide": lambda doc: registers(doc)["TRIG_MATCH"]["fields"][1].update(reset=8),
            "bad access": lambda doc: registers(doc)["STATUS"].update(access="rx"),
            "duplicate name": lambda doc: doc["registers"].append(copy.deepcopy(registers(doc)["STATUS"])),
            "missing group": lambda doc: doc.update(registers=[r for r in doc["registers"] if r["group"] != "readout"]),
            "extra key": lambda doc: doc.update(extra=1),
            "duplicate enum value": lambda doc: doc["enums"]["state"].append("ARMED"),
        }
        for name, change in cases.items():
            with self.subTest(case=name), self.assertRaises(ValueError):
                check(mutated(change))

    def test_enums_are_the_model_names(self):
        enums = MAP["enums"]
        self.assertEqual(tuple(enums["state"]), STATES)
        self.assertEqual(enums["outcome"], ["none", "accepted", "ignored", "rejected", "superseded"])
        self.assertEqual(tuple(enums["counter"]), COUNTERS)
        self.assertEqual(tuple(enums["codec"]), CODECS)
        self.assertEqual(tuple(enums["kind"]), KINDS)
        self.assertEqual(list(KINDS), sorted(_TYPES, key=_TYPES.get))
        self.assertEqual([f"{source}.{lane}" for source, lane in LANES], enums["lane"])
        source = "".join((ROOT / f"model/chronos/{name}.py").read_text()
                         for name in ("admission", "snapshot", "controller", "retention"))
        reasons = set(re.findall(r"stop\('(\w+)'\)", source)) | {"manual"}
        self.assertEqual(set(enums["stop_reason"]), reasons | {"none"})
        self.assertEqual(set(enums["storage_error"]), set(re.findall(r'StorageError\("(\w+)"\)', source)) | {"none"})
        self.assertEqual(list(unpack("COMMAND", 0)), list(COMMANDS) + ["reset_source_id"])
        self.assertEqual(list(unpack("OUTCOME", 0)), list(COMMANDS))

    def test_pack_unpack_round_trip_every_field(self):
        rng = random.Random(0x52454753)
        for name, register in MAP["registers"].items():
            for _ in range(20):
                values = {}
                for key, field in register["fields"].items():
                    limit = len(MAP["enums"][field["enum"]]) if "enum" in field else 1 << field["width"]
                    value = rng.randrange(limit)
                    values[key] = MAP["enums"][field["enum"]][value] if "enum" in field else value
                with self.subTest(register=name):
                    self.assertEqual(unpack(name, pack(name, **values)), values)
        for size in range(len(COMMANDS) + 1):
            for chosen in combinations(COMMANDS, size):
                word = pack("COMMAND", **dict.fromkeys(chosen, 1))
                self.assertEqual({key for key, value in unpack("COMMAND", word).items() if value}, set(chosen))
        for bad in (dict(state="NOPE"), dict(stop_reason=16), dict(drain_timeout=2)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                pack("STATUS", **bad)
        with self.assertRaises(ValueError):
            unpack("STATUS", 1 << 32)


class TriggerSlotTests(unittest.TestCase):
    def test_equal_mask_range_kinds_and_multiple_slot_reasons(self):
        match = matcher([dict(kinds=["BUS_REQ"], mode="equal", value=0x8000, mask=0xFF00),
                         None,
                         dict(kinds=["BUS_REQ", "USER_EVENT"], mode="range", base=0x8000, limit=0x8010)], 4)
        cases = [(request(0), "slot0|slot2"), (request(0x0F), "slot0|slot2"), (request(0x10), "slot0"),
                 (request(0x100), None), (user(0x8000), "slot2"), (user(0x8010), None), (user(0x7FFF), None),
                 (Observation("RETIRE", dict(pc=0x8000, next_pc=0x8004, length=4, boundary=0)), None)]
        for observation, reason in cases:
            with self.subTest(observation=observation):
                self.assertEqual(match(observation), reason)
        edge = matcher([dict(kinds=["USER_EVENT"], mode="range", base=0, limit=FULL)], 1)
        self.assertEqual((edge(user(0)), edge(user(FULL - 1)), edge(user(FULL))), ("slot0", "slot0", None))

    def test_invalid_slots_and_filters_are_rejected(self):
        invalid = ([dict(kinds=[], mode="equal", value=0, mask=0)], [dict(kinds=["X"], mode="equal", value=0, mask=0)],
                   [dict(kinds=["RETIRE"], mode="equal", value=1 << 32, mask=0)],
                   [dict(kinds=["RETIRE"], mode="range", base=5, limit=5)],
                   [dict(kinds=["RETIRE"], mode="range", value=0, mask=0)],
                   [dict(kinds=["RETIRE", "RETIRE"], mode="equal", value=0, mask=0)], [None] * 5, "slot")
        for slots in invalid:
            with self.subTest(slots=slots), self.assertRaises(ValueError):
                matcher(slots, 4)
        self.assertFalse(keeper([])(user(1)))
        with self.assertRaises(ValueError):
            keeper(["USER_EVENT", "USER_EVENT"])


class RegisterFileTests(unittest.TestCase):
    def setUp(self):
        self.config = read_json(ROOT / "configs/baseline.json")
        self.registers = RegisterFile(self.config)
        self.tick = 0

    def cycle(self, *observations, command=None, service=False):
        if command:
            self.registers.write("COMMAND", pack("COMMAND", **command))
        self.tick += 1
        return self.registers.cycle(self.tick, observations, service=service)

    def program(self, **mode):
        registers = self.registers
        registers.write("CFG_SPLIT", pack("CFG_SPLIT", pre_pages=16, post_pages=16))
        registers.write("CFG_MODE", pack("CFG_MODE", **dict(dict(codec="raw-v1", keep_kinds=0x7F), **mode)))
        registers.write("CFG_POST_TICKS_LO", 3)
        registers.write("TRIG1_CTRL", pack("TRIG1_CTRL", enable=1, mode="equal", kinds=1 << KINDS.index("USER_EVENT")))
        registers.write("TRIG1_VALUE", 7)
        registers.write("TRIG1_MASK", FULL)

    def outcome(self):
        return {key: value for key, value in unpack("OUTCOME", self.registers.read("OUTCOME")).items() if value != "none"}

    def state(self):
        return unpack("STATUS", self.registers.read("STATUS"))

    def readout(self):
        registers = self.registers
        session = registers.read("SESSION_ID_LO") | registers.read("SESSION_ID_HI") << 32
        registers.write("READ_SESSION_LO", session & FULL)
        registers.write("READ_SESSION_HI", session >> 32)
        registers.write("READ_OFFSET", 0)
        length = registers.read("READ_LENGTH")
        data = b"".join(registers.read("READ_DATA").to_bytes(4, "little") for _ in range(-(-length // 4)))
        return data[:length]

    def test_identity_and_capabilities(self):
        registers = self.registers
        self.assertEqual(registers.read("CHRONOS_ID").to_bytes(4, "little"), b"CHRN")
        self.assertEqual(unpack("VERSION", registers.read("VERSION")), dict(map_minor=1, map_major=1))
        self.assertEqual(unpack("CAPS", registers.read("CAPS")), dict(sources=4, trigger_slots=4, fifo_depth_log2=4,
                         page_bytes_log2=10, codecs=3, max_record_bytes=128, sink_bytes=8))
        self.assertEqual(registers.read("CAPS_SRAM_BYTES"), 32768)

    def test_register_driven_capture_with_filtered_slot_trigger_and_readout(self):
        self.program(keep_kinds=0x7F & ~(1 << KINDS.index("USER_EVENT")))
        self.assertEqual(self.state()["state"], "DISABLED")
        self.cycle(command=dict(configure=1, arm=1))
        self.assertEqual(self.outcome(), dict(configure="accepted", arm="accepted"))
        for value in range(1, 4):
            self.cycle(request(value), user(value + 5), service=True)
        status = self.state()
        self.assertEqual((status["state"], status["trigger_latched"], status["trigger_software"]),
                         ("POST_TRIGGER", 1, 0))
        self.assertEqual(unpack("TRIG_MATCH", self.registers.read("TRIG_MATCH")),
                         dict(lanes=1 << LANES.index((3, 0)), primary=LANES.index((3, 0)), software=0, slots=0b10))
        self.assertEqual(self.registers.read("TRIG_TICK_LO"), 3)
        self.registers.write("ACCT_SELECT", pack("ACCT_SELECT", source=3, counter="filtered"))
        self.assertEqual(self.registers.read("ACCT_VALUE_LO"), 3)
        for value in range(4, 9):
            self.cycle(request(value), service=True)
        for _ in range(200):
            if self.state()["state"] == "FROZEN":
                break
            self.cycle(service=True)
        status = self.state()
        self.assertEqual((status["state"], status["stop_reason"]), ("FROZEN", "post_window"))
        image = self.readout()
        self.assertEqual(unpack("READ_STATUS", self.registers.read("READ_STATUS")), dict(valid=1, stale=0))
        events = [event for offset in range(0, len(image), 1024)
                  for event in decode_page(image[offset:offset + 1024])["events"]]
        decoded = decode_capture(self.registers.controller.read()[1])
        self.assertEqual(tuple(events), decoded["events"])
        self.assertEqual([event.observation.fields["transaction"] for event in events], [1, 2, 3, 4, 5])
        self.assertEqual(decoded["metadata"]["capture"]["trigger"]["matches"], [dict(source=3, lane=0, reason="slot1")])

    def test_stale_session_and_cleared_snapshot_read_nothing(self):
        self.program()
        self.cycle(command=dict(configure=1, arm=1))
        self.cycle(command=dict(stop=1))
        self.registers.write("READ_SESSION_LO", 99)
        self.assertEqual(unpack("READ_STATUS", self.registers.read("READ_STATUS")), dict(valid=0, stale=1))
        self.assertEqual((self.registers.read("READ_LENGTH"), self.registers.read("READ_DATA")), (0, 0))
        self.assertEqual(self.readout(), b"")
        self.assertEqual(unpack("READ_STATUS", self.registers.read("READ_STATUS")), dict(valid=1, stale=0))
        self.cycle(command=dict(clear=1))
        self.assertEqual(self.readout(), b"")
        self.assertEqual(self.state()["state"], "CLEARING")

    def test_invalid_register_configuration_is_rejected_by_the_command(self):
        self.program()
        self.registers.write("CFG_SPLIT", pack("CFG_SPLIT", pre_pages=16, post_pages=8))
        self.cycle(command=dict(configure=1, arm=1))
        self.assertEqual(self.outcome(), dict(configure="rejected", arm="rejected"))
        self.program()
        self.registers.write("TRIG1_CTRL", pack("TRIG1_CTRL", enable=1, mode="range", kinds=1))
        self.registers.write("TRIG1_BASE", 8)
        self.registers.write("TRIG1_LIMIT", 8)
        self.cycle(command=dict(configure=1))
        self.assertEqual(self.outcome(), dict(configure="rejected"))
        self.assertEqual(self.registers.read("CONFIG_TAG_LO"), 0)
        self.registers.write("TRIG1_LIMIT", 9)
        self.cycle(command=dict(configure=1))
        self.assertEqual(self.outcome(), dict(configure="accepted"))
        self.assertEqual(self.registers.read("CONFIG_TAG_LO"), 1)

    def test_command_word_carries_same_cycle_races_and_source_reset(self):
        self.program()
        self.cycle(command=dict(configure=1, arm=1))
        self.cycle(command=dict(stop=1, software_trigger=1, reset_source=1, reset_source_id=2))
        self.assertEqual(self.outcome(), dict(stop="accepted", reset_source="superseded",
                                              software_trigger="superseded"))
        self.cycle(command=dict(trace_reset=1, arm=1))
        self.assertEqual(self.outcome(), dict(trace_reset="accepted", arm="superseded"))
        self.cycle(command=dict(arm=1))
        self.cycle(command=dict(reset_source=1, reset_source_id=2))
        self.assertEqual(self.registers.controller.capture.model.epochs, [0, 0, 1, 0])
        self.cycle()
        self.assertEqual(self.outcome(), dict(reset_source="accepted"))

    def test_access_rules_and_word_bounds(self):
        registers = self.registers
        for name in ("STATUS", "READ_DATA", "CHRONOS_ID", "NOPE"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                registers.write(name, 0)
        for name in ("COMMAND", "NOPE"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                registers.read(name)
        for word in (-1, 1 << 32, True):
            with self.subTest(word=word), self.assertRaises(ValueError):
                registers.write("CFG_DRAIN_LIMIT", word)
        registers.write("CFG_DRAIN_LIMIT", 5)
        self.assertEqual(registers.read("CFG_DRAIN_LIMIT"), 5)


if __name__ == "__main__":
    unittest.main()

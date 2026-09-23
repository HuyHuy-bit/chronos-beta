import copy
from itertools import combinations
import json
import random
import re
import unittest

from model.chronos.capture_metadata import COUNTERS
from model.chronos.events import Observation
from model.chronos.predicates import KINDS, keeper, matcher
from model.chronos.raw_encode import _TYPES
from model.chronos.registers import LANES, MAP, MAP_PATH, check, pack, unpack
from model.chronos.retention import CODECS
from scripts.config import ROOT

FULL = 0xFFFFFFFF
STATES = ("DISABLED", "ARMED", "POST_TRIGGER", "DRAINING", "FROZEN", "CLEARING")
COMMANDS = ("trace_reset", "clear", "configure", "arm", "stop", "reset_source", "software_trigger")


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
                         for name in ("admission", "snapshot", "retention"))
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


if __name__ == "__main__":
    unittest.main()

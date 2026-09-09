import itertools
import unittest
from dataclasses import FrozenInstanceError
from types import MappingProxyType

from model.chronos.events import Event, Observation, normalize


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.fields = {
            "RETIRE": {"pc": 3, "next_pc": 9, "length": 4, "boundary": 0},
            "TRAP": {"pc": None, "cause": 2, "target": None, "boundary": 1},
            "IRQ_ACCEPT": {"pc": 0x1000, "cause": 11, "target": 0x2000, "boundary": 2},
            "IRQ_PENDING": {"previous": 0, "current": 1},
            "BUS_REQ": {"transaction": 0, "address": 3, "write": True, "data": 0, "mask": 15},
            "BUS_RESP": {"transaction": 0, "data": None, "error": False},
            "USER_EVENT": {"value": 0},
        }

    def test_exact_fields_and_sources(self):
        sources = {"RETIRE": 0, "TRAP": 2, "IRQ_ACCEPT": 2, "IRQ_PENDING": 2,
                   "BUS_REQ": 1, "BUS_RESP": 1, "USER_EVENT": 3}
        for kind, fields in self.fields.items():
            with self.subTest(kind=kind):
                observation = Observation(kind, fields)
                self.assertEqual(observation.source, sources[kind])
                self.assertEqual(dict(observation.fields), fields)

    def test_fields_are_copied_and_immutable(self):
        fields = {"value": 42}
        observation = Observation("USER_EVENT", MappingProxyType(fields))
        fields["value"] = 43
        fields["extra"] = 99
        self.assertEqual(dict(observation.fields), {"value": 42})
        with self.assertRaises(TypeError):
            observation.fields["value"] = 44
        with self.assertRaises(FrozenInstanceError):
            observation.fields = {"value": 45}
        with self.assertRaises(FrozenInstanceError):
            observation.kind = "RETIRE"

    def test_missing_and_extra_fields(self):
        for kind, fields in self.fields.items():
            for missing in fields:
                with self.subTest(kind=kind, missing=missing):
                    candidate = dict(fields)
                    del candidate[missing]
                    with self.assertRaises(ValueError):
                        Observation(kind, candidate)
            with self.subTest(kind=kind, extra=True):
                with self.assertRaises(ValueError):
                    Observation(kind, dict(fields, extra=0))

    def test_unsigned_widths(self):
        widths = {
            "RETIRE": {"pc": 32, "next_pc": 32, "boundary": 64},
            "TRAP": {"pc": 32, "cause": 32, "target": 32, "boundary": 64},
            "IRQ_ACCEPT": {"pc": 32, "cause": 32, "target": 32, "boundary": 64},
            "IRQ_PENDING": {"previous": 32, "current": 32},
            "BUS_REQ": {"transaction": 32, "address": 32, "data": 32, "mask": 4},
            "BUS_RESP": {"transaction": 32, "data": 32},
            "USER_EVENT": {"value": 32},
        }
        for kind, fields in widths.items():
            for name, bits in fields.items():
                for value in (0, (1 << bits) - 1):
                    with self.subTest(kind=kind, field=name, value=value):
                        observation = Observation(kind, dict(self.fields[kind], **{name: value}))
                        self.assertEqual(observation.fields[name], value)
                for value in (-1, 1 << bits, True, False, 1.0, "0", [], {}):
                    with self.subTest(kind=kind, field=name, invalid=value):
                        with self.assertRaises(ValueError):
                            Observation(kind, dict(self.fields[kind], **{name: value}))

    def test_none_only_for_declared_unavailable_fields(self):
        optional = {("TRAP", "pc"), ("TRAP", "target"), ("IRQ_ACCEPT", "pc"),
                    ("IRQ_ACCEPT", "target"), ("BUS_RESP", "data")}
        for kind, fields in self.fields.items():
            for name in fields:
                with self.subTest(kind=kind, field=name):
                    candidate = dict(fields, **{name: None})
                    if (kind, name) in optional:
                        self.assertIsNone(Observation(kind, candidate).fields[name])
                        zero = Observation(kind, dict(fields, **{name: 0}))
                        self.assertNotEqual(Observation(kind, candidate), zero)
                    else:
                        with self.assertRaises(ValueError):
                            Observation(kind, candidate)

    def test_boolean_fields(self):
        for kind, name in (("BUS_REQ", "write"), ("BUS_RESP", "error")):
            for value in (False, True):
                with self.subTest(kind=kind, value=value):
                    self.assertIs(Observation(kind, dict(self.fields[kind], **{name: value})).fields[name], value)
            for value in (0, 1, -1, "false", 0.0):
                with self.subTest(kind=kind, invalid=value):
                    with self.assertRaises(ValueError):
                        Observation(kind, dict(self.fields[kind], **{name: value}))

    def test_instruction_length_is_exactly_four(self):
        for value in (0, 2, 8, True, False, 4.0, "4"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    Observation("RETIRE", dict(self.fields["RETIRE"], length=value))
        self.assertEqual(Observation("RETIRE", self.fields["RETIRE"]).fields["pc"], 3)

    def test_invalid_kinds_and_field_containers(self):
        for kind in ("", "retire", "UNKNOWN", None, True, 0, [], {}):
            with self.subTest(kind=kind):
                with self.assertRaises(ValueError):
                    Observation(kind, {})
        for fields in (None, [], [("value", 0)], 0, "value"):
            with self.subTest(fields=fields):
                with self.assertRaises(ValueError):
                    Observation("USER_EVENT", fields)


class NormalizationTests(unittest.TestCase):
    def setUp(self):
        self.retire = Observation("RETIRE", {"pc": 0, "next_pc": 4, "length": 4, "boundary": 9})
        self.request = Observation("BUS_REQ", {"transaction": 7, "address": 0, "write": False, "data": 0, "mask": 15})
        self.response = Observation("BUS_RESP", {"transaction": 6, "data": 1, "error": False})
        self.trap = Observation("TRAP", {"pc": None, "cause": 2, "target": 0x100, "boundary": 9})
        self.accept = Observation("IRQ_ACCEPT", {"pc": None, "cause": 11, "target": None, "boundary": 9})
        self.pending = Observation("IRQ_PENDING", {"previous": 0, "current": 1})
        self.user = Observation("USER_EVENT", {"value": 99})

    def test_six_event_cycle_has_canonical_bundles_for_every_input_order(self):
        cycle = (self.retire, self.request, self.response, self.trap, self.pending, self.user)
        expected = ((self.retire,), (self.response, self.request), (self.trap, self.pending), (self.user,))
        for permutation in itertools.permutations(cycle):
            self.assertEqual(normalize(permutation), expected)

    def test_empty_and_sparse_cycles(self):
        self.assertEqual(normalize(()), ((), (), (), ()))
        self.assertEqual(normalize([self.request]), ((), (self.request,), (), ()))
        self.assertEqual(normalize([self.pending, self.accept]), ((), (), (self.accept, self.pending), ()))
        self.assertEqual(normalize(iter([self.user])), ((), (), (), (self.user,)))

    def test_all_kinds_are_unique_per_tick(self):
        for observation in (self.retire, self.request, self.response, self.trap, self.accept, self.pending, self.user):
            with self.subTest(kind=observation.kind):
                duplicate = Observation(observation.kind, observation.fields)
                with self.assertRaises(ValueError):
                    normalize([observation, duplicate])

    def test_trap_and_accept_are_mutually_exclusive(self):
        for cycle in ((self.trap, self.accept), (self.accept, self.pending, self.trap)):
            with self.assertRaises(ValueError):
                normalize(cycle)

    def test_invalid_cycle_entries_fail_without_changing_input(self):
        for invalid in (None, {}, "RETIRE", 0, True):
            with self.subTest(invalid=invalid):
                cycle = [self.retire, self.request, invalid]
                original = list(cycle)
                with self.assertRaises(ValueError):
                    normalize(cycle)
                self.assertEqual(cycle, original)
        for invalid in (None, 1, False):
            with self.assertRaises(ValueError):
                normalize(invalid)

    def test_event_identity_and_observation_are_frozen(self):
        event = Event(tick=10, source=1, epoch=2, sequence=3, lane=1, observation=self.request)
        self.assertEqual((event.tick, event.source, event.epoch, event.sequence, event.lane), (10, 1, 2, 3, 1))
        self.assertIs(event.observation, self.request)
        with self.assertRaises(FrozenInstanceError):
            event.sequence = 4
        with self.assertRaises(TypeError):
            event.observation.fields["transaction"] = 8


if __name__ == "__main__":
    unittest.main()

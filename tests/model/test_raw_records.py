from dataclasses import replace
import json
from pathlib import Path
import random
import unittest

from model.chronos.events import Event, Observation
from model.chronos.raw_decode import DecodeError, decode_record
from model.chronos.raw_encode import encode_record


FIXTURE = json.loads((Path(__file__).parent / "fixtures/raw_v1.json").read_text())


def expected_event(row):
    fields = row["event"]
    return Event(tick=fields["tick"], source=fields["source"], epoch=fields["epoch"],
                 sequence=fields["sequence"], lane=fields["lane"],
                 observation=Observation(fields["kind"], fields["fields"]))


class RawRecordTests(unittest.TestCase):
    def setUp(self):
        self.rows = {row["name"]: row for row in FIXTURE["records"]}

    def wire(self, name):
        return bytes.fromhex(self.rows[name]["hex"])

    def reject_byte(self, name, offset, value):
        data = bytearray(self.wire(name))
        data[offset] = value
        with self.assertRaises(DecodeError):
            decode_record(bytes(data))

    def test_encoder_matches_twelve_literal_vectors(self):
        for name, row in self.rows.items():
            with self.subTest(name=name):
                self.assertEqual(encode_record(expected_event(row)), self.wire(name))

    def test_decoder_matches_independent_semantic_vectors(self):
        for name, row in self.rows.items():
            with self.subTest(name=name):
                self.assertEqual(decode_record(self.wire(name)), expected_event(row))

    def test_none_and_valid_zero_remain_distinct(self):
        for name, field, expected in (("trap_pc_zero", "pc", 0), ("trap_pc_zero", "target", None),
                                      ("trap_target_zero", "pc", None), ("trap_target_zero", "target", 0),
                                      ("bus_resp_none", "data", None), ("bus_resp_zero", "data", 0),
                                      ("irq_accept_none", "pc", None), ("irq_accept_zero", "pc", 0)):
            with self.subTest(name=name, field=field):
                self.assertEqual(decode_record(self.wire(name)).observation.fields[field], expected)

    def test_every_truncation_and_trailing_byte_rejected(self):
        for name in self.rows:
            data = self.wire(name)
            for length in range(len(data)):
                with self.subTest(name=name, length=length), self.assertRaises(DecodeError):
                    decode_record(data[:length])
            for suffix in (b"\0", data):
                with self.subTest(name=name, suffix=len(suffix)), self.assertRaises(DecodeError):
                    decode_record(data + suffix)

    def test_header_type_flags_length_source_lane_and_reserved_rejected(self):
        for name in self.rows:
            for offset, value in ((0, 0), (0, 255), (1, 128), (2, 0), (3, 1),
                                  (4, 4), (5, 2), (6, 1), (7, 1)):
                with self.subTest(name=name, offset=offset, value=value):
                    self.reject_byte(name, offset, value)
            wrong_source = (self.rows[name]["event"]["source"] + 1) % 4
            self.reject_byte(name, 4, wrong_source)
        for name in ("retire", "trap_valid", "irq_accept_none", "bus_resp_valid", "user"):
            with self.subTest(lane_one=name):
                self.reject_byte(name, 5, 1)

    def test_only_declared_validity_flags_accepted(self):
        for name in ("retire", "irq_pending", "bus_req", "user"):
            with self.subTest(name=name):
                self.reject_byte(name, 1, 1)
        for name in ("trap_valid", "irq_accept_none"):
            self.reject_byte(name, 1, 4)
        self.reject_byte("bus_resp_valid", 1, 2)

    def test_invalid_boolean_mask_length_and_payload_reserved_rejected(self):
        for name, offset, value in (("retire", 40, 2), ("retire", 40, 0),
                                    ("retire", 41, 1), ("retire", 42, 1), ("retire", 43, 1),
                                    ("bus_req", 44, 2), ("bus_req", 45, 16),
                                    ("bus_req", 46, 1), ("bus_req", 47, 1),
                                    ("bus_resp_valid", 40, 2), ("bus_resp_valid", 41, 1),
                                    ("bus_resp_valid", 42, 1), ("bus_resp_valid", 43, 1)):
            with self.subTest(name=name, offset=offset):
                self.reject_byte(name, offset, value)

    def test_invalid_field_bytes_must_be_canonical_zero(self):
        for name, offset in (("irq_accept_none", 32), ("irq_accept_none", 40),
                              ("trap_pc_zero", 40), ("trap_target_zero", 32),
                              ("bus_resp_none", 36)):
            with self.subTest(name=name, offset=offset):
                self.reject_byte(name, offset, 1)
        for name in ("trap_valid", "bus_resp_valid"):
            self.reject_byte(name, 1, 0)

    def test_unsigned_identity_and_payload_width_maxima(self):
        wire = bytes.fromhex("06012c0001000000" + "ff" * 24 + "ff" * 8 + "01000000")
        event = Event(tick=(1 << 64) - 1, source=1, epoch=(1 << 64) - 1,
                      sequence=(1 << 64) - 1, lane=0,
                      observation=Observation("BUS_RESP", dict(transaction=(1 << 32) - 1,
                                                               data=(1 << 32) - 1, error=True)))
        self.assertEqual(decode_record(wire), event)
        self.assertEqual(encode_record(event), wire)

    def test_encoder_rejects_identity_types_widths_and_lane_rules(self):
        original = expected_event(self.rows["retire"])
        for field in ("tick", "epoch", "sequence"):
            for value in (-1, 1 << 64, True, 1.0, None):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    encode_record(replace(original, **{field: value}))
        for field, values in (("source", (-1, 1, 4, True, 0.0)), ("lane", (-1, 1, 2, True, 0.0))):
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    encode_record(replace(original, **{field: value}))
        for name in ("trap_valid", "irq_accept_none", "bus_resp_valid", "user"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                encode_record(replace(expected_event(self.rows[name]), lane=1))

    def test_encoder_revalidates_supplied_observation_fields(self):
        for fields in (dict(value=True), dict(value=-1), dict(value=1 << 32),
                       dict(value=1, extra=0), {}):
            observation = Observation("USER_EVENT", dict(value=0))
            object.__setattr__(observation, "fields", fields)
            event = replace(expected_event(self.rows["user"]), observation=observation)
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                encode_record(event)

    def test_decoder_requires_exact_bytes_input(self):
        for data in (None, "", 0, [], bytearray(self.wire("retire")), memoryview(self.wire("retire"))):
            with self.subTest(type=type(data).__name__), self.assertRaises(DecodeError):
                decode_record(data)

    def test_bounded_seeded_random_malformed_inputs(self):
        rng = random.Random(0xC4705)
        for case in range(1000):
            data = bytes(rng.randrange(256) for _ in range(rng.randrange(129)))
            with self.subTest(case=case), self.assertRaises(DecodeError):
                decode_record(data)


if __name__ == "__main__":
    unittest.main()

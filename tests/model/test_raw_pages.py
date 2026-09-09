from dataclasses import replace
import json
from pathlib import Path
import random
import unittest
from unittest.mock import patch

from model.chronos.events import Event, Observation
from model.chronos.raw_decode import DecodeError, decode_page
from model.chronos.raw_encode import encode_page


FIXTURE = json.loads((Path(__file__).parent / "fixtures/raw_v1.json").read_text())


def bitwise_crc(data):
    value = 0xFFFFFFFF
    for byte in data:
        value ^= byte
        for _ in range(8):
            value = (value >> 1) ^ (0xEDB88320 if value & 1 else 0)
    return value ^ 0xFFFFFFFF


def reseal_header(page):
    page = bytearray(page)
    page[52:56] = bytes(4)
    page[52:56] = bitwise_crc(page[:64]).to_bytes(4, "little")
    return bytes(page)


def independent_page(records=(), page_bytes=256):
    payload = b"".join(records)
    page = bytearray(page_bytes)
    page[:8] = bytes.fromhex("4348525001004000")
    page[8:16] = (1).to_bytes(8, "little")
    page[16:24] = (2).to_bytes(8, "little")
    page[24:32] = (3).to_bytes(8, "little")
    page[32:36] = len(payload).to_bytes(4, "little")
    page[36:40] = len(records).to_bytes(4, "little")
    tick = min((int.from_bytes(record[24:32], "little") for record in records), default=0)
    page[40:48] = tick.to_bytes(8, "little")
    page[48:52] = bitwise_crc(payload).to_bytes(4, "little")
    page[64:64 + len(payload)] = payload
    return reseal_header(page)


def record_identity(record, *, tick, sequence, epoch=0, lane=None):
    data = bytearray(record)
    data[8:16] = epoch.to_bytes(8, "little")
    data[16:24] = sequence.to_bytes(8, "little")
    data[24:32] = tick.to_bytes(8, "little")
    if lane is not None:
        data[5] = lane
    return bytes(data)


def expected_event(row):
    values = row["event"]
    return Event(tick=values["tick"], source=values["source"], epoch=values["epoch"],
                 sequence=values["sequence"], lane=values["lane"],
                 observation=Observation(values["kind"], values["fields"]))


class RawPageTests(unittest.TestCase):
    def setUp(self):
        self.records = {row["name"]: row for row in FIXTURE["records"]}
        self.page = FIXTURE["pages"][0]
        self.wire = bytes.fromhex(self.page["hex"])
        self.events = tuple(expected_event(self.records[name]) for name in self.page["record_names"])

    def record(self, name):
        return bytes.fromhex(self.records[name]["hex"])

    def encode(self, events=None, **kwargs):
        options = {key: self.page[key] for key in ("session_id", "generation", "config_tag", "page_bytes")}
        options.update(kwargs)
        return encode_page(self.events if events is None else events, **options)

    def changed(self, offset, value, size=1):
        data = bytearray(self.wire)
        data[offset:offset + size] = value.to_bytes(size, "little")
        return reseal_header(data)

    def test_independent_crc_check_and_frozen_page_checksums(self):
        self.assertEqual(bitwise_crc(b"123456789"), 0xCBF43926)
        self.assertEqual(bitwise_crc(b""), 0)
        self.assertEqual(len(self.wire), 256)
        self.assertEqual(bitwise_crc(self.wire[64:196]), 0xD904DA58)
        header = self.wire[:52] + bytes(4) + self.wire[56:64]
        self.assertEqual(bitwise_crc(header), 0x0CFA47A9)
        self.assertEqual(self.wire[48:56], bytes.fromhex("58da04d9a947fa0c"))
        self.assertEqual(self.wire[196:], bytes(60))

    def test_encoder_matches_literal_full_physical_page(self):
        self.assertEqual(self.encode(), self.wire)

    def test_decoder_matches_independent_page_fields(self):
        decoded = decode_page(self.wire, page_bytes=256)
        expected = {key: self.page[key] for key in
                    ("session_id", "generation", "config_tag", "page_bytes", "payload_crc32", "first_tick")}
        expected["events"] = self.events
        self.assertEqual(decoded, expected)

    def test_empty_page_has_zero_payload_count_tick_crc_and_tail(self):
        for size in (256, 1024, 4096):
            with self.subTest(size=size):
                wire = independent_page(page_bytes=size)
                self.assertEqual(encode_page([], session_id=1, generation=2, config_tag=3, page_bytes=size), wire)
                page = decode_page(wire, page_bytes=size, max_events=0)
                self.assertEqual(page["events"], ())
                self.assertEqual(page["first_tick"], 0)
                self.assertEqual(page["payload_crc32"], 0)

    def test_every_truncation_and_trailing_bytes_rejected(self):
        for length in range(256):
            with self.subTest(length=length), self.assertRaises(DecodeError):
                decode_page(self.wire[:length], page_bytes=256)
        for suffix in (b"\0", self.wire):
            with self.assertRaises(DecodeError):
                decode_page(self.wire + suffix, page_bytes=256)

    def test_version_magic_header_flags_and_reserved_rejected_after_valid_crc(self):
        for offset, value in ((0, 0), (4, 0), (4, 2), (5, 1), (6, 63), (7, 1),
                              (56, 1), (57, 1), (58, 1), (59, 1),
                              (60, 1), (61, 1), (62, 1), (63, 1)):
            with self.subTest(offset=offset, value=value), self.assertRaises(DecodeError):
                decode_page(self.changed(offset, value), page_bytes=256)

    def test_header_payload_and_payload_crc_corruption_rejected(self):
        for offset in (8, 48, 52, 64, 195):
            damaged = bytearray(self.wire)
            damaged[offset] ^= 1
            with self.subTest(offset=offset), self.assertRaises(DecodeError):
                decode_page(bytes(damaged), page_bytes=256)
        damaged = self.changed(48, self.page["payload_crc32"] ^ 1, 4)
        with self.assertRaises(DecodeError):
            decode_page(damaged, page_bytes=256)

    def test_nonzero_tail_rejected_despite_valid_crcs(self):
        for offset in (196, 255):
            damaged = bytearray(self.wire)
            damaged[offset] = 1
            with self.subTest(offset=offset), self.assertRaises(DecodeError):
                decode_page(bytes(damaged), page_bytes=256)

    def test_payload_length_record_count_and_minimum_tick_rejected(self):
        cases = ((32, 193, 4), (32, 0xFFFFFFFF, 4), (36, 0, 4),
                 (36, 2, 4), (36, 4, 4), (36, 0xFFFFFFFF, 4),
                 (40, 0, 8), (40, self.page["first_tick"] + 1, 8))
        for offset, value, size in cases:
            with self.subTest(offset=offset, value=value), self.assertRaises(DecodeError):
                decode_page(self.changed(offset, value, size), page_bytes=256)
        empty = bytearray(independent_page())
        empty[40] = 1
        with self.assertRaises(DecodeError):
            decode_page(reseal_header(empty), page_bytes=256)

    def test_partial_and_unknown_records_rejected_with_valid_page_checksums(self):
        for payload in (self.record("retire")[:-1], b"\xff" + self.record("retire")[1:]):
            with self.subTest(payload=payload.hex()), self.assertRaises(DecodeError):
                decode_page(independent_page([payload]), page_bytes=256)

    def test_global_tick_decrease_across_sources_is_allowed(self):
        records = [record_identity(self.record(name), tick=tick, sequence=0)
                   for name, tick in (("retire", 20), ("bus_resp_valid", 5), ("user", 10))]
        page = decode_page(independent_page(records), page_bytes=256)
        self.assertEqual([event.tick for event in page["events"]], [20, 5, 10])
        self.assertEqual(page["first_tick"], 5)
        self.assertEqual(self.encode(page["events"], session_id=1, generation=2, config_tag=3), independent_page(records))

    def test_source_local_tick_epoch_sequence_and_lane_violations_rejected(self):
        first = record_identity(self.record("user"), tick=10, sequence=9, epoch=2)
        cases = [(first, record_identity(self.record("user"), **values)) for values in
                 (dict(tick=9, sequence=10, epoch=2), dict(tick=11, sequence=10, epoch=1),
                  dict(tick=11, sequence=9, epoch=2), dict(tick=11, sequence=8, epoch=2),
                  dict(tick=10, sequence=10, epoch=2), dict(tick=9, sequence=0, epoch=3))]
        cases.append((record_identity(self.record("bus_resp_valid"), tick=10, sequence=0),
                      record_identity(self.record("bus_req"), tick=10, sequence=1, lane=0)))
        for records in cases:
            with self.subTest(records=[record.hex() for record in records]), self.assertRaises(DecodeError):
                decode_page(independent_page(records), page_bytes=256)
        original = replace(self.events[2], tick=10, sequence=9, epoch=2)
        for values in (dict(tick=9, sequence=10), dict(tick=11, epoch=1),
                       dict(tick=11, sequence=9), dict(tick=10, sequence=10)):
            with self.subTest(encoder=values), self.assertRaises(ValueError):
                self.encode([original, replace(original, **values)])

    def test_legal_two_lane_same_tick_and_epoch_reset_preserve_identity(self):
        records = [record_identity(self.record("bus_resp_valid"), tick=10, sequence=0),
                   record_identity(self.record("bus_req"), tick=10, sequence=1),
                   record_identity(self.record("bus_resp_valid"), tick=10, sequence=0, epoch=1)]
        page = decode_page(independent_page(records), page_bytes=256)
        self.assertEqual([(event.epoch, event.sequence, event.lane) for event in page["events"]],
                         [(0, 0, 0), (0, 1, 1), (1, 0, 0)])
        self.assertEqual(self.encode(page["events"], session_id=1, generation=2, config_tag=3), independent_page(records))

    def test_source_sequence_gaps_are_preserved_without_fabrication(self):
        records = [record_identity(self.record("user"), tick=10, sequence=1),
                   record_identity(self.record("user"), tick=11, sequence=100)]
        page = decode_page(independent_page(records), page_bytes=256)
        self.assertEqual([event.sequence for event in page["events"]], [1, 100])
        self.assertEqual(len(page["events"]), 2)

    def test_page_geometry_and_event_bounds_are_strict(self):
        for bound in (-1, True, 1.0, None, "3"):
            with self.subTest(bound=bound), self.assertRaises(DecodeError):
                decode_page(self.wire, page_bytes=256, max_events=bound)
        for bound in (0, 1, 2):
            with self.subTest(bound=bound), self.assertRaises(DecodeError):
                decode_page(self.wire, page_bytes=256, max_events=bound)
        self.assertEqual(len(decode_page(self.wire, page_bytes=256, max_events=3)["events"]), 3)
        for size in (0, 64, 255, 257, 512, True, 256.0, None):
            with self.subTest(size=size):
                with self.assertRaises(DecodeError):
                    decode_page(self.wire, page_bytes=size)
                with self.assertRaises(ValueError):
                    self.encode(page_bytes=size)
        with self.assertRaises(DecodeError):
            decode_page(self.wire)

    def test_bounds_and_checksums_precede_event_allocation(self):
        damaged = bytearray(self.wire)
        damaged[64] ^= 1
        with patch("model.chronos.raw_decode.Event", side_effect=AssertionError("event allocated")):
            for data, limit in ((self.wire, 2), (self.changed(36, 0xFFFFFFFF, 4), 4096),
                                (bytes(damaged), 4096)):
                with self.subTest(limit=limit), self.assertRaises(DecodeError):
                    decode_page(data, page_bytes=256, max_events=limit)

    def test_encoder_identity_widths_and_types(self):
        for key in ("session_id", "generation", "config_tag"):
            for value in (-1, 1 << 64, True, 1.0, None):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    self.encode(**{key: value})

    def test_encoder_exact_fit_and_bounded_iterable_overflow(self):
        event = expected_event(self.records["bus_req"])
        events = [replace(event, tick=index, sequence=index, lane=0) for index in range(4)]
        records = [record_identity(self.record("bus_req"), tick=index, sequence=index,
                                   epoch=event.epoch, lane=0) for index in range(4)]
        expected = independent_page(records)
        self.assertEqual(len(expected), 256)
        self.assertEqual(self.encode(events, session_id=1, generation=2, config_tag=3), expected)
        consumed = []

        def stream():
            for index in range(100):
                consumed.append(index)
                if index > 4:
                    raise AssertionError("encoder consumed past first overflow record")
                yield replace(event, tick=index, sequence=index, lane=0)

        with self.assertRaises(ValueError):
            self.encode(stream())
        self.assertEqual(consumed, [0, 1, 2, 3, 4])

    def test_decoder_requires_bytes(self):
        for value in (None, "", 256, [], bytearray(self.wire), memoryview(self.wire)):
            with self.subTest(type=type(value).__name__), self.assertRaises(DecodeError):
                decode_page(value, page_bytes=256)

    def test_bounded_seeded_single_bit_corruption(self):
        rng = random.Random(0x50414745)
        for case in range(512):
            data = bytearray(self.wire)
            data[rng.randrange(256)] ^= 1 << rng.randrange(8)
            with self.subTest(case=case), self.assertRaises(DecodeError):
                decode_page(bytes(data), page_bytes=256)

    def test_ten_thousand_seeded_valid_raw_streams(self):
        rng = random.Random(0x5241575F5631)
        sources = {"RETIRE": 0, "TRAP": 2, "IRQ_ACCEPT": 2, "IRQ_PENDING": 2,
                   "BUS_REQ": 1, "BUS_RESP": 1, "USER_EVENT": 3}
        kinds = tuple(sources)
        total_events = 0

        def u32():
            return rng.getrandbits(32)

        def optional_u32():
            return None if rng.randrange(3) == 0 else u32()

        for stream in range(10000):
            page_bytes = (256, 1024, 4096)[stream % 3]
            count = rng.randrange(min(24, (page_bytes - 64) // 52) + 1)
            ticks = [rng.randrange((1 << 64) - 1024) for _ in range(4)]
            epochs = [rng.randrange((1 << 64) - 1024) for _ in range(4)]
            sequences = [rng.randrange((1 << 64) - 1024) for _ in range(4)]
            events = []
            for _ in range(count):
                kind = rng.choice(kinds)
                source = sources[kind]
                if rng.randrange(9) == 0:
                    epochs[source] += rng.randrange(1, 4)
                    sequences[source] = rng.randrange((1 << 64) - 1024)
                sequences[source] += rng.randrange(1, 9)
                ticks[source] += rng.randrange(1, 10)
                lane = rng.randrange(2) if kind in ("BUS_REQ", "IRQ_PENDING") else 0
                if kind == "RETIRE":
                    fields = dict(pc=u32(), next_pc=u32(), length=4, boundary=rng.getrandbits(64))
                elif kind in ("TRAP", "IRQ_ACCEPT"):
                    fields = dict(pc=optional_u32(), cause=u32(), target=optional_u32(),
                                  boundary=rng.getrandbits(64))
                elif kind == "IRQ_PENDING":
                    fields = dict(previous=u32(), current=u32())
                elif kind == "BUS_REQ":
                    fields = dict(transaction=u32(), address=u32(), data=u32(),
                                  write=bool(rng.getrandbits(1)), mask=rng.getrandbits(4))
                elif kind == "BUS_RESP":
                    fields = dict(transaction=u32(), data=optional_u32(), error=bool(rng.getrandbits(1)))
                else:
                    fields = dict(value=u32())
                events.append(Event(tick=ticks[source], source=source, epoch=epochs[source],
                                    sequence=sequences[source], lane=lane,
                                    observation=Observation(kind, fields)))
            session_id, generation, config_tag = (rng.getrandbits(64) for _ in range(3))
            with self.subTest(stream=stream, page_bytes=page_bytes, event_count=count):
                wire = encode_page(events, session_id=session_id, generation=generation,
                                   config_tag=config_tag, page_bytes=page_bytes)
                decoded = decode_page(wire, page_bytes=page_bytes, max_events=count)
                self.assertEqual(decoded["events"], tuple(events))
                self.assertEqual(decoded["session_id"], session_id)
                self.assertEqual(decoded["generation"], generation)
                self.assertEqual(decoded["config_tag"], config_tag)
                self.assertEqual(decoded["first_tick"], min((event.tick for event in events), default=0))
                self.assertEqual(len(wire), page_bytes)
            total_events += count
        self.assertEqual(total_events, 75391)


if __name__ == "__main__":
    unittest.main()

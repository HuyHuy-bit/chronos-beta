from dataclasses import replace
import json
from pathlib import Path
import random
import unittest
from unittest.mock import patch

from model.chronos.compact_decode import decode_page, decode_records
from model.chronos.compact_encode import encode_page, encode_records
from model.chronos.events import Event, Observation
from model.chronos.raw_decode import DecodeError
from model.chronos.raw_encode import encode_record as raw_record


FIXTURE = json.loads((Path(__file__).parent / "fixtures/compact_v1.json").read_text())


def semantic(values):
    return Event(tick=values["tick"], source=values["source"], epoch=values["epoch"],
                 sequence=values["sequence"], lane=values["lane"],
                 observation=Observation(values["kind"], values["fields"]))


def retire(pc=0x1000, *, next_pc=None, tick=0, sequence=0, epoch=0, boundary=0):
    return Event(tick, 0, epoch, sequence, 0, Observation("RETIRE", dict(
        pc=pc, next_pc=pc + 4 if next_pc is None else next_pc, length=4, boundary=boundary)))


def bus(address=0x1000, *, tick=0, sequence=0, epoch=0, lane=0, write=False, data=1, transaction=2, mask=15):
    return Event(tick, 1, epoch, sequence, lane, Observation("BUS_REQ", dict(
        address=address, transaction=transaction, data=data, write=write, mask=mask)))


def crc(data):
    value = 0xFFFFFFFF
    for byte in data:
        value ^= byte
        for _ in range(8):
            value = (value >> 1) ^ (0xEDB88320 if value & 1 else 0)
    return value ^ 0xFFFFFFFF


def reseal(page, *, payload=False):
    page = bytearray(page)
    if payload:
        used = int.from_bytes(page[32:36], "little")
        page[48:52] = crc(page[64:64 + used]).to_bytes(4, "little")
    page[52:56] = bytes(4)
    page[52:56] = crc(page[:64]).to_bytes(4, "little")
    return bytes(page)


def changed(data, offset, value, size=1):
    result = bytearray(data)
    result[offset:offset + size] = value.to_bytes(size, "little")
    return bytes(result)


class CompactTests(unittest.TestCase):
    def setUp(self):
        self.blocks = {block["name"]: block for block in FIXTURE["blocks"]}
        self.page = FIXTURE["pages"][0]

    def check_stream(self, events, expected_types=None):
        encoded = encode_records(events)
        if expected_types is not None:
            self.assertEqual([record[0] for record in encoded], expected_types)
        self.assertEqual(decode_records(b"".join(encoded)), tuple(events))
        self.assertLessEqual(sum(map(len, encoded)), sum(len(raw_record(event)) for event in events))
        return encoded

    def test_literal_blocks_encode_and_decode_independently(self):
        for name, block in self.blocks.items():
            with self.subTest(name=name):
                expected = tuple(semantic(value) for value in block["events"])
                records = tuple(bytes.fromhex(record) for record in block["records"])
                self.assertEqual(encode_records(expected), records)
                self.assertEqual(decode_records(b"".join(records)), expected)

    def test_literal_page_and_independent_bitwise_checksums(self):
        wire = bytes.fromhex(self.page["hex"])
        events = tuple(semantic(value) for value in self.page["events"])
        self.assertEqual(crc(b"123456789"), 0xCBF43926)
        self.assertEqual(crc(wire[64:200]), 0x990DF344)
        self.assertEqual(crc(wire[:52] + bytes(4) + wire[56:64]), 0x746EAC9F)
        self.assertEqual(wire[200:], bytes(56))
        options = {key: self.page[key] for key in ("session_id", "generation", "config_tag", "page_bytes")}
        self.assertEqual(encode_page(events, **options), wire)
        expected = dict(options, first_tick=self.page["first_tick"], payload_crc32=0x990DF344, events=events)
        self.assertEqual(decode_page(wire, page_bytes=256), expected)

    def test_final_branch_never_becomes_implicit_sequential_target(self):
        for count in (2, 4):
            events = [retire(0x1000 + 4 * index, tick=index, sequence=index) for index in range(count)]
            events[-1] = retire(0x1000 + 4 * (count - 1), next_pc=0x87654320, tick=count - 1, sequence=count - 1)
            with self.subTest(count=count):
                records = self.check_stream(events, [1, 0x11] if count == 2 else [0x10, 0x11])
                self.assertEqual(decode_records(b"".join(records))[-1].observation.fields["next_pc"], 0x87654320)

    def test_irregular_retirement_ticks_remain_exact(self):
        events = [retire(0x1000 + index * 4, tick=tick, sequence=index)
                  for index, tick in enumerate((10, 11, 19, 20, 21))]
        self.check_stream(events, [0x10, 0x10])

    def test_greedy_count_and_age_limits(self):
        for count, stride, types in ((255, 1, [0x10]), (256, 1, [0x10, 0x11]),
                                     (257, 1, [0x10, 0x10]), (10, 64, [0x10, 0x10]),
                                     (3, 256, [0x10, 0x11]), (3, 257, [1, 0x11, 0x11])):
            events = [retire(0x1000 + index * 4, tick=index * stride, sequence=index) for index in range(count)]
            with self.subTest(count=count, stride=stride):
                records = self.check_stream(events, types)
                for record in records:
                    if record[0] == 0x10:
                        run_count = int.from_bytes(record[36:38], "little")
                        run_stride = int.from_bytes(record[38:40], "little")
                        self.assertLessEqual(run_count, 255)
                        self.assertLessEqual((run_count - 1) * run_stride, 256)

    def test_epoch_sequence_and_boundary_changes_require_absolute_fallback(self):
        first = retire(0x1000, next_pc=0x8000)
        for change in (dict(epoch=1), dict(sequence=3), dict(boundary=1)):
            second = retire(0x1004, tick=1, sequence=1, **{key: value for key, value in change.items() if key != "sequence"})
            if "sequence" in change:
                second = replace(second, sequence=change["sequence"])
            with self.subTest(change=change):
                self.check_stream([first, second], [1, 1])

    def test_missing_trap_record_cannot_cross_execution_boundary(self):
        events = [retire(0x1000, tick=0, sequence=0, boundary=5),
                  retire(0x1004, tick=1, sequence=1, boundary=5),
                  retire(0x1008, tick=2, sequence=2, boundary=6)]
        self.check_stream(events, [0x10, 1])

    def test_trap_invalidates_retirement_base_and_other_sources_end_run(self):
        first = retire(0x1000, next_pc=0x9000, tick=100)
        second = retire(0x1004, tick=101, sequence=1)
        for kind in ("TRAP", "IRQ_ACCEPT"):
            trap = Event(4, 2, 0, 0, 0, Observation(kind, dict(pc=None, target=None, cause=7, boundary=0)))
            with self.subTest(kind=kind):
                self.check_stream([first, trap, second], [1, 2 if kind == "TRAP" else 3, 1])
        user = Event(4, 3, 0, 0, 0, Observation("USER_EVENT", dict(value=99)))
        self.check_stream([first, user, second], [1, 7, 0x11])
        sequential = [retire(tick=0), user, retire(0x1004, tick=1, sequence=1)]
        self.check_stream(sequential, [1, 7, 0x11])

    def test_last_run_member_updates_delta_context(self):
        events = [retire(0x1000, tick=10), retire(0x1004, tick=12, sequence=1),
                  retire(0x1008, next_pc=0x7000, tick=19, sequence=2)]
        records = self.check_stream(events, [0x10, 0x11])
        self.assertEqual(records[-1][8:12], bytes.fromhex("07000400"))

    def test_signed_retirement_delta_and_unsigned_time_boundaries(self):
        first = retire(0x10000, next_pc=0x90000)
        for delta, delta_type in ((-32769, 1), (-32768, 0x11), (32767, 0x11), (32768, 1)):
            second = retire(0x10000 + delta, next_pc=0, tick=1, sequence=1)
            with self.subTest(delta=delta):
                self.check_stream([first, second], [1, delta_type])
        for dt, delta_type in ((65535, 0x11), (65536, 1)):
            with self.subTest(dt=dt):
                self.check_stream([first, retire(0x10004, next_pc=0, tick=dt, sequence=1)], [1, delta_type])

    def test_bus_delta_boundaries_direction_and_response_invalidation(self):
        first = bus(0x10000)
        for delta, kind in ((-32769, 5), (-32768, 0x12), (32767, 0x12), (32768, 5)):
            with self.subTest(delta=delta):
                self.check_stream([first, bus(0x10000 + delta, tick=1, sequence=1)], [5, kind])
        for kwargs, kind in ((dict(tick=65535), 0x12), (dict(tick=65536), 5),
                             (dict(tick=1, write=True), 5), (dict(tick=1, epoch=1), 5),
                             (dict(tick=1, sequence=3), 5)):
            values = dict(tick=1, sequence=1)
            values.update(kwargs)
            with self.subTest(kwargs=kwargs):
                self.check_stream([first, bus(0x10004, **values)], [5, kind])
        response = Event(1, 1, 0, 1, 0, Observation("BUS_RESP", dict(transaction=99, data=None, error=True)))
        self.check_stream([first, response, bus(0x10004, tick=2, sequence=2)], [5, 6, 5])

    def test_bus_context_survives_unrelated_sources_and_same_tick_lane_increases(self):
        user = Event(0, 3, 0, 0, 0, Observation("USER_EVENT", dict(value=1)))
        self.check_stream([bus(tick=10), user, bus(0x1004, tick=10, sequence=1, lane=1)], [5, 7, 0x12])

    def test_rv32_edge_branch_does_not_wrap_implicit_run(self):
        events = [retire(0xFFFFFFF0 + index * 4, tick=index, sequence=index) for index in range(3)]
        events.append(retire(0xFFFFFFFC, next_pc=0, tick=3, sequence=3))
        self.check_stream(events, [0x10, 0x11])

    def test_context_starts_empty_at_every_block_and_page(self):
        for name in ("retire_negative_delta", "bus_negative_delta"):
            base, delta = (bytes.fromhex(record) for record in self.blocks[name]["records"])
            decode_records(base)
            with self.subTest(name=name), self.assertRaises(DecodeError):
                decode_records(delta)
        run = bytes.fromhex(self.blocks["absolute_run"]["records"][0])
        self.assertEqual(len(decode_records(run)), 3)
        first = encode_page([retire()], session_id=1, generation=0, config_tag=1, page_bytes=256)
        second = encode_page([retire(0x1004, tick=1, sequence=1)], session_id=1, generation=1, config_tag=1, page_bytes=256)
        self.assertEqual(first[64], 1)
        self.assertEqual(second[64], 1)
        self.assertEqual(decode_page(second, page_bytes=256)["events"], (retire(0x1004, tick=1, sequence=1),))

    def test_malformed_run_counts_stride_age_and_arithmetic_rejected(self):
        run = bytes.fromhex(self.blocks["absolute_run"]["records"][0])
        mutations = [(36, value, 2) for value in (0, 1, 256, 65535)]
        mutations += [(38, value, 2) for value in (0, 129, 257, 65535)]
        mutations += [(16, (1 << 64) - 1, 8),
                      (24, (1 << 64) - 1, 8), (32, 0xFFFFFFFC, 4)]
        for offset, value, size in mutations:
            with self.subTest(offset=offset, value=value), self.assertRaises(DecodeError):
                decode_records(changed(run, offset, value, size))

    def test_compact_headers_flags_lengths_sources_lanes_and_reserved_rejected(self):
        for name, block in self.blocks.items():
            records = [bytes.fromhex(record) for record in block["records"]]
            prefix = b"".join(records[:-1])
            record = records[-1]
            for offset, value in ((0, 0x13), (1, 1), (2, 0), (3, 1), (4, 3), (5, 2), (6, 1), (7, 1)):
                with self.subTest(name=name, offset=offset), self.assertRaises(DecodeError):
                    decode_records(prefix + changed(record, offset, value))
        run = bytes.fromhex(self.blocks["absolute_run"]["records"][0])
        with self.assertRaises(DecodeError):
            decode_records(changed(run, 5, 1))

    def test_delta_payload_arithmetic_and_reserved_bits_rejected(self):
        retired = self.blocks["retire_negative_delta"]["records"]
        bus_records = self.blocks["bus_negative_delta"]["records"]
        rbase, rd = (bytes.fromhex(value) for value in retired)
        bbase, bd = (bytes.fromhex(value) for value in bus_records)
        with self.assertRaises(DecodeError):
            decode_records(rbase + changed(rd, 8, 0, 2))
        for offset, value in ((20, 2), (21, 16), (22, 1), (23, 1), (20, 0)):
            with self.subTest(offset=offset, value=value), self.assertRaises(DecodeError):
                decode_records(bbase + changed(bd, offset, value))
        for base, delta, address_offset in ((rbase, rd, 32), (bbase, bd, 36)):
            for changed_base in (changed(base, address_offset, 0, 4),
                                 changed(base, 16, (1 << 64) - 1, 8)):
                with self.assertRaises(DecodeError):
                    decode_records(changed_base + delta)
            with self.assertRaises(DecodeError):
                decode_records(changed(base, address_offset, 0xFFFFFFFF, 4) + changed(delta, 10, 1, 2))
        with self.assertRaises(DecodeError):
            decode_records(changed(rbase, 24, (1 << 64) - 1, 8) + rd)
        with self.assertRaises(DecodeError):
            decode_records(changed(bbase, 24, (1 << 64) - 1, 8) + changed(bd, 8, 1, 2))

    def test_every_record_truncation_and_unknown_trailing_byte_rejected(self):
        for name, block in self.blocks.items():
            records = [bytes.fromhex(record) for record in block["records"]]
            prefix = b"".join(records[:-1])
            last = records[-1]
            for length in range(1, len(last)):
                with self.subTest(name=name, length=length), self.assertRaises(DecodeError):
                    decode_records(prefix + last[:length])
            with self.assertRaises(DecodeError):
                decode_records(prefix + last + b"\xff")

    def test_expansion_limit_precedes_run_event_allocation(self):
        run = bytes.fromhex(self.blocks["absolute_run"]["records"][0])
        with patch("model.chronos.compact_decode.Event", side_effect=AssertionError("allocated event")):
            with self.assertRaises(DecodeError):
                decode_records(run, max_events=2)
            for invalid in (changed(run, 36, 65535, 2), changed(run, 16, (1 << 64) - 1, 8),
                            changed(run, 24, (1 << 64) - 1, 8), changed(run, 32, 0xFFFFFFFC, 4)):
                with self.assertRaises(DecodeError):
                    decode_records(invalid)
            with self.assertRaises(DecodeError):
                decode_page(bytes.fromhex(self.page["hex"]), page_bytes=256, max_events=5)
        self.assertEqual(len(decode_records(run, max_events=3)), 3)

    def test_page_fields_counts_anchor_crc_tail_and_versions_rejected(self):
        wire = bytes.fromhex(self.page["hex"])
        for offset, value, size in ((0, 0, 1), (4, 1, 1), (5, 1, 1), (6, 63, 2),
                                    (32, 193, 4), (36, 0, 4), (36, 3, 4), (36, 9, 4),
                                    (40, 0, 8), (56, 5, 4), (56, 7, 4), (60, 1, 4)):
            with self.subTest(offset=offset, value=value), self.assertRaises(DecodeError):
                decode_page(reseal(changed(wire, offset, value, size)), page_bytes=256)
        for offset in (8, 48, 52, 64, 199, 200, 255):
            with self.subTest(corrupt=offset), self.assertRaises(DecodeError):
                decode_page(changed(wire, offset, wire[offset] ^ 1), page_bytes=256)
        with self.assertRaises(DecodeError):
            decode_page(reseal(changed(wire, 64, 0x13), payload=True), page_bytes=256)
        for length in range(256):
            with self.subTest(length=length), self.assertRaises(DecodeError):
                decode_page(wire[:length], page_bytes=256)
        with self.assertRaises(DecodeError):
            decode_page(wire + b"\0", page_bytes=256)

    def test_empty_blocks_pages_input_types_and_bounded_iterables(self):
        self.assertEqual(encode_records([], max_events=0), ())
        self.assertEqual(decode_records(b"", max_events=0), ())
        for size in (256, 1024, 4096):
            wire = encode_page([], session_id=1, generation=0, config_tag=1, page_bytes=size, max_events=0)
            self.assertEqual(decode_page(wire, page_bytes=size, max_events=0)["events"], ())
            self.assertEqual(wire[56:60], bytes(4))
        for bound in (-1, 100001, True, 1.0, None):
            with self.subTest(bound=bound):
                with self.assertRaises(ValueError):
                    encode_records([], max_events=bound)
                with self.assertRaises(DecodeError):
                    decode_records(b"", max_events=bound)
        for value in (None, "", [], bytearray(), memoryview(b"")):
            with self.subTest(type=type(value).__name__), self.assertRaises(DecodeError):
                decode_records(value)
        with self.assertRaises(DecodeError):
            decode_records(bytes(4 * 1024 * 1024 + 1))
        consumed = []

        def stream():
            for index in range(100):
                consumed.append(index)
                if index > 3:
                    raise AssertionError("read past configured bound")
                yield retire(0x1000 + index * 4, tick=index, sequence=index)

        with self.assertRaises(ValueError):
            encode_records(stream(), max_events=3)
        self.assertEqual(consumed, [0, 1, 2, 3])

    def test_encoder_validates_source_order_before_compression(self):
        for events in ([retire(), retire(0x1004)],
                       [retire(tick=2), retire(0x1004, tick=1, sequence=1)],
                       [bus(), bus(0x1004, sequence=1)],
                       [replace(retire(), tick=True)]):
            with self.subTest(events=events), self.assertRaises(ValueError):
                encode_records(events)

    def test_ten_thousand_pc_run_streams(self):
        rng = random.Random(0x50435F52554E)
        total_events = 0
        for stream in range(10000):
            count = rng.choice((255, 256, 257, 300)) if stream % 50 == 0 else rng.randrange(33)
            pc = rng.randrange((1 << 32) - 4 * (count + 1))
            tick = rng.randrange((1 << 64) - 1000000)
            sequence = rng.randrange((1 << 64) - 10000)
            epoch = rng.randrange((1 << 64) - 10000)
            boundary = rng.randrange((1 << 64) - 10000)
            stride = rng.choice((1, 2, 4, 7, 64, 256))
            events = []
            clean = stream % 3 == 0
            for index in range(count):
                if index:
                    pc += 4
                    tick += stride if clean or rng.randrange(7) else rng.randrange(1, 1000)
                    sequence += 1 if clean or rng.randrange(17) else rng.randrange(2, 5)
                    if not clean and rng.randrange(23) == 0:
                        boundary += 1
                    if not clean and rng.randrange(29) == 0:
                        epoch += 1
                next_pc = rng.getrandbits(32) if not clean and rng.randrange(11) == 0 else pc + 4
                events.append(retire(pc, next_pc=next_pc, tick=tick, sequence=sequence,
                                     epoch=epoch, boundary=boundary))
            with self.subTest(stream=stream):
                self.check_stream(events)
            total_events += count
        self.assertEqual(total_events, 212225)

    def test_ten_thousand_delta_family_streams(self):
        rng = random.Random(0x44454C54415F)
        total_events = 0
        for stream in range(10000):
            count = rng.randrange(33)
            clocks = [rng.randrange((1 << 64) - 10000000) for _ in range(4)]
            sequences = [rng.randrange((1 << 64) - 10000) for _ in range(4)]
            epochs = [rng.randrange((1 << 64) - 10000) for _ in range(4)]
            pc, address, boundary = rng.getrandbits(32), rng.getrandbits(32), rng.randrange((1 << 64) - 10000)
            direction = bool(rng.randrange(2))
            events = []
            for _ in range(count):
                kind = rng.choices(("RETIRE", "BUS_REQ", "USER_EVENT", "BUS_RESP", "TRAP"), weights=(4, 4, 1, 1, 1))[0]
                source = {"RETIRE": 0, "BUS_REQ": 1, "USER_EVENT": 3, "BUS_RESP": 1, "TRAP": 2}[kind]
                clocks[source] += rng.choice((1, 2, 17, 65535, 65536))
                sequences[source] += 1 if rng.randrange(9) else rng.randrange(2, 5)
                if rng.randrange(19) == 0:
                    epochs[source] += 1
                if kind == "RETIRE":
                    pc = (pc + rng.choice((-32769, -32768, -4, 4, 32767, 32768))) % (1 << 32)
                    if rng.randrange(13) == 0:
                        boundary += 1
                    event = retire(pc, next_pc=rng.getrandbits(32), tick=clocks[0], sequence=sequences[0],
                                   epoch=epochs[0], boundary=boundary)
                elif kind == "BUS_REQ":
                    address = (address + rng.choice((-32769, -32768, -4, 4, 32767, 32768))) % (1 << 32)
                    if rng.randrange(11) == 0:
                        direction = not direction
                    event = bus(address, tick=clocks[1], sequence=sequences[1], epoch=epochs[1],
                                lane=rng.randrange(2), write=direction, data=rng.getrandbits(32),
                                transaction=rng.getrandbits(32), mask=rng.getrandbits(4))
                else:
                    if kind == "USER_EVENT":
                        fields = dict(value=rng.getrandbits(32))
                    elif kind == "BUS_RESP":
                        fields = dict(transaction=rng.getrandbits(32), data=None if rng.randrange(2) else rng.getrandbits(32),
                                      error=bool(rng.randrange(2)))
                    else:
                        fields = dict(pc=None, cause=rng.getrandbits(32), target=None, boundary=boundary)
                    event = Event(clocks[source], source, epochs[source], sequences[source], 0, Observation(kind, fields))
                events.append(event)
            with self.subTest(stream=stream):
                self.check_stream(events)
            total_events += count
        self.assertEqual(total_events, 159953)


if __name__ == "__main__":
    unittest.main()

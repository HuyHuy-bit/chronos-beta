import random
import unittest

from model.chronos.compact_decode import decode_records
from model.chronos.compact_encode import WATERMARK_END, RunStream, encode_records
from model.chronos.events import Event, Observation
from model.chronos.raw_encode import encode_record as raw_record
from model.chronos.snapshot import SnapshotCapture
from scripts.config import ROOT, read_json


def retire(pc, tick, sequence, *, next_pc=None, epoch=0, boundary=0):
    return Event(tick, 0, epoch, sequence, 0, Observation("RETIRE", dict(
        pc=pc, next_pc=pc + 4 if next_pc is None else next_pc, length=4, boundary=boundary)))


def request(tick, sequence, address=0x2000):
    return Event(tick, 1, 0, sequence, 0, Observation("BUS_REQ", dict(
        transaction=sequence, address=address, write=False, data=0, mask=15)))


def straight(count, *, stride=1, start=0):
    return [retire(0x1000 + 4 * index, start + stride * index, index) for index in range(count)]


def push_all(stream, events):
    return [record for event in events for record in stream.push(event)]


class RunStreamTests(unittest.TestCase):
    def test_watchdog_emits_run_when_next_member_is_impossible(self):
        stream = RunStream()
        events = straight(4)
        self.assertEqual(push_all(stream, events), [])
        self.assertEqual(stream.pending, 4)
        self.assertEqual(stream.advance(3), ())
        self.assertEqual(stream.advance(4), encode_records(events))
        self.assertEqual(stream.pending, 0)
        self.assertEqual(stream.advance(WATERMARK_END), ())

    def test_single_retirement_waits_for_the_age_horizon(self):
        stream = RunStream()
        event = retire(0x1000, 10, 0)
        self.assertEqual(stream.push(event), ())
        self.assertEqual(stream.advance(265), ())
        self.assertEqual(stream.advance(266), (raw_record(event),))

    def test_count_and_age_limits_emit_on_the_completing_push(self):
        for events in (straight(255), straight(3, stride=128), straight(2, stride=256)):
            with self.subTest(count=len(events), stride=events[1].tick - events[0].tick):
                stream = RunStream()
                self.assertEqual(push_all(stream, events), list(encode_records(events)))
                self.assertEqual(stream.pending, 0)
        stream = RunStream()
        self.assertEqual(push_all(stream, straight(256)), list(encode_records(straight(255))))
        self.assertEqual(stream.pending, 1)

    def test_watermark_is_monotonic_and_rejects_late_source0_input(self):
        stream = RunStream()
        stream.push(retire(0x1000, 1, 0))
        self.assertEqual(stream.advance(5), ())
        self.assertEqual(stream.advance(2), ())
        with self.assertRaises(ValueError):
            stream.push(retire(0x1004, 5, 1))
        self.assertEqual(stream.pending, 1)
        self.assertEqual(stream.push(request(3, 0)), (raw_record(retire(0x1000, 1, 0)), raw_record(request(3, 0))))
        for value in (-2, WATERMARK_END + 1, True, 1.0, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                stream.advance(value)

    def test_misordered_input_does_not_mutate(self):
        stream = RunStream()
        stream.push(retire(0x1000, 5, 3))
        for event in (retire(0x1004, 4, 4), retire(0x1004, 6, 3), retire(0x1004, 5, 4)):
            with self.subTest(event=event), self.assertRaises(ValueError):
                stream.push(event)
            self.assertEqual(stream.pending, 1)
        self.assertEqual(stream.push(retire(0x1004, 6, 4)), ())
        self.assertEqual(stream.pending, 2)

    def test_non_retire_input_ends_run_and_keeps_collector_order(self):
        stream = RunStream()
        events = straight(3) + [request(2, 0)]
        self.assertEqual(push_all(stream, events), list(encode_records(events)))

    def test_epoch_change_ends_run_without_a_reset_command(self):
        events = straight(3) + [retire(0x100C, 3, 0, epoch=1), retire(0x1010, 4, 1, epoch=1)]
        stream = RunStream()
        records = push_all(stream, events) + list(stream.advance(WATERMARK_END))
        self.assertEqual(records, list(encode_records(events)))
        self.assertEqual([record[0] for record in records], [0x10, 0x10])

    def test_flush_clears_context_so_each_segment_decodes_alone(self):
        stream = RunStream()
        branch = retire(0x1000, 0, 0, next_pc=0x3000)
        held = retire(0x3000, 1, 1)
        after = retire(0x3004, 2, 2, next_pc=0x5000)
        first = list(stream.push(branch)) + list(stream.push(held))
        first += stream.flush()
        second = list(stream.push(after))
        self.assertEqual([record[0] for record in first], [1, 0x11])
        self.assertEqual(second, [raw_record(after)])
        self.assertEqual(decode_records(b"".join(first)), (branch, held))
        self.assertEqual(decode_records(b"".join(second)), (after,))
        self.assertEqual(stream.flush(), ())

    def stream_events(self, rng):
        clocks, sequences, epochs = [0] * 4, [0] * 4, [0] * 4
        pc, boundary, address = 0x1000, 0, 0x8000
        events = []
        for _ in range(rng.randrange(81)):
            kind = rng.choices(("RETIRE", "BUS_REQ", "BUS_RESP", "USER_EVENT", "TRAP"), weights=(12, 3, 1, 1, 1))[0]
            source = {"RETIRE": 0, "BUS_REQ": 1, "BUS_RESP": 1, "TRAP": 2, "USER_EVENT": 3}[kind]
            clocks[source] += rng.choice((1, 1, 1, 1, 2, 3, 64, 255, 256, 257))
            sequences[source] += 1 if rng.randrange(20) else rng.randrange(2, 4)
            if rng.randrange(40) == 0:
                epochs[source] += 1
            if kind == "RETIRE":
                pc = pc + 4 if rng.randrange(12) else rng.randrange(0x1000, 0x100000, 4)
                if rng.randrange(30) == 0:
                    boundary += 1
                next_pc = pc + 4 if rng.randrange(10) else rng.randrange(0, 0x100000, 4)
                fields = dict(pc=pc, next_pc=next_pc, length=4, boundary=boundary)
            elif kind == "BUS_REQ":
                address = (address + rng.choice((4, 8, -4, 70000))) % (1 << 32)
                fields = dict(transaction=rng.getrandbits(32), address=address, write=bool(rng.randrange(2)),
                              data=rng.getrandbits(32), mask=15)
            elif kind == "BUS_RESP":
                fields = dict(transaction=rng.getrandbits(32), data=None, error=False)
            elif kind == "TRAP":
                fields = dict(pc=None, cause=2, target=None, boundary=boundary)
            else:
                fields = dict(value=rng.getrandbits(32))
            events.append(Event(clocks[source], source, epochs[source], sequences[source], 0,
                                Observation(kind, fields)))
        return events

    def test_two_thousand_random_watermark_schedules_equal_block_encoding(self):
        rng = random.Random(0x5354_5245_414D)
        total = 0
        for index in range(2000):
            events = self.stream_events(rng)
            limits, following = [], WATERMARK_END
            for event in reversed(events):
                limits.append(following)
                if event.source == 0:
                    following = event.tick - 1
            limits.reverse()
            stream = RunStream()
            records = list(stream.advance(-1))
            for event, limit in zip(events, limits):
                records += stream.push(event)
                for _ in range(rng.randrange(3)):
                    records += stream.advance(rng.choice((limit, rng.randint(-1, min(limit, 1 << 40)))))
            records += stream.advance(WATERMARK_END)
            with self.subTest(stream=index):
                self.assertEqual(stream.pending, 0)
                self.assertEqual(records, list(encode_records(events)))
            total += len(events)
        self.assertEqual(total, 79563)

    def test_two_thousand_random_flush_schedules_decode_segment_exactly(self):
        rng = random.Random(0x464C_5553_48)
        for index in range(2000):
            events = self.stream_events(rng)
            stream = RunStream()
            segments, current, inputs, current_inputs = [], [], [], []
            for event in events:
                current += stream.push(event)
                current_inputs.append(event)
                if rng.randrange(6) == 0:
                    current += stream.flush()
                    segments.append(current)
                    inputs.append(current_inputs)
                    current, current_inputs = [], []
            segments.append(current + list(stream.flush()))
            inputs.append(current_inputs)
            with self.subTest(stream=index):
                for records, expected in zip(segments, inputs):
                    self.assertEqual(decode_records(b"".join(records)), tuple(expected))
                    self.assertLessEqual(sum(map(len, records)), sum(len(raw_record(event)) for event in expected))

    def test_model_watermark_bounds_pending_runs_and_drains_to_block_equivalence(self):
        capture = SnapshotCapture(read_json(ROOT / "configs/baseline.json"))
        stream, serviced, records = RunStream(), [], []
        pc = 0x1000

        def service(grants):
            for _ in range(grants):
                event = capture.service()
                if event is not None:
                    serviced.append(event)
                    records.extend(stream.push(event))
            records.extend(stream.advance(capture.model.watermark(0)))

        for tick in range(900):
            items = []
            if not 300 <= tick < 700:
                target = pc + 4 if tick % 50 else 0x1000
                items.append(Observation("RETIRE", dict(pc=pc, next_pc=target, length=4, boundary=tick // 200)))
                pc = target
            if tick % 7 == 0:
                items.append(Observation("BUS_REQ", dict(transaction=tick, address=0x8000 + 4 * tick,
                                                         write=True, data=tick, mask=15)))
            capture.step(tick, items)
            service(32)
            if tick == 300 + 256:
                self.assertEqual(stream.pending, 0)
            self.assertLessEqual(stream.pending, 50)
        capture.step(900, stop=True)
        while not capture.model.complete:
            service(1)
        service(0)
        self.assertEqual(stream.pending, 0)
        self.assertEqual(capture.model.watermark(0), WATERMARK_END)
        self.assertEqual(records, list(encode_records(serviced)))
        self.assertEqual(decode_records(b"".join(records)), tuple(serviced))
        self.assertGreater(sum(record[0] == 0x10 for record in records), 10)


if __name__ == "__main__":
    unittest.main()

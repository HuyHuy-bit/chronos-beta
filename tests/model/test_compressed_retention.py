import copy
import random
import unittest

from model.chronos import compact_decode, raw_decode
from model.chronos.capacity import (completion_budget, default_inventory, measured_inventory, metadata_bytes,
                                    service_envelope)
from model.chronos.capture_session import decode_capture, encode_capture
from model.chronos.compact_encode import MIN_RECORD_BYTES, WATERMARK_END, encode_records
from model.chronos.events import Event, Observation
from model.chronos.predicates import matcher
from model.chronos.raw_decode import DecodeError
from model.chronos.raw_encode import MAX_RECORD_BYTES, encode_record
from model.chronos.retention import PageRing
from model.chronos.snapshot import SnapshotCapture
from scripts.config import ROOT, read_json

TAIL = {"raw-v1": 51, "compact-v1": 87}


def observations(tick):
    return (Observation("RETIRE", dict(pc=4096 + tick * 4, next_pc=4100 + tick * 4, length=4, boundary=tick)),
            Observation("BUS_RESP", dict(transaction=tick, data=tick, error=False)),
            Observation("BUS_REQ", dict(transaction=tick + 1, address=8192, write=True, data=tick, mask=15)),
            Observation("TRAP", dict(pc=None, cause=11, target=256, boundary=tick)),
            Observation("IRQ_PENDING", dict(previous=tick & 1, current=(tick + 1) & 1)),
            Observation("USER_EVENT", dict(value=tick)))
DECODERS = {"raw-v1": raw_decode.decode_page, "compact-v1": compact_decode.decode_page}


def retire(pc, tick, sequence, *, next_pc=None):
    return Event(tick, 0, 0, sequence, 0, Observation("RETIRE", dict(
        pc=pc, next_pc=pc + 4 if next_pc is None else next_pc, length=4, boundary=0)))


def mixed_events(rng, count):
    clocks, sequences = [0] * 4, [0] * 4
    pc, events = 0x1000, []
    for _ in range(count):
        kind = rng.choices(("RETIRE", "TRAP", "BUS_REQ", "BUS_RESP", "USER_EVENT"), weights=(10, 2, 3, 2, 1))[0]
        source = {"RETIRE": 0, "BUS_REQ": 1, "BUS_RESP": 1, "TRAP": 2, "USER_EVENT": 3}[kind]
        clocks[source] += rng.choice((1, 1, 1, 2, 300))
        sequences[source] += 1
        if kind == "RETIRE":
            pc = pc + 4 if rng.randrange(8) else rng.randrange(0x1000, 0x100000, 4)
            fields = dict(pc=pc, next_pc=pc + 4 if rng.randrange(6) else 0x2000, length=4, boundary=0)
        elif kind == "TRAP":
            fields = dict(pc=pc, cause=2, target=0x100, boundary=0)
        elif kind == "BUS_REQ":
            fields = dict(transaction=sequences[1], address=0x8000 + 4 * sequences[1], write=True, data=1, mask=15)
        elif kind == "BUS_RESP":
            fields = dict(transaction=sequences[1], data=None, error=False)
        else:
            fields = dict(value=sequences[3])
        events.append(Event(clocks[source], source, 0, sequences[source], 0, Observation(kind, fields)))
    return events


def fill(ring, events, rng=None):
    following, limits = WATERMARK_END, []
    for event in reversed(events):
        limits.append(following)
        if event.source == 0:
            following = event.tick - 1
    for event, limit in zip(events, reversed(limits)):
        ring.append(event)
        if rng is None or rng.randrange(3):
            ring.advance(limit)
    return ring.freeze()


class CompressedRetentionTests(unittest.TestCase):
    def setUp(self):
        self.config = read_json(ROOT / "configs/baseline.json")

    def geometry(self, page_bytes=1024, post_pages=16):
        pages = self.config["sram_bytes"] // page_bytes
        return dict(self.config, page_bytes=page_bytes, pre_pages=pages - post_pages, post_pages=post_pages)

    def pinned(self, codec):
        ring = PageRing(self.geometry(post_pages=31), codec=codec)
        ring.pin()
        return ring

    def workload(self, capture, ticks, trigger_tick=None, stop_tick=None, grants=16):
        pc = 0x1000
        for tick in range(ticks):
            target = 0x1000 if tick % 64 == 63 else pc + 4
            items = [Observation("RETIRE", dict(pc=pc, next_pc=target, length=4, boundary=0))]
            pc = target
            if tick % 25 == 0:
                items.append(Observation("BUS_REQ", dict(transaction=tick, address=0x8000 + 4 * tick,
                                                         write=True, data=tick, mask=15)))
            if tick == trigger_tick:
                items.append(Observation("USER_EVENT", dict(value=1)))
            capture.step(tick, items, stop=tick == stop_tick)
            for _ in range(grants):
                if not capture.model.complete:
                    capture.service()
        while not capture.model.complete:
            capture.service()
        return decode_capture(capture.freeze())

    def assert_dispositions(self, decoded):
        metadata = decoded["metadata"]
        retained = [sum(event.source == source for event in decoded["events"]) for source in range(4)]
        remainder = 0
        for source, row in enumerate(metadata["capture"]["sources"]):
            counts = row["counters"]
            remainder += (counts["admitted"] - retained[source] - metadata["terminal"]["pending_events"][source]
                          - counts["reset_discarded"] - counts["storage_discarded"])
        self.assertEqual(remainder, metadata["retention"]["evicted_events"])

    def test_record_cost_constants_match_encoders(self):
        samples = [Observation("RETIRE", dict(pc=0, next_pc=4, length=4, boundary=0)),
                   Observation("TRAP", dict(pc=0, cause=0, target=0, boundary=0)),
                   Observation("IRQ_ACCEPT", dict(pc=0, cause=0, target=0, boundary=0)),
                   Observation("IRQ_PENDING", dict(previous=0, current=0)),
                   Observation("BUS_REQ", dict(transaction=0, address=0, write=False, data=0, mask=0)),
                   Observation("BUS_RESP", dict(transaction=0, data=0, error=False)),
                   Observation("USER_EVENT", dict(value=0))]
        sizes = {item.kind: len(encode_record(Event(0, item.source, 0, 0, 0, item))) for item in samples}
        self.assertEqual(max(sizes.values()), MAX_RECORD_BYTES)
        self.assertEqual(min(sizes.values()), 36)
        pair = encode_records([retire(0x1000, 0, 0, next_pc=0x2000), retire(0x2000, 1, 1, next_pc=0x3000)])
        self.assertEqual(len(pair[1]), MIN_RECORD_BYTES)
        self.assertEqual(len(encode_records([retire(0x1000, 0, 0), retire(0x1004, 1, 1)])[0]), 48)
        self.assertLessEqual(48, sizes["RETIRE"])

    def test_compact_pages_decode_alone_and_match_raw_retention(self):
        events = mixed_events(random.Random(7), 400)
        page_counts = {}
        for codec in TAIL:
            pages = fill(self.pinned(codec), events)
            decoded = [DECODERS[codec](page, max_events=100000)["events"] for page in pages]
            self.assertEqual(tuple(event for page in decoded for event in page), tuple(events))
            page_counts[codec] = len(pages)
        self.assertLess(page_counts["compact-v1"], page_counts["raw-v1"])

    def test_sealed_page_tails_stay_within_budget_bounds(self):
        rng = random.Random(0x5441494C)
        worst = dict.fromkeys(TAIL, 0)
        for stream in range(300):
            events = mixed_events(rng, rng.randrange(1, 300))
            for codec in TAIL:
                pages = fill(self.pinned(codec), events, rng)
                for page in pages[:-1]:
                    tail = 960 - int.from_bytes(page[32:36], "little")
                    worst[codec] = max(worst[codec], tail)
                    self.assertLessEqual(tail, TAIL[codec], (stream, codec))
                decoded = [DECODERS[codec](page, max_events=100000)["events"] for page in pages]
                self.assertEqual(tuple(event for page in decoded for event in page), tuple(events))
        self.assertGreater(worst["compact-v1"], 0)

    def test_pending_run_at_trigger_finishes_in_active_prehistory_page(self):
        ring = PageRing(self.geometry(post_pages=30), codec="compact-v1")
        events = [retire(0x1000 + 4 * index, index, index) for index in range(40)]
        for event in events:
            ring.append(event)
        self.assertEqual(ring.summary()["active_slot"], 0)
        ring.pin()
        pages = ring.freeze()
        self.assertEqual([slot for slot, _, _ in ring.directory()], [0])
        self.assertEqual(compact_decode.decode_page(pages[0], max_events=100000)["events"], tuple(events))

    def test_compact_capture_retains_more_history_in_the_same_memory(self):
        trigger = lambda item: "fault" if item.kind == "USER_EVENT" else None
        decoded = {}
        for codec in TAIL:
            capture = SnapshotCapture(self.config, post_ticks=50, match=trigger, codec=codec, measured=True)
            decoded[codec] = self.workload(capture, 20000, trigger_tick=19900)
            self.assertTrue(decoded[codec]["metadata"]["terminal"]["drain_complete"])
            self.assert_dispositions(decoded[codec])
        raw, compact = decoded["raw-v1"]["events"], decoded["compact-v1"]["events"]
        self.assertEqual(compact[-len(raw):], raw)
        self.assertGreater(len(compact), 5 * len(raw))
        retention = decoded["compact-v1"]["metadata"]["retention"]
        self.assertGreater(retention["evicted_events"], retention["evicted_pages"] * (960 // 36))

    def test_compact_eviction_density_is_checked_against_the_compact_bound(self):
        capture = SnapshotCapture(self.config, codec="compact-v1", measured=True)
        decoded = self.workload(capture, 12000, stop_tick=11999)
        self.assertGreater(decoded["metadata"]["retention"]["evicted_pages"], 0)
        wire = capture.freeze()
        fragment = wire[32 + int.from_bytes(wire[12:16], "little"):]
        metadata = copy.deepcopy(decoded["metadata"])
        extra = metadata["retention"]["evicted_pages"] * (960 // 48 * 255) + 1 - metadata["retention"]["evicted_events"]
        metadata["retention"]["evicted_events"] += extra
        for key in ("admitted", "observed"):
            metadata["capture"]["sources"][0]["counters"][key] += extra
        with self.assertRaisesRegex(DecodeError, "evicted record count"):
            encode_capture(fragment, metadata)

    def test_stop_with_pending_run_and_page_tail_drains_completely(self):
        capture = SnapshotCapture(self.config, codec="compact-v1", measured=True)
        decoded = self.workload(capture, 300, stop_tick=130, grants=7)
        self.assertTrue(decoded["metadata"]["terminal"]["drain_complete"])
        self.assertEqual(decoded["metadata"]["retention"]["evicted_events"], 0)
        admitted = sum(row["counters"]["admitted"] for row in decoded["metadata"]["capture"]["sources"])
        self.assertEqual(len(decoded["events"]), admitted)
        self.assertEqual(max(event.tick for event in decoded["events"]), 129)

    def test_compact_storage_failure_keeps_valid_pages(self):
        capture = SnapshotCapture(self.geometry(post_pages=31), codec="compact-v1", measured=True, generation_bits=1)
        tick = 0
        while capture.storage_error is None:
            capture.step(tick, [Observation("TRAP", dict(pc=tick, cause=2, target=0, boundary=tick))])
            for _ in range(7):
                if capture.storage_error is None:
                    capture.service()
            tick += 1
        decoded = decode_capture(capture.freeze(incomplete=True))
        self.assertEqual(decoded["metadata"]["terminal"]["storage_error"], "generation_exhausted")
        sequences = [event.sequence for event in decoded["events"]]
        self.assertEqual(sequences, list(range(sequences[0], sequences[-1] + 1)))
        self.assert_dispositions(decoded)

    def test_measured_budget_replaces_placeholder_costs(self):
        raw = completion_budget(self.config, measured_inventory(self.config, "raw-v1"))
        compact = completion_budget(self.config, measured_inventory(self.config, "compact-v1"))
        self.assertEqual((raw["record_bytes"], raw["required_bytes"], raw["margin_bytes"]), (52, 4144, 11216))
        self.assertEqual((compact["required_bytes"], compact["margin_bytes"]), (4720, 10640))
        self.assertEqual(sum(value for name, value in compact["terms"].items()
                             if name not in ("queue_events", "tail_waste")), 0)
        split = self.geometry(post_pages=8)
        self.assertFalse(completion_budget(split, default_inventory(split))["safe"])
        self.assertTrue(completion_budget(split, measured_inventory(split, "compact-v1"))["safe"])
        with self.assertRaises(ValueError):
            measured_inventory(self.config, "zip")

    def smallest_safe(self, page_bytes, codec):
        for post in range(1, self.config["sram_bytes"] // page_bytes):
            config = self.geometry(page_bytes, post)
            if completion_budget(config, measured_inventory(config, codec))["safe"]:
                return post
        raise AssertionError("no safe split")

    def test_full_queues_at_trigger_complete_within_the_smallest_safe_post_pool(self):
        expected = {(256, "raw-v1"): 24, (256, "compact-v1"): 32, (1024, "raw-v1"): 4,
                    (1024, "compact-v1"): 4, (4096, "raw-v1"): 1, (4096, "compact-v1"): 1}
        match = matcher([dict(kinds=["USER_EVENT"], mode="equal", value=40, mask=0xFFFFFFFF)], 4)
        for (page_bytes, codec), post in expected.items():
            with self.subTest(page_bytes=page_bytes, codec=codec):
                self.assertEqual(self.smallest_safe(page_bytes, codec), post)
                if post > 1:
                    with self.assertRaises(ValueError):
                        SnapshotCapture(self.geometry(page_bytes, post - 1), codec=codec, measured=True)
                capture = SnapshotCapture(self.geometry(page_bytes, post), codec=codec, measured=True,
                                          post_ticks=1 << 20, match=match)
                for tick in range(400):
                    capture.step(tick, observations(tick))
                    for _ in range(3 if tick >= 40 else 0):
                        if capture.storage_error is None and not capture.model.complete:
                            capture.service()
                    if capture.model.stop_reason:
                        break
                while not capture.model.complete and capture.storage_error is None:
                    capture.service()
                self.assertIsNone(capture.storage_error)
                decoded = decode_capture(capture.freeze())
                self.assertEqual(decoded["metadata"]["terminal"]["reason"], "capacity")
                self.assertTrue(decoded["metadata"]["terminal"]["drain_complete"])
                self.assert_dispositions(decoded)

    def test_service_envelope_separates_sustained_input_bursts_and_overload(self):
        raw = service_envelope(self.config, "raw-v1")
        self.assertEqual((raw["event_grants"], raw["page_overhead_grants"], raw["min_events_per_page"],
                          raw["burst_events_per_source"]), (7, 15, 18, 16))
        self.assertAlmostEqual(raw["sustained_events_per_cycle"], 1 / (7 + 15 / 18))
        compact = service_envelope(self.config, "compact-v1")
        self.assertEqual((compact["page_overhead_grants"], compact["min_events_per_page"]), (19, 17))
        self.assertAlmostEqual(compact["cycles_per_event"], 7 + 19 / 17)

        def run(pattern, ticks=3000):
            capture = SnapshotCapture(self.config, codec="compact-v1", measured=True)
            for tick in range(ticks):
                capture.step(tick, [Observation("USER_EVENT", dict(value=tick))] if pattern(tick) else [],
                             service=True)
            return capture.metadata.snapshot()["sources"][3]["counters"]

        interval = int(compact["cycles_per_event"]) + 1
        self.assertEqual(run(lambda tick: tick % interval == 0)["ingress_dropped"], 0)
        self.assertEqual(run(lambda tick: tick < 16)["ingress_dropped"], 0)
        overload = run(lambda tick: tick % 6 == 0)
        self.assertGreater(overload["fifo_dropped"], 0)
        self.assertEqual(overload["observed"], overload["admitted"] + overload["ingress_dropped"])
        self.assertGreater(run(lambda tick: tick < 40)["fifo_dropped"], 0)

    def test_metadata_storage_is_enumerated_separately_from_pages(self):
        result = metadata_bytes(self.config)
        self.assertEqual(result["terms"], dict(counters=256, journal=788, trigger=405, terminal=27, directory=288))
        self.assertEqual(result["bytes"], 1764)


if __name__ == "__main__":
    unittest.main()

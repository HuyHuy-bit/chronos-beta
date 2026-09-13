import json
import unittest
from dataclasses import replace
from pathlib import Path

from model.chronos.events import Event, Observation
from model.chronos.raw_decode import decode_page
from model.chronos.retention import PageRing, StorageError


BASELINE = Path(__file__).resolve().parents[2] / "configs" / "baseline.json"


def user(sequence, *, tick=None, epoch=0):
    return Event(sequence if tick is None else tick, 3, epoch, sequence, 0,
                 Observation("USER_EVENT", {"value": sequence}))


def request(sequence):
    return Event(sequence, 1, 0, sequence, 0, Observation("BUS_REQ", {
        "transaction": sequence, "address": 0, "data": 0, "write": False, "mask": 15,
    }))


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads(BASELINE.read_text())
        self.config.update(page_bytes=256, sram_bytes=8192, pre_pages=2, post_pages=30)

    def membership(self, ring):
        return tuple((slot, generation, tuple(event.sequence for event in
                      decode_page(page, page_bytes=256)["events"]))
                     for slot, generation, page in ring.directory())

    def test_initial_and_empty_freeze(self):
        ring = PageRing(self.config)
        self.assertEqual(ring.summary(), {
            "page_bytes": 256, "pre_pages": 2, "post_pages": 30,
            "pinned": False, "frozen": False, "active_slot": None,
            "evicted_pages": 0, "evicted_events": 0, "eviction_saturated": False,
            "committed_pages": 0, "next_generation": 0,
        })
        ring.seal()
        self.assertEqual(ring.directory(), ())
        self.assertEqual(ring.freeze(), ())
        self.assertTrue(ring.summary()["pinned"])
        self.assertTrue(ring.summary()["frozen"])
        self.assertEqual(ring.summary()["next_generation"], 0)

    def test_builder_is_not_committed_and_seal_is_explicit(self):
        ring = PageRing(self.config)
        ring.append(user(0))
        self.assertEqual(ring.directory(), ())
        self.assertEqual(ring.summary()["active_slot"], 0)
        self.assertEqual(ring.summary()["next_generation"], 1)
        ring.seal()
        self.assertEqual(self.membership(ring), ((0, 0, (0,)),))
        self.assertIsNone(ring.summary()["active_slot"])
        ring.seal()
        self.assertEqual(self.membership(ring), ((0, 0, (0,)),))

    def test_literal_wrap_membership_and_allocation_invalidation(self):
        ring = PageRing(self.config)
        checkpoints = {
            4: (),
            5: ((0, 0, (0, 1, 2, 3, 4)),),
            9: ((0, 0, (0, 1, 2, 3, 4)),),
            10: ((1, 1, (5, 6, 7, 8, 9)),),
            15: ((0, 2, (10, 11, 12, 13, 14)),),
            20: ((1, 3, (15, 16, 17, 18, 19)),),
            25: ((0, 4, (20, 21, 22, 23, 24)),),
            30: ((1, 5, (25, 26, 27, 28, 29)),),
        }
        for sequence in range(31):
            ring.append(user(sequence))
            if sequence in checkpoints:
                self.assertEqual(self.membership(ring), checkpoints[sequence])
            self.assertNotIn(ring.summary()["active_slot"],
                             [slot for slot, _, _ in ring.directory()])
        self.assertEqual(ring.summary()["evicted_pages"], 5)
        self.assertEqual(ring.summary()["evicted_events"], 25)
        ring.freeze()
        self.assertEqual(self.membership(ring), (
            (1, 5, (25, 26, 27, 28, 29)), (0, 6, (30,)),
        ))

    def test_exact_fit_commits_immediately(self):
        ring = PageRing(self.config)
        for sequence in range(4):
            ring.append(request(sequence))
        self.assertIsNone(ring.summary()["active_slot"])
        self.assertEqual(self.membership(ring), ((0, 0, (0, 1, 2, 3)),))
        page = ring.directory()[0][2]
        self.assertEqual(int.from_bytes(page[32:36], "little"), 192)

    def test_pin_keeps_active_pre_builder_then_uses_post_slots(self):
        ring = PageRing(self.config)
        for sequence in range(6):
            ring.append(user(sequence))
        before = ring.directory()
        ring.pin()
        ring.pin()
        self.assertEqual(ring.directory(), before)
        self.assertEqual(ring.summary()["active_slot"], 1)
        for sequence in range(6, 11):
            ring.append(user(sequence))
        self.assertEqual(self.membership(ring), (
            (0, 0, (0, 1, 2, 3, 4)), (1, 1, (5, 6, 7, 8, 9)),
        ))
        self.assertEqual(ring.summary()["active_slot"], 2)
        ring.freeze()
        self.assertEqual(self.membership(ring)[-1], (2, 2, (10,)))
        self.assertEqual(ring.summary()["evicted_events"], 0)

    def test_pin_without_builder_skips_unused_pre_slots(self):
        ring = PageRing(self.config)
        ring.pin()
        ring.append(user(0))
        self.assertEqual(ring.summary()["active_slot"], 2)
        ring.freeze()
        self.assertEqual(self.membership(ring), ((2, 0, (0,)),))

    def test_post_slots_are_allocated_only_once(self):
        ring = PageRing(dict(self.config, pre_pages=30, post_pages=2))
        ring.pin()
        for sequence in range(10):
            ring.append(user(sequence))
        self.assertEqual(ring.summary()["active_slot"], 31)
        with self.assertRaisesRegex(StorageError, "^post_capacity$"):
            ring.append(user(10))
        self.assertEqual(self.membership(ring), (
            (30, 0, (0, 1, 2, 3, 4)), (31, 1, (5, 6, 7, 8, 9)),
        ))
        before = ring.summary()
        with self.assertRaisesRegex(StorageError, "^post_capacity$"):
            ring.append(user(10))
        self.assertEqual(ring.summary(), before)
        self.assertEqual(len(ring.freeze()), 2)

    def test_generation_exhaustion_does_not_invalidate_committed_page(self):
        ring = PageRing(self.config, generation_bits=1)
        for sequence in range(2):
            ring.append(user(sequence))
            ring.seal()
        before = ring.directory(), ring.summary()
        with self.assertRaisesRegex(StorageError, "^generation_exhausted$"):
            ring.append(user(2))
        self.assertEqual((ring.directory(), ring.summary()), before)
        self.assertEqual(ring.summary()["next_generation"], 2)

    def test_generation_failure_can_seal_prior_builder(self):
        ring = PageRing(self.config, generation_bits=1)
        for sequence in range(10):
            ring.append(user(sequence))
        with self.assertRaisesRegex(StorageError, "^generation_exhausted$"):
            ring.append(user(10))
        self.assertEqual(self.membership(ring), (
            (0, 0, (0, 1, 2, 3, 4)), (1, 1, (5, 6, 7, 8, 9)),
        ))
        self.assertEqual(ring.summary()["evicted_pages"], 0)

    def test_bad_event_leaves_full_builder_and_directory_unchanged(self):
        ring = PageRing(self.config)
        for sequence in range(5):
            ring.append(user(sequence))
        before = ring.directory(), ring.summary()
        for event in (user(4), user(5, tick=3), replace(user(5), lane=1),
                      replace(user(5), tick=True), None):
            with self.subTest(event=event), self.assertRaises(ValueError):
                ring.append(event)
            self.assertEqual((ring.directory(), ring.summary()), before)
        ring.append(user(5))
        self.assertEqual(self.membership(ring), ((0, 0, (0, 1, 2, 3, 4)),))

    def test_identity_order_survives_eviction(self):
        ring = PageRing(dict(self.config, pre_pages=1, post_pages=31))
        ring.append(user(4, tick=8, epoch=2))
        ring.seal()
        ring.append(request(0))
        ring.seal()
        for event in (user(4, tick=9, epoch=2), user(5, tick=7, epoch=2),
                      user(5, tick=9, epoch=1), user(5, tick=8, epoch=2)):
            with self.subTest(event=event), self.assertRaises(ValueError):
                ring.append(event)
        ring.append(user(0, tick=9, epoch=3))
        ring.freeze()
        self.assertEqual(self.membership(ring), ((0, 2, (0,)),))

    def test_cross_source_time_can_decrease(self):
        ring = PageRing(self.config)
        ring.append(user(0, tick=100))
        ring.append(request(0))
        ring.freeze()
        page = decode_page(ring.directory()[0][2], page_bytes=256)
        self.assertEqual(page["first_tick"], 0)
        self.assertEqual(tuple(event.tick for event in page["events"]), (100, 0))

    def test_inputs_and_frozen_views_are_defensive(self):
        ring = PageRing(self.config, session_id=12, config_tag=34)
        event = user(0)
        ring.append(event)
        self.config["page_bytes"] = 4096
        object.__setattr__(event, "sequence", 99)
        object.__setattr__(event.observation, "fields", {"value": 99})
        frozen = ring.freeze()
        parsed = decode_page(frozen[0], page_bytes=256)
        self.assertEqual(parsed["events"][0], user(0))
        self.assertEqual((parsed["session_id"], parsed["config_tag"]), (12, 34))
        self.assertIs(ring.freeze(), frozen)
        summary = ring.summary()
        summary["evicted_events"] = 99
        self.assertEqual(ring.summary()["evicted_events"], 0)
        for action in (lambda: ring.append(user(1)), ring.seal, ring.pin):
            with self.assertRaises(ValueError):
                action()
            self.assertEqual(ring.freeze(), frozen)
        with self.assertRaises(TypeError):
            frozen[0][0] = 0

    def test_eviction_counters_saturate_independently(self):
        limit = (1 << 64) - 1
        for pages, events, expected_pages, expected_events, saturated in (
            (limit - 1, limit - 1, limit, limit, False),
            (limit, 0, limit, 1, True),
            (0, limit, 1, limit, True),
        ):
            with self.subTest(pages=pages, events=events):
                ring = PageRing(dict(self.config, pre_pages=1, post_pages=31))
                ring.append(user(0))
                ring.seal()
                ring._evicted_pages = pages
                ring._evicted_events = events
                ring.append(user(1))
                summary = ring.summary()
                self.assertEqual(summary["evicted_pages"], expected_pages)
                self.assertEqual(summary["evicted_events"], expected_events)
                self.assertEqual(summary["eviction_saturated"], saturated)
                ring.seal()
                ring.append(user(2))
                self.assertTrue(ring.summary()["eviction_saturated"])

    def test_invalid_configuration_and_identities(self):
        for change in ({"source_count": 3}, {"post_pages": 2}, {"fifo_depth": 3}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                PageRing(dict(self.config, **change))
        for bits in (0, 65, True, 1.0, None):
            with self.subTest(bits=bits), self.assertRaises(ValueError):
                PageRing(self.config, generation_bits=bits)
        for field in ("session_id", "config_tag"):
            for value in (-1, 1 << 64, True, 1.0, None):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    PageRing(self.config, **{field: value})


if __name__ == "__main__":
    unittest.main()

import copy
import unittest
from unittest.mock import patch

from model.chronos.capture_session import decode_capture
from model.chronos.events import Observation
from model.chronos.retention import StorageError
from model.chronos.snapshot import SnapshotCapture
from scripts.config import ROOT, read_json


def user(value=0):
    return Observation("USER_EVENT", {"value": value})


def request(value=0):
    return Observation("BUS_REQ", dict(transaction=value, address=value, write=False,
                                       data=value, mask=15))


def response(value=0):
    return Observation("BUS_RESP", dict(transaction=value, data=value, error=False))


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.config = dict(read_json(ROOT / "configs/baseline.json"), pre_pages=2, post_pages=30)

    def capture(self, **kwargs):
        return SnapshotCapture(copy.deepcopy(self.config), **kwargs)

    def grants(self, capture, count=16):
        for _ in range(count):
            capture.service()

    def emit(self, capture, tick, observation):
        capture.step(tick, [observation])
        self.grants(capture)

    def finish(self, capture, tick, *, incomplete=False):
        capture.step(tick, stop=True)
        return decode_capture(capture.freeze(incomplete=incomplete))

    def assert_dispositions(self, decoded):
        metadata = decoded["metadata"]
        source_counts = [entry["counters"] for entry in metadata["capture"]["sources"]]
        pending = metadata["terminal"]["pending_events"]
        evicted = metadata["retention"]["evicted_events"]
        retained = [sum(event.source == source for event in decoded["events"]) for source in range(4)]
        remainder = []
        for source, counts in enumerate(source_counts):
            self.assertEqual(counts["observed"], counts["filtered"] + counts["admitted"]
                             + counts["ingress_dropped"])
            self.assertEqual(counts["ingress_dropped"], counts["fifo_dropped"] + counts["capacity_dropped"])
            remainder.append(counts["admitted"] - retained[source] - pending[source]
                             - counts["reset_discarded"] - counts["storage_discarded"])
            self.assertGreaterEqual(remainder[-1], 0)
        self.assertEqual(sum(remainder), evicted)

    def test_empty_stopped_capture_has_authoritative_empty_accounting(self):
        capture = self.capture()
        decoded = self.finish(capture, 0)
        self.assertEqual(decoded["events"], ())
        self.assertEqual(decoded["metadata"]["scope"], "capture-snapshot")
        self.assertEqual(decoded["fragment"]["manifest"]["scope"], "event-fragment")
        self.assertEqual(decoded["metadata"]["terminal"], dict(reason="manual", drain_complete=True,
                         storage_error=None, pending_events=[0, 0, 0, 0], rejected_cycle_events=0))
        self.assert_dispositions(decoded)

    def test_repeated_wraps_retain_literal_page_membership(self):
        capture = self.capture()
        for tick in range(80):
            self.emit(capture, tick, request(tick))
        directory = capture.ring.directory()
        self.assertEqual([(slot, generation) for slot, generation, _ in directory], [(0, 2), (1, 3)])
        decoded = self.finish(capture, 80)
        self.assertEqual([event.sequence for event in decoded["events"]], list(range(40, 80)))
        self.assertEqual([event.tick for event in decoded["events"]], list(range(40, 80)))
        self.assertEqual([event.observation.fields["transaction"] for event in decoded["events"]],
                         list(range(40, 80)))
        self.assertEqual(decoded["metadata"]["retention"]["evicted_pages"], 2)
        self.assertEqual(decoded["metadata"]["retention"]["evicted_events"], 40)
        self.assertEqual(decoded["metadata"]["capture"]["sources"][1]["counters"]["admitted"], 80)
        self.assertEqual(len(capture.model.emitted), 80)
        self.assert_dispositions(decoded)

    def test_trigger_after_wrap_preserves_history_and_adds_post_page(self):
        capture = self.capture(match=lambda observation: "fault" if observation.kind == "USER_EVENT" else None)
        for tick in range(80):
            self.emit(capture, tick, request(tick))
        before = capture.ring.directory()
        self.emit(capture, 80, user(99))
        self.assertTrue(capture.ring.summary()["pinned"])
        decoded = decode_capture(capture.freeze())
        self.assertEqual(capture.ring.directory()[:2], before)
        self.assertEqual([event.sequence for event in decoded["events"][:-1]], list(range(40, 80)))
        self.assertEqual(decoded["events"][-1].observation, user(99))
        self.assertEqual([page["generation"] for page in decoded["fragment"]["pages"]], [2, 3, 4])
        self.assertEqual(decoded["metadata"]["capture"]["trigger"]["tick"], 80)
        self.assert_dispositions(decoded)

    def test_active_pre_builder_completes_once_after_pin(self):
        capture = self.capture(post_ticks=1000,
                               match=lambda observation: "fault" if observation.kind == "USER_EVENT" else None)
        for tick in range(21):
            self.emit(capture, tick, request(tick))
        before = capture.ring.directory()
        self.assertEqual([(slot, generation) for slot, generation, _ in before], [(0, 0)])
        self.assertEqual(capture.ring.summary()["active_slot"], 1)
        self.emit(capture, 21, user(999))
        for tick in range(22, 52):
            self.emit(capture, tick, request(tick))
        decoded = self.finish(capture, 52)
        self.assertEqual(capture.ring.directory()[0], before[0])
        self.assertEqual([(slot, generation) for slot, generation, _ in capture.ring.directory()],
                         [(0, 0), (1, 1), (2, 2)])
        self.assertEqual(len(decoded["events"]), 52)
        self.assertEqual(decoded["metadata"]["retention"]["evicted_events"], 0)
        self.assert_dispositions(decoded)

    def test_trigger_pins_before_same_cycle_completion_allocates_page(self):
        config = dict(self.config, pre_pages=1, post_pages=31)
        capture = SnapshotCapture(config, keep=lambda observation: observation.kind != "USER_EVENT",
                                  match=lambda observation: "fault" if observation.kind == "USER_EVENT" else None)
        for tick in range(20):
            self.emit(capture, tick, request(tick))
        original = capture.ring.directory()[0]
        capture.step(20, [request(20)])
        self.grants(capture, 15)
        capture.step(21, [user(99)], service=True)
        decoded = decode_capture(capture.freeze())
        self.assertEqual(capture.ring.directory()[0], original)
        self.assertEqual([event.sequence for event in decoded["events"]], list(range(21)))
        self.assertEqual(decoded["metadata"]["retention"]["evicted_events"], 0)
        self.assert_dispositions(decoded)

    def test_full_fifo_filtered_trigger_is_retained_outside_ordinary_queues(self):
        capture = self.capture(keep=lambda observation: observation.fields["value"] != 99,
                               match=lambda observation: "filtered-fault" if observation.fields["value"] == 99 else None)
        for tick in range(16):
            capture.step(tick, [user(tick)])
        capture.step(16, [user(99)])
        self.grants(capture, 16 * 16)
        decoded = decode_capture(capture.freeze())
        source = decoded["metadata"]["capture"]["sources"][3]
        self.assertEqual(source["counters"]["observed"], 17)
        self.assertEqual(source["counters"]["filtered"], 1)
        self.assertEqual(source["counters"]["fifo_dropped"], 0)
        self.assertEqual(source["ranges"], [dict(epoch=0, first_sequence=16, last_sequence=16,
                         first_tick=16, last_tick=16, reason="filtered", count=1)])
        descriptor = decoded["metadata"]["capture"]["trigger"]
        match = dict(source=3, lane=0, reason="filtered-fault")
        self.assertEqual(descriptor, dict(tick=16, matches=[match], primary=match))
        self.assertEqual([event.sequence for event in decoded["events"]], list(range(16)))
        self.assert_dispositions(decoded)

    def test_terminal_fifo_loss_survives_without_later_ordinary_event(self):
        capture = self.capture()
        for tick in range(18):
            capture.step(tick, [user(tick)])
        capture.step(18, stop=True)
        self.grants(capture, 16 * 16)
        decoded = decode_capture(capture.freeze())
        source = decoded["metadata"]["capture"]["sources"][3]
        self.assertEqual(source["counters"]["fifo_dropped"], 2)
        self.assertEqual(source["ranges"], [dict(epoch=0, first_sequence=16, last_sequence=17,
                         first_tick=16, last_tick=17, reason="fifo", count=2)])
        self.assertEqual(len(decoded["events"]), 16)
        self.assert_dispositions(decoded)

    def test_capacity_bundle_rejection_exports_both_source_dispositions(self):
        capture = self.capture(post_ticks=1000, match=lambda observation: "start")
        for tick in range(179):
            self.emit(capture, tick, user(tick))
        capture.step(179, [request(1), response(1), user(179)])
        decoded = decode_capture(capture.freeze())
        self.assertEqual(decoded["metadata"]["terminal"]["reason"], "capacity")
        self.assertEqual(len(decoded["events"]), 179)
        sources = decoded["metadata"]["capture"]["sources"]
        self.assertEqual(sources[1]["counters"]["capacity_dropped"], 2)
        self.assertEqual(sources[3]["counters"]["capacity_dropped"], 1)
        self.assertEqual(sources[1]["ranges"], [dict(epoch=0, first_sequence=0, last_sequence=1,
                         first_tick=179, last_tick=179, reason="capacity", count=2)])
        self.assertEqual(sources[3]["ranges"], [dict(epoch=0, first_sequence=179, last_sequence=179,
                         first_tick=179, last_tick=179, reason="capacity", count=1)])
        self.assert_dispositions(decoded)

    def test_journal_overflow_preserves_existing_exact_range(self):
        capture = self.capture(journal_capacity=1, keep=lambda observation: observation.fields["value"] == 1)
        capture.step(0, [user(0)])
        self.emit(capture, 1, user(1))
        capture.step(2, [user(2)])
        capture.step(3, [user(3)])
        decoded = self.finish(capture, 4)
        source = decoded["metadata"]["capture"]["sources"][3]
        self.assertTrue(source["journal_overflow"])
        self.assertEqual(source["counters"]["filtered"], 3)
        self.assertEqual(source["ranges"], [dict(epoch=0, first_sequence=0, last_sequence=0,
                         first_tick=0, last_tick=0, reason="filtered", count=1)])
        self.assertEqual([event.sequence for event in decoded["events"]], [1])
        self.assert_dispositions(decoded)

    def test_zero_journal_capacity_exports_unknown_range_and_exact_totals(self):
        capture = self.capture(journal_capacity=0, keep=lambda observation: False)
        capture.step(0, [user(1)])
        decoded = self.finish(capture, 1)
        source = decoded["metadata"]["capture"]["sources"][3]
        self.assertTrue(source["journal_overflow"])
        self.assertEqual(source["ranges"], [])
        self.assertEqual(source["counters"]["filtered"], 1)
        self.assertEqual(decoded["events"], ())
        self.assert_dispositions(decoded)

    def test_metadata_counter_saturation_does_not_truncate_retained_events(self):
        capture = self.capture(metadata_bits=2)
        for tick in range(4):
            self.emit(capture, tick, user(tick))
        decoded = self.finish(capture, 4)
        source = decoded["metadata"]["capture"]["sources"][3]
        self.assertEqual(source["counters"]["observed"], 3)
        self.assertEqual(source["counters"]["admitted"], 3)
        self.assertEqual(set(source["saturated"]), {"observed", "admitted"})
        self.assertEqual([event.sequence for event in decoded["events"]], [0, 1, 2, 3])

    def test_exact_metadata_limit_is_not_saturation(self):
        capture = self.capture(metadata_bits=2)
        for tick in range(3):
            self.emit(capture, tick, user(tick))
        decoded = self.finish(capture, 3)
        self.assertEqual(decoded["metadata"]["capture"]["sources"][3]["saturated"], [])
        self.assert_dispositions(decoded)

    def test_source_reset_discards_partial_work_before_new_epoch_observation(self):
        capture = self.capture()
        capture.step(0, [user(0)])
        capture.step(1, [user(1)])
        self.grants(capture, 15)
        capture.step(2, [user(2)], reset_source=3, service=True)
        self.assertEqual(capture.model.emitted, [])
        self.grants(capture, 15)
        decoded = self.finish(capture, 3)
        self.assertEqual([(event.epoch, event.sequence, event.tick) for event in decoded["events"]], [(1, 0, 2)])
        source = decoded["metadata"]["capture"]["sources"][3]
        self.assertEqual(source["counters"]["reset_discarded"], 2)
        self.assertEqual(source["ranges"], [dict(epoch=0, first_sequence=0, last_sequence=1,
                         first_tick=0, last_tick=1, reason="reset_discarded", count=2)])
        self.assert_dispositions(decoded)

    def test_sequence_exhaustion_exports_rejected_whole_cycle(self):
        capture = self.capture(counter_bits=2)
        capture.step(0, [request(0), response(0)])
        capture.step(1, [request(1), response(1)])
        capture.step(2, [request(2), response(2), user(2)])
        self.grants(capture, 4 * 16)
        decoded = decode_capture(capture.freeze())
        self.assertEqual(decoded["metadata"]["terminal"]["reason"], "sequence_exhausted")
        self.assertEqual(decoded["metadata"]["terminal"]["rejected_cycle_events"], 3)
        self.assertEqual([event.sequence for event in decoded["events"]], [0, 1, 2, 3])
        self.assertEqual(decoded["metadata"]["capture"]["sources"][3]["counters"]["observed"], 0)
        self.assert_dispositions(decoded)

    def test_epoch_exhaustion_preserves_queued_old_epoch(self):
        capture = self.capture(counter_bits=1)
        capture.step(0, [user(1)], reset_source=3)
        capture.step(1, reset_source=3)
        self.grants(capture)
        decoded = decode_capture(capture.freeze())
        self.assertEqual(decoded["metadata"]["terminal"]["reason"], "epoch_exhausted")
        self.assertEqual([(event.epoch, event.sequence) for event in decoded["events"]], [(1, 0)])
        self.assertEqual(decoded["metadata"]["capture"]["sources"][3]["counters"]["reset_discarded"], 0)
        self.assert_dispositions(decoded)

    def test_time_exhaustion_retains_last_representable_tick(self):
        capture = self.capture(counter_bits=2)
        capture.step(3, [user(9)])
        self.grants(capture)
        decoded = decode_capture(capture.freeze())
        self.assertEqual(decoded["metadata"]["terminal"]["reason"], "time_exhausted")
        self.assertEqual([event.tick for event in decoded["events"]], [3])
        self.assert_dispositions(decoded)

    def test_trace_reset_has_priority_and_starts_new_time_and_identity(self):
        capture = self.capture(session_id=7)
        self.emit(capture, 10, user(1))
        capture.step(11, [user(99)], stop=True, trace_reset=True, reset_source=3, service=True)
        self.assertFalse(capture.frozen)
        self.assertEqual(capture.ring.directory(), ())
        self.assertIsNone(capture.model.stop_reason)
        self.assertEqual(capture.model.emitted, [])
        self.emit(capture, 0, user(2))
        decoded = self.finish(capture, 1)
        self.assertEqual(decoded["metadata"]["session_id"], 8)
        self.assertEqual([(event.epoch, event.sequence, event.tick) for event in decoded["events"]], [(0, 0, 0)])
        self.assertEqual(decoded["events"][0].observation, user(2))
        self.assertEqual(decoded["metadata"]["capture"]["sources"][3]["counters"]["observed"], 1)
        self.assert_dispositions(decoded)

    def test_stop_excludes_same_cycle_reset_observation_and_trigger_but_services(self):
        capture = self.capture(match=lambda observation: "fault" if observation.fields["value"] == 99 else None)
        capture.step(0, [user(1)])
        self.grants(capture, 15)
        capture.step(1, [user(99)], stop=True, reset_source=3, service=True)
        decoded = decode_capture(capture.freeze())
        self.assertEqual(decoded["metadata"]["terminal"]["reason"], "manual")
        self.assertIsNone(decoded["metadata"]["capture"]["trigger"])
        self.assertEqual([(event.epoch, event.sequence) for event in decoded["events"]], [(0, 0)])
        self.assertEqual(decoded["metadata"]["capture"]["sources"][3]["counters"]["observed"], 1)
        self.assert_dispositions(decoded)

    def test_pending_work_requires_explicit_incomplete_freeze(self):
        capture = self.capture()
        capture.step(0, [user(0), request(0)])
        with self.assertRaises(ValueError):
            capture.freeze(incomplete=True)
        capture.step(1, stop=True)
        with self.assertRaises(ValueError):
            capture.freeze()
        wire = capture.freeze(incomplete=True)
        self.assertEqual(capture.freeze(incomplete=True), wire)
        decoded = decode_capture(wire)
        self.assertEqual(decoded["metadata"]["terminal"]["pending_events"], [0, 1, 0, 1])
        self.assertFalse(decoded["metadata"]["terminal"]["drain_complete"])
        self.assertEqual(decoded["events"], ())
        self.assert_dispositions(decoded)

    def test_generation_failure_preserves_last_valid_page_and_classifies_event(self):
        capture = SnapshotCapture(dict(self.config, pre_pages=1, post_pages=31), generation_bits=1)
        for tick in range(40):
            self.emit(capture, tick, request(tick))
        previous = capture.ring.directory()
        self.emit(capture, 40, request(40))
        self.assertEqual(capture.storage_error, "generation_exhausted")
        self.assertEqual(capture.ring.directory(), previous)
        with self.assertRaises(ValueError):
            capture.freeze()
        decoded = decode_capture(capture.freeze(incomplete=True))
        self.assertEqual([event.sequence for event in decoded["events"]], list(range(20, 40)))
        self.assertEqual(decoded["metadata"]["terminal"]["reason"], "storage_failure")
        self.assertEqual(decoded["metadata"]["terminal"]["storage_error"], "generation_exhausted")
        self.assertFalse(decoded["metadata"]["terminal"]["drain_complete"])
        source = decoded["metadata"]["capture"]["sources"][1]
        self.assertEqual(source["counters"]["storage_discarded"], 1)
        self.assertEqual(source["ranges"], [dict(epoch=0, first_sequence=40, last_sequence=40,
                         first_tick=40, last_tick=40, reason="storage_discarded", count=1)])
        self.assert_dispositions(decoded)

    def test_storage_failure_stops_service_and_preserves_remaining_queue(self):
        capture = self.capture()
        capture.step(0, [user(0)])
        capture.step(1, [user(1)])
        with patch.object(capture.ring, "append", side_effect=StorageError("post_capacity")):
            self.grants(capture)
        self.assertEqual(capture.storage_error, "post_capacity")
        before = list(capture.model.queues[3])
        try:
            capture.service()
        except ValueError:
            pass
        self.assertEqual(list(capture.model.queues[3]), before)
        decoded = decode_capture(capture.freeze(incomplete=True))
        self.assertEqual(decoded["metadata"]["terminal"]["pending_events"], [0, 0, 0, 1])
        self.assertEqual(decoded["metadata"]["capture"]["sources"][3]["counters"]["storage_discarded"], 1)
        self.assert_dispositions(decoded)

    def test_frozen_bytes_are_immutable_and_trace_reset_starts_fresh_session(self):
        capture = self.capture(session_id=9)
        self.emit(capture, 0, user(5))
        capture.step(1, stop=True)
        frozen = capture.freeze()
        directory = capture.ring.directory()
        self.assertTrue(capture.frozen)
        for operation in (lambda: capture.step(2, [user(6)]), capture.service,
                          lambda: capture.ring.append(capture.model.emitted[0]), capture.ring.seal, capture.ring.pin):
            with self.assertRaises(ValueError):
                operation()
        decoded = decode_capture(frozen)
        decoded["metadata"]["capture"]["sources"][3]["counters"]["observed"] = 999
        self.assertEqual(capture.freeze(), frozen)
        self.assertEqual(capture.ring.directory(), directory)
        capture.step(2, trace_reset=True)
        self.assertFalse(capture.frozen)
        self.emit(capture, 0, user(7))
        current = self.finish(capture, 1)
        historical = decode_capture(frozen)
        self.assertEqual(current["metadata"]["session_id"], 10)
        self.assertEqual(historical["metadata"]["session_id"], 9)
        self.assertEqual(historical["events"][0].observation, user(5))
        self.assertEqual(current["events"][0].observation, user(7))

    def test_invalid_reset_source_does_not_mutate_observation_accounting(self):
        capture = self.capture()
        self.emit(capture, 0, user(1))
        before = capture.metadata.snapshot()
        for source in (-1, 4, True, 1.0):
            with self.subTest(source=source), self.assertRaises(ValueError):
                capture.step(1, [user(2)], reset_source=source)
            self.assertEqual(capture.metadata.snapshot(), before)
        self.emit(capture, 1, user(2))
        decoded = self.finish(capture, 2)
        self.assertEqual([event.sequence for event in decoded["events"]], [0, 1])
        self.assert_dispositions(decoded)


if __name__ == "__main__":
    unittest.main()

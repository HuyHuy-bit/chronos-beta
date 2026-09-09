import copy
import json
import random
import unittest

from model.chronos.admission import CaptureModel
from model.chronos.events import Observation
from scripts.config import ROOT, read_json


def observation(kind, value=0):
    fields = {
        "RETIRE": dict(pc=value, next_pc=value + 4, length=4, boundary=0),
        "BUS_REQ": dict(transaction=value, address=value, write=False, data=value, mask=15),
        "BUS_RESP": dict(transaction=value, data=value, error=False),
        "TRAP": dict(pc=None, cause=value, target=None, boundary=1),
        "IRQ_ACCEPT": dict(pc=None, cause=value, target=None, boundary=1),
        "IRQ_PENDING": dict(previous=0, current=value),
        "USER_EVENT": dict(value=value),
    }
    return Observation(kind, fields[kind])


def identity(event):
    return event.source, event.epoch, event.sequence, event.lane, event.tick, event.observation.kind


def six_events(value=0):
    return [observation(kind, value) for kind in
            ("IRQ_PENDING", "BUS_REQ", "USER_EVENT", "RETIRE", "TRAP", "BUS_RESP")]


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.config = read_json(ROOT / "configs/baseline.json")

    def model(self, **kwargs):
        return CaptureModel(copy.deepcopy(self.config), **kwargs)

    def drain_events(self, model, count):
        for _ in range(16 * count):
            model.service()

    def assert_accounting(self, model):
        for values in model.stats.values():
            self.assertEqual(values["observed"], values["filtered"] + values["admitted"]
                             + values["ingress_dropped"])
            self.assertEqual(values["ingress_dropped"], values["fifo_dropped"]
                             + values["capacity_dropped"])

    def test_six_lanes_have_literal_identity_and_order(self):
        model = self.model()
        model.step(7, six_events())
        actual = [identity(event) for queue in model.queues for event in queue]
        self.assertEqual(actual, [
            (0, 0, 0, 0, 7, "RETIRE"),
            (1, 0, 0, 0, 7, "BUS_RESP"), (1, 0, 1, 1, 7, "BUS_REQ"),
            (2, 0, 0, 0, 7, "TRAP"), (2, 0, 1, 1, 7, "IRQ_PENDING"),
            (3, 0, 0, 0, 7, "USER_EVENT"),
        ])
        self.assertEqual(list(model.sequences), [1, 2, 2, 1])
        self.assert_accounting(model)

    def test_two_lane_bundle_rejects_atomically_with_one_slot_free(self):
        model = self.model()
        for tick in range(15):
            model.step(tick, [observation("BUS_REQ", tick)])
        model.step(15, [observation("BUS_REQ", 30), observation("BUS_RESP", 30),
                        observation("USER_EVENT", 30)])
        self.assertEqual([len(queue) for queue in model.queues], [0, 15, 0, 1])
        self.assertEqual(list(model.sequences), [0, 17, 0, 1])
        stats = model.stats[(1, 0)]
        self.assertEqual((stats["observed"], stats["admitted"], stats["fifo_dropped"]), (17, 15, 2))
        self.assert_accounting(model)

    def test_filtering_precedes_bundle_space_check_and_preserves_lane(self):
        model = self.model(keep=lambda event: event.kind != "BUS_RESP")
        for tick in range(15):
            model.step(tick, [observation("BUS_REQ", tick)])
        model.step(15, [observation("BUS_REQ", 30), observation("BUS_RESP", 30)])
        self.assertEqual(identity(model.queues[1][-1]), (1, 0, 16, 1, 15, "BUS_REQ"))
        stats = model.stats[(1, 0)]
        self.assertEqual((stats["observed"], stats["filtered"], stats["admitted"],
                          stats["ingress_dropped"]), (17, 1, 16, 0))

    def test_full_fifo_does_not_borrow_end_of_cycle_service_slot(self):
        model = self.model()
        for tick in range(16):
            model.step(tick, [observation("USER_EVENT", tick)])
        for _ in range(15):
            model.service()
        model.step(16, [observation("USER_EVENT", 16)], service=True)
        self.assertEqual(len(model.emitted), 1)
        self.assertEqual(len(model.queues[3]), 15)
        self.assertEqual(model.stats[(3, 0)]["fifo_dropped"], 1)
        self.assertEqual(model.queues[3][-1].sequence, 15)

    def test_partial_service_holds_fifo_and_round_robin_advances_on_completion(self):
        model = self.model()
        model.step(0, six_events())
        for _ in range(15):
            model.service()
        self.assertEqual(len(model.emitted), 0)
        self.assertEqual([len(queue) for queue in model.queues], [1, 2, 2, 1])
        model.service()
        self.assertEqual(identity(model.emitted[0]), (0, 0, 0, 0, 0, "RETIRE"))
        self.drain_events(model, 5)
        self.assertEqual([event.source for event in model.emitted], [0, 1, 2, 3, 1, 2])
        self.assertFalse(model.complete)
        model.stop()
        self.assertTrue(model.complete)

    def test_idle_grants_and_skipped_ticks_do_not_create_credit(self):
        model = self.model()
        for _ in range(100):
            model.service()
        model.step(0)
        model.step(100, [observation("USER_EVENT")], service=True)
        for _ in range(14):
            model.service()
        self.assertEqual(len(model.emitted), 0)
        model.service()
        self.assertEqual(len(model.emitted), 1)

    def test_filtered_trigger_survives_full_fifo_and_records_all_reasons(self):
        def match(event):
            if event.kind == "USER_EVENT" and event.fields["value"] == 99:
                return "user"
            if event.kind == "TRAP":
                return "trap"
            return None

        model = self.model(keep=lambda event: not (event.kind == "USER_EVENT"
                           and event.fields["value"] == 99), match=match)
        for tick in range(16):
            model.step(tick, [observation("USER_EVENT", tick)])
        model.step(16, [observation("USER_EVENT", 99), observation("TRAP")])
        self.assertEqual(model.trigger["tick"], 16)
        self.assertEqual(tuple(model.trigger["matches"]), ((2, 0, "trap"), (3, 0, "user")))
        self.assertEqual(tuple(model.trigger["primary"]), (2, 0, "trap"))
        self.assertEqual(model.stats[(3, 0)]["filtered"], 1)
        self.assertEqual(model.stats[(3, 0)]["ingress_dropped"], 0)
        self.assertFalse(model.complete)
        self.drain_events(model, 17)
        self.assertTrue(model.complete)

    def test_post_window_zero_one_two_is_inclusive(self):
        for post_ticks in (0, 1, 2):
            with self.subTest(post_ticks=post_ticks):
                model = self.model(post_ticks=post_ticks, match=lambda event: "hit")
                for tick in range(10, 14):
                    model.step(tick, [observation("USER_EVENT", tick)])
                self.assertEqual([event.tick for event in model.queues[3]],
                                 list(range(10, 11 + post_ticks)))
                self.assertEqual(model.stats[(3, 0)]["observed"], post_ticks + 1)
                self.assertEqual(model.trigger["tick"], 10)
                self.assertEqual(model.rejected_cycle_events, 0)
                self.drain_events(model, post_ticks + 1)
                self.assertTrue(model.complete)

    def test_expired_window_excludes_sequence_exhaustion(self):
        model = self.model(counter_bits=2, post_ticks=1, match=lambda event: "hit")
        for tick in (0, 1, 2):
            model.step(tick, [observation("BUS_RESP"), observation("BUS_REQ")])
        self.assertEqual(model.stats[(1, 0)]["observed"], 4)
        self.assertEqual(model.rejected_cycle_events, 0)
        self.assertNotEqual(model.stop_reason, "sequence_exhausted")

    def test_completed_post_events_keep_reserved_storage_credit(self):
        model = self.model(post_ticks=1000, match=lambda event: "hit")
        for tick in range(89):
            model.step(tick, [observation("USER_EVENT", tick)])
            self.drain_events(model, 1)
        self.assertEqual(len(model.emitted), 89)
        self.assertEqual(model.reserved_bytes, 15344)
        model.step(89, [observation("USER_EVENT", 89)])
        self.assertEqual(model.stats[(3, 0)]["capacity_dropped"], 1)
        self.assertEqual(len(model.emitted), 89)
        self.assertEqual(model.reserved_bytes, 15344)
        self.assertEqual(model.peak_reserved_bytes, 15344)
        self.assertTrue(model.complete)
        self.assert_accounting(model)

    def test_capacity_rejection_closes_later_source_bundles(self):
        model = self.model(post_ticks=1000, match=lambda event: "hit")
        for tick in range(88):
            model.step(tick, [observation("USER_EVENT", tick)])
            self.drain_events(model, 1)
        model.step(88, six_events())
        self.assertEqual(len(model.queues[0]), 1)
        for source, expected in ((1, 2), (2, 2), (3, 1)):
            self.assertEqual(model.stats[(source, 0)]["capacity_dropped"], expected)
        self.assertFalse(model.complete)
        self.drain_events(model, 1)
        self.assertTrue(model.complete)

    def test_two_event_capacity_failure_rejects_later_one_event_bundle(self):
        model = self.model(post_ticks=1000, match=lambda event: "hit")
        for tick in range(88):
            model.step(tick, [observation("USER_EVENT", tick)])
            self.drain_events(model, 1)
        model.step(88, [observation("BUS_RESP"), observation("BUS_REQ"), observation("USER_EVENT")])
        self.assertEqual(model.stats[(1, 0)]["capacity_dropped"], 2)
        self.assertEqual(model.stats[(3, 0)]["capacity_dropped"], 1)
        self.assertEqual(model.reserved_bytes, 15216)
        self.assertTrue(model.complete)

    def test_unsafe_reserve_and_wrong_source_count_rejected(self):
        for changes in (dict(pre_pages=24, post_pages=8), dict(source_count=3)):
            with self.subTest(changes=changes):
                config = dict(self.config, **changes)
                with self.assertRaises(ValueError):
                    CaptureModel(config)

    def test_source_reset_discards_partial_work_and_restarts_identity(self):
        model = self.model(post_ticks=100, match=lambda event: "hit")
        model.step(0, [observation("USER_EVENT")])
        for _ in range(15):
            model.service()
        reserved = model.reserved_bytes
        model.source_reset(3)
        self.assertEqual(model.reserved_bytes, reserved - 128)
        self.assertEqual(model.stats[(3, 0)]["reset_discarded"], 1)
        self.assertEqual(model.epochs[3], 1)
        self.assertEqual(model.sequences[3], 0)
        model.step(1, [observation("USER_EVENT", 1)])
        model.service()
        self.assertEqual(len(model.emitted), 0)
        for _ in range(15):
            model.service()
        self.assertEqual(identity(model.emitted[0]), (3, 1, 0, 0, 1, "USER_EVENT"))
        self.assert_accounting(model)

    def test_reset_keeps_completed_output_and_its_reservation(self):
        model = self.model(post_ticks=100, match=lambda event: "hit")
        model.step(0, [observation("USER_EVENT")])
        self.drain_events(model, 1)
        reserved = model.reserved_bytes
        model.source_reset(3)
        self.assertEqual(len(model.emitted), 1)
        self.assertEqual(model.reserved_bytes, reserved)
        self.assertEqual(model.stats[(3, 0)]["reset_discarded"], 0)

    def test_sequence_exhaustion_rejects_whole_cycle(self):
        model = self.model(counter_bits=2)
        for tick in (0, 1):
            model.step(tick, [observation("BUS_REQ"), observation("BUS_RESP")])
        model.step(2, six_events())
        self.assertEqual(model.stop_reason, "sequence_exhausted")
        self.assertEqual(model.rejected_cycle_events, 6)
        self.assertEqual(list(model.sequences), [0, 4, 0, 0])
        self.assertEqual(sum(values["observed"] for values in model.stats.values()), 4)
        self.assertFalse(model.complete)
        self.drain_events(model, 4)
        self.assertTrue(model.complete)

    def test_maximum_tick_is_processed_then_time_exhaustion_drains(self):
        model = self.model(counter_bits=2)
        model.step(3, [observation("USER_EVENT")])
        self.assertEqual(model.stop_reason, "time_exhausted")
        self.assertEqual(model.queues[3][0].tick, 3)
        self.assertFalse(model.complete)
        self.drain_events(model, 1)
        self.assertTrue(model.complete)

    def test_trigger_time_window_overflow_accounts_no_partial_cycle(self):
        model = self.model(counter_bits=2, post_ticks=2, match=lambda event: "hit")
        model.step(2, six_events())
        self.assertEqual(model.stop_reason, "time_window_overflow")
        self.assertEqual(model.rejected_cycle_events, 6)
        self.assertEqual(list(model.sequences), [0, 0, 0, 0])
        self.assertEqual(sum(values["observed"] for values in model.stats.values()), 0)
        self.assertTrue(model.complete)

    def test_epoch_exhaustion_does_not_reuse_epoch(self):
        model = self.model(counter_bits=1)
        model.source_reset(3)
        model.step(0, [observation("USER_EVENT")])
        model.source_reset(3)
        self.assertEqual(model.epochs[3], 1)
        self.assertEqual(model.stop_reason, "epoch_exhausted")

    def test_invalid_cycle_and_callback_results_are_atomic(self):
        for kwargs, events in (
            ({}, [observation("RETIRE"), observation("RETIRE")]),
            ({"keep": lambda event: 1}, six_events()),
            ({"match": lambda event: ""}, six_events()),
            ({"match": lambda event: True}, six_events()),
        ):
            with self.subTest(kwargs=kwargs, events=len(events)):
                model = self.model(**kwargs)
                with self.assertRaises(ValueError):
                    model.step(0, events)
                self.assertEqual(list(model.sequences), [0, 0, 0, 0])
                self.assertEqual([len(queue) for queue in model.queues], [0, 0, 0, 0])
                self.assertEqual(sum(values["observed"] for values in model.stats.values()), 0)

    def test_invalid_ticks_and_constructor_bounds(self):
        for tick in (True, -1, 4, 1.5):
            with self.subTest(tick=tick):
                with self.assertRaises(ValueError):
                    self.model(counter_bits=2).step(tick)
        model = self.model()
        model.step(2)
        for tick in (2, 1):
            with self.assertRaises(ValueError):
                model.step(tick)
        for kwargs in (dict(counter_bits=0), dict(counter_bits=65), dict(counter_bits=True),
                       dict(post_ticks=-1), dict(post_ticks=True)):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.model(**kwargs)

    def test_rejected_input_and_late_callback_failure_allow_same_tick_retry(self):
        state = {"bad_keep": False, "bad_match": False}

        def keep(event):
            return 1 if state["bad_keep"] and event.kind == "USER_EVENT" else True

        def match(event):
            return "" if state["bad_match"] and event.kind == "USER_EVENT" else None

        model = self.model(keep=keep, match=match)
        model.step(0, six_events())
        snapshot = copy.deepcopy(model.summary())
        for mode in ("bad_keep", "bad_match"):
            state[mode] = True
            with self.assertRaises(ValueError):
                model.step(1, six_events(), service=True)
            self.assertEqual(model.summary(), snapshot)
            state[mode] = False
        for tick in (0, -1, True, 1.5):
            with self.assertRaises(ValueError):
                model.step(tick, six_events(), service=True)
            self.assertEqual(model.summary(), snapshot)
        with self.assertRaises(ValueError):
            model.step(1, [observation("RETIRE"), observation("RETIRE")], service=True)
        self.assertEqual(model.summary(), snapshot)
        model.step(1, six_events())
        self.assertEqual(list(model.sequences), [2, 4, 4, 2])
        for _ in range(15):
            model.service()
        self.assertEqual(len(model.emitted), 0)
        model.service()
        self.assertEqual(len(model.emitted), 1)

    def test_first_stop_reason_and_summary_certainty(self):
        model = self.model()
        model.step(0, six_events())
        model.stop("manual")
        model.stop("later")
        self.assertEqual(model.stop_reason, "manual")
        self.assertFalse(model.complete)
        with self.assertRaises(ValueError):
            model.source_reset(0)
        self.drain_events(model, 6)
        self.assertTrue(model.complete)
        summary = model.summary()
        self.assertEqual(summary["certainty"], "aggregate_only")
        self.assertEqual(json.loads(json.dumps(summary))["rejected_cycle_events"], 0)

    def test_seeded_independent_admission_ledger(self):
        source_for = {"RETIRE": 0, "BUS_RESP": 1, "BUS_REQ": 1,
                      "TRAP": 2, "IRQ_ACCEPT": 2, "IRQ_PENDING": 2, "USER_EVENT": 3}
        order = {"RETIRE": 0, "BUS_RESP": 0, "BUS_REQ": 1,
                 "TRAP": 0, "IRQ_ACCEPT": 0, "IRQ_PENDING": 1, "USER_EVENT": 0}
        key_for = {"RETIRE": "pc", "BUS_RESP": "transaction", "BUS_REQ": "transaction",
                   "TRAP": "cause", "IRQ_ACCEPT": "cause", "IRQ_PENDING": "current",
                   "USER_EVENT": "value"}

        def keep(event):
            return event.fields[key_for[event.kind]] % 5 != 0

        for seed in range(12):
            with self.subTest(seed=seed):
                rng = random.Random(seed)
                model = self.model(keep=keep)
                ledger = [[] for _ in range(4)]
                sequences = [0] * 4
                epochs = [0] * 4
                blank = dict(observed=0, filtered=0, admitted=0, ingress_dropped=0,
                             fifo_dropped=0, capacity_dropped=0, reset_discarded=0)
                totals = {(source, 0): dict(blank) for source in range(4)}
                completed = []
                last_completed = 3
                for tick in range(160):
                    if rng.randrange(23) == 0:
                        source = rng.randrange(4)
                        totals[(source, epochs[source])]["reset_discarded"] += len(ledger[source])
                        epochs[source] += 1
                        sequences[source] = 0
                        ledger[source] = []
                        totals[(source, epochs[source])] = dict(blank)
                        model.source_reset(source)
                    kinds = [kind for kind in ("RETIRE", "BUS_RESP", "BUS_REQ", "IRQ_PENDING", "USER_EVENT")
                             if rng.randrange(3)]
                    boundary = rng.choice((None, "TRAP", "IRQ_ACCEPT"))
                    if boundary:
                        kinds.append(boundary)
                    events = [observation(kind, rng.randrange(100)) for kind in kinds]
                    rng.shuffle(events)
                    for source in range(4):
                        counts = totals[(source, epochs[source])]
                        group = sorted((event for event in events if source_for[event.kind] == source),
                                       key=lambda event: order[event.kind])
                        eligible = []
                        for lane, event in enumerate(group):
                            item = (source, epochs[source], sequences[source], lane, tick, event.kind)
                            sequences[source] += 1
                            counts["observed"] += 1
                            if keep(event):
                                eligible.append(item)
                            else:
                                counts["filtered"] += 1
                        if len(eligible) <= 16 - len(ledger[source]):
                            ledger[source].extend(eligible)
                            counts["admitted"] += len(eligible)
                        else:
                            counts["ingress_dropped"] += len(eligible)
                            counts["fifo_dropped"] += len(eligible)
                    model.step(tick, events)
                    for _ in range(rng.randrange(3)):
                        candidates = [(last_completed + distance) % 4 for distance in range(1, 5)]
                        available = next((source for source in candidates if ledger[source]), None)
                        if available is not None:
                            completed.append(ledger[available].pop(0))
                            last_completed = available
                        self.drain_events(model, 1)
                    self.assertEqual([[identity(event) for event in queue] for queue in model.queues], ledger)
                    self.assertEqual([identity(event) for event in model.emitted], completed)
                    self.assertEqual(list(model.sequences), sequences)
                    self.assertEqual(list(model.epochs), epochs)
                    for key, expected in totals.items():
                        actual = model.stats.get(key, {})
                        self.assertEqual({field: actual.get(field, 0) for field in expected}, expected)
                    self.assert_accounting(model)


if __name__ == "__main__":
    unittest.main()

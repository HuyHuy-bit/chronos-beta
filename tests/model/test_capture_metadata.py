import copy
import json
import unittest

from model.chronos.capture_metadata import COUNTERS, CaptureMetadata, validate_metadata


NAMES = ("observed", "filtered", "admitted", "ingress_dropped", "reset_discarded",
         "fifo_dropped", "capacity_dropped", "storage_discarded")


def metadata_fixture():
    return {
        "counter_bits": 64,
        "journal_capacity": 4,
        "sources": [
            {"source": source, "counters": {name: 0 for name in NAMES}, "saturated": [],
             "journal_overflow": False, "ranges": []}
            for source in range(4)
        ],
        "trigger": None,
    }


def interval_fixture():
    return {"epoch": 2, "first_sequence": 4, "last_sequence": 6, "first_tick": 11,
            "last_tick": 12, "reason": "fifo", "count": 3}


class MetadataWriterTests(unittest.TestCase):
    def test_initial_state_and_counter_names(self):
        self.assertEqual(COUNTERS, NAMES)
        self.assertEqual(CaptureMetadata().snapshot(), metadata_fixture())
        self.assertEqual(json.loads(json.dumps(CaptureMetadata().snapshot())), metadata_fixture())

    def test_constructor_bounds(self):
        for bits in (1, 64):
            for capacity in (0, 16):
                self.assertEqual(CaptureMetadata(counter_bits=bits, journal_capacity=capacity).snapshot()["counter_bits"], bits)
        for name, values in (("counter_bits", (0, 65, True, 1.0, None)),
                             ("journal_capacity", (-1, 17, False, 4.0, None))):
            for value in values:
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    CaptureMetadata(**{name: value})

    def test_exact_boundary_and_independent_sticky_saturation(self):
        metadata = CaptureMetadata(counter_bits=2)
        metadata.add(1, observed=3, filtered=3)
        self.assertEqual(metadata.snapshot()["sources"][1]["saturated"], [])
        validate_metadata(metadata.snapshot())
        metadata.add(1, observed=0)
        self.assertEqual(metadata.snapshot()["sources"][1]["saturated"], [])
        metadata.add(1, filtered=1, observed=1)
        state = metadata.snapshot()["sources"][1]
        self.assertEqual(state["saturated"], ["observed", "filtered"])
        self.assertEqual((state["counters"]["observed"], state["counters"]["filtered"]), (3, 3))
        metadata.add(1, admitted=3, storage_discarded=1 << 100)
        metadata.add(1, observed=0, filtered=0)
        state = metadata.snapshot()["sources"][1]
        self.assertEqual(state["saturated"], ["observed", "filtered", "storage_discarded"])
        self.assertEqual(state["counters"]["admitted"], 3)
        self.assertEqual(metadata.snapshot()["sources"][0], metadata_fixture()["sources"][0])
        validate_metadata(metadata.snapshot())

    def test_add_validates_whole_call_before_mutation(self):
        metadata = CaptureMetadata()
        metadata.add(0, observed=2, admitted=2)
        before = metadata.snapshot()
        for deltas in ({"observed": 1, "unknown": 0}, {"observed": 1, "filtered": -1},
                       {"observed": 1, "admitted": True}, {"observed": 1.0}, {"observed": None}):
            with self.subTest(deltas=deltas), self.assertRaises(ValueError):
                metadata.add(0, **deltas)
            self.assertEqual(metadata.snapshot(), before)
        for source in (-1, 4, True, 1.0, None):
            with self.subTest(source=source), self.assertRaises(ValueError):
                metadata.add(source, observed=1)
            self.assertEqual(metadata.snapshot(), before)

    def test_mark_does_not_update_counters_and_coalesces_exact_interval(self):
        metadata = CaptureMetadata(journal_capacity=1)
        for sequence, tick in ((4, 11), (5, 11), (6, 12)):
            metadata.mark(1, epoch=2, sequence=sequence, tick=tick, reason="fifo")
        state = metadata.snapshot()["sources"][1]
        self.assertEqual(state["ranges"], [interval_fixture()])
        self.assertEqual(state["counters"], dict.fromkeys(NAMES, 0))
        self.assertFalse(state["journal_overflow"])
        with self.assertRaises(ValueError):
            validate_metadata(metadata.snapshot())
        metadata.add(1, observed=3, ingress_dropped=3, fifo_dropped=3)
        validate_metadata(metadata.snapshot())

    def test_full_journal_overflow_stops_every_later_mark(self):
        metadata = CaptureMetadata(journal_capacity=1)
        metadata.mark(0, epoch=0, sequence=0, tick=1, reason="filtered")
        before = metadata.snapshot()["sources"][0]["ranges"]
        metadata.mark(0, epoch=0, sequence=2, tick=3, reason="fifo")
        metadata.mark(0, epoch=0, sequence=1, tick=2, reason="filtered")
        state = metadata.snapshot()["sources"][0]
        self.assertTrue(state["journal_overflow"])
        self.assertEqual(state["ranges"], before)
        self.assertEqual(state["counters"], dict.fromkeys(NAMES, 0))

    def test_zero_capacity_and_independent_source_journals(self):
        metadata = CaptureMetadata(journal_capacity=0)
        metadata.mark(2, epoch=0, sequence=0, tick=0, reason="capacity")
        for source, state in enumerate(metadata.snapshot()["sources"]):
            self.assertEqual(state["ranges"], [])
            self.assertEqual(state["journal_overflow"], source == 2)
        validate_metadata(metadata.snapshot())
        metadata = CaptureMetadata(journal_capacity=16)
        for source in range(4):
            for sequence in range(0, 32, 2):
                metadata.mark(source, epoch=0, sequence=sequence, tick=sequence, reason="filtered")
        self.assertEqual([len(state["ranges"]) for state in metadata.snapshot()["sources"]], [16] * 4)
        metadata.mark(0, epoch=0, sequence=32, tick=32, reason="filtered")
        self.assertEqual([state["journal_overflow"] for state in metadata.snapshot()["sources"]], [True, False, False, False])

    def test_merge_requires_last_entry_epoch_reason_sequence_and_time(self):
        cases = (
            {"epoch": 1, "sequence": 5, "tick": 11, "reason": "fifo"},
            {"epoch": 0, "sequence": 5, "tick": 11, "reason": "capacity"},
            {"epoch": 0, "sequence": 6, "tick": 11, "reason": "fifo"},
            {"epoch": 0, "sequence": 5, "tick": 9, "reason": "fifo"},
            {"epoch": 0, "sequence": 4, "tick": 11, "reason": "fifo"},
        )
        for mark in cases:
            with self.subTest(mark=mark):
                metadata = CaptureMetadata()
                metadata.mark(0, epoch=0, sequence=4, tick=10, reason="fifo")
                metadata.mark(0, **mark)
                self.assertEqual(len(metadata.snapshot()["sources"][0]["ranges"]), 2)

    def test_reset_discard_order_never_bridges_later_observations(self):
        metadata = CaptureMetadata()
        metadata.mark(1, epoch=0, sequence=0, tick=1, reason="reset_discarded")
        metadata.mark(1, epoch=0, sequence=10, tick=9, reason="fifo")
        metadata.mark(1, epoch=0, sequence=1, tick=2, reason="reset_discarded")
        metadata.mark(1, epoch=0, sequence=2, tick=3, reason="reset_discarded")
        ranges = metadata.snapshot()["sources"][1]["ranges"]
        self.assertEqual([(entry["first_sequence"], entry["last_sequence"], entry["reason"]) for entry in ranges],
                         [(0, 0, "reset_discarded"), (10, 10, "fifo"), (1, 2, "reset_discarded")])
        metadata.add(1, observed=4, admitted=3, ingress_dropped=1, fifo_dropped=1, reset_discarded=3)
        validate_metadata(metadata.snapshot())

    def test_mark_validation_is_atomic_even_after_overflow(self):
        metadata = CaptureMetadata(journal_capacity=0)
        base = {"epoch": 0, "sequence": 0, "tick": 0, "reason": "filtered"}
        metadata.mark(0, **base)
        before = metadata.snapshot()
        for key in ("epoch", "sequence", "tick"):
            for value in (-1, 1 << 64, True, 0.0, None):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    metadata.mark(0, **dict(base, **{key: value}))
                self.assertEqual(metadata.snapshot(), before)
        for reason in ("", "unknown", None, [], True):
            with self.subTest(reason=reason), self.assertRaises(ValueError):
                metadata.mark(0, **dict(base, reason=reason))
        metadata = CaptureMetadata()
        metadata.mark(3, epoch=(1 << 64) - 1, sequence=(1 << 64) - 1,
                      tick=(1 << 64) - 1, reason="storage_discarded")
        metadata.add(3, observed=1, admitted=1, storage_discarded=1)
        validate_metadata(metadata.snapshot())

    def test_trigger_first_descriptor_is_preserved_and_copied(self):
        metadata = CaptureMetadata()
        matches = [[0, 0, "pc"], [1, 0, "response"], [1, 1, "request"],
                   [2, 0, "trap"], [2, 1, "pending"], [3, 0, "user"]]
        metadata.latch_trigger((1 << 64) - 1, matches)
        expected = {"tick": (1 << 64) - 1,
                    "matches": [{"source": source, "lane": lane, "reason": reason} for source, lane, reason in matches],
                    "primary": {"source": 0, "lane": 0, "reason": "pc"}, "software": False}
        matches[0][2] = "changed"
        metadata.latch_trigger(0, ((3, 0, "later"),))
        self.assertEqual(metadata.snapshot()["trigger"], expected)
        validate_metadata(metadata.snapshot())

    def test_trigger_validation_even_after_latching(self):
        metadata = CaptureMetadata()
        metadata.latch_trigger(0, ((0, 0, "first"),))
        before = metadata.snapshot()
        invalid = ([], (), None, "x", [[0, 0]], [[True, 0, "x"]], [[0, True, "x"]],
                   [[4, 0, "x"]], [[0, 1, "x"]], [[3, 1, "x"]], [[1, 2, "x"]],
                   [[1, 0, "x"], [0, 0, "y"]], [[1, 0, "x"], [1, 0, "y"]],
                   [[0, 0, ""]], [[0, 0, "x" * 65]], [[0, 0, "é" * 33]],
                   [[0, 0, "\ud800"]], [[0, 0, None]], [[0, 0, "x"]] * 7)
        for matches in invalid:
            with self.subTest(matches=matches), self.assertRaises(ValueError):
                metadata.latch_trigger(1, matches)
            self.assertEqual(metadata.snapshot(), before)
        for tick in (-1, 1 << 64, True, 0.0):
            with self.subTest(tick=tick), self.assertRaises(ValueError):
                metadata.latch_trigger(tick, ((0, 0, "x"),))
        for software in (None, 1, "yes"):
            with self.subTest(software=software), self.assertRaises(ValueError):
                metadata.latch_trigger(1, (), software=software)
        self.assertEqual(metadata.snapshot(), before)
        exact = CaptureMetadata()
        exact.latch_trigger(0, ((0, 0, "é" * 32),))
        validate_metadata(exact.snapshot())

    def test_software_trigger_may_have_no_hardware_match(self):
        metadata = CaptureMetadata()
        metadata.latch_trigger(5, (), software=True)
        self.assertEqual(metadata.snapshot()["trigger"],
                         {"tick": 5, "matches": [], "primary": None, "software": True})
        validate_metadata(metadata.snapshot())
        merged = CaptureMetadata()
        merged.latch_trigger(5, ((1, 1, "bus"),), software=True)
        self.assertEqual(merged.snapshot()["trigger"]["primary"], {"source": 1, "lane": 1, "reason": "bus"})
        validate_metadata(merged.snapshot())

    def test_snapshot_is_defensive_at_every_nested_boundary(self):
        metadata = CaptureMetadata(counter_bits=1)
        metadata.add(0, observed=2, admitted=2)
        metadata.mark(0, epoch=0, sequence=0, tick=0, reason="filtered")
        metadata.latch_trigger(0, ((0, 0, "pc"),))
        expected = metadata.snapshot()
        snapshot = metadata.snapshot()
        snapshot["sources"][0]["counters"]["observed"] = 0
        snapshot["sources"][0]["saturated"].clear()
        snapshot["sources"][0]["ranges"][0]["count"] = 99
        snapshot["sources"][1]["ranges"].append({})
        snapshot["trigger"]["matches"][0]["reason"] = "changed"
        snapshot["trigger"]["primary"]["reason"] = "changed"
        snapshot["sources"].clear()
        self.assertEqual(metadata.snapshot(), expected)


class MetadataValidationTests(unittest.TestCase):
    def setUp(self):
        self.value = metadata_fixture()
        self.value["sources"][0]["counters"] = {
            "observed": 10, "filtered": 2, "admitted": 5, "ingress_dropped": 3,
            "reset_discarded": 1, "fifo_dropped": 2, "capacity_dropped": 1, "storage_discarded": 1,
        }
        self.value["sources"][1]["ranges"] = [interval_fixture()]
        self.value["sources"][1]["counters"].update(observed=3, ingress_dropped=3, fifo_dropped=3)

    def invalid(self, path, replacement):
        value = copy.deepcopy(self.value)
        owner = value
        for key in path[:-1]:
            owner = owner[key]
        owner[path[-1]] = replacement
        with self.assertRaises(ValueError):
            validate_metadata(value)

    def test_literal_shape_accounting_and_return_identity(self):
        self.assertIs(validate_metadata(self.value), self.value)
        self.value["sources"][1]["journal_overflow"] = True
        self.assertIs(validate_metadata(self.value), self.value)

    def test_missing_and_extra_keys_at_every_structure(self):
        for path in ((), ("sources", 0), ("sources", 0, "counters"), ("sources", 1, "ranges", 0)):
            owner = self.value
            for key in path:
                owner = owner[key]
            for key in owner:
                candidate = copy.deepcopy(self.value)
                target = candidate
                for part in path:
                    target = target[part]
                del target[key]
                with self.subTest(path=path, missing=key), self.assertRaises(ValueError):
                    validate_metadata(candidate)
            candidate = copy.deepcopy(self.value)
            target = candidate
            for part in path:
                target = target[part]
            target["extra"] = None
            with self.subTest(path=path, extra=True), self.assertRaises(ValueError):
                validate_metadata(candidate)

    def test_strict_types_sizes_and_source_order(self):
        for value in (None, [], True):
            with self.assertRaises(ValueError):
                validate_metadata(value)
        cases = [(("counter_bits",), value) for value in (0, 65, True, 64.0)]
        cases += [(("journal_capacity",), value) for value in (-1, 17, False, 4.0)]
        cases += [(("sources",), value) for value in ((), [], self.value["sources"][:3], self.value["sources"] + [{}])]
        cases += [(("sources", 0, "source"), value) for value in (1, True, 0.0, -1, 4)]
        cases += [(("sources", 0, "journal_overflow"), value) for value in (0, 1, None, "false")]
        cases += [(("sources", 1, "ranges"), value) for value in ((), {}, [interval_fixture()] * 5)]
        cases += [(("sources", 0, "counters", "observed"), value) for value in (True, -1, 1 << 64, 10.0)]
        for path, value in cases:
            with self.subTest(path=path, value=value):
                self.invalid(path, value)

    def test_interval_types_bounds_and_arithmetic(self):
        path = ("sources", 1, "ranges", 0)
        for name in ("epoch", "first_sequence", "last_sequence", "first_tick", "last_tick", "count"):
            for value in (True, -1, 1 << 64, 1.0, None):
                with self.subTest(field=name, value=value):
                    self.invalid(path + (name,), value)
        for name, value in (("count", 0), ("count", 2), ("last_sequence", 3),
                            ("last_tick", 10), ("reason", "mixed"), ("reason", [])):
            with self.subTest(field=name, value=value):
                self.invalid(path + (name,), value)
        self.invalid(("journal_capacity",), 0)

    def test_saturated_names_are_ordered_unique_known_and_at_limit(self):
        for names in ((), None, ["unknown"], [True], [[]], ["observed"], ["observed"] * 9):
            with self.subTest(names=names):
                self.invalid(("sources", 0, "saturated"), names)
        value = metadata_fixture()
        value["counter_bits"] = 2
        value["sources"][0]["counters"].update(observed=3, filtered=3)
        validate_metadata(value)
        for names in (["observed", "observed"], ["filtered", "observed"]):
            value["sources"][0]["saturated"] = names
            with self.assertRaises(ValueError):
                validate_metadata(value)
        value["sources"][0]["saturated"] = ["observed", "filtered"]
        validate_metadata(value)

    def test_exact_accounting_relations_are_independent(self):
        for name, value in (("observed", 11), ("fifo_dropped", 3),
                            ("reset_discarded", 5), ("storage_discarded", 5)):
            with self.subTest(name=name):
                self.invalid(("sources", 0, "counters", name), value)
        self.value["sources"][0]["counters"].update(reset_discarded=2, storage_discarded=3)
        validate_metadata(self.value)

    def test_saturation_skips_only_affected_equations(self):
        for name in ("observed", "filtered", "admitted", "ingress_dropped", "fifo_dropped",
                     "capacity_dropped", "reset_discarded", "storage_discarded"):
            with self.subTest(name=name):
                value = copy.deepcopy(self.value)
                value["counter_bits"] = 4
                state = value["sources"][0]
                state["counters"][name] = 15
                state["saturated"] = [name]
                validate_metadata(value)
        value = copy.deepcopy(self.value)
        value["counter_bits"] = 4
        state = value["sources"][0]
        state["counters"]["observed"] = 15
        state["saturated"] = ["observed"]
        state["counters"]["fifo_dropped"] = 3
        with self.assertRaises(ValueError):
            validate_metadata(value)
        state["counters"]["fifo_dropped"] = 2
        state["counters"]["storage_discarded"] = 5
        with self.assertRaises(ValueError):
            validate_metadata(value)

    def test_classified_counts_cannot_exceed_corresponding_exact_counter(self):
        for reason, counter in (("filtered", "filtered"), ("fifo", "fifo_dropped"),
                                ("capacity", "capacity_dropped"), ("reset_discarded", "reset_discarded"),
                                ("storage_discarded", "storage_discarded")):
            with self.subTest(reason=reason):
                value = copy.deepcopy(self.value)
                value["counter_bits"] = 4
                state = value["sources"][0]
                count = state["counters"][counter] + 1
                state["ranges"] = [dict(interval_fixture(), first_sequence=0, last_sequence=count - 1,
                                        count=count, reason=reason)]
                with self.assertRaises(ValueError):
                    validate_metadata(value)
                state["counters"][counter] = 15
                state["saturated"] = [counter]
                state["ranges"][0].update(last_sequence=15, count=16)
                validate_metadata(value)

    def test_classified_totals_sum_separate_ranges(self):
        extra = dict(interval_fixture(), first_sequence=8, last_sequence=8, count=1)
        self.value["sources"][1]["ranges"].append(extra)
        with self.assertRaises(ValueError):
            validate_metadata(self.value)
        self.value["sources"][1]["counters"].update(observed=4, ingress_dropped=4, fifo_dropped=4)
        validate_metadata(self.value)

    def test_overlapping_identities_reject_across_reasons_but_epochs_are_distinct(self):
        state = self.value["sources"][1]
        state["counters"].update(observed=4, filtered=1)
        state["ranges"].insert(0, dict(interval_fixture(), first_sequence=6, last_sequence=6,
                                     count=1, reason="filtered"))
        with self.assertRaises(ValueError):
            validate_metadata(self.value)
        state["ranges"][0]["epoch"] = 3
        validate_metadata(self.value)
        state["ranges"][0].update(epoch=2, first_sequence=7, last_sequence=7)
        validate_metadata(self.value)

    def test_serialized_trigger_is_strict_and_primary_is_not_bool_equivalent(self):
        self.value["trigger"] = {"tick": 3, "matches": [{"source": 1, "lane": 0, "reason": "bus"}],
                                 "primary": {"source": 1, "lane": 0, "reason": "bus"}, "software": False}
        validate_metadata(self.value)
        for path, replacement in (
            (("trigger", "tick"), True), (("trigger", "matches"), ()),
            (("trigger", "matches"), []), (("trigger", "matches", 0, "lane"), True),
            (("trigger", "matches", 0, "reason"), "\ud800"),
            (("trigger", "primary", "source"), True), (("trigger", "primary", "reason"), "other"),
            (("trigger", "primary"), None), (("trigger", "matches", 0), {"source": 1, "lane": 0}),
            (("trigger",), {"tick": 3, "matches": [], "primary": {}, "extra": 0}),
            (("trigger", "software"), 1), (("trigger", "software"), None),
            (("trigger",), {"tick": 3, "matches": [], "primary": None}),
            (("trigger",), {"tick": 3, "matches": [], "primary": None, "software": False}),
            (("trigger",), {"tick": 3, "matches": [{"source": 1, "lane": 0, "reason": "bus"}],
                            "primary": None, "software": True}),
        ):
            with self.subTest(path=path, replacement=replacement):
                self.invalid(path, replacement)


if __name__ == "__main__":
    unittest.main()

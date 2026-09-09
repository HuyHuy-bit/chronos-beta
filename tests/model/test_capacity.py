import json
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

from model.chronos.capacity import Inventory, completion_budget, default_inventory


BASELINE = Path(__file__).resolve().parents[2] / "configs" / "baseline.json"


class CapacityTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads(BASELINE.read_text())

    def test_baseline_completion_reserve(self):
        inventory = default_inventory(self.config)
        self.assertEqual(inventory, Inventory((16, 16, 16, 16), 4, 1, 1024, 2, 0))
        budget = completion_budget(self.config, inventory)
        self.assertEqual(budget, {
            "payload_bytes": 15360,
            "event_bytes": 8192,
            "overhead_bytes": 3952,
            "required_bytes": 12144,
            "margin_bytes": 3216,
            "safe": True,
            "terms": {
                "queue_events": 8192,
                "pending_runs": 512,
                "skid_events": 128,
                "builder_bytes": 1024,
                "control_records": 256,
                "restart_bytes": 0,
                "tail_waste": 2032,
            },
        })
        self.assertEqual(json.loads(json.dumps(budget)), budget)

    def test_eight_page_reserve_is_unsafe(self):
        self.config.update(pre_pages=24, post_pages=8)
        budget = completion_budget(self.config, default_inventory(self.config))
        self.assertEqual(budget["payload_bytes"], 7680)
        self.assertEqual(budget["required_bytes"], 11128)
        self.assertEqual(budget["overhead_bytes"], 2936)
        self.assertEqual(budget["margin_bytes"], -3448)
        self.assertFalse(budget["safe"])

    def test_restart_cost_can_exhaust_safe_margin(self):
        inventory = default_inventory(self.config)
        for restart, required, margin, safe in (
            (3216, 15360, 0, True),
            (3217, 15361, -1, False),
        ):
            with self.subTest(restart=restart):
                budget = completion_budget(self.config, replace(inventory, restart_bytes=restart))
                self.assertEqual(budget["required_bytes"], required)
                self.assertEqual(budget["margin_bytes"], margin)
                self.assertEqual(budget["safe"], safe)

    def test_independent_inventory_costs(self):
        inventory = Inventory((1, 2, 3, 4), 2, 3, 17, 1, 29)
        budget = completion_budget(self.config, inventory)
        self.assertEqual(budget["event_bytes"], 1280)
        self.assertEqual(budget["overhead_bytes"], 2846)
        self.assertEqual(budget["required_bytes"], 4126)
        self.assertEqual(budget["margin_bytes"], 11234)

    def test_empty_inventory_still_reserves_tail_waste(self):
        budget = completion_budget(self.config, Inventory((0, 0, 0, 0)))
        self.assertEqual(budget["event_bytes"], 0)
        self.assertEqual(budget["required_bytes"], 2032)
        self.assertEqual(budget["margin_bytes"], 13328)

    def test_other_valid_geometries(self):
        for page, sram, depth, payload, required, margin in (
            (256, 8192, 2, 3072, 4208, -1136),
            (4096, 131072, 16, 64512, 15216, 49296),
        ):
            with self.subTest(page_bytes=page):
                config = dict(self.config, page_bytes=page, sram_bytes=sram, fifo_depth=depth)
                budget = completion_budget(config, default_inventory(config))
                self.assertEqual(budget["payload_bytes"], payload)
                self.assertEqual(budget["required_bytes"], required)
                self.assertEqual(budget["margin_bytes"], margin)
                self.assertEqual(budget["safe"], margin >= 0)

    def test_inventory_is_immutable(self):
        inventory = Inventory((0, 0, 0, 0))
        with self.assertRaises(FrozenInstanceError):
            inventory.restart_bytes = 1

    def test_reject_invalid_inventory(self):
        for queues in ((), (0,) * 3, (0,) * 5, [0] * 4, None, "0000"):
            with self.subTest(queues=queues), self.assertRaises(ValueError):
                Inventory(queues)
        for value in (-1, True, False, 1.0, "1", None):
            for lane in range(4):
                queues = [0] * 4
                queues[lane] = value
                with self.subTest(lane=lane, value=value), self.assertRaises(ValueError):
                    Inventory(tuple(queues))
            for field in ("pending_runs", "skid_events", "builder_bytes", "control_records", "restart_bytes"):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    Inventory((0, 0, 0, 0), **{field: value})
        for inventory in (None, {}, (0, 0, 0, 0)):
            with self.subTest(inventory=inventory), self.assertRaises(ValueError):
                completion_budget(self.config, inventory)

    def test_geometry_is_validated_by_both_entry_points(self):
        for change in (
            {"source_count": 3},
            {"fifo_depth": 3},
            {"post_pages": 8},
            {"page_bytes": 4096},
            {"page_header_bytes": 0},
            {"post_pages": True},
        ):
            config = dict(self.config, **change)
            with self.subTest(change=change):
                with self.assertRaises(ValueError):
                    default_inventory(config)
                with self.assertRaises(ValueError):
                    completion_budget(config, Inventory((0, 0, 0, 0)))


if __name__ == "__main__":
    unittest.main()

from itertools import combinations
import unittest
from unittest.mock import patch

from model.chronos.capture_session import decode_capture
from model.chronos.controller import COMMANDS, STATES, CaptureController
from model.chronos.events import Observation
from model.chronos.retention import StorageError
from scripts.config import ROOT, read_json


def user(value=0):
    return Observation("USER_EVENT", {"value": value})


def request(value=0):
    return Observation("BUS_REQ", dict(transaction=value, address=value, write=False, data=value, mask=15))


LEGAL = {
    "trace_reset": dict.fromkeys(STATES, "accepted"),
    "clear": {"DISABLED": "accepted", "FROZEN": "accepted"},
    "configure": {"DISABLED": "accepted", "FROZEN": "accepted"},
    "arm": {"DISABLED": "accepted"},
    "stop": {"ARMED": "accepted", "POST_TRIGGER": "accepted", "DRAINING": "ignored"},
    "reset_source": {"ARMED": "accepted", "POST_TRIGGER": "accepted"},
    "software_trigger": {"ARMED": "accepted", "POST_TRIGGER": "ignored"},
}
DOMINANT = {"trace_reset", "clear", "arm", "stop"}
NEXT = {"trace_reset": "DISABLED", "clear": "CLEARING", "arm": "ARMED", "stop": "FROZEN",
        "software_trigger": "POST_TRIGGER"}


def oracle(state, commands):
    outcomes, blocked, following = {}, False, state
    for name in COMMANDS:
        if name not in commands:
            continue
        if blocked:
            outcomes[name] = "superseded"
            continue
        outcomes[name] = LEGAL[name].get(state, "rejected")
        if outcomes[name] == "accepted":
            following = NEXT.get(name, following)
            blocked = name in DOMINANT
    return outcomes, following


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.config = read_json(ROOT / "configs/baseline.json")
        self.tick = 0

    def settings(self, **changes):
        return dict(dict(config=dict(self.config), post_ticks=1000, drain_limit=1 << 20), **changes)

    def cycle(self, controller, *observations, **commands):
        self.tick += 1
        return controller.cycle(self.tick, observations, **commands)

    def armed(self, **changes):
        controller = CaptureController()
        self.assertEqual(self.cycle(controller, configure=self.settings(**changes), arm=True)["state"], "ARMED")
        return controller

    def reach(self, state):
        if state == "DISABLED":
            controller = CaptureController()
            self.cycle(controller, configure=self.settings())
        elif state == "CLEARING":
            controller = self.reach("FROZEN")
            self.cycle(controller, clear=True)
        else:
            controller = self.armed()
            if state == "POST_TRIGGER":
                self.cycle(controller, software_trigger=True)
            elif state == "DRAINING":
                self.cycle(controller, user(1), request(1))
                self.cycle(controller, stop=True)
            elif state == "FROZEN":
                self.cycle(controller, stop=True)
        self.assertEqual(controller.state, state)
        return controller

    def drain(self, controller, limit=4096):
        for _ in range(limit):
            if controller.state == "FROZEN":
                break
            self.cycle(controller, service=True)
        self.assertEqual(controller.state, "FROZEN")
        session, wire = controller.read()
        return session, decode_capture(wire)

    def test_every_same_cycle_command_subset_in_every_state_matches_oracle(self):
        cases = 0
        for state in STATES:
            for size in range(len(COMMANDS) + 1):
                for commands in combinations(COMMANDS, size):
                    controller = self.reach(state)
                    before = controller.status()
                    epoch = None if controller.capture is None else controller.capture.model.epochs[3]
                    arguments = {name: True for name in commands if name not in ("configure", "reset_source")}
                    if "configure" in commands:
                        arguments["configure"] = self.settings()
                    if "reset_source" in commands:
                        arguments["reset_source"] = 3
                    result = self.cycle(controller, **arguments)
                    outcomes, following = oracle(state, commands)
                    with self.subTest(state=state, commands=commands):
                        self.assertEqual(result["outcomes"], outcomes)
                        self.assertEqual(result["state"], following)
                        self.assertEqual(controller.state, following)
                        self.assertEqual(set(result["reasons"]), {name for name, outcome in outcomes.items()
                                                                  if outcome == "rejected"})
                        after = controller.status()
                        self.assertEqual(after["config_tag"] - before["config_tag"],
                                         outcomes.get("configure") == "accepted")
                        self.assertEqual(after["session_id"] - before["session_id"], outcomes.get("arm") == "accepted")
                        if outcomes.get("reset_source") == "accepted":
                            self.assertEqual(controller.capture.model.epochs[3], epoch + 1)
                        elif epoch is not None and controller.capture is not None:
                            self.assertEqual(controller.capture.model.epochs[3], epoch)
                        if outcomes.get("software_trigger") == "accepted":
                            self.assertTrue(after["trigger"]["software"])
                        elif state == "ARMED" and controller.capture is not None:
                            self.assertIsNone(after["trigger"])
                        if following == "FROZEN":
                            self.assertEqual(controller.read(), controller.read())
                        else:
                            with self.assertRaises(ValueError):
                                controller.read()
                    cases += 1
        self.assertEqual(cases, 6 * 128)

    def test_hardware_trigger_post_window_lifecycle_and_identical_reads(self):
        controller = self.armed(post_ticks=2, triggers=[dict(kinds=["USER_EVENT"], mode="equal", value=7,
                                                             mask=0xFFFFFFFF)])
        self.cycle(controller, request(1), service=True)
        self.assertEqual(self.cycle(controller, user(7), service=True)["state"], "POST_TRIGGER")
        trigger_tick = self.tick
        self.cycle(controller, request(2), service=True)
        self.assertEqual(self.cycle(controller, request(3), service=True)["state"], "DRAINING")
        self.cycle(controller, request(4), service=True)
        session, decoded = self.drain(controller)
        self.assertEqual(controller.read(), controller.read())
        metadata = decoded["metadata"]
        self.assertEqual((session, metadata["session_id"], metadata["config_tag"]), (1, 1, 1))
        self.assertEqual(metadata["terminal"]["reason"], "post_window")
        self.assertTrue(metadata["terminal"]["drain_complete"])
        match = dict(source=3, lane=0, reason="slot0")
        self.assertEqual(metadata["capture"]["trigger"],
                         dict(tick=trigger_tick, matches=[match], primary=match, software=False))
        self.assertEqual(max(event.tick for event in decoded["events"]), trigger_tick + 2)
        self.assertEqual(metadata["capture"]["sources"][1]["counters"]["observed"], 3)

    def test_software_trigger_alone_merged_and_after_first_trigger(self):
        controller = self.armed(post_ticks=0)
        self.assertEqual(self.cycle(controller, software_trigger=True)["state"], "FROZEN")
        tick = self.tick
        _, decoded = self.drain(controller)
        self.assertEqual(decoded["metadata"]["capture"]["trigger"],
                         dict(tick=tick, matches=[], primary=None, software=True))
        self.assertEqual(decoded["metadata"]["terminal"]["reason"], "post_window")

        every_user = [dict(kinds=["USER_EVENT"], mode="equal", value=0, mask=0)]
        merged = self.armed(triggers=every_user)
        self.cycle(merged, user(1), software_trigger=True)
        self.assertEqual(merged.status()["trigger"], dict(tick=self.tick, matches=((3, 0, "slot0"),),
                                                          primary=(3, 0, "slot0"), software=True))

        later = self.armed(triggers=every_user)
        self.cycle(later, user(1))
        first = later.status()["trigger"]
        result = self.cycle(later, user(2), software_trigger=True)
        self.assertEqual(result["outcomes"], dict(software_trigger="ignored"))
        self.assertEqual(later.status()["trigger"], first)
        self.cycle(later, stop=True)
        _, decoded = self.drain(later)
        self.assertFalse(decoded["metadata"]["capture"]["trigger"]["software"])

    def test_software_trigger_tick_is_the_command_cycle(self):
        controller = self.armed(post_ticks=1)
        self.cycle(controller, request(1), service=True)
        self.cycle(controller, request(2), software_trigger=True, service=True)
        tick = self.tick
        self.cycle(controller, request(3), service=True)
        self.cycle(controller, request(4), service=True)
        _, decoded = self.drain(controller)
        self.assertEqual(decoded["metadata"]["capture"]["trigger"]["tick"], tick)
        self.assertEqual([event.observation.fields["transaction"] for event in decoded["events"]], [1, 2, 3])

    def test_drain_timeout_freezes_incomplete_with_pending_counts(self):
        controller = self.armed(drain_limit=3)
        self.cycle(controller, user(1), request(1))
        self.assertEqual(self.cycle(controller, stop=True)["state"], "DRAINING")
        self.assertEqual(self.cycle(controller)["state"], "DRAINING")
        self.assertEqual(self.cycle(controller, service=True)["state"], "FROZEN")
        self.assertTrue(controller.status()["drain_timeout"])
        terminal = decode_capture(controller.read()[1])["metadata"]["terminal"]
        self.assertEqual((terminal["reason"], terminal["drain_complete"], terminal["pending_events"]),
                         ("manual", False, [0, 1, 0, 1]))

    def test_default_drain_limit_covers_full_queues_at_one_grant_per_cycle(self):
        controller = CaptureController()
        self.cycle(controller, configure=dict(config=dict(self.config), post_ticks=0), arm=True)
        for value in range(20):
            self.cycle(controller, user(value), request(value))
        self.cycle(controller, stop=True, service=True)
        cycles = 1
        while controller.state == "DRAINING":
            self.cycle(controller, service=True)
            cycles += 1
        self.assertFalse(controller.status()["drain_timeout"])
        self.assertEqual(cycles, 2 * 16 * 7)
        self.assertTrue(decode_capture(controller.read()[1])["metadata"]["terminal"]["drain_complete"])

    def test_storage_error_freezes_incomplete_in_the_same_cycle(self):
        controller = self.armed()
        self.cycle(controller, user(1), request(2))
        with patch.object(controller.capture.ring, "append", side_effect=StorageError("post_capacity")):
            for _ in range(16):
                result = self.cycle(controller, service=True)
        self.assertEqual(result["state"], "FROZEN")
        terminal = decode_capture(controller.read()[1])["metadata"]["terminal"]
        self.assertEqual((terminal["reason"], terminal["storage_error"]), ("storage_failure", "post_capacity"))

    def test_clear_scrub_timing_rejects_arm_and_reads_until_disabled(self):
        controller = self.reach("FROZEN")
        history = controller.read()
        cycles = self.config["sram_bytes"] // (self.config["sink_width_bits"] // 8)
        self.assertEqual(self.cycle(controller, clear=True)["state"], "CLEARING")
        self.assertEqual(controller.status()["scrub_remaining"], cycles - 1)
        with self.assertRaises(ValueError):
            controller.read()
        self.assertEqual(self.cycle(controller, arm=True)["outcomes"], dict(arm="rejected"))
        for _ in range(cycles - 3):
            self.cycle(controller)
        self.assertEqual(controller.state, "CLEARING")
        self.assertEqual(self.cycle(controller)["state"], "DISABLED")
        self.assertEqual(decode_capture(history[1])["metadata"]["session_id"], 1)
        self.assertEqual(self.cycle(controller, arm=True)["outcomes"], dict(arm="accepted"))
        self.assertEqual(controller.session_id, 2)

    def test_trace_reset_invalidates_readout_keeps_configuration_and_never_reuses_sessions(self):
        controller = self.reach("FROZEN")
        history = controller.read()
        self.assertEqual(self.cycle(controller, trace_reset=True, arm=True)["outcomes"],
                         dict(trace_reset="accepted", arm="superseded"))
        with self.assertRaises(ValueError):
            controller.read()
        self.assertEqual(self.cycle(controller, arm=True)["outcomes"], dict(arm="accepted"))
        self.cycle(controller, trace_reset=True)
        self.cycle(controller, arm=True)
        self.cycle(controller, stop=True)
        session, wire = controller.read()
        self.assertEqual((history[0], session, controller.config_tag), (1, 3, 1))
        self.assertEqual(decode_capture(wire)["metadata"]["session_id"], 3)

    def test_configuration_is_atomic_and_immutable_while_capturing(self):
        controller = CaptureController()
        self.assertEqual(self.cycle(controller, arm=True)["reasons"], dict(arm="unconfigured"))
        self.assertEqual(self.cycle(controller, clear=True)["outcomes"], dict(clear="rejected"))
        self.cycle(controller, configure=self.settings(post_ticks=0))
        invalid = (dict(config=dict(self.config), post_ticks=-1), dict(config=dict(self.config)),
                   self.settings(extra=1), self.settings(drain_limit=0), self.settings(drain_limit=1 << 32),
                   self.settings(keep_kinds=["NOPE"]), self.settings(keep=lambda item: True),
                   self.settings(triggers=[dict(kinds=["RETIRE"], mode="range", base=8, limit=8)]),
                   self.settings(triggers=[dict(kinds=["RETIRE"], mode="equal", value=0, mask=0)] * 5),
                   self.settings(config=dict(self.config, pre_pages=8)),
                   self.settings(config=[]), self.settings(config=dict(self.config, post_pages=2, pre_pages=30)),
                   self.settings(codec='zip'))
        for settings in invalid:
            with self.subTest(settings=settings):
                result = self.cycle(controller, configure=settings)
                self.assertEqual(result["outcomes"], dict(configure="rejected"))
                self.assertEqual(controller.config_tag, 1)
        self.cycle(controller, arm=True)
        result = self.cycle(controller, configure=self.settings(post_ticks=9))
        self.assertEqual(result["outcomes"], dict(configure="rejected"))
        self.cycle(controller, software_trigger=True, service=True)
        self.assertEqual(controller.state, "FROZEN")
        self.assertEqual(decode_capture(controller.read()[1])["metadata"]["terminal"]["reason"], "post_window")
        self.assertEqual(self.cycle(controller, configure=self.settings())["outcomes"], dict(configure="accepted"))
        self.assertEqual(controller.state, "FROZEN")
        self.assertEqual(controller.config_tag, 2)

    def test_arm_cycle_observations_are_not_captured(self):
        controller = CaptureController()
        self.cycle(controller, user(1), request(1), configure=self.settings(), arm=True)
        self.cycle(controller, stop=True)
        decoded = decode_capture(controller.read()[1])
        self.assertEqual(sum(row["counters"]["observed"] for row in decoded["metadata"]["capture"]["sources"]), 0)

    def test_malformed_arguments_raise_before_mutation(self):
        controller = self.reach("ARMED")
        before = controller.status()
        tick = self.tick
        for arguments in (dict(tick=tick), dict(tick=-1), dict(tick=1 << 64), dict(tick=tick + 1, stop=1),
                          dict(tick=tick + 1, reset_source=4), dict(tick=tick + 1, reset_source=True),
                          dict(tick=tick + 1, configure="x"), dict(tick=tick + 1, observations=[user(), user()]),
                          dict(tick=tick + 1, observations=[object()])):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                controller.cycle(**arguments)
            self.assertEqual(controller.status(), before)
        self.assertEqual(controller.cycle(tick + 1, stop=True)["state"], "FROZEN")


if __name__ == "__main__":
    unittest.main()

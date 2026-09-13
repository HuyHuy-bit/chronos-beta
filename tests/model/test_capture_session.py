import copy
import json
import random
import unittest

from model.chronos.capture_session import decode_capture, encode_capture
from model.chronos.events import Observation
from model.chronos.raw_decode import DecodeError
from model.chronos.snapshot import SnapshotCapture
from scripts.config import ROOT, read_json


def crc(data):
    value = 0xFFFFFFFF
    for byte in data:
        for bit in range(8):
            feedback = (value ^ (byte >> bit)) & 1
            value >>= 1
            if feedback:
                value ^= 0xEDB88320
    return value ^ 0xFFFFFFFF


def frame(metadata, fragment):
    header = bytearray(bytes.fromhex('434852434150000001002000'))
    for value in (len(metadata), len(fragment), crc(metadata), crc(fragment), 0):
        header.extend(value.to_bytes(4, 'little'))
    header[28:32] = crc(header).to_bytes(4, 'little')
    return bytes(header) + metadata + fragment


class CaptureSessionTests(unittest.TestCase):
    def setUp(self):
        capture = SnapshotCapture(read_json(ROOT / 'configs/baseline.json'))
        capture.step(0, [Observation('USER_EVENT', dict(value=99))])
        for _ in range(16):
            capture.service()
        capture.step(1, stop=True)
        self.raw = capture.freeze()
        self.metadata = decode_capture(self.raw)['metadata']
        self.fragment = self.raw[32 + int.from_bytes(self.raw[12:16], 'little'):]

    def altered(self, metadata):
        return frame(json.dumps(metadata, sort_keys=True, separators=(',', ':')).encode(), self.fragment)

    def test_independent_preamble_framing_and_crc(self):
        self.assertEqual(crc(b'123456789'), 0xCBF43926)
        self.assertEqual(self.raw, self.altered(self.metadata))
        self.assertEqual(encode_capture(self.fragment, self.metadata), self.raw)
        self.assertEqual(decode_capture(self.raw)['events'][0].observation.fields['value'], 99)

    def test_all_input_output_bounds_and_types(self):
        limits = dict(max_bytes=len(self.raw), max_metadata_bytes=int.from_bytes(self.raw[12:16], 'little'),
                      max_pages=1, max_events=1)
        self.assertEqual(len(decode_capture(self.raw, **limits)['events']), 1)
        for key, value in limits.items():
            with self.subTest(key=key), self.assertRaises(DecodeError):
                decode_capture(self.raw, **dict(limits, **{key: value - 1}))
            for invalid in (True, -1, None, 1.5):
                with self.subTest(key=key, invalid=invalid), self.assertRaises(DecodeError):
                    decode_capture(self.raw, **{key: invalid})
        for invalid in (None, '', bytearray(self.raw), memoryview(self.raw)):
            with self.assertRaises(DecodeError):
                decode_capture(invalid)

    def test_every_truncated_prefix_and_trailing_data_rejected(self):
        for length in range(len(self.raw)):
            with self.subTest(length=length), self.assertRaises(DecodeError):
                decode_capture(self.raw[:length])
        with self.assertRaises(DecodeError):
            decode_capture(self.raw + b'\0')

    def test_json_duplicates_constants_nesting_and_encoding(self):
        valid = json.dumps(self.metadata).encode()
        bad = [b'null', b'[]', b'\xff', b'{', b'[' * 1500 + b']' * 1500,
               valid[:-1] + b',"scope":"capture-snapshot"}',
               valid.replace(b'"schema_version": 1', b'"schema_version": NaN')]
        for raw in bad:
            with self.subTest(prefix=raw[:40]), self.assertRaises(DecodeError):
                decode_capture(frame(raw, self.fragment))

    def test_missing_unknown_top_fields_and_identity(self):
        for key in self.metadata:
            metadata = copy.deepcopy(self.metadata)
            del metadata[key]
            with self.subTest(missing=key), self.assertRaises(DecodeError):
                decode_capture(self.altered(metadata))
        for changes in (dict(extra=0), dict(schema_version=True), dict(scope='event-fragment'),
                        dict(provenance='board'), dict(session_id=2), dict(config_tag=-1)):
            with self.subTest(changes=changes), self.assertRaises(DecodeError):
                decode_capture(self.altered(dict(self.metadata, **changes)))

    def test_retention_geometry_generation_and_freeze_contradictions(self):
        for changes in (dict(pre_pages=1, post_pages=1), dict(pre_pages=15), dict(page_bytes=256),
                        dict(next_generation=10), dict(committed_pages=2), dict(pinned=False),
                        dict(frozen=False), dict(active_slot=0), dict(eviction_saturated=True),
                        dict(evicted_events=1), dict(evicted_pages=1), dict(extra=0)):
            metadata = copy.deepcopy(self.metadata)
            metadata['retention'].update(changes)
            with self.subTest(changes=changes), self.assertRaises(DecodeError):
                decode_capture(self.altered(metadata))

    def test_terminal_pending_and_completion_contradictions(self):
        for changes in (dict(drain_complete=True, pending_events=[0, 0, 0, 1]),
                        dict(storage_error='post_capacity'), dict(storage_error='other'),
                        dict(pending_events=[0]), dict(pending_events=[0, 0, 0, True]),
                        dict(reason=''), dict(reason='x' * 65), dict(reason='\ud800'),
                        dict(rejected_cycle_events=-1), dict(drain_complete=1)):
            metadata = copy.deepcopy(self.metadata)
            metadata['terminal'].update(changes)
            with self.subTest(changes=changes), self.assertRaises(DecodeError):
                decode_capture(self.altered(metadata))

    def test_unaccounted_admitted_work_and_counter_contradictions(self):
        metadata = copy.deepcopy(self.metadata)
        row = metadata['capture']['sources'][3]['counters']
        row['observed'] += 1
        row['admitted'] += 1
        with self.assertRaises(DecodeError):
            decode_capture(self.altered(metadata))
        metadata = copy.deepcopy(self.metadata)
        metadata['capture']['sources'][3]['counters']['observed'] += 1
        with self.assertRaises(DecodeError):
            decode_capture(self.altered(metadata))

    def test_classified_omission_cannot_also_be_retained(self):
        metadata = copy.deepcopy(self.metadata)
        row = metadata['capture']['sources'][3]
        row['counters']['observed'] += 1
        row['counters']['filtered'] += 1
        row['ranges'] = [dict(epoch=0, first_sequence=0, last_sequence=0, first_tick=0,
                              last_tick=0, reason='filtered', count=1)]
        with self.assertRaisesRegex(DecodeError, 'also classified'):
            decode_capture(self.altered(metadata))

    def test_empty_snapshot_and_incomplete_snapshot(self):
        capture = SnapshotCapture(read_json(ROOT / 'configs/baseline.json'))
        capture.step(0, stop=True)
        result = decode_capture(capture.freeze(), max_events=0, max_pages=0)
        self.assertTrue(result['metadata']['terminal']['drain_complete'])
        capture.step(0, trace_reset=True)
        capture.step(0, [Observation('USER_EVENT', dict(value=1))])
        capture.step(1, stop=True)
        result = decode_capture(capture.freeze(incomplete=True), max_events=0, max_pages=0)
        self.assertEqual(result['metadata']['terminal']['pending_events'], [0, 0, 0, 1])
        self.assertFalse(result['metadata']['terminal']['drain_complete'])

    def test_corruption_campaign_rejects_all_regions(self):
        rng = random.Random(0x503143)
        for case in range(512):
            raw = bytearray(self.raw)
            raw[rng.randrange(len(raw))] ^= 1 << rng.randrange(8)
            with self.subTest(case=case), self.assertRaises(DecodeError):
                decode_capture(bytes(raw))


if __name__ == '__main__':
    unittest.main()

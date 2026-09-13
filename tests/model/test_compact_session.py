import copy
import unittest

from model.chronos import compact_decode, compact_encode, raw_decode, raw_encode
from model.chronos.capture_session import decode_capture, encode_capture
from model.chronos.events import Event, Observation
from model.chronos.raw_decode import DecodeError
from model.chronos.raw_session import decode_fragment, encode_fragment
from model.chronos.snapshot import SnapshotCapture
from scripts.config import ROOT, read_json
from test_compact import crc, retire


def fragment(groups, codec='compact-v1', page_bytes=256):
    encoder = compact_encode if codec == 'compact-v1' else raw_encode
    decoder = compact_decode if codec == 'compact-v1' else raw_decode
    pages = [encoder.encode_page(group, session_id=1, generation=index,
                                 config_tag=1, page_bytes=page_bytes)
             for index, group in enumerate(groups)]
    directory = []
    for wire in pages:
        page = decoder.decode_page(wire, page_bytes=page_bytes)
        directory.append(dict(generation=page['generation'], record_count=len(page['events']),
                              payload_crc32=page['payload_crc32']))
    manifest = dict(schema_version=1, scope='event-fragment', provenance='model', session_id=1,
                    config_tag=1, config_sha256='0' * 64, page_bytes=page_bytes,
                    source_profile='rv32-single-clock-v1', codecs=[codec], pages=directory)
    return pages, manifest


class CompactSessionTests(unittest.TestCase):
    def setUp(self):
        self.events = tuple(retire(4096 + 4 * i, tick=i, sequence=i) for i in range(80))
        self.pages, self.manifest = fragment([self.events[:40], self.events[40:]])
        self.wire = encode_fragment(self.pages, self.manifest)

    def test_expanded_directory_and_independent_page_restart(self):
        self.assertEqual(self.wire[8], 2)
        self.assertEqual(decode_fragment(self.wire)['events'], self.events)
        for page, group in zip(self.pages, (self.events[:40], self.events[40:])):
            self.assertEqual(int.from_bytes(page[36:40], 'little'), 1)
            self.assertEqual(int.from_bytes(page[56:60], 'little'), 40)
            self.assertEqual(compact_decode.decode_page(page, page_bytes=256)['events'], group)
        self.assertEqual([row['record_count'] for row in self.manifest['pages']], [40, 40])

    def test_cumulative_expansion_limit(self):
        for limit in (0, 40, 79):
            with self.subTest(limit=limit), self.assertRaises(DecodeError):
                decode_fragment(self.wire, max_events=limit)
        for limit in (80, 200000):
            self.assertEqual(decode_fragment(self.wire, max_events=limit)['events'], self.events)

    def test_version_codec_mismatch_and_unknown_version(self):
        for version in (0, 1, 3):
            wire = bytearray(self.wire)
            wire[8] = version
            wire[28:32] = bytes(4)
            wire[28:32] = crc(wire[:32]).to_bytes(4, 'little')
            with self.assertRaises(DecodeError):
                decode_fragment(bytes(wire))

    def test_codec_page_mismatch_and_unsupported_codec(self):
        for codecs in (['raw-v1'], ['raw-v1', 'compact-v1'], ['compact-v2']):
            with self.subTest(codecs=codecs), self.assertRaises(DecodeError):
                encode_fragment(self.pages, dict(self.manifest, codecs=codecs))
        pages, manifest = fragment([self.events[:2]], 'raw-v1')
        self.assertEqual(encode_fragment(pages, manifest)[8], 1)
        with self.assertRaises(DecodeError):
            encode_fragment(pages, dict(manifest, codecs=['compact-v1']))
        with self.assertRaises(DecodeError):
            raw_decode.decode_page(self.pages[0], page_bytes=256)

    def test_directory_cannot_substitute_encoded_record_count(self):
        manifest = copy.deepcopy(self.manifest)
        manifest['pages'][0]['record_count'] = 1
        with self.assertRaises(DecodeError):
            encode_fragment(self.pages, manifest)

    def test_cross_page_order_checked_after_expansion(self):
        for groups in ([self.events[40:], self.events[:40]], [self.events[:40], self.events[:40]]):
            pages, manifest = fragment(groups)
            with self.assertRaises(DecodeError):
                encode_fragment(pages, manifest)

    def test_collector_order_preserved_across_sources(self):
        user = Event(0, 3, 0, 0, 0, Observation('USER_EVENT', dict(value=9)))
        events = (self.events[10], user, self.events[11])
        pages, manifest = fragment([events])
        self.assertEqual(decode_fragment(encode_fragment(pages, manifest))['events'], events)

    def test_raw_snapshot_rejects_compact_fragment(self):
        capture = SnapshotCapture(read_json(ROOT / 'configs/baseline.json'))
        capture.step(0, [Observation('USER_EVENT', dict(value=99))])
        for _ in range(16):
            capture.service()
        capture.step(1, stop=True)
        decoded = decode_capture(capture.freeze())
        manifest = copy.deepcopy(decoded['fragment']['manifest'])
        manifest['codecs'] = ['compact-v1']
        pages = []
        for page, entry in zip(decoded['fragment']['pages'], manifest['pages']):
            wire = compact_encode.encode_page(page['events'], session_id=manifest['session_id'],
                generation=page['generation'], config_tag=manifest['config_tag'], page_bytes=manifest['page_bytes'])
            entry['payload_crc32'] = int.from_bytes(wire[48:52], 'little')
            pages.append(wire)
        compact = encode_fragment(pages, manifest)
        with self.assertRaisesRegex(DecodeError, 'requires raw-v1'):
            encode_capture(compact, decoded['metadata'])


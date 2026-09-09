import copy
import json
import random
import unittest

from model.chronos.events import Event, Observation
from model.chronos.raw_decode import DecodeError
from model.chronos.raw_encode import encode_page
from model.chronos.raw_session import decode_fragment, encode_fragment


def crc(data):
    remainder = 0xFFFFFFFF
    for byte in data:
        for bit in range(8):
            feedback = (remainder ^ (byte >> bit)) & 1
            remainder >>= 1
            if feedback:
                remainder ^= 0xEDB88320
    return remainder ^ 0xFFFFFFFF


def frame(metadata, pages=(), page_bytes=256):
    header = bytearray(b'CHRONOS\0\x01\x00\x20\x00')
    for value in (len(metadata), len(pages), page_bytes, crc(metadata), 0):
        header.extend(value.to_bytes(4, 'little'))
    header[28:32] = crc(header).to_bytes(4, 'little')
    return bytes(header) + metadata + b''.join(pages)


def metadata_bytes(manifest):
    return json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()


class FragmentTests(unittest.TestCase):
    def setUp(self):
        self.event = Event(9, 3, 0, 0, 0, Observation('USER_EVENT', {'value': 0x11223344}))
        self.page = encode_page([self.event], session_id=0x102030405060708,
                                generation=7, config_tag=0x1234, page_bytes=256)
        self.manifest = dict(schema_version=1, scope='event-fragment', provenance='model',
                             session_id=0x102030405060708, config_tag=0x1234,
                             config_sha256='0123456789abcdef' * 4, page_bytes=256,
                             source_profile='rv32-single-clock-v1', codecs=['raw-v1'],
                             pages=[dict(generation=7, record_count=1,
                                         payload_crc32=crc(self.page[64:100]))])

    def directory(self, pages):
        manifest = copy.deepcopy(self.manifest)
        manifest['pages'] = [dict(generation=int.from_bytes(page[16:24], 'little'),
                                   record_count=int.from_bytes(page[36:40], 'little'),
                                   payload_crc32=int.from_bytes(page[48:52], 'little'))
                             for page in pages]
        return manifest

    def reference(self, manifest=None, pages=None):
        return frame(metadata_bytes(self.manifest if manifest is None else manifest),
                     [self.page] if pages is None else pages)

    def test_encoder_matches_independent_container_framing(self):
        self.assertEqual(crc(b'123456789'), 0xCBF43926)
        actual = encode_fragment([self.page], self.manifest)
        self.assertEqual(actual, self.reference())
        self.assertEqual(actual[:12], bytes.fromhex('4348524f4e4f530001002000'))

    def test_independent_fragment_decodes_exact_events_and_identity(self):
        result = decode_fragment(self.reference())
        self.assertEqual(result['manifest'], self.manifest)
        self.assertEqual(result['events'], (self.event,))
        self.assertEqual(result['pages'][0]['generation'], 7)
        self.assertEqual(result['manifest']['scope'], 'event-fragment')

    def test_empty_fragment(self):
        manifest = self.directory([])
        raw = self.reference(manifest, [])
        self.assertEqual(encode_fragment([], manifest), raw)
        result = decode_fragment(raw, max_pages=0, max_events=0)
        self.assertEqual(result['pages'], ())
        self.assertEqual(result['events'], ())

    def test_empty_page(self):
        page = encode_page([], session_id=self.manifest['session_id'], generation=9,
                           config_tag=0x1234, page_bytes=256)
        result = decode_fragment(self.reference(self.directory([page]), [page]), max_events=0)
        self.assertEqual(len(result['pages']), 1)
        self.assertEqual(result['events'], ())

    def test_all_parser_limits_and_exact_boundaries(self):
        raw = self.reference()
        limits = dict(max_bytes=len(raw), max_manifest_bytes=len(metadata_bytes(self.manifest)),
                      max_pages=1, max_events=1)
        self.assertEqual(decode_fragment(raw, **limits)['events'], (self.event,))
        for key, value in limits.items():
            with self.subTest(limit=key), self.assertRaises(DecodeError):
                decode_fragment(raw, **dict(limits, **{key: value - 1}))
        for key in limits:
            for invalid in (-1, True, 1.5, None):
                with self.subTest(limit=key, value=invalid), self.assertRaises(DecodeError):
                    decode_fragment(raw, **{key: invalid})

    def test_cumulative_output_budget_across_pages(self):
        event = Event(10, 3, 0, 2, 0, self.event.observation)
        second = encode_page([event], session_id=self.manifest['session_id'], generation=10,
                             config_tag=0x1234, page_bytes=256)
        raw = self.reference(self.directory([self.page, second]), [self.page, second])
        with self.assertRaises(DecodeError):
            decode_fragment(raw, max_events=1)
        self.assertEqual(decode_fragment(raw, max_events=2)['events'], (self.event, event))

    def test_every_truncated_prefix_and_trailing_bytes_rejected(self):
        raw = self.reference()
        for length in range(len(raw)):
            with self.subTest(length=length), self.assertRaises(DecodeError):
                decode_fragment(raw[:length])
        with self.assertRaises(DecodeError):
            decode_fragment(raw + b'\0')

    def test_crc_corruption_in_each_region(self):
        raw = self.reference()
        start = 32 + len(metadata_bytes(self.manifest))
        for offset in (12, 28, 32, start + 8, start + 52, start + 64, len(raw) - 1):
            corrupt = bytearray(raw)
            corrupt[offset] ^= 1
            with self.subTest(offset=offset), self.assertRaises(DecodeError):
                decode_fragment(bytes(corrupt))

    def test_preamble_fields_checked_with_valid_checksum(self):
        for start, replacement in ((0, b'X'), (8, b'\x02'), (9, b'\x01'),
                                   (10, b'\x21'), (16, (2).to_bytes(4, 'little')),
                                   (20, (512).to_bytes(4, 'little'))):
            raw = bytearray(self.reference())
            raw[start:start + len(replacement)] = replacement
            raw[28:32] = bytes(4)
            raw[28:32] = crc(raw[:32]).to_bytes(4, 'little')
            with self.subTest(offset=start), self.assertRaises(DecodeError):
                decode_fragment(bytes(raw))

    def test_json_format_and_type_rejections_with_valid_checksums(self):
        valid = metadata_bytes(self.manifest)
        malformed = [b'{', b'[]', b'null', b'\xff', valid[:-1] + b',"scope":"event-fragment"}',
                     valid.replace(b'"schema_version":1', b'"schema_version":NaN'),
                     valid.replace(b'"schema_version":1', b'"schema_version":Infinity'),
                     b'[' * 1500 + b']' * 1500]
        for metadata in malformed:
            with self.subTest(prefix=metadata[:32]), self.assertRaises(DecodeError):
                decode_fragment(frame(metadata, [self.page]))
        spaced = json.dumps(self.manifest, indent=2).encode()
        self.assertEqual(decode_fragment(frame(spaced, [self.page]))['events'], (self.event,))

    def test_unknown_missing_and_mistyped_manifest_fields(self):
        changes = [dict(extra=0), dict(schema_version=True), dict(schema_version=2),
                   dict(scope='complete-capture'), dict(provenance='physical-board'),
                   dict(source_profile='unknown'), dict(codecs=['raw-v1', 'unknown']),
                   dict(config_sha256='A' * 64), dict(config_sha256='g' * 64),
                   dict(session_id=-1), dict(config_tag=1 << 64), dict(page_bytes=True),
                   dict(pages={})]
        for changeset in changes:
            manifest = dict(self.manifest, **changeset)
            with self.subTest(changes=changeset), self.assertRaises(DecodeError):
                decode_fragment(self.reference(manifest))
        for key in self.manifest:
            manifest = copy.deepcopy(self.manifest)
            del manifest[key]
            with self.subTest(missing=key), self.assertRaises(DecodeError):
                decode_fragment(self.reference(manifest))

    def test_directory_and_page_identity_must_match(self):
        for key, value in (('session_id', 9), ('config_tag', 0), ('page_bytes', 1024)):
            with self.subTest(key=key), self.assertRaises(DecodeError):
                decode_fragment(self.reference(dict(self.manifest, **{key: value})))
        for key, value in (('generation', 8), ('record_count', 0), ('payload_crc32', 0),
                           ('extra', 0), ('generation', True)):
            manifest = copy.deepcopy(self.manifest)
            manifest['pages'][0][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(DecodeError):
                decode_fragment(self.reference(manifest))

    def test_swapped_duplicate_or_decreasing_page_generations(self):
        second = encode_page([], session_id=self.manifest['session_id'], generation=8,
                             config_tag=0x1234, page_bytes=256)
        for pages in ([second, self.page], [self.page, self.page]):
            with self.assertRaises(DecodeError):
                decode_fragment(self.reference(self.directory(pages), pages))
        with self.assertRaises(DecodeError):
            decode_fragment(self.reference(self.directory([self.page, second]), [second, self.page]))

    def test_source_order_checked_across_page_boundaries(self):
        for tick, epoch, sequence in ((8, 0, 1), (10, 0, 0), (9, 0, 1)):
            event = Event(tick, 3, epoch, sequence, 0, self.event.observation)
            second = encode_page([event], session_id=self.manifest['session_id'], generation=8,
                                 config_tag=0x1234, page_bytes=256)
            raw = self.reference(self.directory([self.page, second]), [self.page, second])
            with self.subTest(tick=tick, epoch=epoch, sequence=sequence), self.assertRaises(DecodeError):
                decode_fragment(raw)

    def test_cross_source_timestamp_order_and_epoch_gaps_allowed(self):
        events = [Event(3, 0, 0, 0, 0, Observation('RETIRE', dict(pc=0, next_pc=4,
                                                                 length=4, boundary=0))),
                  Event(10, 3, 2, 0, 0, self.event.observation)]
        second = encode_page(events, session_id=self.manifest['session_id'], generation=12,
                             config_tag=0x1234, page_bytes=256)
        raw = self.reference(self.directory([self.page, second]), [self.page, second])
        self.assertEqual(decode_fragment(raw)['events'], (self.event, *events))

    def test_encoder_rejects_invalid_pages_directory_and_types(self):
        for pages in (iter([self.page]), b'', [bytearray(self.page)], [self.page[:-1]], []):
            with self.subTest(pages=type(pages)), self.assertRaises(ValueError):
                encode_fragment(pages, self.manifest)
        for data in (None, '', bytearray(self.reference()), memoryview(self.reference())):
            with self.subTest(data=type(data)), self.assertRaises(DecodeError):
                decode_fragment(data)

    def test_seeded_corrupt_container_campaign(self):
        raw = self.reference()
        rng = random.Random(0xC470)
        for case in range(500):
            mutated = bytearray(raw)
            offset = rng.randrange(len(mutated))
            mutated[offset] ^= 1 << rng.randrange(8)
            with self.subTest(case=case, offset=offset), self.assertRaises(DecodeError):
                decode_fragment(bytes(mutated))


if __name__ == '__main__':
    unittest.main()

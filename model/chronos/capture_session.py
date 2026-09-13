import json
import struct
import zlib

from .capture_metadata import validate_metadata
from .raw_decode import DecodeError
from .raw_session import decode_fragment


def _integer(value, maximum, name):
    if type(value) is not int or not 0 <= value <= maximum:
        raise DecodeError(f'invalid {name}')


def _shape(value, keys, name):
    if type(value) is not dict or value.keys() != set(keys.split()):
        raise DecodeError(f'invalid {name} fields')


def _validate(metadata, fragment):
    _shape(metadata, 'schema_version scope provenance session_id config_tag capture retention terminal', 'capture')
    if type(metadata['schema_version']) is not int or metadata['schema_version'] != 1:
        raise DecodeError('unsupported capture schema')
    if metadata['scope'] != 'capture-snapshot' or metadata['provenance'] != 'model':
        raise DecodeError('unsupported capture scope or provenance')
    manifest = fragment['manifest']
    if manifest['codecs'] != ['raw-v1']:
        raise DecodeError('snapshot retention currently requires raw-v1 pages')
    for key in ('session_id', 'config_tag'):
        _integer(metadata[key], (1 << 64) - 1, key)
        if metadata[key] != manifest[key]:
            raise DecodeError('capture and fragment identity mismatch')
    try:
        validate_metadata(metadata['capture'])
    except ValueError as error:
        raise DecodeError(f'invalid capture counters or journal: {error}') from error
    retention = metadata['retention']
    _shape(retention, 'page_bytes pre_pages post_pages pinned frozen active_slot evicted_pages '
           'evicted_events eviction_saturated committed_pages next_generation', 'retention')
    for key in ('pinned', 'frozen'):
        if retention[key] is not True:
            raise DecodeError('capture pages must be pinned and frozen')
    if retention['active_slot'] is not None or type(retention['eviction_saturated']) is not bool:
        raise DecodeError('invalid frozen retention state')
    for key in ('page_bytes', 'pre_pages', 'post_pages', 'committed_pages', 'evicted_pages', 'evicted_events'):
        _integer(retention[key], (1 << 64) - 1, key)
    _integer(retention['next_generation'], 1 << 64, 'next_generation')
    if retention['page_bytes'] != manifest['page_bytes'] or min(retention['pre_pages'], retention['post_pages']) < 1:
        raise DecodeError('invalid retention geometry')
    total_slots = retention['pre_pages'] + retention['post_pages']
    memory_bytes = total_slots * retention['page_bytes']
    if not 8192 <= memory_bytes <= 131072 or memory_bytes & (memory_bytes - 1):
        raise DecodeError('retention has unsupported memory geometry')
    if retention['committed_pages'] != len(fragment['pages']) or len(fragment['pages']) > total_slots:
        raise DecodeError('retention count differs from fragment')
    if any(page['generation'] >= retention['next_generation'] for page in fragment['pages']):
        raise DecodeError('page generation was not allocated')
    first_generation = retention['next_generation'] - len(fragment['pages'])
    if first_generation < 0 or any(page['generation'] != first_generation + index
                                    for index, page in enumerate(fragment['pages'])):
        raise DecodeError('retained directory is not the allocated generation suffix')
    if retention['eviction_saturated'] and max(retention['evicted_pages'], retention['evicted_events']) != (1 << 64) - 1:
        raise DecodeError('invalid eviction saturation flag')
    if not retention['eviction_saturated']:
        if retention['next_generation'] != retention['committed_pages'] + retention['evicted_pages']:
            raise DecodeError('allocated page dispositions do not balance')
        max_records = (retention['page_bytes'] - 64) // 36
        if not retention['evicted_pages'] <= retention['evicted_events'] <= retention['evicted_pages'] * max_records:
            raise DecodeError('evicted record count is incompatible with evicted pages')
    terminal = metadata['terminal']
    _shape(terminal, 'reason drain_complete storage_error pending_events rejected_cycle_events', 'terminal')
    reason = terminal['reason']
    if type(reason) is not str or not reason:
        raise DecodeError('invalid terminal reason')
    try:
        if len(reason.encode('utf-8')) > 64:
            raise DecodeError('terminal reason exceeds bound')
    except UnicodeError as error:
        raise DecodeError('invalid terminal reason encoding') from error
    if type(terminal['drain_complete']) is not bool or terminal['storage_error'] not in (None, 'post_capacity', 'generation_exhausted'):
        raise DecodeError('invalid terminal status')
    pending = terminal['pending_events']
    if type(pending) is not list or len(pending) != 4:
        raise DecodeError('invalid pending event counts')
    for count in pending:
        _integer(count, (1 << 32) - 1, 'pending count')
    _integer(terminal['rejected_cycle_events'], (1 << 64) - 1, 'rejected events')
    if terminal['drain_complete'] and (any(pending) or terminal['storage_error'] is not None):
        raise DecodeError('complete drain has pending or failed storage')
    retained = [0] * 4
    for event in fragment['events']:
        ranges = metadata['capture']['sources'][event.source]['ranges']
        if any(entry['epoch'] == event.epoch and entry['first_sequence'] <= event.sequence <= entry['last_sequence']
               for entry in ranges):
            raise DecodeError('retained event is also classified as omitted')
        retained[event.source] += 1
    evicted = 0
    exact = not retention['eviction_saturated']
    for source, row in enumerate(metadata['capture']['sources']):
        fields = ('admitted', 'reset_discarded', 'storage_discarded')
        if set(fields).intersection(row['saturated']):
            exact = False
            continue
        counts = row['counters']
        remainder = counts['admitted'] - counts['reset_discarded'] - counts['storage_discarded'] - pending[source] - retained[source]
        if remainder < 0:
            raise DecodeError('retained or pending events exceed admitted work')
        evicted += remainder
    if exact and evicted != retention['evicted_events']:
        raise DecodeError('admitted event dispositions do not balance')


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise DecodeError('duplicate capture metadata key')
        value[key] = item
    return value


def _constant(value):
    raise DecodeError(f'invalid JSON constant {value}')


def encode_capture(fragment, metadata):
    decoded = decode_fragment(fragment)
    _validate(metadata, decoded)
    raw = json.dumps(metadata, ensure_ascii=True, sort_keys=True, allow_nan=False,
                     separators=(',', ':')).encode('utf-8')
    if len(raw) > 65536 or 32 + len(raw) + len(fragment) > 16777216:
        raise ValueError('capture exceeds encoder bounds')
    header = bytearray(struct.pack('<8sBBHIIIII', b'CHRCAP\0\0', 1, 0, 32, len(raw), len(fragment),
                                  zlib.crc32(raw), zlib.crc32(fragment), 0))
    struct.pack_into('<I', header, 28, zlib.crc32(header))
    return bytes(header) + raw + fragment


def decode_capture(data, *, max_bytes=16777216, max_metadata_bytes=65536,
                   max_pages=4096, max_events=100000):
    for name, value in (('max_bytes', max_bytes), ('max_metadata_bytes', max_metadata_bytes),
                        ('max_pages', max_pages), ('max_events', max_events)):
        if type(value) is not int or value < 0:
            raise DecodeError(f'invalid {name}')
    if type(data) is not bytes or len(data) > max_bytes or len(data) < 32:
        raise DecodeError('capture input type or size is invalid')
    if data[:12] != b'CHRCAP\0\0\x01\x00\x20\x00':
        raise DecodeError('unsupported capture preamble')
    value = lambda offset: int.from_bytes(data[offset:offset + 4], 'little')
    if value(28) != zlib.crc32(data[:28] + bytes(4)):
        raise DecodeError('capture header checksum mismatch')
    metadata_size, fragment_size = value(12), value(16)
    if metadata_size > max_metadata_bytes or len(data) != 32 + metadata_size + fragment_size:
        raise DecodeError('capture lengths exceed framing or caller bounds')
    middle = 32 + metadata_size
    raw, fragment = data[32:middle], data[middle:]
    if value(20) != zlib.crc32(raw) or value(24) != zlib.crc32(fragment):
        raise DecodeError('capture content checksum mismatch')
    try:
        metadata = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs, parse_constant=_constant)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise DecodeError(f'invalid capture JSON: {error}') from error
    decoded = decode_fragment(fragment, max_bytes=max_bytes, max_pages=max_pages, max_events=max_events)
    _validate(metadata, decoded)
    return dict(metadata=metadata, fragment=decoded, events=decoded['events'])

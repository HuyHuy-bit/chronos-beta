import json
import struct
import zlib

from .raw_decode import DecodeError, decode_page


def _uint(value, bits, name):
    if type(value) is not int or not 0 <= value < 1 << bits:
        raise DecodeError(f'{name} must be u{bits}')


def _manifest(value, max_pages):
    keys = {'schema_version', 'scope', 'provenance', 'session_id', 'config_tag',
            'config_sha256', 'page_bytes', 'source_profile', 'codecs', 'pages'}
    if type(value) is not dict or value.keys() != keys:
        raise DecodeError('manifest fields do not match the fragment schema')
    _uint(value['schema_version'], 32, 'schema_version')
    if value['schema_version'] != 1 or value['scope'] != 'event-fragment':
        raise DecodeError('unsupported manifest version or scope')
    if value['provenance'] != 'model' or value['source_profile'] != 'rv32-single-clock-v1':
        raise DecodeError('unsupported provenance or source profile')
    if type(value['codecs']) is not list or value['codecs'] != ['raw-v1']:
        raise DecodeError('unsupported codecs')
    _uint(value['session_id'], 64, 'session_id')
    _uint(value['config_tag'], 64, 'config_tag')
    _uint(value['page_bytes'], 32, 'page_bytes')
    if value['page_bytes'] not in (256, 1024, 4096):
        raise DecodeError('unsupported page size')
    digest = value['config_sha256']
    if type(digest) is not str or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
        raise DecodeError('config_sha256 must be 64 lowercase hex characters')
    directory = value['pages']
    if type(directory) is not list or len(directory) > max_pages:
        raise DecodeError('directory exceeds page limit or is not a list')
    previous = -1
    for entry in directory:
        if type(entry) is not dict or entry.keys() != {'generation', 'record_count', 'payload_crc32'}:
            raise DecodeError('invalid page directory entry')
        _uint(entry['generation'], 64, 'generation')
        _uint(entry['record_count'], 32, 'record_count')
        _uint(entry['payload_crc32'], 32, 'payload_crc32')
        if entry['generation'] <= previous:
            raise DecodeError('page generations must strictly increase')
        previous = entry['generation']


def _pages(pages, manifest, max_events):
    decoded = []
    events = []
    previous = {}
    for raw, entry in zip(pages, manifest['pages']):
        page = decode_page(raw, page_bytes=manifest['page_bytes'],
                           max_events=max_events - len(events))
        if page['session_id'] != manifest['session_id'] or page['config_tag'] != manifest['config_tag']:
            raise DecodeError('page identity does not match manifest')
        if (page['generation'] != entry['generation'] or len(page['events']) != entry['record_count']
                or page['payload_crc32'] != entry['payload_crc32']):
            raise DecodeError('page does not match directory')
        for event in page['events']:
            prior = previous.get(event.source)
            if prior is not None:
                if event.tick < prior.tick or event.epoch < prior.epoch:
                    raise DecodeError('source time or epoch decreases across pages')
                if event.epoch == prior.epoch and (event.sequence <= prior.sequence or
                        (event.tick == prior.tick and event.lane <= prior.lane)):
                    raise DecodeError('source identity or lane order decreases across pages')
            previous[event.source] = event
            events.append(event)
        decoded.append(page)
    return {'manifest': manifest, 'pages': tuple(decoded), 'events': tuple(events)}


def encode_fragment(pages, manifest):
    if type(pages) not in (list, tuple) or len(pages) > 4096:
        raise ValueError('pages must be a bounded list or tuple')
    _manifest(manifest, 4096)
    if len(pages) != len(manifest['pages']):
        raise ValueError('page count does not match directory')
    if any(type(page) is not bytes or len(page) != manifest['page_bytes'] for page in pages):
        raise ValueError('pages must be exact immutable page bytes')
    data = json.dumps(manifest, sort_keys=True, ensure_ascii=True, allow_nan=False,
                      separators=(',', ':')).encode('utf-8')
    if len(data) > 65536 or 32 + len(data) + len(pages) * manifest['page_bytes'] > 16777216:
        raise ValueError('fragment exceeds encoder resource limits')
    _pages(pages, manifest, 100000)
    preamble = bytearray(struct.pack('<8sBBHIIIII', b'CHRONOS\0', 1, 0, 32, len(data),
                                    len(pages), manifest['page_bytes'], zlib.crc32(data), 0))
    struct.pack_into('<I', preamble, 28, zlib.crc32(preamble))
    return bytes(preamble) + data + b''.join(pages)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DecodeError(f'duplicate manifest key: {key}')
        result[key] = value
    return result


def _nonfinite(value):
    raise DecodeError(f'nonfinite JSON number: {value}')


def decode_fragment(data, *, max_bytes=16777216, max_manifest_bytes=65536,
                    max_pages=4096, max_events=100000):
    for name, bound in (('max_bytes', max_bytes), ('max_manifest_bytes', max_manifest_bytes),
                        ('max_pages', max_pages), ('max_events', max_events)):
        if type(bound) is not int or bound < 0:
            raise DecodeError(f'{name} must be a nonnegative integer')
    if type(data) is not bytes or len(data) > max_bytes:
        raise DecodeError('fragment must be bytes within the input bound')
    if len(data) < 32:
        raise DecodeError('truncated fragment preamble')
    if data[:8] != b'CHRONOS\0' or data[8:12] != b'\x01\x00\x20\x00':
        raise DecodeError('unsupported fragment magic, version, or header size')
    number = lambda offset: int.from_bytes(data[offset:offset + 4], 'little')
    if number(28) != zlib.crc32(data[:28] + bytes(4)):
        raise DecodeError('preamble checksum mismatch')
    manifest_bytes, count, page_bytes = number(12), number(16), number(20)
    if manifest_bytes > max_manifest_bytes or count > max_pages:
        raise DecodeError('manifest or page count exceeds caller limit')
    if page_bytes not in (256, 1024, 4096):
        raise DecodeError('unsupported fragment page size')
    start = 32 + manifest_bytes
    if len(data) != start + count * page_bytes:
        raise DecodeError('fragment length does not match framing')
    metadata = data[32:start]
    if number(24) != zlib.crc32(metadata):
        raise DecodeError('manifest checksum mismatch')
    try:
        manifest = json.loads(metadata.decode('utf-8'), object_pairs_hook=_unique,
                              parse_constant=_nonfinite)
    except (UnicodeError, ValueError, RecursionError) as error:
        raise DecodeError(f'invalid manifest: {error}') from error
    _manifest(manifest, max_pages)
    if manifest['page_bytes'] != page_bytes or len(manifest['pages']) != count:
        raise DecodeError('manifest does not match preamble')
    if sum(entry['record_count'] for entry in manifest['pages']) > max_events:
        raise DecodeError('fragment exceeds cumulative event limit')
    pages = (data[offset:offset + page_bytes] for offset in range(start, len(data), page_bytes))
    return _pages(pages, manifest, max_events)

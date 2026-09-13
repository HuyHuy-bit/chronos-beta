import struct
import zlib

from .events import Event, Observation
from .raw_encode import encode_record


def _prepare(events, max_events):
    if type(max_events) is not int or not 0 <= max_events <= 100000:
        raise ValueError("max_events must be an integer in 0..100000")
    try:
        iterator = iter(events)
    except TypeError as error:
        raise ValueError("events must be iterable") from error
    entries = []
    previous = {}
    for event in iterator:
        if len(entries) == max_events:
            raise ValueError("events exceed the input count bound")
        raw = encode_record(event)
        event = Event(event.tick, event.source, event.epoch, event.sequence, event.lane,
                      Observation(event.observation.kind, event.observation.fields))
        prior = previous.get(event.source)
        if prior is not None:
            if event.tick < prior.tick or event.epoch < prior.epoch:
                raise ValueError("source time or epoch decreases")
            if event.epoch == prior.epoch:
                if event.sequence <= prior.sequence:
                    raise ValueError("source sequence must increase within an epoch")
                if event.tick == prior.tick and event.lane <= prior.lane:
                    raise ValueError("source lane must increase within an epoch and tick")
        previous[event.source] = event
        entries.append((event, raw))
    return entries


def _run(entries, start):
    first = entries[start][0]
    fields = first.observation.fields
    if fields["next_pc"] != fields["pc"] + 4:
        return 1, 0
    count = 1
    stride = 0
    while start + count < len(entries) and count < 255:
        event = entries[start + count][0]
        if event.observation.kind != "RETIRE":
            break
        member = event.observation.fields
        if count == 1:
            stride = event.tick - first.tick
        if not 1 <= stride <= 256 or count * stride > 256:
            break
        if (event.epoch != first.epoch or event.sequence != first.sequence + count
                or event.tick != first.tick + count * stride
                or member["boundary"] != fields["boundary"]
                or member["pc"] != fields["pc"] + 4 * count
                or member["next_pc"] != member["pc"] + 4):
            break
        count += 1
    return count, stride


def _encode(entries):
    output = []
    size = 0
    retirement = None
    bus = None
    index = 0
    while index < len(entries):
        event, record = entries[index]
        fields = event.observation.fields
        kind = event.observation.kind
        consumed = 1
        if kind == "RETIRE":
            count, stride = _run(entries, index)
            if count >= 2:
                record = struct.pack("<BBHBBHQQQIHHQ", 0x10, 0, 48, 0, 0, 0,
                                     event.epoch, event.sequence, event.tick, fields["pc"],
                                     count, stride, fields["boundary"])
                consumed = count
            elif retirement is not None:
                base = retirement.observation.fields
                dt = event.tick - retirement.tick
                dpc = fields["pc"] - base["pc"]
                if (event.epoch == retirement.epoch and fields["boundary"] == base["boundary"]
                        and event.sequence == retirement.sequence + 1
                        and 1 <= dt <= 65535 and -32768 <= dpc <= 32767):
                    record = struct.pack("<BBHBBHHhI", 0x11, 0, 16, 0, 0, 0,
                                         dt, dpc, fields["next_pc"])
            retirement = entries[index + consumed - 1][0]
        elif kind == "BUS_REQ":
            if bus is not None:
                base = bus.observation.fields
                dt = event.tick - bus.tick
                daddress = fields["address"] - base["address"]
                if (event.epoch == bus.epoch and fields["write"] == base["write"]
                        and event.sequence == bus.sequence + 1
                        and 0 <= dt <= 65535 and -32768 <= daddress <= 32767):
                    record = struct.pack("<BBHBBHHhIIBBH", 0x12, 0, 24, 1, event.lane, 0,
                                         dt, daddress, fields["transaction"], fields["data"],
                                         fields["write"], fields["mask"], 0)
            bus = event
        elif kind in ("TRAP", "IRQ_ACCEPT"):
            retirement = None
        elif kind == "BUS_RESP":
            bus = None
        size += len(record)
        if size > 4 * 1024 * 1024:
            raise ValueError("encoded block exceeds 4 MiB")
        output.append(record)
        index += consumed
    return tuple(output)


def encode_records(events, *, max_events=4096):
    return _encode(_prepare(events, max_events))


def encode_page(events, *, session_id, generation, config_tag, page_bytes=1024, max_events=4096):
    if type(page_bytes) is not int or page_bytes not in (256, 1024, 4096):
        raise ValueError("page_bytes must be 256, 1024, or 4096")
    for value in (session_id, generation, config_tag):
        if type(value) is not int or not 0 <= value < 1 << 64:
            raise ValueError("page identities must be u64 integers")
    entries = _prepare(events, max_events)
    records = _encode(entries)
    payload = b"".join(records)
    if len(payload) > page_bytes - 64:
        raise ValueError("encoded records exceed page payload")
    header = bytearray(struct.pack(
        "<4sBBHQQQIIQIIII", b"CHRP", 2, 0, 64, session_id, generation, config_tag,
        len(payload), len(records), min((event.tick for event, _ in entries), default=0),
        zlib.crc32(payload), 0, len(entries), 0,
    ))
    struct.pack_into("<I", header, 52, zlib.crc32(header))
    return bytes(header) + payload + bytes(page_bytes - 64 - len(payload))

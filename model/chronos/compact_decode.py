from zlib import crc32

from .events import Event, Observation
from .raw_decode import DecodeError, decode_record


_LENGTHS = {1: 52, 2: 52, 3: 52, 4: 40, 5: 48, 6: 44, 7: 36,
            0x10: 48, 0x11: 16, 0x12: 24}
_U32_MAX = (1 << 32) - 1
_U64_MAX = (1 << 64) - 1


def _number(data, offset, size, *, signed=False):
    return int.from_bytes(data[offset:offset + size], "little", signed=signed)


def _limit(max_events):
    if type(max_events) is not int or not 0 <= max_events <= 100000:
        raise DecodeError("max_events must be an integer from zero to 100000")


def _scan(data, max_events):
    spans = []
    expanded = 0
    cursor = 0
    while cursor < len(data):
        if len(data) - cursor < 8:
            raise DecodeError("truncated compact record header")
        kind = data[cursor]
        size = _number(data, cursor + 2, 2)
        if kind not in _LENGTHS:
            raise DecodeError("unsupported compact record type")
        if size != _LENGTHS[kind] or cursor + size > len(data):
            raise DecodeError("compact record length does not match type or input")
        count = 1
        if kind >= 0x10:
            source = 1 if kind == 0x12 else 0
            if data[cursor + 1] or data[cursor + 4] != source:
                raise DecodeError("invalid compact flags or source")
            if data[cursor + 5] > (1 if kind == 0x12 else 0) or any(data[cursor + 6:cursor + 8]):
                raise DecodeError("invalid compact lane or reserved bytes")
        if kind == 0x10:
            count = _number(data, cursor + 36, 2)
            stride = _number(data, cursor + 38, 2)
            if not 2 <= count <= 255 or not 1 <= stride <= 256 or (count - 1) * stride > 256:
                raise DecodeError("PC run count, stride, or age exceeds bounds")
        expanded += count
        if expanded > max_events:
            raise DecodeError("compact expansion exceeds max_events")
        spans.append((cursor, size, kind, count))
        cursor += size
    return spans, expanded


def _order(previous, tick, epoch, sequence, lane):
    if previous is None:
        return
    if tick < previous.tick or epoch < previous.epoch:
        raise DecodeError("source time or epoch decreases")
    if epoch == previous.epoch:
        if sequence <= previous.sequence:
            raise DecodeError("source sequence does not increase within epoch")
        if tick == previous.tick and lane <= previous.lane:
            raise DecodeError("source lane does not increase within epoch and tick")


def _retirement(tick, epoch, sequence, pc, next_pc, boundary):
    return Event(tick, 0, epoch, sequence, 0,
                 Observation("RETIRE", {"pc": pc, "next_pc": next_pc, "length": 4, "boundary": boundary}))


def _expand(data, spans):
    events = []
    previous = [None] * 4
    retirement = None
    request = None
    for offset, size, kind, count in spans:
        record = data[offset:offset + size]
        if kind <= 7:
            event = decode_record(record)
            _order(previous[event.source], event.tick, event.epoch, event.sequence, event.lane)
            events.append(event)
            previous[event.source] = event
            observation_kind = event.observation.kind
            if observation_kind == "RETIRE":
                retirement = event
            elif observation_kind in ("TRAP", "IRQ_ACCEPT"):
                retirement = None
            elif observation_kind == "BUS_REQ":
                request = event
            elif observation_kind == "BUS_RESP":
                request = None
        elif kind == 0x10:
            epoch = _number(record, 8, 8)
            sequence = _number(record, 16, 8)
            tick = _number(record, 24, 8)
            pc = _number(record, 32, 4)
            stride = _number(record, 38, 2)
            boundary = _number(record, 40, 8)
            if sequence + count - 1 > _U64_MAX or tick + (count - 1) * stride > _U64_MAX:
                raise DecodeError("PC run identity or time overflows")
            if pc + 4 * count > _U32_MAX:
                raise DecodeError("PC run address overflows")
            _order(previous[0], tick, epoch, sequence, 0)
            for index in range(count):
                address = pc + 4 * index
                event = _retirement(tick + index * stride, epoch, sequence + index,
                                    address, address + 4, boundary)
                events.append(event)
            retirement = events[-1]
            previous[0] = retirement
        elif kind == 0x11:
            if retirement is None:
                raise DecodeError("retirement delta has no base")
            dt = _number(record, 8, 2)
            tick = retirement.tick + dt
            sequence = retirement.sequence + 1
            pc = retirement.observation.fields["pc"] + _number(record, 10, 2, signed=True)
            if dt == 0 or tick > _U64_MAX or sequence > _U64_MAX or not 0 <= pc <= _U32_MAX:
                raise DecodeError("retirement delta time, sequence, or address is invalid")
            _order(previous[0], tick, retirement.epoch, sequence, 0)
            retirement = _retirement(tick, retirement.epoch, sequence, pc, _number(record, 12, 4),
                                     retirement.observation.fields["boundary"])
            previous[0] = retirement
            events.append(retirement)
        else:
            if request is None:
                raise DecodeError("bus request delta has no base")
            if record[20] > 1 or record[21] > 15 or any(record[22:24]):
                raise DecodeError("invalid bus delta fields or reserved bytes")
            write = bool(record[20])
            if write != request.observation.fields["write"]:
                raise DecodeError("bus delta changes write direction")
            tick = request.tick + _number(record, 8, 2)
            sequence = request.sequence + 1
            address = request.observation.fields["address"] + _number(record, 10, 2, signed=True)
            if tick > _U64_MAX or sequence > _U64_MAX or not 0 <= address <= _U32_MAX:
                raise DecodeError("bus delta time, sequence, or address overflows")
            lane = record[5]
            _order(previous[1], tick, request.epoch, sequence, lane)
            request = Event(tick, 1, request.epoch, sequence, lane,
                            Observation("BUS_REQ", {"transaction": _number(record, 12, 4), "address": address,
                                                    "data": _number(record, 16, 4), "write": write, "mask": record[21]}))
            previous[1] = request
            events.append(request)
    return tuple(events)


def decode_records(data, *, max_events=4096) -> tuple[Event, ...]:
    _limit(max_events)
    if type(data) is not bytes:
        raise DecodeError("compact record input must be bytes")
    if len(data) > 4 * 1024 * 1024:
        raise DecodeError("compact record input exceeds 4 MiB")
    spans, _ = _scan(data, max_events)
    return _expand(data, spans)


def decode_page(data, *, page_bytes=1024, max_events=4096) -> dict:
    _limit(max_events)
    if type(page_bytes) is not int or page_bytes not in (256, 1024, 4096):
        raise DecodeError("unsupported compact page size")
    if type(data) is not bytes or len(data) != page_bytes:
        raise DecodeError("compact page input must be exactly page_bytes bytes")
    if data[:4] != b"CHRP" or data[4:6] != b"\x02\x00" or _number(data, 6, 2) != 64:
        raise DecodeError("unsupported compact page header")
    if any(data[60:64]):
        raise DecodeError("nonzero compact page reserved bytes")
    payload_size = _number(data, 32, 4)
    encoded_count = _number(data, 36, 4)
    expanded_count = _number(data, 56, 4)
    if payload_size > page_bytes - 64 or encoded_count > payload_size // 16:
        raise DecodeError("compact page payload or encoded count exceeds bounds")
    if expanded_count > max_events or not encoded_count <= expanded_count <= encoded_count * 255:
        raise DecodeError("compact page expanded count exceeds bounds")
    if crc32(data[:52] + bytes(4) + data[56:64]) != _number(data, 52, 4):
        raise DecodeError("compact page header checksum mismatch")
    end = 64 + payload_size
    payload_crc = _number(data, 48, 4)
    payload = data[64:end]
    if crc32(payload) != payload_crc:
        raise DecodeError("compact page payload checksum mismatch")
    if any(data[end:]):
        raise DecodeError("nonzero unused compact page bytes")
    spans, actual_expanded = _scan(payload, max_events)
    if len(spans) != encoded_count or actual_expanded != expanded_count:
        raise DecodeError("compact page counts do not match payload")
    events = _expand(payload, spans)
    first_tick = _number(data, 40, 8)
    if first_tick != min((event.tick for event in events), default=0):
        raise DecodeError("compact page minimum tick does not match events")
    return {"session_id": _number(data, 8, 8), "generation": _number(data, 16, 8),
            "config_tag": _number(data, 24, 8), "page_bytes": page_bytes,
            "payload_crc32": payload_crc, "first_tick": first_tick, "events": events}

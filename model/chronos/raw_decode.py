from .events import Event, Observation


class DecodeError(ValueError):
    pass


def _crc32(data):
    value = 0xFFFFFFFF
    for byte in data:
        value ^= byte
        for _ in range(8):
            value = (value >> 1) ^ (0xEDB88320 if value & 1 else 0)
    return value ^ 0xFFFFFFFF


def _uint(data, start, size):
    return int.from_bytes(data[start:start + size], "little")


def _parse_record(data):
    if len(data) < 32 or len(data) > 52:
        raise DecodeError("record size is outside supported bounds")
    record_type = data[0]
    flags = data[1]
    sizes = {1: 52, 2: 52, 3: 52, 4: 40, 5: 48, 6: 44, 7: 36}
    if record_type not in sizes:
        raise DecodeError("unsupported record type")
    if _uint(data, 2, 2) != len(data) or len(data) != sizes[record_type]:
        raise DecodeError("record length does not match its type")
    allowed_flags = 3 if record_type in (2, 3) else 1 if record_type == 6 else 0
    if flags & ~allowed_flags:
        raise DecodeError("unsupported record flags")
    source = data[4]
    expected_source = {1: 0, 2: 2, 3: 2, 4: 2, 5: 1, 6: 1, 7: 3}[record_type]
    if source != expected_source:
        raise DecodeError("record type and source disagree")
    lane = data[5]
    if lane > (1 if record_type in (4, 5) else 0):
        raise DecodeError("invalid record lane")
    if any(data[6:8]):
        raise DecodeError("nonzero record reserved bytes")
    if record_type == 1:
        if data[40] != 4 or any(data[41:44]):
            raise DecodeError("invalid retirement length or reserved bytes")
        kind = "RETIRE"
        fields = {"pc": _uint(data, 32, 4), "next_pc": _uint(data, 36, 4),
                  "length": 4, "boundary": _uint(data, 44, 8)}
    elif record_type in (2, 3):
        pc = _uint(data, 32, 4)
        target = _uint(data, 40, 4)
        if (not flags & 1 and pc != 0) or (not flags & 2 and target != 0):
            raise DecodeError("unavailable trap field has nonzero wire bytes")
        kind = "TRAP" if record_type == 2 else "IRQ_ACCEPT"
        fields = {"pc": pc if flags & 1 else None, "cause": _uint(data, 36, 4),
                  "target": target if flags & 2 else None, "boundary": _uint(data, 44, 8)}
    elif record_type == 4:
        kind = "IRQ_PENDING"
        fields = {"previous": _uint(data, 32, 4), "current": _uint(data, 36, 4)}
    elif record_type == 5:
        if data[44] > 1 or data[45] > 15 or any(data[46:48]):
            raise DecodeError("invalid bus request fields or reserved bytes")
        kind = "BUS_REQ"
        fields = {"transaction": _uint(data, 32, 4), "address": _uint(data, 36, 4),
                  "data": _uint(data, 40, 4), "write": bool(data[44]), "mask": data[45]}
    elif record_type == 6:
        value = _uint(data, 36, 4)
        if not flags & 1 and value != 0:
            raise DecodeError("unavailable response data has nonzero wire bytes")
        if data[40] > 1 or any(data[41:44]):
            raise DecodeError("invalid bus response fields or reserved bytes")
        kind = "BUS_RESP"
        fields = {"transaction": _uint(data, 32, 4), "data": value if flags & 1 else None,
                  "error": bool(data[40])}
    else:
        kind = "USER_EVENT"
        fields = {"value": _uint(data, 32, 4)}
    return {"tick": _uint(data, 24, 8), "source": source, "epoch": _uint(data, 8, 8),
            "sequence": _uint(data, 16, 8), "lane": lane, "kind": kind, "fields": fields}


def _event(parsed):
    try:
        observation = Observation(parsed["kind"], parsed["fields"])
        return Event(parsed["tick"], parsed["source"], parsed["epoch"],
                     parsed["sequence"], parsed["lane"], observation)
    except ValueError as error:
        raise DecodeError("invalid observation fields") from error


def decode_record(data) -> Event:
    if type(data) is not bytes:
        raise DecodeError("record input must be bytes")
    return _event(_parse_record(data))


def decode_page(data, *, page_bytes=1024, max_events=4096) -> dict:
    if type(page_bytes) is not int or page_bytes not in (256, 1024, 4096):
        raise DecodeError("unsupported page size")
    if type(max_events) is not int or max_events < 0:
        raise DecodeError("max_events must be a nonnegative integer")
    if type(data) is not bytes:
        raise DecodeError("page input must be bytes")
    if len(data) != page_bytes:
        raise DecodeError("page length does not match page_bytes")
    if data[:4] != b"CHRP" or data[4:6] != b"\x01\x00" or _uint(data, 6, 2) != 64:
        raise DecodeError("unsupported page header")
    if any(data[56:64]):
        raise DecodeError("nonzero page flags or reserved bytes")
    payload_bytes = _uint(data, 32, 4)
    count = _uint(data, 36, 4)
    if payload_bytes > page_bytes - 64:
        raise DecodeError("payload length exceeds page capacity")
    if count > max_events or count > payload_bytes // 36:
        raise DecodeError("record count exceeds output or payload bounds")
    if _crc32(data[:52] + b"\x00\x00\x00\x00" + data[56:64]) != _uint(data, 52, 4):
        raise DecodeError("page header checksum mismatch")
    payload_end = 64 + payload_bytes
    payload_crc = _uint(data, 48, 4)
    if _crc32(data[64:payload_end]) != payload_crc:
        raise DecodeError("page payload checksum mismatch")
    if any(data[payload_end:]):
        raise DecodeError("nonzero unused page bytes")
    parsed_records = []
    previous = {}
    cursor = 64
    while cursor < payload_end:
        if len(parsed_records) >= count:
            raise DecodeError("payload contains more records than declared")
        if payload_end - cursor < 32:
            raise DecodeError("truncated record header")
        size = _uint(data, cursor + 2, 2)
        if size < 32 or cursor + size > payload_end:
            raise DecodeError("record length exceeds payload bounds")
        parsed = _parse_record(data[cursor:cursor + size])
        source = parsed["source"]
        if source in previous:
            before = previous[source]
            if parsed["tick"] < before["tick"] or parsed["epoch"] < before["epoch"]:
                raise DecodeError("source time or epoch decreases")
            if parsed["epoch"] == before["epoch"]:
                if parsed["sequence"] <= before["sequence"]:
                    raise DecodeError("source sequence does not increase within epoch")
                if parsed["tick"] == before["tick"] and parsed["lane"] <= before["lane"]:
                    raise DecodeError("source lane does not increase within epoch and tick")
        previous[source] = parsed
        parsed_records.append(parsed)
        cursor += size
    if len(parsed_records) != count:
        raise DecodeError("record count does not match payload")
    first_tick = _uint(data, 40, 8)
    if first_tick != min((parsed["tick"] for parsed in parsed_records), default=0):
        raise DecodeError("page minimum tick does not match records")
    return {"session_id": _uint(data, 8, 8), "generation": _uint(data, 16, 8),
            "config_tag": _uint(data, 24, 8), "page_bytes": page_bytes,
            "payload_crc32": payload_crc, "first_tick": first_tick,
            "events": tuple(_event(parsed) for parsed in parsed_records)}

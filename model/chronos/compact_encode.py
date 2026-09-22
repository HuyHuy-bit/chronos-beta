import struct
import zlib

from .events import Event, Observation
from .raw_encode import encode_record


WATERMARK_END = 1 << 64
MIN_RECORD_BYTES = 16


def _checked(event, previous):
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
    return event, raw


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
        event, raw = _checked(event, previous)
        previous[event.source] = event
        entries.append((event, raw))
    return entries


def _starts_run(event):
    fields = event.observation.fields
    return event.observation.kind == "RETIRE" and fields["next_pc"] == fields["pc"] + 4


def _extends(first, count, stride, event):
    if count >= 255 or event.observation.kind != "RETIRE":
        return None
    if count == 1:
        stride = event.tick - first.tick
    if not 1 <= stride <= 256 or count * stride > 256:
        return None
    fields = first.observation.fields
    member = event.observation.fields
    if (event.epoch != first.epoch or event.sequence != first.sequence + count
            or event.tick != first.tick + count * stride
            or member["boundary"] != fields["boundary"]
            or member["pc"] != fields["pc"] + 4 * count
            or member["next_pc"] != member["pc"] + 4):
        return None
    return stride


def _run(entries, start):
    first = entries[start][0]
    if not _starts_run(first):
        return 1, 0
    count = 1
    stride = 0
    while start + count < len(entries):
        extended = _extends(first, count, stride, entries[start + count][0])
        if extended is None:
            break
        stride = extended
        count += 1
    return count, stride


def _pc_run(first, count, stride):
    fields = first.observation.fields
    return struct.pack("<BBHBBHQQQIHHQ", 0x10, 0, 48, 0, 0, 0, first.epoch, first.sequence,
                       first.tick, fields["pc"], count, stride, fields["boundary"])


def _single(event, record, context):
    fields = event.observation.fields
    kind = event.observation.kind
    if kind == "RETIRE":
        base = context.get("retirement")
        if base is not None:
            dt = event.tick - base.tick
            dpc = fields["pc"] - base.observation.fields["pc"]
            if (event.epoch == base.epoch and fields["boundary"] == base.observation.fields["boundary"]
                    and event.sequence == base.sequence + 1
                    and 1 <= dt <= 65535 and -32768 <= dpc <= 32767):
                record = struct.pack("<BBHBBHHhI", 0x11, 0, 16, 0, 0, 0, dt, dpc, fields["next_pc"])
        context["retirement"] = event
    elif kind == "BUS_REQ":
        base = context.get("bus")
        if base is not None:
            dt = event.tick - base.tick
            daddress = fields["address"] - base.observation.fields["address"]
            if (event.epoch == base.epoch and fields["write"] == base.observation.fields["write"]
                    and event.sequence == base.sequence + 1
                    and 0 <= dt <= 65535 and -32768 <= daddress <= 32767):
                record = struct.pack("<BBHBBHHhIIBBH", 0x12, 0, 24, 1, event.lane, 0,
                                     dt, daddress, fields["transaction"], fields["data"],
                                     fields["write"], fields["mask"], 0)
        context["bus"] = event
    elif kind in ("TRAP", "IRQ_ACCEPT"):
        context["retirement"] = None
    elif kind == "BUS_RESP":
        context["bus"] = None
    return record


def _encode(entries):
    output = []
    size = 0
    context = {}
    index = 0
    while index < len(entries):
        event, record = entries[index]
        count, stride = _run(entries, index)
        if count >= 2:
            record = _pc_run(event, count, stride)
            context["retirement"] = entries[index + count - 1][0]
        else:
            record = _single(event, record, context)
        size += len(record)
        if size > 4 * 1024 * 1024:
            raise ValueError("encoded block exceeds 4 MiB")
        output.append(record)
        index += count
    return tuple(output)


class RunStream:
    def __init__(self):
        self._previous = {}
        self._context = {}
        self._run = None
        self._watermark = -1

    @property
    def pending(self):
        return 0 if self._run is None else self._run[2]

    def _emit(self):
        first, raw, count, stride, last = self._run
        self._run = None
        if count >= 2:
            self._context["retirement"] = last
            return _pc_run(first, count, stride)
        return _single(first, raw, self._context)

    def push(self, event):
        event, raw = _checked(event, self._previous)
        if event.source == 0 and event.tick <= self._watermark:
            raise ValueError("source-0 event does not follow the declared watermark")
        self._previous[event.source] = event
        output = []
        if self._run is not None:
            first, first_raw, count, stride, _ = self._run
            extended = _extends(first, count, stride, event)
            if extended is not None:
                self._run = (first, first_raw, count + 1, extended, event)
                if count + 1 == 255 or (count + 1) * extended > 256:
                    output.append(self._emit())
                return tuple(output)
            output.append(self._emit())
        if _starts_run(event):
            self._run = (event, raw, 1, 0, event)
        else:
            output.append(_single(event, raw, self._context))
        return tuple(output)

    def advance(self, watermark):
        if type(watermark) is not int or not -1 <= watermark <= WATERMARK_END:
            raise ValueError("watermark must be an integer in -1..2**64")
        self._watermark = max(self._watermark, watermark)
        if self._run is None:
            return ()
        first, _, count, stride, _ = self._run
        if self._watermark >= first.tick + (256 if count == 1 else count * stride):
            return (self._emit(),)
        return ()

    def flush(self):
        output = () if self._run is None else (self._emit(),)
        self._context = {}
        return output


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

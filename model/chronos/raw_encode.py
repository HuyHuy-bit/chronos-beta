import struct
import zlib

from .events import Event, Observation


_TYPES = {
    "RETIRE": 1,
    "TRAP": 2,
    "IRQ_ACCEPT": 3,
    "IRQ_PENDING": 4,
    "BUS_REQ": 5,
    "BUS_RESP": 6,
    "USER_EVENT": 7,
}


def _unsigned(value, bits, name):
    if type(value) is not int or not 0 <= value < 1 << bits:
        raise ValueError(f"{name} must be a u{bits} integer")


def encode_record(event):
    if not isinstance(event, Event):
        raise ValueError("record input must be an Event")
    for name in ("tick", "epoch", "sequence"):
        _unsigned(getattr(event, name), 64, name)
    _unsigned(event.source, 2, "source")
    _unsigned(event.lane, 1, "lane")
    if not isinstance(event.observation, Observation):
        raise ValueError("event observation must be an Observation")
    observation = Observation(event.observation.kind, event.observation.fields)
    if event.source != observation.source:
        raise ValueError("source does not match observation kind")
    if observation.kind not in ("BUS_REQ", "IRQ_PENDING") and event.lane != 0:
        raise ValueError("this observation kind requires lane zero")
    fields = observation.fields
    flags = 0
    if observation.kind == "RETIRE":
        payload = struct.pack("<IIB3xQ", fields["pc"], fields["next_pc"],
                              fields["length"], fields["boundary"])
    elif observation.kind in ("TRAP", "IRQ_ACCEPT"):
        flags = int(fields["pc"] is not None) | (int(fields["target"] is not None) << 1)
        payload = struct.pack("<IIIQ", fields["pc"] or 0, fields["cause"],
                              fields["target"] or 0, fields["boundary"])
    elif observation.kind == "IRQ_PENDING":
        payload = struct.pack("<II", fields["previous"], fields["current"])
    elif observation.kind == "BUS_REQ":
        payload = struct.pack("<IIIBBH", fields["transaction"], fields["address"],
                              fields["data"], fields["write"], fields["mask"], 0)
    elif observation.kind == "BUS_RESP":
        flags = int(fields["data"] is not None)
        payload = struct.pack("<IIB3x", fields["transaction"], fields["data"] or 0,
                              fields["error"])
    else:
        payload = struct.pack("<I", fields["value"])
    header = struct.pack("<BBHBBHQQQ", _TYPES[observation.kind], flags, 32 + len(payload),
                         event.source, event.lane, 0, event.epoch, event.sequence, event.tick)
    return header + payload


def encode_page(events, *, session_id, generation, config_tag, page_bytes=1024):
    if type(page_bytes) is not int or page_bytes not in (256, 1024, 4096):
        raise ValueError("page_bytes must be 256, 1024, or 4096")
    for name, value in (("session_id", session_id), ("generation", generation),
                        ("config_tag", config_tag)):
        _unsigned(value, 64, name)
    try:
        iterator = iter(events)
    except TypeError as error:
        raise ValueError("page events must be iterable") from error
    payload = bytearray()
    previous = {}
    count = 0
    first_tick = None
    for event in iterator:
        record = encode_record(event)
        if len(payload) + len(record) > page_bytes - 64:
            raise ValueError("records exceed page payload capacity")
        prior = previous.get(event.source)
        if prior is not None:
            if event.tick < prior.tick or event.epoch < prior.epoch:
                raise ValueError("source tick and epoch must not decrease")
            if event.epoch == prior.epoch:
                if event.sequence <= prior.sequence:
                    raise ValueError("source sequence must increase within an epoch")
                if event.tick == prior.tick and event.lane <= prior.lane:
                    raise ValueError("source lane must increase within an epoch and tick")
        previous[event.source] = event
        payload.extend(record)
        count += 1
        first_tick = event.tick if first_tick is None else min(first_tick, event.tick)
    header = bytearray(struct.pack(
        "<4sBBHQQQIIQIIII", b"CHRP", 1, 0, 64, session_id, generation, config_tag,
        len(payload), count, first_tick if first_tick is not None else 0,
        zlib.crc32(payload), 0, 0, 0,
    ))
    struct.pack_into("<I", header, 52, zlib.crc32(header))
    return bytes(header + payload + bytes(page_bytes - 64 - len(payload)))

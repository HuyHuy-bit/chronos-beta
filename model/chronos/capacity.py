from dataclasses import dataclass
from typing import Optional

from scripts.config import validate
from .compact_encode import MIN_RECORD_BYTES
from .raw_encode import MAX_RECORD_BYTES


@dataclass(frozen=True)
class Inventory:
    queue_events: tuple
    pending_runs: int = 0
    skid_events: int = 0
    builder_bytes: int = 0
    control_records: int = 0
    restart_bytes: int = 0
    record_bytes: Optional[int] = None
    tail_bytes: Optional[int] = None

    def __post_init__(self):
        if type(self.queue_events) is not tuple or len(self.queue_events) != 4:
            raise ValueError("queue_events must be a four-element tuple")
        counts = self.queue_events + (
            self.pending_runs,
            self.skid_events,
            self.builder_bytes,
            self.control_records,
            self.restart_bytes,
        )
        if any(type(value) is not int or value < 0 for value in counts):
            raise ValueError("inventory counts and byte costs must be nonnegative integers")
        if self.record_bytes is not None and (type(self.record_bytes) is not int or self.record_bytes < 1):
            raise ValueError("record_bytes must be a positive integer")
        if self.tail_bytes is not None and (type(self.tail_bytes) is not int or self.tail_bytes < 0):
            raise ValueError("tail_bytes must be a nonnegative integer")


def _validate_config(config):
    validate(config)
    if config["source_count"] != 4:
        raise ValueError("the capacity model requires four sources")


def default_inventory(config):
    _validate_config(config)
    return Inventory(
        queue_events=(config["fifo_depth"],) * 4,
        pending_runs=4,
        skid_events=1,
        builder_bytes=config["page_bytes"],
        control_records=2,
    )


def _tail_bytes(codec):
    if codec == "raw-v1":
        return MAX_RECORD_BYTES - 1
    if codec == "compact-v1":
        # A seal happens when used + pending reservation + next raw record exceeds the payload;
        # the flush then writes at least the smallest compact record into the reserved space.
        return 2 * MAX_RECORD_BYTES - MIN_RECORD_BYTES - 1
    raise ValueError("codec must be raw-v1 or compact-v1")


def measured_inventory(config, codec):
    _validate_config(config)
    return Inventory(queue_events=(config["fifo_depth"],) * 4, record_bytes=MAX_RECORD_BYTES,
                     tail_bytes=_tail_bytes(codec))


def completion_budget(config, inventory):
    _validate_config(config)
    if not isinstance(inventory, Inventory):
        raise ValueError("inventory must be an Inventory")
    record_bytes = config["max_record_bytes"] if inventory.record_bytes is None else inventory.record_bytes
    if record_bytes > config["max_record_bytes"]:
        raise ValueError("record_bytes exceeds the configured maximum record")
    tail_bytes = record_bytes - 1 if inventory.tail_bytes is None else inventory.tail_bytes
    terms = {
        "queue_events": sum(inventory.queue_events) * record_bytes,
        "pending_runs": inventory.pending_runs * record_bytes,
        "skid_events": inventory.skid_events * record_bytes,
        "builder_bytes": inventory.builder_bytes,
        "control_records": inventory.control_records * record_bytes,
        "restart_bytes": inventory.restart_bytes,
        "tail_waste": config["post_pages"] * tail_bytes,
    }
    payload = config["post_pages"] * (config["page_bytes"] - config["page_header_bytes"])
    required = sum(terms.values())
    return {
        "payload_bytes": payload,
        "record_bytes": record_bytes,
        "event_bytes": terms["queue_events"],
        "overhead_bytes": required - terms["queue_events"],
        "required_bytes": required,
        "margin_bytes": payload - required,
        "safe": required <= payload,
        "terms": terms,
    }


def service_envelope(config, codec):
    _validate_config(config)
    grant = config["sink_width_bits"] // 8
    payload = config["page_bytes"] - config["page_header_bytes"]
    tail = _tail_bytes(codec)
    event_grants = -(-MAX_RECORD_BYTES // grant)
    page_grants = -(-(config["page_header_bytes"] + tail) // grant)
    events_per_page = -(-(payload - tail) // MAX_RECORD_BYTES)
    cycles = event_grants + page_grants / events_per_page
    return {
        "grant_bytes": grant,
        "event_grants": event_grants,
        "page_overhead_grants": page_grants,
        "min_events_per_page": events_per_page,
        "cycles_per_event": cycles,
        "sustained_events_per_cycle": 1 / cycles,
        "burst_events_per_source": config["fifo_depth"],
    }


def metadata_bytes(config, *, journal_capacity=4, counter_bits=64):
    _validate_config(config)
    counter = -(-counter_bits // 8)
    pages = config["pre_pages"] + config["post_pages"]
    terms = {
        "counters": 4 * 8 * counter,
        "journal": 4 * journal_capacity * (8 + 8 + 8 + 8 + 8 + 8 + 1) + 4,
        "trigger": 8 + 1 + 6 * (1 + 1 + 64),
        "terminal": 1 + 1 + 1 + 4 * 4 + 8,
        "directory": pages * (8 + 1),
    }
    return {"bytes": sum(terms.values()), "terms": terms}

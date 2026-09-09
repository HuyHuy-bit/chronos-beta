from dataclasses import dataclass

from scripts.config import validate


@dataclass(frozen=True)
class Inventory:
    queue_events: tuple
    pending_runs: int = 0
    skid_events: int = 0
    builder_bytes: int = 0
    control_records: int = 0
    restart_bytes: int = 0

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


def completion_budget(config, inventory):
    _validate_config(config)
    if not isinstance(inventory, Inventory):
        raise ValueError("inventory must be an Inventory")
    record_bytes = config["max_record_bytes"]
    terms = {
        "queue_events": sum(inventory.queue_events) * record_bytes,
        "pending_runs": inventory.pending_runs * record_bytes,
        "skid_events": inventory.skid_events * record_bytes,
        "builder_bytes": inventory.builder_bytes,
        "control_records": inventory.control_records * record_bytes,
        "restart_bytes": inventory.restart_bytes,
        "tail_waste": config["post_pages"] * (record_bytes - 1),
    }
    payload = config["post_pages"] * (config["page_bytes"] - config["page_header_bytes"])
    required = sum(terms.values())
    return {
        "payload_bytes": payload,
        "event_bytes": terms["queue_events"],
        "overhead_bytes": required - terms["queue_events"],
        "required_bytes": required,
        "margin_bytes": payload - required,
        "safe": required <= payload,
        "terms": terms,
    }

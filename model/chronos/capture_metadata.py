from copy import deepcopy


COUNTERS = (
    "observed", "filtered", "admitted", "ingress_dropped", "reset_discarded",
    "fifo_dropped", "capacity_dropped", "storage_discarded",
)
_REASONS = ("filtered", "fifo", "capacity", "reset_discarded", "storage_discarded")
_REASON_COUNTERS = {"filtered": "filtered", "fifo": "fifo_dropped", "capacity": "capacity_dropped",
                    "reset_discarded": "reset_discarded", "storage_discarded": "storage_discarded"}
_U64_MAX = (1 << 64) - 1


def _integer(value, name, minimum=0, maximum=_U64_MAX):
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise ValueError(f"invalid {name}")
    return value


def _keys(value, names, name):
    if type(value) is not dict or value.keys() != set(names):
        raise ValueError(f"invalid {name} fields")


def _reason(value):
    if type(value) is not str or not value:
        raise ValueError("trigger reason must be a nonempty string")
    if len(value) > 64:
        raise ValueError("trigger reason exceeds 64 UTF-8 bytes")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise ValueError("trigger reason must be valid UTF-8") from error
    if size > 64:
        raise ValueError("trigger reason exceeds 64 UTF-8 bytes")


def _match(source, lane, reason):
    _integer(source, "trigger source", maximum=3)
    _integer(lane, "trigger lane", maximum=0 if source in (0, 3) else 1)
    _reason(reason)
    return {"source": source, "lane": lane, "reason": reason}


def _make_trigger(tick, matches):
    _integer(tick, "trigger tick")
    if type(matches) not in (list, tuple) or not 1 <= len(matches) <= 6:
        raise ValueError("trigger matches must contain one to six entries")
    result = []
    for entry in matches:
        if type(entry) not in (list, tuple) or len(entry) != 3:
            raise ValueError("invalid trigger match")
        result.append(_match(*entry))
    pairs = [(entry["source"], entry["lane"]) for entry in result]
    if pairs != sorted(set(pairs)):
        raise ValueError("trigger source/lane pairs must be unique and sorted")
    return {"tick": tick, "matches": result, "primary": dict(result[0])}


def _validate_trigger(value):
    if value is None:
        return
    _keys(value, ("tick", "matches", "primary"), "trigger")
    matches = value["matches"]
    if type(matches) is not list or not 1 <= len(matches) <= 6:
        raise ValueError("invalid serialized trigger matches")
    entries = []
    for match in matches:
        _keys(match, ("source", "lane", "reason"), "trigger match")
        entries.append((match["source"], match["lane"], match["reason"]))
    expected = _make_trigger(value["tick"], entries)
    primary = value["primary"]
    _keys(primary, ("source", "lane", "reason"), "primary trigger match")
    _match(primary["source"], primary["lane"], primary["reason"])
    if primary != expected["primary"]:
        raise ValueError("primary trigger match must equal the first match")


class CaptureMetadata:
    def __init__(self, *, counter_bits=64, journal_capacity=4):
        _integer(counter_bits, "counter_bits", minimum=1, maximum=64)
        _integer(journal_capacity, "journal_capacity", maximum=16)
        self._counter_bits = counter_bits
        self._journal_capacity = journal_capacity
        self._limit = (1 << counter_bits) - 1
        self._sources = [
            {"source": source, "counters": dict.fromkeys(COUNTERS, 0), "saturated": [],
             "journal_overflow": False, "ranges": []}
            for source in range(4)
        ]
        self._trigger = None

    def add(self, source, **deltas):
        _integer(source, "source", maximum=3)
        for name, value in deltas.items():
            if name not in COUNTERS:
                raise ValueError("unknown counter")
            _integer(value, "counter delta", maximum=None)
        state = self._sources[source]
        saturated = set(state["saturated"])
        for name, value in deltas.items():
            previous = state["counters"][name]
            if value > self._limit - previous:
                state["counters"][name] = self._limit
                saturated.add(name)
            else:
                state["counters"][name] = previous + value
        state["saturated"] = [name for name in COUNTERS if name in saturated]

    def mark(self, source, *, epoch, sequence, tick, reason):
        _integer(source, "source", maximum=3)
        _integer(epoch, "epoch")
        _integer(sequence, "sequence")
        _integer(tick, "tick")
        if type(reason) is not str or reason not in _REASONS:
            raise ValueError("unsupported omission reason")
        state = self._sources[source]
        if state["journal_overflow"]:
            return
        ranges = state["ranges"]
        if ranges:
            previous = ranges[-1]
            if (previous["epoch"] == epoch and previous["reason"] == reason
                    and sequence == previous["last_sequence"] + 1
                    and tick >= previous["last_tick"] and previous["count"] < _U64_MAX):
                previous["last_sequence"] = sequence
                previous["last_tick"] = tick
                previous["count"] += 1
                return
        if len(ranges) >= self._journal_capacity:
            state["journal_overflow"] = True
            return
        ranges.append({"epoch": epoch, "first_sequence": sequence, "last_sequence": sequence,
                       "first_tick": tick, "last_tick": tick, "reason": reason, "count": 1})

    def latch_trigger(self, tick, matches):
        trigger = _make_trigger(tick, matches)
        if self._trigger is None:
            self._trigger = trigger

    def snapshot(self):
        return {"counter_bits": self._counter_bits, "journal_capacity": self._journal_capacity,
                "sources": deepcopy(self._sources), "trigger": deepcopy(self._trigger)}


def validate_metadata(value):
    _keys(value, ("counter_bits", "journal_capacity", "sources", "trigger"), "metadata")
    bits = _integer(value["counter_bits"], "counter_bits", minimum=1, maximum=64)
    capacity = _integer(value["journal_capacity"], "journal_capacity", maximum=16)
    limit = (1 << bits) - 1
    sources = value["sources"]
    if type(sources) is not list or len(sources) != 4:
        raise ValueError("metadata must contain four sources")
    for source, state in enumerate(sources):
        _keys(state, ("source", "counters", "saturated", "journal_overflow", "ranges"), "source")
        if _integer(state["source"], "source", maximum=3) != source:
            raise ValueError("metadata sources must be in source order")
        counters = state["counters"]
        _keys(counters, COUNTERS, "counters")
        for name in COUNTERS:
            _integer(counters[name], name, maximum=limit)
        saturated = state["saturated"]
        if type(saturated) is not list or len(saturated) > len(COUNTERS):
            raise ValueError("invalid saturated counter list")
        for name in saturated:
            if type(name) is not str or name not in COUNTERS or counters[name] != limit:
                raise ValueError("saturated counters must be known and at limit")
        if saturated != [name for name in COUNTERS if name in saturated]:
            raise ValueError("saturated counter names must be unique and ordered")
        if type(state["journal_overflow"]) is not bool:
            raise ValueError("journal_overflow must be bool")
        ranges = state["ranges"]
        if type(ranges) is not list or len(ranges) > capacity:
            raise ValueError("omission ranges exceed journal capacity")
        classified = dict.fromkeys(_REASONS, 0)
        intervals = []
        for interval in ranges:
            _keys(interval, ("epoch", "first_sequence", "last_sequence", "first_tick",
                             "last_tick", "reason", "count"), "omission interval")
            for name in ("epoch", "first_sequence", "last_sequence", "first_tick", "last_tick"):
                _integer(interval[name], name)
            _integer(interval["count"], "interval count", minimum=1)
            if type(interval["reason"]) is not str or interval["reason"] not in _REASONS:
                raise ValueError("unsupported omission reason")
            if interval["count"] != interval["last_sequence"] - interval["first_sequence"] + 1:
                raise ValueError("interval count does not match sequence bounds")
            if interval["last_tick"] < interval["first_tick"]:
                raise ValueError("interval tick decreases")
            classified[interval["reason"]] += interval["count"]
            intervals.append((interval["epoch"], interval["first_sequence"], interval["last_sequence"]))
        for reason, count in classified.items():
            counter = _REASON_COUNTERS[reason]
            if counter not in saturated and count > counters[counter]:
                raise ValueError("classified omissions exceed exact counter")
        intervals.sort()
        for before, after in zip(intervals, intervals[1:]):
            if before[0] == after[0] and after[1] <= before[2]:
                raise ValueError("omission intervals overlap within a source epoch")
        if not any(name in saturated for name in ("observed", "filtered", "admitted", "ingress_dropped")):
            if counters["observed"] != counters["filtered"] + counters["admitted"] + counters["ingress_dropped"]:
                raise ValueError("observed accounting does not balance")
        if not any(name in saturated for name in ("ingress_dropped", "fifo_dropped", "capacity_dropped")):
            if counters["ingress_dropped"] != counters["fifo_dropped"] + counters["capacity_dropped"]:
                raise ValueError("ingress drop accounting does not balance")
        if not any(name in saturated for name in ("reset_discarded", "storage_discarded", "admitted")):
            if counters["reset_discarded"] + counters["storage_discarded"] > counters["admitted"]:
                raise ValueError("downstream discards exceed admissions")
    _validate_trigger(value["trigger"])
    return value

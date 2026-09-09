from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType


_SCHEMAS = {
    "RETIRE": {"pc": "u32", "next_pc": "u32", "length": "length", "boundary": "u64"},
    "TRAP": {"pc": "u32?", "cause": "u32", "target": "u32?", "boundary": "u64"},
    "IRQ_ACCEPT": {"pc": "u32?", "cause": "u32", "target": "u32?", "boundary": "u64"},
    "IRQ_PENDING": {"previous": "u32", "current": "u32"},
    "BUS_REQ": {"transaction": "u32", "address": "u32", "write": "bool", "data": "u32", "mask": "u4"},
    "BUS_RESP": {"transaction": "u32", "data": "u32?", "error": "bool"},
    "USER_EVENT": {"value": "u32"},
}
_ORDER = (
    ("RETIRE",),
    ("BUS_RESP", "BUS_REQ"),
    ("TRAP", "IRQ_ACCEPT", "IRQ_PENDING"),
    ("USER_EVENT",),
)
_SOURCES = {kind: source for source, kinds in enumerate(_ORDER) for kind in kinds}


def _valid_field(value: object, rule: str) -> bool:
    if rule.endswith("?"):
        if value is None:
            return True
        rule = rule[:-1]
    if rule == "bool":
        return type(value) is bool
    if type(value) is not int:
        return False
    if rule == "length":
        return value == 4
    return 0 <= value < 1 << int(rule[1:])


@dataclass(frozen=True)
class Observation:
    kind: str
    fields: Mapping[str, object]

    def __post_init__(self):
        if not isinstance(self.kind, str) or self.kind not in _SCHEMAS:
            raise ValueError("unsupported observation kind")
        if not isinstance(self.fields, Mapping):
            raise ValueError("observation fields must be a mapping")
        fields = dict(self.fields)
        schema = _SCHEMAS[self.kind]
        if fields.keys() != schema.keys():
            raise ValueError(f"{self.kind} requires exactly these fields: {', '.join(schema)}")
        for name, rule in schema.items():
            if not _valid_field(fields[name], rule):
                raise ValueError(f"{self.kind}.{name} must satisfy {rule}")
        object.__setattr__(self, "fields", MappingProxyType(fields))

    @property
    def source(self) -> int:
        return _SOURCES[self.kind]


@dataclass(frozen=True)
class Event:
    tick: int
    source: int
    epoch: int
    sequence: int
    lane: int
    observation: Observation


def normalize(observations: Iterable[Observation]) -> tuple[tuple[Observation, ...], ...]:
    try:
        cycle = tuple(observations)
    except TypeError as error:
        raise ValueError("observations must be iterable") from error
    by_kind = {}
    for observation in cycle:
        if not isinstance(observation, Observation):
            raise ValueError("cycle entries must be observations")
        if observation.kind in by_kind:
            raise ValueError(f"duplicate observation kind: {observation.kind}")
        by_kind[observation.kind] = observation
    if "TRAP" in by_kind and "IRQ_ACCEPT" in by_kind:
        raise ValueError("TRAP and IRQ_ACCEPT are mutually exclusive within a tick")
    return tuple(tuple(by_kind[kind] for kind in kinds if kind in by_kind) for kinds in _ORDER)

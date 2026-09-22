KINDS = ("RETIRE", "TRAP", "IRQ_ACCEPT", "IRQ_PENDING", "BUS_REQ", "BUS_RESP", "USER_EVENT")
KEY_FIELDS = {"RETIRE": "pc", "TRAP": "cause", "IRQ_ACCEPT": "cause", "IRQ_PENDING": "current",
              "BUS_REQ": "address", "BUS_RESP": "transaction", "USER_EVENT": "value"}
_SLOT_KEYS = {"equal": {"kinds", "mode", "value", "mask"}, "range": {"kinds", "mode", "base", "limit"}}


def _kinds(kinds, *, empty=False):
    if type(kinds) not in (list, tuple) or (not kinds and not empty) or len(set(kinds)) != len(kinds) \
            or any(kind not in KINDS for kind in kinds):
        raise ValueError("kinds must be a list of unique event kinds")
    return frozenset(kinds)


def _u32(value, name):
    if type(value) is not int or not 0 <= value < 1 << 32:
        raise ValueError(f"trigger {name} must be u32")
    return value


def _slot(slot):
    if type(slot) is not dict or slot.get("mode") not in _SLOT_KEYS or slot.keys() != _SLOT_KEYS[slot["mode"]]:
        raise ValueError("trigger slot must be {kinds, mode=equal, value, mask} or {kinds, mode=range, base, limit}")
    kinds = _kinds(slot["kinds"])
    if slot["mode"] == "equal":
        value, mask = _u32(slot["value"], "value"), _u32(slot["mask"], "mask")
        return kinds, lambda key: key & mask == value & mask
    base, limit = _u32(slot["base"], "base"), _u32(slot["limit"], "limit")
    if base >= limit:
        raise ValueError("trigger range must be nonempty [base, limit)")
    return kinds, lambda key: base <= key < limit


def matcher(slots, capacity):
    if type(slots) not in (list, tuple) or len(slots) > capacity:
        raise ValueError(f"at most {capacity} trigger slots are available")
    checks = [None if slot is None else _slot(slot) for slot in slots]

    def match(observation):
        hits = [f"slot{index}" for index, entry in enumerate(checks) if entry is not None
                and observation.kind in entry[0] and entry[1](observation.fields[KEY_FIELDS[observation.kind]])]
        return "|".join(hits) or None
    return match


def keeper(kinds):
    kept = _kinds(kinds, empty=True)
    return lambda observation: observation.kind in kept


def slot_mask(reason):
    return sum(1 << int(name[4:]) for name in reason.split("|"))

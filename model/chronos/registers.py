from scripts.config import ROOT, read_json, validate
from .controller import COMMANDS, CaptureController
from .predicates import KINDS, slot_mask

MAP_PATH = ROOT / "spec/registers.json"
GROUPS = ("identity", "configuration", "commands", "triggers", "status", "accounting", "readout")
LANES = ((0, 0), (1, 0), (1, 1), (2, 0), (2, 1), (3, 0))


def check(doc):
    if type(doc) is not dict or doc.keys() != {"schema_version", "status", "word_bits", "window_bytes",
                                               "byte_order", "enums", "registers"}:
        raise ValueError("register map fields")
    if doc["schema_version"] != 1 or doc["word_bits"] != 32 or doc["byte_order"] != "little":
        raise ValueError("unsupported register map version, word size, or byte order")
    enums = doc["enums"]
    if type(enums) is not dict or any(type(values) is not list or not values or len(set(values)) != len(values)
                                      or any(type(value) is not str for value in values) for values in enums.values()):
        raise ValueError("enums must be nonempty lists of unique names")
    registers, offsets = {}, set()
    for register in doc["registers"]:
        if type(register) is not dict or register.keys() != {"name", "offset", "access", "group", "fields"}:
            raise ValueError("register fields")
        name, offset = register["name"], register["offset"]
        if type(offset) is not str or not offset.startswith("0x"):
            raise ValueError(f"{name}: offset must be a hex string")
        offset = int(offset, 16)
        if name in registers or offset in offsets or offset % 4 or offset >= doc["window_bytes"]:
            raise ValueError(f"{name}: duplicate, misaligned, or out-of-window register")
        if register["access"] not in ("ro", "rw", "wo") or register["group"] not in GROUPS:
            raise ValueError(f"{name}: unknown access or group")
        fields, used = {}, 0
        for field in register["fields"]:
            if type(field) is not dict or not {"name", "lsb", "width"} <= field.keys() <= {"name", "lsb", "width",
                                                                                         "reset", "enum"}:
                raise ValueError(f"{name}: field keys")
            lsb, width = field["lsb"], field["width"]
            if type(lsb) is not int or type(width) is not int or width < 1 or lsb < 0 or lsb + width > 32:
                raise ValueError(f"{name}.{field['name']}: field outside the word")
            bits = ((1 << width) - 1) << lsb
            if bits & used or field["name"] in fields:
                raise ValueError(f"{name}.{field['name']}: overlapping or duplicate field")
            used |= bits
            if "enum" in field and (field["enum"] not in enums or len(enums[field["enum"]]) > 1 << width):
                raise ValueError(f"{name}.{field['name']}: enum missing or too wide")
            if type(field.get("reset", 0)) is not int or not 0 <= field.get("reset", 0) < 1 << width:
                raise ValueError(f"{name}.{field['name']}: reset value does not fit")
            fields[field["name"]] = field
        offsets.add(offset)
        registers[name] = dict(register, offset=offset, fields=fields)
    missing = set(GROUPS) - {register["group"] for register in registers.values()}
    if missing:
        raise ValueError(f"register map lacks groups {sorted(missing)}")
    return dict(doc, registers=registers)


def load(path=MAP_PATH):
    return check(read_json(path))


MAP = load()


def pack(name, **values):
    register = MAP["registers"][name]
    word = 0
    for key, value in values.items():
        field = register["fields"][key]
        if "enum" in field and type(value) is str:
            value = MAP["enums"][field["enum"]].index(value)
        if type(value) is bool:
            value = int(value)
        if type(value) is not int or not 0 <= value < 1 << field["width"]:
            raise ValueError(f"{name}.{key} does not fit")
        word |= value << field["lsb"]
    return word


def unpack(name, word):
    if type(word) is not int or not 0 <= word < 1 << 32:
        raise ValueError("register words are u32")
    result = {}
    for key, field in MAP["registers"][name]["fields"].items():
        value = word >> field["lsb"] & (1 << field["width"]) - 1
        result[key] = MAP["enums"][field["enum"]][value] if "enum" in field else value
    return result


def _reset(name):
    return sum(field.get("reset", 0) << field["lsb"] for field in MAP["registers"][name]["fields"].values())


def _kinds(mask):
    return [kind for index, kind in enumerate(KINDS) if mask >> index & 1]


class RegisterFile:
    def __init__(self, config):
        self.config = validate(dict(config))
        self.controller = CaptureController()
        self._words = {name: _reset(name) for name, register in MAP["registers"].items() if register["access"] == "rw"}
        self._command = None
        self._outcomes = {}

    def write(self, name, word):
        if name not in MAP["registers"] or MAP["registers"][name]["access"] == "ro":
            raise ValueError(f"{name} is not writable")
        unpack(name, word)
        if name == "COMMAND":
            self._command = word
        else:
            self._words[name] = word

    def _wide(self, name):
        return self._words[f"{name}_LO"] | self._words[f"{name}_HI"] << 32

    def settings(self):
        split, mode = unpack("CFG_SPLIT", self._words["CFG_SPLIT"]), unpack("CFG_MODE", self._words["CFG_MODE"])
        triggers = []
        for slot in range(self.config["trigger_slots"]):
            control = unpack(f"TRIG{slot}_CTRL", self._words[f"TRIG{slot}_CTRL"])
            if not control["enable"]:
                triggers.append(None)
                continue
            names = ("value", "mask") if control["mode"] == "equal" else ("base", "limit")
            triggers.append(dict(kinds=_kinds(control["kinds"]), mode=control["mode"],
                                 **{key: self._words[f"TRIG{slot}_{key.upper()}"] for key in names}))
        settings = dict(config=dict(self.config, **split), post_ticks=self._wide("CFG_POST_TICKS"),
                        codec=mode["codec"], keep_kinds=_kinds(mode["keep_kinds"]), triggers=triggers)
        if self._words["CFG_DRAIN_LIMIT"]:
            settings["drain_limit"] = self._words["CFG_DRAIN_LIMIT"]
        return settings

    def cycle(self, tick, observations=(), *, service=False):
        word, self._command = self._command, None
        command = unpack("COMMAND", word or 0)
        commands = {name: bool(command[name]) for name in COMMANDS if name not in ("configure", "reset_source")}
        if command["configure"]:
            commands["configure"] = self.settings()
        if command["reset_source"]:
            commands["reset_source"] = command["reset_source_id"]
        result = self.controller.cycle(tick, observations, service=service, **commands)
        if word is not None:
            self._outcomes = result["outcomes"]
        return result

    def _readout(self):
        if self.controller.state != "FROZEN":
            return None
        if self.controller.read()[0] != self._wide("READ_SESSION"):
            return None
        return b"".join(page for _, _, page in self.controller.capture.ring.directory())

    def _trigger_match(self, trigger):
        if trigger is None:
            return _reset("TRIG_MATCH")
        lanes = slots = 0
        for source, lane, reason in trigger["matches"]:
            lanes |= 1 << LANES.index((source, lane))
            slots |= slot_mask(reason)
        primary = trigger["primary"]
        return pack("TRIG_MATCH", lanes=lanes, primary=7 if primary is None else LANES.index(tuple(primary[:2])),
                    software=trigger["software"], slots=slots)

    def read(self, name):
        register = MAP["registers"].get(name)
        if register is None or register["access"] == "wo":
            raise ValueError(f"{name} is not readable")
        if register["access"] == "rw":
            return self._words[name]
        status = self.controller.status()
        capture = self.controller.capture
        metadata = None if capture is None else capture.metadata.snapshot()
        trigger = status["trigger"]
        if name in ("CHRONOS_ID", "VERSION"):
            return _reset(name)
        if name == "CAPS":
            config = self.config
            return pack("CAPS", sources=config["source_count"], trigger_slots=config["trigger_slots"],
                        fifo_depth_log2=config["fifo_depth"].bit_length() - 1,
                        page_bytes_log2=config["page_bytes"].bit_length() - 1, codecs=3,
                        max_record_bytes=config["max_record_bytes"], sink_bytes=config["sink_width_bits"] // 8)
        if name == "CAPS_SRAM_BYTES":
            return self.config["sram_bytes"]
        if name == "STATUS":
            rows = [] if metadata is None else metadata["sources"]
            return pack("STATUS", state=status["state"], stop_reason=status["stop_reason"] or "none",
                        storage_error=status["storage_error"] or "none", drain_timeout=status["drain_timeout"],
                        trigger_latched=trigger is not None, trigger_software=bool(trigger and trigger["software"]),
                        configured=status["configured"],
                        journal_overflow=sum(row["journal_overflow"] << row["source"] for row in rows),
                        counter_saturated=sum(bool(row["saturated"]) << row["source"] for row in rows))
        if name == "OUTCOME":
            return pack("OUTCOME", **{key: self._outcomes.get(key, "none") for key in COMMANDS})
        if name == "DRAIN_CYCLES":
            return status["drain_cycles"]
        if name == "TRIG_MATCH":
            return self._trigger_match(trigger)
        if name.startswith(("SESSION_ID", "CONFIG_TAG", "TRIG_TICK", "ACCT_VALUE")):
            if name.startswith("SESSION_ID"):
                value = status["session_id"]
            elif name.startswith("CONFIG_TAG"):
                value = status["config_tag"]
            elif name.startswith("TRIG_TICK"):
                value = 0 if trigger is None else trigger["tick"]
            else:
                select = unpack("ACCT_SELECT", self._words["ACCT_SELECT"])
                value = 0 if metadata is None else metadata["sources"][select["source"]]["counters"][select["counter"]]
            return value >> 32 if name.endswith("_HI") else value & 0xFFFFFFFF
        wire = self._readout()
        if name == "READ_LENGTH":
            return 0 if wire is None else len(wire)
        if name == "READ_STATUS":
            return pack("READ_STATUS", valid=wire is not None, stale=wire is None)
        if wire is None:
            return 0
        offset = self._words["READ_OFFSET"]
        self._words["READ_OFFSET"] = min(offset + 4, 0xFFFFFFFF)
        return int.from_bytes(wire[offset:offset + 4].ljust(4, b"\0"), "little")

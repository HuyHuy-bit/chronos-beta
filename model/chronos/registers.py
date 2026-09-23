from scripts.config import ROOT, read_json

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

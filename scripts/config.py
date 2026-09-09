import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "spec/config.schema.json"


def read_json(path):
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate key: {key}")
            result[key] = value
        return result

    return json.loads(Path(path).read_text(), object_pairs_hook=unique_object)


def validate(config):
    schema = read_json(SCHEMA)
    if type(config) is not dict:
        raise ValueError("configuration must be an object")
    missing = set(schema["required"]) - config.keys()
    extra = config.keys() - schema["properties"].keys()
    if missing or extra:
        raise ValueError(f"missing keys: {sorted(missing)}; unknown keys: {sorted(extra)}")
    types = {"integer": int, "string": str, "null": type(None)}
    for key, rules in schema["properties"].items():
        value = config[key]
        if type(value) is not types[rules["type"]]:
            raise ValueError(f"{key}: expected {rules['type']}")
        if "enum" in rules and value not in rules["enum"]:
            raise ValueError(f"{key}: unsupported value {value!r}")
        if "minimum" in rules and value < rules["minimum"]:
            raise ValueError(f"{key}: below minimum")
        if "maximum" in rules and value > rules["maximum"]:
            raise ValueError(f"{key}: above maximum")
    for key in ("fifo_depth", "sram_bytes"):
        value = config[key]
        if value & (value - 1):
            raise ValueError(f"{key}: must be a power of two")
    if config["sram_bytes"] % config["page_bytes"]:
        raise ValueError("SRAM must contain whole pages")
    if config["pre_pages"] + config["post_pages"] != config["sram_bytes"] // config["page_bytes"]:
        raise ValueError("pre/post page allocation must equal SRAM capacity")
    if config["max_record_bytes"] > config["page_bytes"] - config["page_header_bytes"]:
        raise ValueError("a complete record must fit in page payload")
    return config


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("config", type=Path)
    args = parser.parse_args()
    try:
        validate(read_json(args.config))
    except (OSError, ValueError) as error:
        parser.exit(1, f"FAIL: {error}\n")
    print("PASS: provisional configuration structure; capacity/service proof remains pending")


if __name__ == "__main__":
    main()

from types import MappingProxyType

from scripts.config import validate
from .compact_encode import RunStream, encode_page as encode_compact_page
from .events import Event, Observation
from .raw_encode import encode_page, encode_record


CODECS = ("raw-v1", "compact-v1")


class StorageError(ValueError):
    pass


class PageRing:
    def __init__(self, config, *, session_id=1, config_tag=1, generation_bits=64, codec="raw-v1"):
        validate(config)
        if config["source_count"] != 4:
            raise ValueError("the page ring requires four sources")
        if type(generation_bits) is not int or not 1 <= generation_bits <= 64:
            raise ValueError("generation_bits must be in 1..64")
        for value in (session_id, config_tag):
            if type(value) is not int or not 0 <= value < 1 << 64:
                raise ValueError("session_id and config_tag must be u64 integers")
        if codec not in CODECS:
            raise ValueError("codec must be raw-v1 or compact-v1")
        self._codec = codec
        self._stream = RunStream()
        self._reserve = 0
        self.sealed_overhead_bytes = 0
        self._config = MappingProxyType(dict(config))
        self._session_id = session_id
        self._config_tag = config_tag
        self._generation_limit = 1 << generation_bits
        self._next_generation = 0
        self._next_pre_slot = 0
        self._next_post_slot = config["pre_pages"]
        self._pinned = False
        self._frozen = False
        self._committed = {}
        self._active_slot = None
        self._active_generation = None
        self._builder = []
        self._builder_bytes = 0
        self._last = {}
        self._evicted_pages = 0
        self._evicted_events = 0
        self._eviction_saturated = False
        self._snapshot = None

    def _writable(self):
        if self._frozen:
            raise ValueError("page ring is frozen")

    def _allocate(self):
        if self._next_generation == self._generation_limit:
            raise StorageError("generation_exhausted")
        if self._pinned:
            if self._next_post_slot == self._config["pre_pages"] + self._config["post_pages"]:
                raise StorageError("post_capacity")
            slot = self._next_post_slot
            self._next_post_slot += 1
        else:
            slot = self._next_pre_slot
            self._next_pre_slot = (slot + 1) % self._config["pre_pages"]
        old = self._committed.pop(slot, None)
        if old is not None:
            limit = (1 << 64) - 1
            pages = self._evicted_pages + 1
            events = self._evicted_events + old[2]
            self._eviction_saturated |= pages > limit or events > limit
            self._evicted_pages = min(pages, limit)
            self._evicted_events = min(events, limit)
        self._active_slot = slot
        self._active_generation = self._next_generation
        self._next_generation += 1

    def append(self, event):
        self._writable()
        record = encode_record(event)
        observation = Observation(event.observation.kind, event.observation.fields)
        event = Event(event.tick, event.source, event.epoch, event.sequence, event.lane, observation)
        prior = self._last.get(event.source)
        if prior is not None:
            if event.tick < prior.tick or event.epoch < prior.epoch:
                raise ValueError("source tick and epoch must not decrease")
            if event.epoch == prior.epoch:
                if event.sequence <= prior.sequence:
                    raise ValueError("source sequence must increase within an epoch")
                if event.tick == prior.tick and event.lane <= prior.lane:
                    raise ValueError("source lane must increase within an epoch and tick")
        capacity = self._config["page_bytes"] - self._config["page_header_bytes"]
        if self._builder_bytes + self._reserve + len(record) > capacity:
            self.seal()
        if self._active_slot is None:
            self._allocate()
        self._builder.append(event)
        self._last[event.source] = event
        if self._codec == "compact-v1":
            self._take(self._stream.push(event), len(record))
            return
        self._builder_bytes += len(record)
        if self._builder_bytes == capacity:
            self.seal()

    def _take(self, records, raw_bytes=0):
        self._builder_bytes += sum(map(len, records))
        if self._stream.pending == 0:
            self._reserve = 0
        elif self._stream.pending == 1 and raw_bytes:
            self._reserve = raw_bytes

    def advance(self, watermark):
        self._writable()
        if self._codec == "compact-v1":
            self._take(self._stream.advance(watermark))

    def seal(self):
        self._writable()
        if self._codec == "compact-v1":
            self._take(self._stream.flush())
        if not self._builder:
            return
        identity = dict(session_id=self._session_id, generation=self._active_generation,
                        config_tag=self._config_tag, page_bytes=self._config["page_bytes"])
        if self._codec == "compact-v1":
            page = encode_compact_page(self._builder, max_events=100000, **identity)
        else:
            page = encode_page(self._builder, **identity)
        assert int.from_bytes(page[32:36], "little") == self._builder_bytes
        self.sealed_overhead_bytes += len(page) - self._builder_bytes
        self._committed[self._active_slot] = (self._active_generation, page, len(self._builder))
        self._active_slot = None
        self._active_generation = None
        self._builder = []
        self._builder_bytes = 0

    def pin(self):
        self._writable()
        self._pinned = True

    def freeze(self):
        if self._frozen:
            return self._snapshot
        self.pin()
        self.seal()
        self._frozen = True
        self._snapshot = tuple(page for _, _, page in self.directory())
        return self._snapshot

    def directory(self):
        return tuple(sorted(((slot, entry[0], entry[1]) for slot, entry in self._committed.items()),
                            key=lambda entry: entry[1]))

    def summary(self):
        return {
            "page_bytes": self._config["page_bytes"],
            "pre_pages": self._config["pre_pages"],
            "post_pages": self._config["post_pages"],
            "pinned": self._pinned,
            "frozen": self._frozen,
            "active_slot": self._active_slot,
            "evicted_pages": self._evicted_pages,
            "evicted_events": self._evicted_events,
            "eviction_saturated": self._eviction_saturated,
            "committed_pages": len(self._committed),
            "next_generation": self._next_generation,
        }

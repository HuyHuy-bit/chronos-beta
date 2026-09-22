import hashlib
import json

from .admission import CaptureModel
from .capacity import measured_inventory
from .capture_metadata import COUNTERS, CaptureMetadata
from .capture_session import encode_capture
from .events import normalize
from .compact_decode import decode_page as decode_compact_page
from .raw_decode import decode_page
from .raw_session import encode_fragment
from .retention import PageRing, StorageError


class SnapshotCapture:
    def __init__(self, config, *, post_ticks=0, counter_bits=64, metadata_bits=64,
                 journal_capacity=4, generation_bits=64, session_id=1, config_tag=1,
                 keep=None, match=None, codec='raw-v1', measured=False):
        self.config = dict(config)
        self.session_id = session_id
        self.config_tag = config_tag
        self._post_ticks = post_ticks
        self._counter_bits = counter_bits
        self._metadata_bits = metadata_bits
        self._journal_capacity = journal_capacity
        self._generation_bits = generation_bits
        if type(measured) is not bool:
            raise ValueError('measured must be bool')
        self._codec = codec
        self._measured = measured
        if keep is not None and not callable(keep) or match is not None and not callable(match):
            raise ValueError('keep and match must be callable')
        self._user_keep = keep
        self._user_match = match
        self._initialize()

    def _initialize(self):
        self._decisions = {}
        self.ring = PageRing(self.config, session_id=self.session_id, config_tag=self.config_tag,
                             generation_bits=self._generation_bits, codec=self._codec)
        inventory = measured_inventory(self.config, self._codec) if self._measured else None
        self.model = CaptureModel(self.config, post_ticks=self._post_ticks, counter_bits=self._counter_bits,
                                  keep=self._keep, match=self._match, inventory=inventory)

        self.metadata = CaptureMetadata(counter_bits=self._metadata_bits, journal_capacity=self._journal_capacity)
        self.storage_error = None
        self.frozen = False
        self._export = None

    def _advance(self):
        self.ring.advance(self.model.watermark(0))

    def _keep(self, observation):
        result = True if self._user_keep is None else self._user_keep(observation)
        self._decisions[observation.source, observation.kind] = result
        return result

    def _match(self, observation):
        result = None if self._user_match is None else self._user_match(observation)
        if result is not None:
            try:
                if type(result) is not str or not result or len(result.encode('utf-8')) > 64:
                    raise ValueError('trigger reason must be 1..64 UTF-8 bytes')
            except UnicodeError as error:
                raise ValueError('invalid trigger reason encoding') from error
        return result

    def _totals(self):
        totals = [dict.fromkeys(COUNTERS, 0) for _ in range(4)]
        for (source, _), counts in self.model.stats.items():
            for key, value in counts.items():
                totals[source][key] += value
        return totals

    def _sync(self, before):
        after = self._totals()
        for source in range(4):
            self.metadata.add(source, **{key: value - before[source][key] for key, value in after[source].items()})

    def _reset_source(self, source):
        if type(source) is not int or not 0 <= source < 4:
            raise ValueError('invalid reset source')
        queued = tuple(self.model.queues[source])
        epoch = self.model.epochs[source]
        before = self._totals()
        self.model.source_reset(source)
        self._sync(before)
        if self.model.epochs[source] != epoch:
            for event in queued:
                self.metadata.mark(source, epoch=event.epoch, sequence=event.sequence,
                                   tick=event.tick, reason='reset_discarded')

    def step(self, tick, observations=(), service=False, stop=False, trace_reset=False, reset_source=None,
             software_trigger=False):
        if any(type(flag) is not bool for flag in (service, stop, trace_reset, software_trigger)):
            raise ValueError('control flags must be bool')
        if trace_reset:
            if self.session_id == (1 << 64) - 1:
                raise ValueError('session identity exhausted')
            self.session_id += 1
            self._initialize()
            return
        if self.frozen or self.storage_error is not None:
            raise ValueError('capture is frozen or storage is halted')
        if stop:
            self.model.stop('manual')
            self.ring.pin()
            if service:
                self.service()
            else:
                self._advance()
            return
        if reset_source is not None:
            self._reset_source(reset_source)
        bundles = normalize(observations)
        before = self._totals()
        epochs, sequences = self.model.epochs[:], self.model.sequences[:]
        self._decisions = {}
        self.model.step(tick, tuple(event for bundle in bundles for event in bundle), service=False,
                        software_trigger=software_trigger)
        self._sync(before)
        for source, bundle in enumerate(bundles):
            if self.model.sequences[source] == sequences[source]:
                continue
            counts = self._totals()[source]
            reason = 'capacity' if counts['capacity_dropped'] > before[source]['capacity_dropped'] else 'fifo'
            dropped = counts['ingress_dropped'] > before[source]['ingress_dropped']
            for lane, observation in enumerate(bundle):
                filtered = not self._decisions[source, observation.kind]
                if filtered or dropped:
                    self.metadata.mark(source, epoch=epochs[source], sequence=sequences[source] + lane,
                                       tick=tick, reason='filtered' if filtered else reason)
        if self.model.trigger is not None:
            self.ring.pin()
            self.metadata.latch_trigger(self.model.trigger['tick'], self.model.trigger['matches'],
                                        software=self.model.trigger['software'])
        if self.model.stop_reason is not None and not self.ring.summary()['pinned']:
            self.ring.pin()
        if service:
            self.service()
        else:
            self._advance()

    def service(self):
        if self.frozen or self.storage_error is not None:
            raise ValueError('capture is frozen or storage is halted')
        event = self.model.service()
        if event is not None:
            try:
                self.ring.append(event)
            except StorageError as error:
                self.storage_error = str(error)
                self.model.stop('storage_failure')
                self.ring.pin()
                self.metadata.add(event.source, storage_discarded=1)
                self.metadata.mark(event.source, epoch=event.epoch, sequence=event.sequence,
                                   tick=event.tick, reason='storage_discarded')
        if self.storage_error is None:
            self._advance()
        return event

    def freeze(self, *, incomplete=False):
        if type(incomplete) is not bool:
            raise ValueError('incomplete must be bool')
        if self.frozen:
            return self._export
        if self.model.stop_reason is None:
            raise ValueError('stop admission before freezing')
        if not incomplete and (not self.model.complete or self.storage_error is not None):
            raise ValueError('capture has undrained or failed work')
        pages = self.ring.freeze()
        directory = []
        decoder = decode_compact_page if self._codec == 'compact-v1' else decode_page
        for raw in pages:
            page = decoder(raw, page_bytes=self.config['page_bytes'], max_events=100000)
            directory.append(dict(generation=page['generation'], record_count=len(page['events']),
                                  payload_crc32=page['payload_crc32']))
        manifest = dict(schema_version=1, scope='event-fragment', provenance='model', session_id=self.session_id,
                        config_tag=self.config_tag, page_bytes=self.config['page_bytes'],
                        source_profile='rv32-single-clock-v1', codecs=[self._codec], pages=directory,
                        config_sha256=hashlib.sha256(json.dumps(self.config, sort_keys=True,
                                              separators=(',', ':')).encode()).hexdigest())
        fragment = encode_fragment(pages, manifest)
        metadata = dict(schema_version=2, scope='capture-snapshot', provenance='model',
                        session_id=self.session_id, config_tag=self.config_tag,
                        capture=self.metadata.snapshot(), retention=self.ring.summary(),
                        terminal=dict(reason=self.model.stop_reason, drain_complete=self.model.complete and self.storage_error is None,
                                      storage_error=self.storage_error, pending_events=list(map(len, self.model.queues)),
                                      rejected_cycle_events=self.model.rejected_cycle_events))
        self._export = encode_capture(fragment, metadata)
        self.frozen = True
        return self._export

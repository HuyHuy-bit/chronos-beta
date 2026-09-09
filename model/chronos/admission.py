from collections import deque

from scripts.config import validate
from .capacity import completion_budget, default_inventory
from .events import Event, normalize


class CaptureModel:
    def __init__(self, config, post_ticks=0, counter_bits=64, keep=None, match=None):
        validate(config)
        if config['source_count'] != 4:
            raise ValueError('the synthetic profile requires four sources')
        if type(counter_bits) is not int or not 1 <= counter_bits <= 64:
            raise ValueError('counter_bits must be in 1..64')
        self.limit = (1 << counter_bits) - 1
        if type(post_ticks) is not int or not 0 <= post_ticks <= self.limit:
            raise ValueError('post_ticks exceeds the counter range')
        if keep is not None and not callable(keep):
            raise ValueError('keep must be callable')
        if match is not None and not callable(match):
            raise ValueError('match must be callable')
        self.config = dict(config)
        self.budget = completion_budget(config, default_inventory(config))
        if not self.budget['safe']:
            raise ValueError('insufficient post reserve for the declared completion inventory')
        self.post_ticks = post_ticks
        self.keep = keep if keep is not None else lambda observation: True
        self.match = match if match is not None else lambda observation: None
        self.queues = tuple(deque() for _ in range(4))
        self.emitted = []
        self.epochs = [0] * 4
        self.sequences = [0] * 4
        self.stats = {(source, 0): self._new_stats() for source in range(4)}
        self.trigger = None
        self.stop_reason = None
        self.rejected_cycle_events = 0
        self._last_tick = -1
        self._round_robin = 0
        self._active = None
        self._remaining = 0
        self._post_completed = 0
        self.peak_reserved_bytes = self.reserved_bytes

    @staticmethod
    def _new_stats():
        return dict.fromkeys(('observed', 'filtered', 'admitted', 'ingress_dropped',
                              'reset_discarded', 'fifo_dropped', 'capacity_dropped'), 0)

    @property
    def reserved_bytes(self):
        entries = sum(map(len, self.queues)) + self._post_completed
        return self.budget['overhead_bytes'] + entries * self.config['max_record_bytes']

    @property
    def complete(self):
        return self.stop_reason is not None and not any(self.queues)

    def stop(self, reason='manual'):
        if type(reason) is not str or not reason:
            raise ValueError('stop reason must be a nonempty string')
        if self.stop_reason is None:
            self.stop_reason = reason

    def step(self, tick, observations=(), service=False):
        if type(tick) is not int or not 0 <= tick <= self.limit or tick <= self._last_tick:
            raise ValueError('tick must increase within the active counter range')
        if type(service) is not bool:
            raise ValueError('service must be bool')
        bundles = normalize(observations)
        decisions = []
        for source, bundle in enumerate(bundles):
            for lane, observation in enumerate(bundle):
                retain = self.keep(observation)
                reason = self.match(observation)
                if type(retain) is not bool:
                    raise ValueError('keep must return bool')
                if reason is not None and (type(reason) is not str or not reason):
                    raise ValueError('match must return a nonempty string or None')
                decisions.append((source, lane, observation, retain, reason))
        self._last_tick = tick
        if self.trigger is not None and tick > self.trigger['tick'] + self.post_ticks:
            self.stop('post_window')
        if self.stop_reason is not None:
            if service:
                self.service()
            return
        if any(self.sequences[source] + len(bundle) > self.limit + 1
               for source, bundle in enumerate(bundles)):
            self.rejected_cycle_events += len(decisions)
            self.stop('sequence_exhausted')
            if service:
                self.service()
            return
        matches = tuple((source, lane, reason)
                        for source, lane, _, _, reason in decisions if reason is not None)
        if self.trigger is None and matches:
            if tick + self.post_ticks > self.limit:
                self.rejected_cycle_events += len(decisions)
                self.stop('time_window_overflow')
                if service:
                    self.service()
                return
            self.trigger = {'tick': tick, 'matches': matches, 'primary': matches[0]}
        eligible = [[] for _ in range(4)]
        for source, lane, observation, retain, _ in decisions:
            sequence = self.sequences[source]
            self.sequences[source] += 1
            counts = self.stats[source, self.epochs[source]]
            counts['observed'] += 1
            if retain:
                eligible[source].append(Event(tick, source, self.epochs[source],
                                              sequence, lane, observation))
            else:
                counts['filtered'] += 1
        capacity_closed = False
        for source, events in enumerate(eligible):
            if not events:
                continue
            counts = self.stats[source, self.epochs[source]]
            cost = len(events) * self.config['max_record_bytes']
            if capacity_closed or (self.trigger is not None and
                                   self.reserved_bytes + cost > self.budget['payload_bytes']):
                capacity_closed = True
                counts['capacity_dropped'] += len(events)
                counts['ingress_dropped'] += len(events)
            elif len(self.queues[source]) + len(events) > self.config['fifo_depth']:
                counts['fifo_dropped'] += len(events)
                counts['ingress_dropped'] += len(events)
            else:
                self.queues[source].extend(events)
                counts['admitted'] += len(events)
                self.peak_reserved_bytes = max(self.peak_reserved_bytes, self.reserved_bytes)
        if capacity_closed:
            self.stop('capacity')
        if self.trigger is not None and tick == self.trigger['tick'] + self.post_ticks:
            self.stop('post_window')
        if tick == self.limit:
            self.stop('time_exhausted')
        if service:
            self.service()

    def service(self):
        if self._active is None:
            for offset in range(4):
                candidate = (self._round_robin + offset) % 4
                if self.queues[candidate]:
                    self._active = candidate
                    self._remaining = self.config['max_record_bytes']
                    break
        if self._active is None:
            return None
        self._remaining -= self.config['sink_width_bits'] // 8
        if self._remaining > 0:
            return None
        source = self._active
        event = self.queues[source].popleft()
        self.emitted.append(event)
        if self.trigger is not None:
            self._post_completed += 1
        self._round_robin = (source + 1) % 4
        self._active = None
        return event

    def source_reset(self, source):
        if type(source) is not int or not 0 <= source < 4:
            raise ValueError('source must be in 0..3')
        if self.stop_reason is not None:
            raise ValueError('source reset requires active admission')
        if self.epochs[source] == self.limit:
            self.stop('epoch_exhausted')
            return
        counts = self.stats[source, self.epochs[source]]
        counts['reset_discarded'] += len(self.queues[source])
        self.queues[source].clear()
        if self._active == source:
            self._active = None
            self._remaining = 0
        self.epochs[source] += 1
        self.sequences[source] = 0
        self.stats[source, self.epochs[source]] = self._new_stats()

    def summary(self):
        return {
            'status': 'complete' if self.complete else 'draining' if self.stop_reason else
                      'post' if self.trigger is not None else 'armed',
            'stop_reason': self.stop_reason,
            'trigger': self.trigger,
            'certainty': 'aggregate_only',
            'stats': [dict(source=source, epoch=epoch, **counts)
                      for (source, epoch), counts in sorted(self.stats.items())],
            'queue_occupancies': list(map(len, self.queues)),
            'completed_events': len(self.emitted),
            'reserved_bytes': self.reserved_bytes,
            'margin_bytes': self.budget['payload_bytes'] - self.reserved_bytes,
            'peak_reserved_bytes': self.peak_reserved_bytes,
            'rejected_cycle_events': self.rejected_cycle_events,
        }

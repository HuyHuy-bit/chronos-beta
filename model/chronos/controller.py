from .events import normalize
from .snapshot import SnapshotCapture


STATES = ('DISABLED', 'ARMED', 'POST_TRIGGER', 'DRAINING', 'FROZEN', 'CLEARING')
COMMANDS = ('trace_reset', 'clear', 'configure', 'arm', 'stop', 'reset_source', 'software_trigger')
_SETTINGS = ('config', 'post_ticks', 'keep', 'match', 'drain_limit')
_U64_MAX = (1 << 64) - 1


def _prepare(settings):
    if not {'config', 'post_ticks'} <= settings.keys() <= set(_SETTINGS):
        raise ValueError('settings require config and post_ticks and allow keep, match, drain_limit')
    config = settings['config']
    if type(config) is not dict:
        raise ValueError('config must be an object')
    config = dict(config)
    SnapshotCapture(config, post_ticks=settings['post_ticks'], keep=settings.get('keep'),
                    match=settings.get('match'))
    grant_bytes = config['sink_width_bits'] // 8
    default = 4 * config['fifo_depth'] * -(-config['max_record_bytes'] // grant_bytes)
    drain_limit = settings.get('drain_limit', default)
    if type(drain_limit) is not int or not 1 <= drain_limit < 1 << 32:
        raise ValueError('drain_limit must be in 1..2**32-1')
    return dict(config=config, post_ticks=settings['post_ticks'], keep=settings.get('keep'),
                match=settings.get('match'), drain_limit=drain_limit)


class CaptureController:
    def __init__(self):
        self.state = 'DISABLED'
        self.session_id = 0
        self.config_tag = 0
        self._settings = None
        self._capture = None
        self._drain_limit = None
        self._export = None
        self._last_tick = -1
        self._drain_cycles = 0
        self._scrub_remaining = 0
        self.drain_timeout = False

    @property
    def capture(self):
        return self._capture

    def status(self):
        model = None if self._capture is None else self._capture.model
        return dict(state=self.state, session_id=self.session_id, config_tag=self.config_tag,
                    configured=self._settings is not None,
                    stop_reason=None if model is None else model.stop_reason,
                    trigger=None if model is None or model.trigger is None else dict(model.trigger),
                    drain_cycles=self._drain_cycles, drain_timeout=self.drain_timeout,
                    scrub_remaining=self._scrub_remaining,
                    storage_error=None if self._capture is None else self._capture.storage_error)

    def read(self):
        if self.state != 'FROZEN':
            raise ValueError('no frozen snapshot is readable')
        return self._export

    def _freeze(self, *, incomplete=False):
        self._export = (self.session_id, self._capture.freeze(incomplete=incomplete))
        self.state = 'FROZEN'

    def _scrub(self):
        self._scrub_remaining -= 1
        if self._scrub_remaining == 0:
            self.state = 'DISABLED'

    def cycle(self, tick, observations=(), *, configure=None, arm=False, software_trigger=False,
              stop=False, clear=False, trace_reset=False, reset_source=None, service=False):
        if type(tick) is not int or not self._last_tick < tick <= _U64_MAX:
            raise ValueError('tick must increase within u64')
        if any(type(flag) is not bool for flag in (arm, software_trigger, stop, clear, trace_reset, service)):
            raise ValueError('command flags must be bool')
        if configure is not None and type(configure) is not dict:
            raise ValueError('configure must be a settings object')
        if reset_source is not None and (type(reset_source) is not int or not 0 <= reset_source < 4):
            raise ValueError('reset_source must be in 0..3')
        observations = tuple(observations)
        normalize(observations)
        requested = dict(trace_reset=trace_reset, clear=clear, configure=configure is not None, arm=arm,
                         stop=stop, reset_source=reset_source is not None, software_trigger=software_trigger)
        self._last_tick = tick
        start = self.state
        capturing = start in ('ARMED', 'POST_TRIGGER')
        outcomes, reasons = {}, {}

        def result():
            for name in COMMANDS:
                if requested[name] and name not in outcomes:
                    outcomes[name] = 'superseded'
            return dict(state=self.state, outcomes=outcomes, reasons=reasons)

        def reject(name, reason):
            outcomes[name] = 'rejected'
            reasons[name] = reason

        if trace_reset:
            outcomes['trace_reset'] = 'accepted'
            self._capture = self._export = None
            self._drain_cycles = self._scrub_remaining = 0
            self.drain_timeout = False
            self.state = 'DISABLED'
            return result()
        if clear:
            if start not in ('DISABLED', 'FROZEN'):
                reject('clear', 'stop and freeze before clearing')
            elif self._settings is None:
                reject('clear', 'unconfigured memory geometry')
            else:
                outcomes['clear'] = 'accepted'
                config = self._settings['config']
                self._capture = self._export = None
                self._drain_cycles = 0
                self.drain_timeout = False
                self._scrub_remaining = config['sram_bytes'] // (config['sink_width_bits'] // 8)
                self.state = 'CLEARING'
                self._scrub()
                return result()
        if configure is not None:
            if start not in ('DISABLED', 'FROZEN'):
                reject('configure', 'configuration is immutable while capturing or clearing')
            elif self.config_tag == _U64_MAX:
                reject('configure', 'configuration tag exhausted')
            else:
                try:
                    self._settings = _prepare(configure)
                except ValueError as error:
                    reject('configure', str(error))
                else:
                    self.config_tag += 1
                    outcomes['configure'] = 'accepted'
        if arm:
            if start != 'DISABLED':
                reject('arm', 'clear before arming' if start == 'FROZEN' else 'arm requires DISABLED')
            elif self._settings is None:
                reject('arm', 'unconfigured')
            elif self.session_id == _U64_MAX:
                reject('arm', 'session identity exhausted')
            else:
                outcomes['arm'] = 'accepted'
                settings = self._settings
                self.session_id += 1
                self._capture = SnapshotCapture(settings['config'], post_ticks=settings['post_ticks'],
                                                keep=settings['keep'], match=settings['match'],
                                                session_id=self.session_id, config_tag=self.config_tag)
                self._drain_limit = settings['drain_limit']
                self._drain_cycles = 0
                self.drain_timeout = False
                self.state = 'ARMED'
                return result()
        if start == 'CLEARING':
            for name in ('stop', 'reset_source', 'software_trigger'):
                if requested[name]:
                    reject(name, 'clearing')
            self._scrub()
            return result()
        if stop:
            if capturing:
                outcomes['stop'] = 'accepted'
            elif start == 'DRAINING':
                outcomes['stop'] = 'ignored'
            else:
                reject('stop', 'no active capture')
        stopping = outcomes.get('stop') == 'accepted'
        if reset_source is not None and not stopping:
            if capturing:
                outcomes['reset_source'] = 'accepted'
            else:
                reject('reset_source', 'source reset requires active admission')
        if software_trigger and not stopping:
            if start == 'ARMED':
                outcomes['software_trigger'] = 'accepted'
            elif start == 'POST_TRIGGER':
                outcomes['software_trigger'] = 'ignored'
            else:
                reject('software_trigger', 'no armed capture')
        if start not in ('ARMED', 'POST_TRIGGER', 'DRAINING'):
            return result()
        capture = self._capture
        capture.step(tick, observations if capturing else (), service=service, stop=stopping,
                     reset_source=reset_source if outcomes.get('reset_source') == 'accepted' else None,
                     software_trigger=outcomes.get('software_trigger') == 'accepted')
        if capture.storage_error is not None:
            self._freeze(incomplete=True)
        elif capture.model.stop_reason is not None:
            if capture.model.complete:
                self._freeze()
            else:
                self._drain_cycles += 1
                if self._drain_cycles >= self._drain_limit:
                    self.drain_timeout = True
                    self._freeze(incomplete=True)
                else:
                    self.state = 'DRAINING'
        elif capture.model.trigger is not None:
            self.state = 'POST_TRIGGER'
        return result()

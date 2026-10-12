"""Music sync: colours follow whatever the default audio output is playing.

Audio comes from the PipeWire/Pulse monitor of the default sink via `parec`.
Bulbs accept about 10 colour changes a second (measured: 10/s all acked in
~200 ms, 20/s backs up to >1 s and drops), so each bulb keeps one update in
flight and only sends when the colour or level has moved noticeably.
"""

import colorsys
import math
import json
import os
import select
import signal
import socket
import subprocess
import sys
import threading
import time

import numpy as np

import aidot
import matter

try:
    import aubio  # python-aubio: real-time tempo tracking; optional
except ImportError:
    aubio = None

RATE = 48000
HOP = 1024          # ~21 ms of audio per analysis step
WINDOW = 2048
MIN_INTERVAL = 0.11  # seconds between updates to one bulb
ACK_TIMEOUT = 0.5    # resend allowed if the previous update was never acked
PID_FILE = aidot.CACHE_FILE.parent / "music.pid"
LOG_FILE = aidot.CACHE_FILE.parent / "music.log"
# Pre-sync state of each light, so a style switch (a new process taking over)
# still restores what the lights looked like before sync began.
SAVED_FILE = aidot.CACHE_FILE.parent / "music-saved.json"
_saved: dict = {}
_saved_lock = threading.Lock()


def remember(device_id: str, attrs: dict) -> None:
    with _saved_lock:
        _saved[device_id] = attrs
        aidot.write_private_json(SAVED_FILE, _saved)


# --- audio analysis -------------------------------------------------------

# Range of the mel-band "centre of mass" (0 = all bass, 1 = all treble) on real
# music, mapped to Smooth's warm-to-cool shift.
BRIGHTNESS_LOW, BRIGHTNESS_HIGH = 0.36, 0.57  # 5th/95th percentile on a live DJ set (40 bands)
CAPTURE_LATENCY = 0.04  # measured: monitor capture runs a steady ~38 ms behind
BULB_LATENCY = 0.10     # a bulb shows a change this long after it is sent: ~0.1 s over Matter,
                        # ~0.2 s over the AiDot protocol (run() sets it per protocol)


ENV_SUB = 256               # envelope step: 5.3 ms, finer than the 21 ms analysis blocks
ENV_FS = RATE / ENV_SUB


def lowpass(f0: float, q: float = 0.707):
    """A biquad low-pass as an aubio filter (runs in C)."""
    w = 2 * math.pi * f0 / RATE
    cw, alpha = math.cos(w), math.sin(w) / (2 * q)
    a0 = 1 + alpha
    f = aubio.digital_filter(3)
    f.set_biquad((1 - cw) / 2 / a0, (1 - cw) / a0, (1 - cw) / 2 / a0, -2 * cw / a0, (1 - alpha) / a0)
    return f


class BeatTracker:
    """Predicts beats so the lights land on them.

    Tempo comes from aubio's beat detections (robust across genres) with off-beat
    and skipped intervals folded back, refined +-5% against the onset envelope.
    Phase comes from the kick: where the energy below 150 Hz jumps within that
    period over the last 8 s. Every beat is sent early by the capture delay plus
    the bulb's reaction time.

    Graded offline against beat_this (neural reference) on four DJ sets and a song:
    aubio-only lights hit 9-14% of beats (aubio locked onto off-beat hats ~25% of
    the time, which also kept prediction from engaging); this tracker 33-68%,
    68% on house, with hits within ~10 ms of the beat.
    """

    WINDOW = 8.0

    def __init__(self):
        self.aubio = aubio.tempo("default", 1024, 512, RATE)
        self.filters = [lowpass(150), lowpass(150)]
        size = int(self.WINDOW * ENV_FS)
        self.kick = np.zeros(size)     # low-band onsets: phase
        self.onsets = np.zeros(size)   # all onsets: tempo refinement
        self.prev_low = self.prev_full = 0.0
        self.n = 0                     # envelope samples so far
        self.det = []                  # recent aubio detection times
        self.period = None             # envelope samples per beat
        self.next_beat = None          # envelope index of the next beat
        self.last_estimate = 0
        self.last_emitted = -1e9
        self.last_detection = -1e9
        self.now = 0.0
        self.detected = False
        self.detections = 0
        self.fire_index = 0
        self.pending = []              # (beat number, arrival time) of beats shown but not yet heard
        self.due = []                  # beat numbers heard in this block (for the bar tracker)

    @property
    def bpm(self) -> float | None:
        if self.period and self.now - self.last_detection < 2.0:
            return 60 * ENV_FS / self.period
        return None

    def feed(self, samples: np.ndarray, now: float) -> bool:
        x = np.ascontiguousarray(samples, dtype=np.float32)
        self.now = now
        self.detected = False
        for i in range(0, len(x), 512):
            if self.aubio(x[i:i + 512])[0]:
                self.detected = True
                self.detections += 1
                self.last_detection = now
                self.det.append(now - (len(x) - i - 512) / RATE)
        self.det = self.det[-16:]

        low = x
        for f in self.filters:
            low = f(low)
        k = len(x) // ENV_SUB
        low_db = np.log10(np.mean(low[:k * ENV_SUB].reshape(k, ENV_SUB) ** 2, axis=1) + 1e-10)
        full_db = np.log10(np.mean(x[:k * ENV_SUB].reshape(k, ENV_SUB) ** 2, axis=1) + 1e-10)
        self.kick = np.concatenate((self.kick[k:], np.maximum(0, np.diff(np.concatenate(([self.prev_low], low_db))))))
        self.onsets = np.concatenate((self.onsets[k:], np.maximum(0, np.diff(np.concatenate(([self.prev_full], full_db))))))
        self.prev_low, self.prev_full = low_db[-1], full_db[-1]
        self.n += k
        if self.n - self.last_estimate >= 0.5 * ENV_FS and self.n >= len(self.kick) and len(self.det) >= 6:
            self.last_estimate = self.n
            self._estimate()

        self.due = [b for b, t in self.pending if now >= t]
        self.pending = [(b, t) for b, t in self.pending if now < t]
        fire = False
        if self.next_beat is not None:
            lead = (CAPTURE_LATENCY + BULB_LATENCY) * ENV_FS
            while self.next_beat - lead <= self.n:
                if self.next_beat - self.last_emitted > 0.6 * self.period:  # never re-emit after a re-estimate
                    fire = True
                    self.fire_index += 1
                    self.last_emitted = self.next_beat
                    self.pending.append((self.fire_index, now + (self.next_beat - self.n) / ENV_FS))
                self.next_beat += self.period
        return fire

    def _estimate(self) -> None:
        ibi = np.diff(self.det)
        median = float(np.median(ibi))
        folded = []
        for v in ibi:  # off-beat (half) and skipped (double/triple) intervals back to one beat
            for m in (1, 2, 0.5, 3):
                if abs(v / m / median - 1) < 0.12:
                    folded.append(v / m)
                    break
        if len(folded) < 4:
            return
        rough = float(np.median(folded)) * ENV_FS
        # aubio's intervals are quantised to 10.7 ms, which would smear the phase
        # search over 8 s; refine within +-5% from the envelope's own repetition
        e = self.onsets + self.kick
        e = e - e.mean()
        lags = np.arange(int(rough * 0.95), int(rough * 1.05) + 2)
        score = np.array([np.dot(e[:-l], e[l:]) + 0.5 * (np.dot(e[:-2 * l], e[2 * l:]) if 2 * l < len(e) else 0)
                          for l in lags])
        i = int(np.argmax(score))
        frac = 0.0
        if 0 < i < len(score) - 1 and score[i - 1] - 2 * score[i] + score[i + 1] != 0:
            frac = 0.5 * (score[i - 1] - score[i + 1]) / (score[i - 1] - 2 * score[i] + score[i + 1])
        period = lags[i] + frac
        if self.period and abs(period / self.period - 1) < 0.04:
            period = 0.8 * self.period + 0.2 * period  # steady tempo: smooth it
        self.period = period
        # phase: where along the period do the kicks land (recent ones count more)?
        size = len(self.kick)
        steps = np.arange(0, size / period - 1) * period
        weights = np.linspace(1.0, 0.5, len(steps))
        best, best_phase = -1.0, 0.0
        for phase in np.arange(0, period, 1.0):
            idx = size - 1 - phase - steps
            idx = idx[idx >= 1]
            i0 = idx.astype(int)
            vals = self.kick[i0] + (idx - i0) * (self.kick[np.minimum(i0 + 1, size - 1)] - self.kick[i0])
            total = float((vals * weights[:len(vals)]).sum())
            if total > best:
                best, best_phase = total, phase
        upcoming = self.n - 1 - best_phase + period
        while upcoming <= self.n:
            upcoming += period
        self.next_beat = upcoming


class BarTracker:
    """Guesses which beat starts the bar; aubio's tempo has no downbeats.

    Measured against beat_this downbeats on four DJ sets, the signals that mark
    the "1" are: raw bass level (beat 1 had the strongest bass of its bar in
    37-65% of bars; it is high on 1 and 3, low on 2 and 4), and how much the
    chords and the sound's character changed since the previous beat (highest on
    1, lowest on 3). Hi-hats point the wrong way. Each beat's features are
    z-scored against the last 16 beats so loudness doesn't matter; the position
    that keeps scoring highest is the "1".
    """

    DECAY = 0.97     # per beat; how long earlier bars still count
    MARGIN = 1.0     # lead (in accumulated z-scores) needed to (re)lock
    CHALLENGE = 4    # beats a challenger must lead before the "1" moves

    def __init__(self):
        self.scores = np.zeros(4)
        self.history = {"bass": [], "timbre": [], "chord": []}
        self.previous = None   # (mel log spectrum, chroma) of the previous beat
        self.phase = None      # which beat number (mod 4) is the "1"
        self.challenger = 0    # beats in a row another position has clearly led

    @property
    def locked(self) -> bool:
        return self.phase is not None

    def _z(self, name: str, value: float) -> float:
        hist = self.history[name]
        hist.append(value)
        del hist[:-16]
        if len(hist) < 8:
            return 0.0
        arr = np.array(hist)
        return float((value - arr.mean()) / (arr.std() + 1e-9))

    def on_beat(self, index: int, raw_bass: float, mel_log: np.ndarray, chroma: np.ndarray) -> None:
        timbre = chord = 0.0
        if self.previous is not None:
            timbre = float(np.abs(mel_log - self.previous[0]).mean())
            denom = float(np.linalg.norm(chroma) * np.linalg.norm(self.previous[1])) or 1.0
            chord = 1.0 - float(chroma @ self.previous[1]) / denom
        self.previous = (mel_log.copy(), chroma.copy())
        score = self._z("bass", math.log10(raw_bass + 1e-12)) + 0.7 * self._z("timbre", timbre) \
            + 0.7 * self._z("chord", chord)
        self.scores *= self.DECAY
        self.scores[index % 4] += score
        if index < 16:
            return
        top, margin = int(np.argmax(self.scores)), self.MARGIN
        if self.phase is None:
            ordered = np.sort(self.scores)
            if ordered[-1] - ordered[-2] > margin:
                self.phase = top
        elif top != self.phase and self.scores[top] - self.scores[self.phase] > margin:
            # hysteresis: only move the "1" when another position keeps winning clearly
            self.challenger += 1
            if self.challenger >= self.CHALLENGE:
                self.phase, self.challenger = top, 0
        else:
            self.challenger = 0

    def position(self, index: int) -> int | None:
        """0 for the first beat of the bar, 1-3 for the others; None while unsure."""
        if self.phase is None:
            return None
        return (index - self.phase) % 4


class MelBands:
    """Loudness per mel band (spaced like hearing), from aubio's filterbank."""

    COUNT = 40
    LOW = 4  # the lowest bands carry kick and bass

    def __init__(self):
        self.bank = aubio.filterbank(self.COUNT, 2048)
        self.bank.set_mel_coeffs(RATE, 40, 12000)
        self.low = np.arange(self.COUNT) < self.LOW
        self.peaks = np.full(self.COUNT, 1e-6)

    def feed(self, spectrum) -> np.ndarray:
        energy = np.asarray(self.bank(spectrum), dtype=np.float64)
        self.raw_bass = float(energy[:self.LOW].sum())  # before gain levelling, for kick detection
        self.raw_log = np.log10(energy + 1e-12)          # for the bar tracker's timbre change
        # Automatic gain per band, so quiet and loud passages both use the full range.
        self.peaks = np.maximum(energy, self.peaks * 0.997)
        return np.clip(energy / np.maximum(self.peaks, 1e-6), 0, 1)


class SectionDetector:
    """Notices when a song moves to a new part (verse, chorus, drop) from its timbre.

    Compares the average MFCC fingerprint of the last 4 s with the 8 s before,
    scaled by how much the music normally varies. Tested: synthetic boundaries
    found within ~1 s with no false triggers; a live DJ set gave a change every
    30-60 s.
    """

    # 12 s let mix transitions fire 3 changes in 45 s (27 times in a 1h50 set);
    # 24 s (~16 bars) removed every burst and kept a change every ~45 s.
    RECENT, BEFORE, MIN_GAP = 4.0, 8.0, 24.0

    def __init__(self):
        self.mfcc = aubio.mfcc(2048, 40, 13, RATE)
        self.frames = []
        self.history = []
        self.last_change = -1e9
        self.last_check = 0.0

    def feed(self, spectrum, samples: np.ndarray, now: float) -> bool:
        coeffs = np.array(self.mfcc(spectrum), dtype=np.float64)[1:]  # c0 is just loudness
        loudness = float(np.log10(np.sqrt(np.mean(samples ** 2)) + 1e-6))
        self.frames.append((now, coeffs, loudness))
        while self.frames and self.frames[0][0] < now - self.RECENT - self.BEFORE:
            self.frames.pop(0)
        if now - self.last_check < 0.5:
            return False
        self.last_check = now
        cut = now - self.RECENT
        recent = [f for f in self.frames if f[0] >= cut]
        before = [f for f in self.frames if f[0] < cut]
        if len(before) < 0.6 * self.BEFORE * RATE / HOP or len(recent) < 0.8 * self.RECENT * RATE / HOP:
            return False
        rc, bc = np.array([f[1] for f in recent]), np.array([f[1] for f in before])
        timbre = float(np.sqrt(np.mean(((rc.mean(0) - bc.mean(0)) / (bc.std(0) + 1e-3)) ** 2)))
        rl, bl = np.array([f[2] for f in recent]), np.array([f[2] for f in before])
        score = timbre + 0.5 * float(abs(rl.mean() - bl.mean()) / (bl.std() + 0.05))
        threshold = 1.2
        if len(self.history) >= 10:
            hist = np.array(self.history[-60:])
            median = float(np.median(hist))
            threshold = max(threshold, median + 2.5 * 1.4826 * float(np.median(np.abs(hist - median))))
        self.history.append(score)
        # the first few comparisons have no sense yet of how much this song varies
        if len(self.history) > 10 and score > threshold and now - self.last_change > self.MIN_GAP:
            self.last_change = now
            self.frames = recent  # the new part becomes the baseline
            return True
        return False


class Chroma:
    """Energy per note name (C..B) across octaves, for the Melody style.

    A single-voice pitch tracker (aubio pitch) followed the bassline or was
    unsure ~83% of the time on a full mix; chroma picks out the notes being
    played even with drums and several instruments.
    """

    N = 8192  # ~170 ms window: fine enough to separate notes above ~130 Hz

    def __init__(self):
        freqs = np.fft.rfftfreq(self.N, 1 / RATE)
        self.band = (freqs >= 130) & (freqs <= 4200)
        midi = 69 + 12 * np.log2(freqs[self.band] / 440)
        self.near = np.abs(midi - np.round(midi)) < 0.35
        self.pitch_class = (np.round(midi).astype(int) % 12)[self.near]
        self.window = np.hanning(self.N).astype(np.float32)
        self.buffer = np.zeros(self.N, dtype=np.float32)
        self.profile = np.zeros(12)

    def feed(self, samples: np.ndarray) -> np.ndarray:
        self.buffer = np.concatenate((self.buffer[len(samples):], samples))
        magnitude = np.abs(np.fft.rfft(self.buffer * self.window))[self.band][self.near]
        frame = np.bincount(self.pitch_class, weights=magnitude ** 2, minlength=12)
        self.frame = frame / (frame.sum() + 1e-9)    # this instant, for the bar tracker
        self.profile = 0.8 * self.profile + 0.2 * self.frame
        return self.profile


def note_hue(pitch_class: int) -> float:
    """Circle of fifths around the colour wheel: notes that sound good together look related."""
    return (pitch_class * 7 % 12) / 12


TRANSITIONS = {"smooth": 1}  # Matter transition (x0.1 s) per style; Beat and Melody change instantly
SPREADS = {"smooth": 0.15}    # hue gap between lights; Smooth spreads wider for a gradient


class AutoSelector:
    """Picks Beat, Smooth or Melody for the part of the song that is playing.

    kick    punchiness of the bass: how sharply the raw low bands jump up, relative
            to their level. Kicks jump ~4x a bar; basslines and pads don't. (Comparing
            bass on beats with bass overall stayed ~1.0 either way: gain levelling
            and beat timing smear it.)
    energy  loudness over a few seconds relative to the loudest of the last ~minute
    chords  share of the three strongest notes in the chroma profile

    kick + high energy -> Beat; kick + lower energy + clear chords -> Melody;
    no kick (breakdown, build, intro) -> Smooth.

    Every measure has two thresholds (on/off) because real sections sit near any
    single one: a softer-kick groove read punch 0.07-0.09 and chords ~0.72, and
    with one threshold each Auto flipped between all three styles every ~15 s.
    A change must be indicated for 4 bars and the current style must have lasted
    16 bars (EDM phrases), and it happens on the "1". A real drop (strong punch and
    high energy for 4 beats) switches to Beat at once.
    """

    KICK_ON, KICK_OFF = 0.10, 0.065       # drops 0.10-0.21, soft-kick groove 0.07-0.09, breakdown 0.03-0.07
    ENERGY_ON, ENERGY_OFF = 0.78, 0.70    # drops ~0.9-1.0, grooves/breakdowns 0.4-0.7
    CHORDS_ON, CHORDS_OFF = 0.72, 0.66    # top-3 note share; ran 0.47-0.88

    def __init__(self):
        self.active = "smooth"
        self.wanted = "smooth"
        self.wanted_beats = 0
        self.active_beats = 0
        self.tick = 0
        self.prev_bass = 0.0
        self.bass_rise = 0.0
        self.bass_level = 1e-9
        self.punch = 0.0
        self.loudness = 0.0
        self.loud_peak = 1e-4
        self.energy = 0.0
        self.chords = 0.0
        self.kick = self.high = self.clear = False
        self.switches = 0

    @staticmethod
    def _hysteresis(value: float, state: bool, on: float, off: float) -> bool:
        return value > (off if state else on)

    def feed(self, tracker: "BeatTracker", raw_bass: float, rms: float, profile: np.ndarray,
             beat: bool, position: int | None) -> bool:
        """Update the measurements; returns True when the style changed."""
        self.bass_rise += (max(0.0, raw_bass - self.prev_bass) - self.bass_rise) * 0.005  # ~4 s
        self.bass_level += (raw_bass - self.bass_level) * 0.005
        self.prev_bass = raw_bass
        self.punch = self.bass_rise / (self.bass_level + 1e-9)
        self.loudness += (rms - self.loudness) * 0.01            # ~2 s
        self.loud_peak = max(self.loudness, self.loud_peak * 0.99975)  # halves in ~60 s
        self.energy = self.loudness / self.loud_peak
        self.chords += (float(np.sort(profile)[-3:].sum()) - self.chords) * 0.01

        self.kick = self._hysteresis(self.punch, self.kick, self.KICK_ON, self.KICK_OFF)
        self.high = self._hysteresis(self.energy, self.high, self.ENERGY_ON, self.ENERGY_OFF)
        self.clear = self._hysteresis(self.chords, self.clear, self.CHORDS_ON, self.CHORDS_OFF)
        if not self.kick:
            wanted = "smooth"
        elif self.high:
            wanted = "beat"
        else:
            wanted = "melody" if self.clear else "beat"

        if not beat:
            # without a kick there may be no beats to count; keep time in ~0.5 s steps
            if tracker.bpm is not None:
                return False
            self.tick += 1
            if self.tick % 23:
                return False
        if wanted == self.wanted:
            self.wanted_beats += 1
        else:
            self.wanted, self.wanted_beats = wanted, 1
        self.active_beats += 1
        if wanted == self.active:
            return False
        drop = (wanted == "beat" and self.active == "smooth" and self.wanted_beats >= 4
                and self.punch > self.KICK_ON and self.energy >= self.ENERGY_ON)
        settled = self.wanted_beats >= 16 and self.active_beats >= 64 and position in (0, None)
        if settled and wanted == "smooth" and self.punch >= 0.075:
            # the kick is already coming back (a drop is near): entering Smooth now
            # gave 3-6 s blips on a full DJ set
            settled = False
        if drop or settled:
            self.active, self.active_beats = wanted, 0
            self.switches += 1
            return True
        return False


class Analyzer:
    """Turns audio blocks into colours and a level for the chosen style.

    feed() returns (hue, level 0-1, beat). `hues` holds one hue per light when
    the style gives lights different colours (Melody shows the strongest notes).
    """

    PALETTE_STEPS = (0.0, 0.09, -0.09, 0.18)  # a section's family of related colours

    def __init__(self, style: str):
        self.style = style
        self.buffer = np.zeros(WINDOW, dtype=np.float32)
        self.window = np.hanning(WINDOW).astype(np.float32)
        freqs = np.fft.rfftfreq(WINDOW, 1 / RATE)
        self.freqs = freqs
        self.centroid_band = (freqs >= 100) & (freqs <= 8000)
        self.bands = [(freqs >= lo) & (freqs < hi) for lo, hi in ((30, 150), (150, 2000), (2000, 12000))]
        self.peaks = np.full(3, 1e-3)
        self.bass_history = []
        self.hue = 0.0
        self.hues = None
        self.level = 0.0
        self.last_beat = 0.0
        self.beats = 0
        self.quiet_since = None
        self.base = float(np.random.rand())  # current section's palette
        self.palette_index = 0
        self.nudge = 0.0
        self.sections = 0
        self.accents = 0
        self.notes = []
        self.note_since = {}
        self.tracker = BeatTracker() if aubio else None
        self.auto = AutoSelector() if style == "auto" and aubio else None
        self.active = "beat" if style == "auto" and not aubio else style
        self.sections_detector = SectionDetector() if aubio else None
        self.onsets = aubio.onset("hfc", 1024, 512, RATE) if aubio else None
        if self.onsets:
            self.onsets.set_threshold(0.6)   # 0.35 flagged ~4 hits/s on a DJ set: too busy
            self.onsets.set_minioi_ms(120)
            self.onsets.set_silence(-55)
        # Melody colours by note; the bar tracker also listens for chord changes.
        self.chroma = Chroma() if style == "melody" or aubio else None
        self.pvoc = aubio.pvoc(2048, HOP) if aubio else None  # shared by mel bands and sections
        self.mel = MelBands() if aubio else None
        self.bar = BarTracker() if aubio else None
        self.bar_position = None
        self.pending_section = None
        self.brightness = 0.5
        self.smooth_brightness = 0.5

    def _new_section(self) -> None:
        self.sections += 1
        self.base = (self.base + 0.3 + 0.3 * float(np.random.rand())) % 1.0

    def feed(self, samples: np.ndarray, now: float) -> tuple[float, float, bool]:
        samples = np.ascontiguousarray(samples, dtype=np.float32)
        spectrum = None
        if self.mel:
            spectrum = self.pvoc(samples)
            mel = self.mel.feed(spectrum)
            loud = float(mel.mean())
            bass = float(mel[self.mel.low].mean())
            self.brightness = float((np.arange(MelBands.COUNT) * mel).sum() / (mel.sum() + 1e-9)) / (MelBands.COUNT - 1)
        else:
            # Without aubio: three fixed bands from our own FFT.
            self.buffer = np.concatenate((self.buffer[len(samples):], samples))
            fft = np.abs(np.fft.rfft(self.buffer * self.window))
            energy = np.array([np.sqrt(np.mean(fft[m] ** 2)) for m in self.bands])
            self.peaks = np.maximum(energy, self.peaks * 0.997)
            norm = np.clip(energy / np.maximum(self.peaks, 1e-3), 0, 1)
            loud = float(0.5 * norm[0] + 0.3 * norm[1] + 0.2 * norm[2])
            bass = float(energy[0])
            magnitude = fft[self.centroid_band]
            centroid = float((self.freqs[self.centroid_band] * magnitude).sum() / (magnitude.sum() + 1e-9))
            self.brightness = float(np.clip((np.log2(max(centroid, 1.0)) - np.log2(300)) / (np.log2(4000) - np.log2(300)), 0, 1))
        profile = self.chroma.feed(samples) if self.chroma else None

        rms = float(np.sqrt(np.mean(samples ** 2)))
        if rms < 2e-4:
            self.quiet_since = self.quiet_since or now
        else:
            self.quiet_since = None
        if self.quiet_since and now - self.quiet_since > 1.5:
            # Nothing playing: drift slowly at a low glow instead of freezing.
            self.hue = (self.hue + 0.0015) % 1.0
            self.hues = None
            self.level += (0.12 - self.level) * 0.05
            return self.hue, self.level, False

        position = None
        if self.tracker:
            beat = self.tracker.feed(samples, now)
            for number in self.tracker.due:  # the bass and chords of a beat are known once it is heard
                self.bar.on_beat(number, self.mel.raw_bass, self.mel.raw_log, self.chroma.frame)
            if beat:
                position = self.bar.position(self.tracker.fire_index)
        else:
            # Fallback without aubio: bass energy jumping well above its ~1 s average.
            self.bass_history = (self.bass_history + [bass])[-48:]
            average = sum(self.bass_history) / len(self.bass_history)
            beat = bass > average * 1.45 and norm[0] > 0.35 and now - self.last_beat > 0.18
        if beat:
            self.last_beat = now
            self.beats += 1
            self.bar_position = position

        # A new part takes effect on the next "1" of a bar (or soon after, if the bar is unclear).
        if self.sections_detector and self.sections_detector.feed(spectrum, samples, now):
            self.pending_section = now
        if self.pending_section is not None and (
                (beat and (position == 0 or position is None)) or now - self.pending_section > 2.5):
            self.pending_section = None
            self._new_section()

        # Accents: hits between beats (snares, hats) flick the brightness up.
        accent = False
        if self.onsets:
            for i in range(0, len(samples), 512):
                if self.onsets(samples[i:i + 512])[0]:
                    accent = True
            if accent and not beat and now - self.last_beat > 0.09:
                self.accents += 1
                self.level = max(self.level, 0.75)

        if self.auto and self.auto.feed(self.tracker, self.mel.raw_bass, rms, profile, beat, position):
            self.active = self.auto.active
        if self.active != "melody":
            self.hues = None  # only Melody gives each light its own note
        if self.active == "smooth":
            self._smooth(loud, beat, position)
        elif self.active == "melody":
            self._melody(profile, loud, beat, position, now)
        else:
            self._beat(loud, beat, position)
        return float(self.hue), float(self.level), beat

    def _beat(self, loud: float, beat: bool, position: int | None) -> None:
        # Each beat steps to another colour of the section's palette; the first
        # beat of a bar makes the bigger jump and the full flash.
        if beat:
            if position is None:
                step, flash = 1 + int(np.random.rand() * (len(self.PALETTE_STEPS) - 1)), 1.0
            elif position == 0:
                step, flash = 2, 1.0
            else:
                step, flash = 1, 0.85
            self.palette_index = (self.palette_index + step) % len(self.PALETTE_STEPS)
            self.hue = (self.base + self.PALETTE_STEPS[self.palette_index]) % 1.0
            self.level = flash
        else:
            self.level = max(self.level * 0.9, loud * 0.8)

    def _smooth(self, loud: float, beat: bool, position: int | None) -> None:
        # Glide around the colour wheel: a full lap every ~80 beats, swung up to
        # ~110 degrees warmer/cooler by how bright the sound is, kicked forward on
        # every beat (twice as hard on the "1"). The first version moved ~18 deg/s
        # with a +-45 deg swing, which barely showed on three bulbs.
        bpm = self.tracker.bpm if self.tracker else None
        self.base = (self.base + (bpm / 60 if bpm else 2.0) * HOP / RATE * 0.0125) % 1.0
        brightness = float(np.clip((self.brightness - BRIGHTNESS_LOW) / (BRIGHTNESS_HIGH - BRIGHTNESS_LOW), 0, 1)) \
            if self.mel else self.brightness
        # follow the sound's character over ~0.5 s, not every hi-hat
        self.smooth_brightness += (brightness - self.smooth_brightness) * 0.04
        brightness = self.smooth_brightness
        if beat:
            self.nudge = 0.12 if position == 0 else 0.06
            self.level = 1.0 if position == 0 else max(self.level, 0.8)
        self.nudge *= 0.95
        target = (self.base + (brightness - 0.5) * 0.6 + self.nudge) % 1.0
        delta = ((target - self.hue + 0.5) % 1.0) - 0.5
        self.hue = (self.hue + delta * 0.12) % 1.0
        # breathing: rises with each beat, sinks back with the music (Pulse brightness only)
        self.level = max(self.level * 0.97, min(1.0, 0.25 + loud * 1.2))

    def _melody(self, profile: np.ndarray, loud: float, beat: bool, position: int | None, now: float) -> None:
        # Colour per note (circle of fifths, so notes that sound good together look
        # related); with several lights each shows one of the strongest notes, so
        # together they show the chord. Colours change on the beat to whatever is
        # playing then; changing whenever the notes moved (2-4 times a second with
        # soft fades) read as random. Without a steady beat, a note held for 0.3 s
        # changes the colour instead.
        ranked = [int(n) for n in np.argsort(profile)[::-1][:3] if profile[n] > 0.12]
        for n in ranked:
            self.note_since.setdefault(n, now)
        self.note_since = {n: t for n, t in self.note_since.items() if n in ranked}
        steady_beat = self.tracker is not None and self.tracker.bpm is not None
        held = [n for n in ranked if now - self.note_since[n] >= (0.0 if steady_beat else 0.3)]
        if held and (beat or not steady_beat) and held != self.notes:
            self.notes = held
            self.hues = [(note_hue(n) + self.base) % 1.0 for n in self.notes]  # sections rotate the wheel
            self.hue = self.hues[0]
            self.level = max(self.level, 0.85)
        if beat:
            self.level = 1.0 if position == 0 else max(self.level, 0.85)
        self.level = max(self.level * 0.9, min(1.0, 0.25 + loud * 1.2))


def to_rgbw(hue: float) -> int:
    r, g, b = (int(round(c * 255)) for c in colorsys.hsv_to_rgb(hue, 1.0, 1.0))
    value = (r << 24) | (g << 16) | (b << 8)
    return value - (1 << 32) if value >= 1 << 31 else value


def to_hex(hue: float) -> str:
    r, g, b = (int(round(c * 255)) for c in colorsys.hsv_to_rgb(hue, 1.0, 1.0))
    return f"#{r:02x}{g:02x}{b:02x}"


# --- bulb streaming -------------------------------------------------------

class BulbStream(threading.Thread):
    """Holds one bulb's session and pushes the latest target, one update in flight."""

    def __init__(self, lights: "aidot.Lights", device: dict, stop: threading.Event, restore: dict | None):
        super().__init__(daemon=True)
        self.lights, self.device, self.stop = lights, device, stop
        self.target = None            # (hue, dimming)
        self.sent = (None, None)
        self.restore = restore
        self.session = None
        self.lock = threading.Lock()
        self.sent_count = self.acked = self.expired = 0

    def connect(self) -> None:
        ip = self.lights.ips.get(self.device["id"])
        if not ip:
            raise aidot.LightError("no IP")
        self.session = aidot.Session(self.device, self.lights.user_id, ip)
        if self.restore is None:
            self.restore = self.session.get()
            remember(self.device["id"], self.restore)

    def set_target(self, hue: float, dimming: int) -> None:
        with self.lock:
            self.target = (hue, dimming)

    def run(self) -> None:
        in_flight, last_send, last_ping = None, 0.0, time.monotonic()
        while not self.stop.is_set():
            if self.session is None:
                try:
                    self.connect()
                    self.session.set({"OnOff": 1})
                    self.sent = (None, None)
                except (OSError, aidot.LightError, ValueError) as err:
                    emit({"event": "warn", "light": self.device["name"], "message": f"connect: {err}"})
                    self.session = None
                    self.stop.wait(1)
                    continue
            try:
                readable, _, _ = select.select([self.session.sock], [], [], 0.02)
                if readable:
                    self.session.sock.settimeout(1.0)
                    reply = self.session._recv()
                    if reply.get("method") == "setDevAttrResp":
                        in_flight = None
                        self.acked += 1
                now = time.monotonic()
                if in_flight and now - in_flight > ACK_TIMEOUT:
                    in_flight = None
                    self.expired += 1
                with self.lock:
                    target = self.target
                if target and not in_flight and now - last_send >= MIN_INTERVAL:
                    attr = {}
                    hue, dimming = target
                    if self.sent[0] is None or abs(((hue - self.sent[0] + 0.5) % 1.0) - 0.5) >= 0.012:
                        attr["RGBW"] = to_rgbw(hue)
                    if self.sent[1] is None or abs(dimming - self.sent[1]) >= 4:
                        attr["Dimming"] = dimming
                    if attr:
                        self.session._action("setDevAttrReq", attr)
                        self.sent = (hue if "RGBW" in attr else self.sent[0],
                                     dimming if "Dimming" in attr else self.sent[1])
                        in_flight, last_send, last_ping = now, now, now
                        self.sent_count += 1
                if now - last_ping > 10:
                    self.session._send({"service": "test", "method": "pingreq", "seq": "123456",
                                        "srcAddr": "123456", "payload": {}}, msgtype=2)
                    last_ping = now
            except (OSError, aidot.LightError, ValueError) as err:
                emit({"event": "warn", "light": self.device["name"], "message": f"stream: {err!r}"})
                try:
                    self.session.close()
                except OSError:
                    pass
                self.session = None

    def put_back(self) -> None:
        """Return the bulb to the state it had before music sync started."""
        if not self.restore:
            return
        # The bulb ignores colour/level sent together with OnOff=0, so restore
        # them while it is on and switch it off afterwards.
        attr = {k: self.restore[k] for k in ("Dimming", "CCT", "RGBW") if k in self.restore}
        attr["OnOff"] = 1
        for attempt in range(4):
            try:
                if self.session is None:
                    self.connect()
                self.session.set(attr)
                if not self.restore.get("OnOff", 1):
                    # switching off mid-fade would store the half-faded level
                    time.sleep(1.5)
                    self.session.set({"OnOff": 0})
                return
            except (OSError, aidot.LightError, ValueError) as err:
                emit({"event": "warn", "light": self.device["name"], "message": f"restore: {err!r}"})
                self.session = None
                time.sleep(0.5 * (attempt + 1))


def recover(credentials: str) -> bool:
    """Restore lights from the saved state of a sync that died without restoring."""
    saved = aidot.load_json(SAVED_FILE, {})
    if not saved:
        return False
    streams = []
    if any("matter" in v for v in saved.values()) and matter.available():
        m = matter.MatterLights()
        shared = SharedMatterClient(m.client)
        streams += [MatterStream(shared, n, threading.Event(), saved[matter.device_id(n["node_id"])], 0)
                    for n in m.nodes.values() if matter.device_id(n["node_id"]) in saved]
    if any("matter" not in v for v in saved.values()):
        lights = aidot.Lights(credentials)
        streams += [BulbStream(lights, d, threading.Event(), saved[d["id"]])
                    for d in lights.devices if d["id"] in saved]
    threads = [threading.Thread(target=s.put_back) for s in streams]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    SAVED_FILE.unlink(missing_ok=True)
    return True


def forget_saved() -> None:
    """A manual change outside sync supersedes any saved pre-sync state."""
    if not PID_FILE.exists():
        SAVED_FILE.unlink(missing_ok=True)


class SharedMatterClient:
    """One server connection for all streams, reopened if the server restarts."""

    def __init__(self, client):
        self.client = client
        self.lock = threading.Lock()

    def get(self):
        with self.lock:
            if self.client.closed:
                if not matter.available():  # also (re)starts the container
                    raise matter.MatterError("Matter server unavailable")
                self.client = matter.Client()
            return self.client


class MatterStream(threading.Thread):
    """Same job as BulbStream over Matter: no per-bulb session, instant transitions.

    Commands go through the shared Matter server connection; at most one is in
    flight per bulb, which measured ~9-11 updates/s at ~0.1 s each.
    """

    def __init__(self, client, node: dict, stop: threading.Event, restore: dict | None, transition: int):
        super().__init__(daemon=True)
        self.client, self.node_id, self.stop, self.transition = client, node["node_id"], stop, transition
        self.device = {"id": matter.device_id(self.node_id),
                       "name": node["attributes"].get("0/40/5") or f"Matter light {self.node_id}"}
        self.session = True if node.get("available") else None  # "connected" for the status line
        self.target = None
        self.sent = (None, None)
        self.lock = threading.Lock()
        self.in_flight = 0
        if restore is None:
            restore = {"matter": matter.snapshot(node)}
            remember(self.device["id"], restore)
        self.restore = restore

    def set_target(self, hue: float, dimming: int) -> None:
        with self.lock:
            self.target = (hue, dimming)

    def _done(self, _reply) -> None:
        with self.lock:
            self.in_flight -= 1

    def _send(self, cmds) -> None:
        with self.lock:
            self.in_flight += len(cmds)
        for cluster, name, payload in cmds:
            self.client.get().command(self.node_id, cluster, name, payload, callback=self._done)

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                self._stream()
            except matter.MatterError as err:
                emit({"event": "warn", "light": self.device["name"], "message": f"matter: {err}"})
                self.session = None
                with self.lock:
                    self.in_flight = 0  # replies to the old connection will never come
                self.stop.wait(2)

    def _stream(self) -> None:
        last_send = 0.0
        self._send(matter.color_commands(on=True))
        self.session = True
        self.sent = (None, None)  # resend everything after a reconnect
        while not self.stop.wait(0.01):
            now = time.monotonic()
            if self.client.client.closed:
                raise matter.MatterError("lost the connection to the Matter server")
            with self.lock:
                if self.in_flight and now - last_send > 1.0:
                    self.in_flight = 0  # a reply went missing; don't stall the bulb
                target, busy = self.target, self.in_flight > 0
            if not target or busy or now - last_send < 0.085:
                continue
            hue, dimming = target
            kwargs = {"transition": self.transition}
            if self.sent[0] is None or abs(((hue - self.sent[0] + 0.5) % 1.0) - 0.5) >= 0.008:
                kwargs["hue"], kwargs["sat"] = round(hue * 254), 254
            if self.sent[1] is None or abs(dimming - self.sent[1]) >= 3:
                kwargs["level"] = round(dimming * 254 / 100)
            if len(kwargs) > 1:
                self._send(matter.color_commands(**kwargs))
                self.sent = (hue if "hue" in kwargs else self.sent[0],
                             dimming if "level" in kwargs else self.sent[1])
                last_send = now

    def put_back(self) -> None:
        saved = (self.restore or {}).get("matter")
        if saved:
            try:
                client = self.client.get()
            except matter.MatterError:
                return  # state stays saved; music-stop / plugin start recovers it later
            slots = [client.command(self.node_id, c, n, p) for c, n, p in matter.restore_commands(saved)]
            for slot in slots:
                slot[0].wait(5)


# --- single instance ------------------------------------------------------

def stop_running(timeout: float = 12.0, handover: bool = False) -> bool:
    """Stop a running sync. A handover (SIGUSR1) skips its restore; the caller takes over."""
    try:
        pid = int(PID_FILE.read_text())
        # never signal a recycled pid that isn't ours
        if b"aidot.py" not in open(f"/proc/{pid}/cmdline", "rb").read():
            return False
        os.kill(pid, signal.SIGUSR1 if handover else signal.SIGTERM)
    except (OSError, ValueError):
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        time.sleep(0.1)
    return True


_log = None


def emit(payload: dict) -> None:
    line = json.dumps(payload)
    if _log and payload.get("event") != "level":
        _log.write(time.strftime("%H:%M:%S ") + line + "\n")
        _log.flush()
    try:
        print(line, flush=True)
    except (BrokenPipeError, OSError):
        pass  # the plugin went away; keep running until we're told to stop


def run(credentials: str, targets: list[str], style: str, pulse: bool = False) -> None:
    stop_running(handover=True)
    # Saved state exists if a sync is handing over, or if one died before it could
    # restore (plugin reload, logout). Either way the lights still show sync colours,
    # so the saved state is the real "before". Manual changes delete the file.
    previous = aidot.load_json(SAVED_FILE, {})
    _saved.update(previous)
    use_matter = matter.available()
    global BULB_LATENCY
    BULB_LATENCY = 0.10 if use_matter else 0.20
    if use_matter:
        lights = matter.MatterLights()
        devices = lights.select(targets)
    else:
        lights = aidot.Lights(credentials)
        devices = lights.select(targets)
        if any(d["id"] not in lights.ips for d in devices):
            lights.rediscover()

    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))
    global _log
    _log = open(LOG_FILE, "a")
    stop = threading.Event()
    handover = threading.Event()

    def on_signal(signum, _frame):
        emit({"event": "signal", "signal": signal.Signals(signum).name})
        if signum == signal.SIGUSR1:
            handover.set()
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGUSR1):
        signal.signal(sig, on_signal)
    # if stdout breaks we must not die mid-sync
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)

    if use_matter:
        # Beat changes land instantly; Smooth eases 0.1 s so the ~10 updates/s blend into a glide.
        transition = TRANSITIONS.get(style, 0)
        shared = SharedMatterClient(lights.client)
        streams = [MatterStream(shared, n, stop, previous.get(matter.device_id(n["node_id"])), transition)
                   for n in devices]
    else:
        streams = [BulbStream(lights, d, stop, previous.get(d["id"])) for d in devices]
    for s in streams:
        s.start()

    parec = subprocess.Popen(
        ["parec", "-d", "@DEFAULT_MONITOR@", "--raw", "--format=float32le", f"--rate={RATE}",
         "--channels=1", "--latency-msec=20", "--client-name=aidot-lights",
         "--stream-name=Music sync"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    analyzer = Analyzer(style)
    emit({"event": "started", "style": style, "lights": [s.device["name"] for s in streams],
          "resumed": bool(previous), "protocol": "matter" if use_matter else "aidot"})
    # Lights sit apart in hue; Smooth spreads them wider so the room shows a gradient.
    several = len(streams) > 1
    last_emit = 0.0
    switches = 0
    try:
        while not stop.is_set():
            try:
                chunk = parec.stdout.read(HOP * 4)
            except InterruptedError:
                continue
            if not chunk or len(chunk) < HOP * 4:
                if parec.poll() is not None:
                    emit({"event": "error", "message": "audio capture stopped"})
                    break
                continue
            now = time.monotonic()
            sections_before = analyzer.sections
            hue, level, beat = analyzer.feed(np.frombuffer(chunk, dtype="<f4"), now)
            if analyzer.sections != sections_before:
                emit({"event": "section", "n": analyzer.sections, "palette": to_hex(analyzer.base)})
            # full brightness by default; --pulse makes brightness follow loudness too
            dimming = int(round(5 + 95 * min(1.0, level) ** 1.4)) if pulse else 100
            spread = SPREADS.get(analyzer.active, 0.08) if several else 0.0
            if analyzer.auto and analyzer.auto.switches != switches:
                switches = analyzer.auto.switches
                emit({"event": "auto", "style": analyzer.active})
            for i, s in enumerate(streams):
                if hasattr(s, "transition"):
                    s.transition = TRANSITIONS.get(analyzer.active, 0)
                if analyzer.hues:
                    s.set_target(analyzer.hues[i % len(analyzer.hues)], dimming)
                else:
                    s.set_target((hue + i * spread) % 1.0, dimming)
            if now - last_emit > 0.25:
                connected = sum(1 for s in streams if s.session is not None)
                bpm = analyzer.tracker.bpm if analyzer.tracker else None
                emit({"event": "level", "rgb": to_hex(hue), "level": round(level, 2),
                      "beats": analyzer.beats, "connected": connected,
                      "bpm": round(bpm) if bpm else None, "engine": "aubio" if analyzer.tracker else "basic",
                      "section": analyzer.sections, "accents": analyzer.accents,
                      "notes": ["C C# D D# E F F# G G# A A# B".split()[n] for n in analyzer.notes],
                      "mode": analyzer.active})
                last_emit = now
    except Exception as err:
        emit({"event": "error", "message": repr(err)})
        raise
    finally:
        stop.set()
        parec.terminate()
        for s in streams:
            s.join(timeout=1)
        if handover.is_set():
            # the new process owns the lights and the saved state now
            for s in streams:
                if hasattr(s.session, "close"):
                    s.session.close()
        else:
            threads = [threading.Thread(target=s.put_back) for s in streams]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)
            SAVED_FILE.unlink(missing_ok=True)
        try:
            if PID_FILE.read_text() == str(os.getpid()):
                PID_FILE.unlink()
        except OSError:
            pass
        emit({"event": "stopped"})

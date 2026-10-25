"""Progress display of the Speak tab: step estimate, bar position and the HTML of the single bar.

No Gradio or torch import here, so the numbers can be tested alone (tests/test_progress_ui.py).

What is counted. voxcpm makes speech in steps: one pass of its generation loop makes one patch of
PATCH_SECONDS (0.16 s, 6.25 steps per second) of audio. voxcpm_patch.PROGRESS counts the passes.
Before the first step the model reads the voice and prepares the text; after the last step it decodes
the audio. Those parts have no step count. The bar stands still at fixed positions during them
(CLIP_POSITIONS) and the text says so.

Estimate of the steps of a clip, from the text, before the clip starts:
  steps = characters x steps_per_character
  steps_per_character = 6.25 / characters_per_second of the voice
The speech rate of the voice is the length of its transcript divided by the length of its recording
(clamped to 8-20 characters per second; 15 when the voice has no transcript; 62 characters in about 4 s
were measured in the README). After each finished clip the measured steps per character replace the
prior for this voice (CALIBRATION), so line 2 and later runs are closer than line 1.
The code's own ceiling is max_len = min(6 x text tokens + 10, 4096) steps (voxcpm core and
VoxCPM2Model._generate_with_prompt_cache); the loop reports it as the length of its range and the
estimate is never above it. 6 steps per token is the limit above which voxcpm calls a take a bad case.

Fraction of a clip while it makes steps: linear up to 90% of the estimate. Past that the estimate
grows: the remaining steps are taken to be 10% of the estimate scaled by 0.9 x estimate / steps, so the
fraction keeps rising, stays below 1 and never goes back (step_fraction). The bar of a run is
(finished clips + fraction of the current clip) / number of clips and never goes down.
"""
import html as _html
import re
import time

PATCH_SECONDS = 0.16
STEPS_PER_SECOND = 1 / PATCH_SECONDS  # 6.25
DEFAULT_CHARS_PER_SECOND = 15.0
MIN_CHARS_PER_SECOND, MAX_CHARS_PER_SECOND = 8.0, 20.0
MIN_STEPS = 6
OVERRUN_START = 0.9  # share of the estimate after which the estimate grows
UPDATE_INTERVAL = 0.5  # seconds between two updates of the page

# Fraction of a clip's bar share at the start of each phase. [asserted] Until measured with the real
# model: the steps take most of the time, reading the voice and decoding take a few seconds each.
SPEECH_START, SPEECH_END = 0.05, 0.93
CLIP_POSITIONS = {"idle": 0.0, "denoise": 0.0, "voice": 0.01, "prepare": 0.03, "wrapup": SPEECH_END, "decode": 0.94, "saving": 0.98}

# Steps per character of each voice, kept for the app's lifetime: {key: steps per character}.
CALIBRATION = {}


def mmss(seconds):
    seconds = int(seconds)
    return f"{seconds // 60}:{seconds % 60:02d}"


# ---------------------------------------------------------------- estimate

def spoken_characters(text):
    """Characters that are spoken: whitespace collapsed, a leading '(style)' instruction dropped."""
    text = re.sub(r"^\([^)]*\)\s*", "", text or "")
    return len(" ".join(text.split()))


def prior_steps_per_character(characters_per_second=None):
    rate = DEFAULT_CHARS_PER_SECOND if not characters_per_second else characters_per_second
    rate = min(max(rate, MIN_CHARS_PER_SECOND), MAX_CHARS_PER_SECOND)
    return STEPS_PER_SECOND / rate


def steps_per_character(key, prior):
    return CALIBRATION.get(key, prior)


def calibrate(key, characters, steps):
    """Remember the steps per character of a finished clip: the mean of the old value and the new one."""
    if characters <= 0 or steps <= 0:
        return
    measured = steps / characters
    old = CALIBRATION.get(key)
    CALIBRATION[key] = measured if old is None else (old + measured) / 2


def estimate_steps(text, per_character):
    return max(MIN_STEPS, round(spoken_characters(text) * per_character))


def step_fraction(steps, estimate):
    """Fraction of the steps done, from 0 up to but never reaching 1, rising with every step.

    Linear (steps / estimate) up to OVERRUN_START of the estimate. Past it the estimate grows: the
    remaining steps are 10% of the estimate times (OVERRUN_START x estimate / steps), which shrinks
    slower than the steps grow.
    """
    if steps <= 0 or estimate <= 0:
        return 0.0
    switch = OVERRUN_START * estimate
    if steps <= switch:
        return steps / estimate
    remaining = (1 - OVERRUN_START) * estimate * switch / steps
    return steps / (steps + remaining)


def expected_total(steps, estimate):
    """The estimate of all steps of the clip, grown if the clip has run past the first estimate."""
    fraction = step_fraction(steps, estimate)
    return estimate if fraction <= 0 or steps <= OVERRUN_START * estimate else round(steps / fraction)


def clip_fraction(phase, steps, estimate):
    """Position inside the bar share of one clip, 0 up to but never reaching 1."""
    if phase == "speech":
        return SPEECH_START + (SPEECH_END - SPEECH_START) * step_fraction(steps, estimate)
    return CLIP_POSITIONS.get(phase, 0.0)


# ---------------------------------------------------------------- the run

class RunProgress:
    """State of one Speak run: which clip, which phase, how far. Renders the HTML of the bar.

    snapshot is a function returning voxcpm_patch.PROGRESS.snapshot(). All methods are called from
    the one thread that serves the event.
    """

    def __init__(self, total, snapshot, now=time.time, interval=UPDATE_INTERVAL):
        self.total = total
        self._snapshot = snapshot
        self._now = now
        self.interval = interval
        self.started = now()
        self.stage = "starting"  # starting, loading_model, loading_denoiser, clip, saved, done, error
        self.note = ""
        self.hint = ""
        self.index = 0
        self.estimate = MIN_STEPS
        self.finished = 0
        self.patch_seconds = PATCH_SECONDS
        self._shown = 0.0
        self._stage_since = now()
        self._last_html = None
        self._last_emit = float("-inf")

    # ---- changes of state
    def _stage(self, stage, note=""):
        self.stage, self.note, self._stage_since = stage, note, self._now()

    def loading_model(self, precision, hint, replacing):
        if replacing:
            self._stage("loading_model", "Loading the model again, because the precision changed")
        else:
            self._stage("loading_model", "Loading the model (first time only)")
        self.hint = f"{precision}, takes {hint}. No step count for this part."

    def loading_denoiser(self):
        self._stage("loading_denoiser", "Loading the denoiser (first time only)")
        self.hint = "No step count for this part."

    def start_clip(self, index, estimate):
        self.index, self.estimate = index, estimate
        self._stage("clip")

    def clip_saved(self, name, seconds):
        self.finished += 1
        self._stage("saved", f"{name} ({seconds:.1f} s of speech)")

    def done(self, where):
        self._stage("done", where)

    def fail(self, message):
        self._stage("error", message)
        return self.force()

    # ---- what to show
    def _prefix(self):
        return f"Clip {self.index} of {self.total}: " if self.total > 1 else ""

    def _clip_view(self, snap):
        phase, steps = snap["phase"], snap["steps"]
        patch = snap.get("patch_seconds") or self.patch_seconds
        estimate = self.estimate
        if snap.get("max_steps"):
            estimate = max(1, min(estimate, snap["max_steps"]))
        p = self._prefix()
        no_count = f"No step count for this part. {mmss(snap['phase_seconds'])} in this part."
        if phase == "speech":
            total = expected_total(steps, estimate)
            sub = f"Step {steps} of about {total}, about {total * patch:.0f} s of speech expected."
            if steps > OVERRUN_START * estimate:
                sub += " Longer than first estimated."
            line = f"{p}{steps * patch:.1f} s of speech made"
            if snap["attempt"] > 1:
                line += f" (try {snap['attempt']})"
                sub = "The first take was too long for the text, so it is made again. " + sub
            return clip_fraction(phase, steps, estimate), False, line, sub
        lines = {
            "denoise": "Cleaning up the voice recording (denoiser)",
            "voice": "Reading the voice recording",
            "prepare": "Preparing the text",
            "wrapup": "Checking the take",
            "decode": "Turning the speech into sound",
        }
        if phase == "saving":
            line = f"Saving clip {self.index} of {self.total}" if self.total > 1 else "Saving the clip"
        else:
            line = p + lines.get(phase, "Starting")
        if phase == "wrapup" and snap["attempt"] >= 1:
            no_count = f"{snap['steps'] * patch:.1f} s of speech made. {mmss(snap['phase_seconds'])} in this part."
        return clip_fraction(phase, steps, estimate), True, line, no_count

    def view(self):
        """(state, fraction or None, busy, line, sub). fraction None: no position (indeterminate)."""
        stage = self.stage
        if stage == "starting":
            return "starting", None, True, "Starting", ""
        if stage in ("loading_model", "loading_denoiser"):
            return stage, None, True, self.note, self.hint
        if stage == "clip":
            clip, busy, line, sub = self._clip_view(self._snapshot())
            return "clip", (self.finished + clip) / self.total, busy, line, sub
        if stage == "saved":
            return "saved", self.finished / self.total, False, f"Saved clip {self.finished} of {self.total}: {self.note}", ""
        if stage == "done":
            return "done", 1.0, False, f"Done: {self.note} in {mmss(self._now() - self.started)}", ""
        return "error", self._shown, False, f"Stopped: {self.note}", "Nothing more is made. Press Generate to try again."

    def html(self):
        state, fraction, busy, line, sub = self.view()
        if fraction is not None:
            if state != "done":
                fraction = min(fraction, 0.999)
            self._shown = max(self._shown, fraction)
            fraction = self._shown
        return render_html(state, fraction, busy, line, sub, self._now() - self.started)

    # ---- when to send
    def force(self):
        """The HTML now, sent regardless of the update interval."""
        page = self.html()
        self._last_html, self._last_emit = page, self._now()
        return page

    def poll(self):
        """The HTML if it changed and at least `interval` seconds passed since the last send; else None."""
        page = self.html()
        now = self._now()
        if page == self._last_html or now - self._last_emit < self.interval:
            return None
        self._last_html, self._last_emit = page, now
        return page


# ---------------------------------------------------------------- HTML

CSS = """
.vc-progress { box-sizing: border-box; min-height: 8.2rem; padding: 0.8rem 1rem; border: 1px solid var(--border-color-primary, #d4d4d8);
  border-radius: var(--radius-lg, 8px); background: var(--background-fill-secondary, #f4f4f5); color: var(--body-text-color, #27272a); }
.vc-top { display: flex; justify-content: space-between; align-items: baseline; height: 1.7rem; font-variant-numeric: tabular-nums; }
.vc-pct { font-size: 1.3rem; font-weight: 600; }
.vc-elapsed { font-size: 0.9rem; opacity: 0.75; }
.vc-track { box-sizing: border-box; height: 0.9rem; margin: 0.3rem 0 0.7rem; overflow: hidden; border-radius: 999px;
  border: 1px solid var(--border-color-primary, #d4d4d8); background: var(--background-fill-primary, #ffffff); }
.vc-fill { height: 100%; border-radius: 999px; background: var(--color-accent, #f97316); transition: width 0.4s linear; }
.vc-busy { background: repeating-linear-gradient(135deg, var(--color-accent, #f97316) 0 8px, rgba(249, 115, 22, 0.55) 8px 16px); }
.vc-wait { opacity: 0.45; }
.vc-error .vc-fill { background: #dc2626; }
.vc-error .vc-pct { color: #dc2626; }
.vc-done .vc-fill { background: #16a34a; }
.vc-line { min-height: 2.8em; font-size: 1rem; line-height: 1.4; overflow-wrap: anywhere; }
.vc-sub { min-height: 2.8em; font-size: 0.85rem; line-height: 1.4; opacity: 0.75; overflow-wrap: anywhere; }
"""


def render_html(state, fraction, busy, line, sub, elapsed):
    """One progress element with a fixed structure and fixed heights, so a new value changes text and width only."""
    if state == "idle":
        pct, label, width = None, "--", 0.0
    elif fraction is None:
        pct, label, width = None, "--", 100.0
    else:
        pct = 100 if state == "done" else min(99, int(fraction * 100))
        label, width = f"{pct}%", (100.0 if state == "done" else fraction * 100)
    classes = "vc-fill" + (" vc-busy vc-wait" if fraction is None and state != "idle" else " vc-busy" if busy else "")
    value = "" if pct is None else f' aria-valuenow="{pct}"'
    esc = _html.escape
    return (
        f'<div class="vc-progress vc-{state}" role="progressbar" aria-label="Speech progress" aria-valuemin="0" '
        f'aria-valuemax="100"{value} aria-valuetext="{esc(line, quote=True)}" data-state="{state}" '
        f'data-pct="{"" if pct is None else pct}">'
        f'<div class="vc-top"><span class="vc-pct">{label}</span><span class="vc-elapsed">{"" if state == "idle" else mmss(elapsed) + " elapsed"}</span></div>'
        f'<div class="vc-track"><div class="{classes}" style="width: {width:.1f}%"></div></div>'
        f'<div class="vc-line">{esc(line)}</div>'
        f'<div class="vc-sub">{esc(sub)}</div>'
        "</div>"
    )


def idle_html():
    return render_html("idle", None, False, "Ready. Press Generate.", "", 0)

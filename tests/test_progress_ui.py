#!/usr/bin/env python3
"""Checks the progress bar of the Speak tab with a stand-in model. The real model is never loaded.

Run:    .venv/bin/python tests/test_progress_ui.py            # exit 0 when every check passes
Serve:  .venv/bin/python tests/test_progress_ui.py --serve    # the stand-in UI on 127.0.0.1:7861 until Ctrl-C

What it does:
  1. Imports app without launching it, points it at a temporary voice and output folder, and replaces
     voxcpm_patch.load_model with a loader for a stand-in model. The stand-in's generate walks the same
     hook as the real loop (voxcpm_patch.PROGRESS: phases, then `iterate` over a range with a break) with
     short sleeps, and returns a sine wave of N x 0.16 s.
  2. Starts the UI on 127.0.0.1:7861 (never 7860) and calls the Speak endpoint through gradio_client
     with two lines in "one file per line" mode. Every update is stored with its arrival time.
  3. Checks the updates (see check_updates) and the pure functions of progress_ui.
"""
import argparse
import re
import shutil
import socket
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

import numpy as np
import soundfile as sf

import app
import progress_ui
import voxcpm_patch

PORT = 7861
SAMPLE_RATE = 48000
PATCH = 0.16
LOAD_SECONDS = 1.5
PHASE_SECONDS = 0.8  # voice, prepare and decode each last this long
STEP_SECONDS = 0.1
CHARS_PER_SECOND = 15.0  # of the stand-in voice: transcript characters over seconds of recording

VOICE_SECONDS = 20
LINE_1 = "The quick brown fox jumps over the lazy dog near the river bank. " * 2 + "It is a calm morning."
LINE_2 = ("A second line, longer than the first one, so that the estimate must be made again for it. " * 2).strip()
LINES = [LINE_1, LINE_2]
TRUE_STEPS = {LINE_1: 66, LINE_2: 96}  # the stand-in model needs more steps than the prior estimates


class StubModel:
    """Same surface as the part of voxcpm.VoxCPM that app.py uses."""

    denoiser = None
    tts_model = SimpleNamespace(sample_rate=SAMPLE_RATE)

    def generate(self, text, cfg_value=2.0, inference_timesteps=10, denoise=False, **voice):
        progress = voxcpm_patch.PROGRESS
        steps = TRUE_STEPS[text]
        progress.set_phase("voice")  # what the wrapper around build_prompt_cache does
        time.sleep(PHASE_SECONDS)
        progress.set_phase("prepare")
        time.sleep(PHASE_SECONDS)
        for i in progress.iterate(range(1000)):  # what tqdm_with_progress does around `for i in tqdm(range(max_len))`
            time.sleep(STEP_SECONDS)
            if i + 1 >= steps:
                break
        progress.set_phase("decode")  # what the wrapper around audio_vae.decode does
        time.sleep(PHASE_SECONDS)
        t = np.arange(int(steps * PATCH * SAMPLE_RATE)) / SAMPLE_RATE
        return (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def stub_load_model(dtype, device="mps", load_denoiser=False):
    time.sleep(LOAD_SECONDS)
    return StubModel()


EMITTED = []  # (time, state) of every page the handler sends, noted where it leaves RunProgress
REGULAR = set()  # indexes in EMITTED of pages sent by poll(), which is limited to one per interval


def install_probes():
    """Slow disk for the saving phase, and a log of what the handler sends and when (the client's own
    arrival times are batched by gradio_client, so the rate is measured at the source)."""
    original_write = app.write_clip

    def slow_write(*args, **kwargs):
        time.sleep(PHASE_SECONDS)
        return original_write(*args, **kwargs)

    app.write_clip = slow_write
    original_poll, original_force = progress_ui.RunProgress.poll, progress_ui.RunProgress.force

    def poll(self):
        page = original_poll(self)
        if page is not None:
            REGULAR.add(len(EMITTED))
            EMITTED.append((time.time(), self.stage))
        return page

    def force(self):
        page = original_force(self)
        EMITTED.append((time.time(), self.stage))
        return page

    progress_ui.RunProgress.poll, progress_ui.RunProgress.force = poll, force


def make_workdir():
    """Temporary folder with a stand-in voice (sine wave, transcript) and an output folder."""
    root = Path(tempfile.mkdtemp(prefix="progress-ui-test-"))
    voices, outputs = root / "voices", root / "outputs"
    voices.mkdir()
    outputs.mkdir()
    t = np.arange(VOICE_SECONDS * SAMPLE_RATE) / SAMPLE_RATE
    sf.write(voices / "stub.wav", (0.2 * np.sin(2 * np.pi * 180 * t)).astype(np.float32), SAMPLE_RATE)
    words = ("calm morning light over the quiet farm and the river ").split()
    transcript = ""
    while len(transcript) < VOICE_SECONDS * CHARS_PER_SECOND:
        transcript += words[len(transcript.split()) % len(words)] + " "
    (voices / "stub.txt").write_text(transcript.strip()[: int(VOICE_SECONDS * CHARS_PER_SECOND)] + "\n", encoding="utf-8")
    return root, voices, outputs


def port_free(port):
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def start_stub_ui(voices, outputs):
    assert PORT != 7860, "7860 belongs to the real app"
    assert port_free(PORT), f"port {PORT} is already in use"
    app.VOICES_DIR = voices
    app.DEFAULT_OUT_DIR = outputs
    voxcpm_patch.load_model = stub_load_model
    install_probes()
    demo = app.build_ui().queue()
    demo.launch(**app.launch_settings(PORT), prevent_thread_lock=True, inbrowser=False, quiet=True)
    return demo


# ---------------------------------------------------------------- the checks

failures = []


def check(condition, message):
    print(("ok    " if condition else "FAIL  ") + message)
    if not condition:
        failures.append(message)


def parse(page):
    """The numbers and texts of one bar."""
    def grab(pattern):
        m = re.search(pattern, page, re.S)
        return m.group(1) if m else None

    pct = grab(r'data-pct="(\d*)"')
    return {
        "state": grab(r'data-state="(\w+)"'),
        "pct": int(pct) if pct else None,
        "line": grab(r'<div class="vc-line">(.*?)</div>') or "",
        "sub": grab(r'<div class="vc-sub">(.*?)</div>') or "",
        "bars": len(re.findall(r'role="progressbar"', page)),
        "bar_roots": len(re.findall(r'class="vc-progress ', page)),
        "width": float(grab(r'class="vc-fill[^"]*" style="width: ([\d.]+)%"') or -1),
        "page": page,
    }


def check_updates(updates, outputs, wall_seconds):
    n = len(updates)
    check(n > 20, f"{n} updates arrived")
    bars = [parse(u["page"]) for u in updates]

    check(all(b["bars"] == 1 and b["bar_roots"] == 1 for b in bars), "every update holds exactly one progress element")
    check(all(a["page"] != b["page"] for a, b in zip(bars, bars[1:])), "no update repeats the previous page")

    pcts = [b["pct"] for b in bars if b["pct"] is not None]
    check(all(a <= b for a, b in zip(pcts, pcts[1:])), "the percentage never decreases")
    widths = [b["width"] for b in bars if b["pct"] is not None]
    check(all(a <= b + 1e-9 for a, b in zip(widths, widths[1:])), "the width of the bar never decreases")
    check(bars[0]["pct"] is None, f"the first update is the model load, without a percentage (state {bars[0]['state']})")

    done = [b for b in bars if b["state"] == "done"]
    check(len(done) == 1 and bars[-1]["state"] == "done", "exactly the last update is the done state")
    check(bars[-1]["pct"] == 100, "the last update shows 100%")
    check(all(b["pct"] is None or b["pct"] < 100 for b in bars[:-1]), "no update before the last shows 100%")
    last_saved = max(i for i, u in enumerate(updates) if u["audio"] is not None)
    check(last_saved == n - 1, "the last clip is saved in the update that shows 100%")

    for k in (1, 2):
        lo, hi = (k - 1) * 50, k * 50
        values = {b["pct"] for b in bars if b["pct"] is not None and lo < b["pct"] < hi and b["line"].startswith(f"Clip {k} of 2")}
        check(len(values) > 5, f"clip {k}: {len(values)} distinct intermediate percentages between {lo} and {hi}")

    # Text: phases in order.
    texts = [b["line"] for b in bars]
    expected = [
        "Loading the model (first time only)",
        "Clip 1 of 2: Reading the voice recording",
        "Clip 1 of 2: Preparing the text",
        "Clip 1 of 2: 0.",
        "Clip 1 of 2: Turning the speech into sound",
        "Saving clip 1 of 2",
        "Saved clip 1 of 2:",
        "Clip 2 of 2: Reading the voice recording",
        "Clip 2 of 2: Preparing the text",
        "Clip 2 of 2: 0.",
        "Clip 2 of 2: Turning the speech into sound",
        "Saving clip 2 of 2",
        "Done: saved 2 files in",
    ]
    position, missing = 0, []
    for phrase in expected:
        found = next((i for i in range(position, n) if texts[i].startswith(phrase)), None)
        if found is None:
            missing.append(phrase)
        else:
            position = found
    check(not missing, f"the phase texts appear in order (missing: {missing})" if missing else "the phase texts appear in order")

    # Speech made: grows, ends at the true length.
    for k, line in enumerate(LINES, 1):
        made = [float(m.group(1)) for t in texts if (m := re.match(rf"Clip {k} of 2: ([\d.]+) s of speech made", t))]
        check(all(a <= b for a, b in zip(made, made[1:])), f"clip {k}: seconds of speech made never decrease")
        want = TRUE_STEPS[line] * PATCH
        slack = (progress_ui.UPDATE_INTERVAL + app.POLL_SECONDS + 0.1) / STEP_SECONDS * PATCH  # steps made since the last page
        check(made and want - slack <= made[-1] <= want + 1e-9,
              f"clip {k}: last reading {made[-1] if made else None} s of speech, the clip has {want:.2f} s (at most {slack:.2f} s behind)")

    # Estimate: clip 1 from the voice's rate, clip 2 from the measured steps per character of clip 1.
    def first_estimate(k):
        for b in bars:
            if b["line"].startswith(f"Clip {k} of 2: ") and "s of speech made" in b["line"]:
                m = re.search(r"of about (\d+),", b["sub"])
                if m:
                    return int(m.group(1))
        return None

    prior = round(progress_ui.spoken_characters(LINE_1) * progress_ui.STEPS_PER_SECOND / CHARS_PER_SECOND)
    check(first_estimate(1) == prior, f"clip 1 starts with the estimate from the voice rate: {first_estimate(1)} (expected {prior})")
    calibrated = round(progress_ui.spoken_characters(LINE_2) * TRUE_STEPS[LINE_1] / progress_ui.spoken_characters(LINE_1))
    check(first_estimate(2) == calibrated, f"clip 2 starts with the estimate from clip 1's measured steps: {first_estimate(2)} (expected {calibrated})")
    grown = [int(m.group(1)) for b in bars if b["line"].startswith("Clip 2 of 2: ") and (m := re.search(r"of about (\d+),", b["sub"]))]
    check(max(grown) >= TRUE_STEPS[LINE_2] - 1 and all(a <= b for a, b in zip(grown, grown[1:])),
          f"clip 2: the estimate grew from {grown[0]} to {max(grown)} while the clip ran past it ({TRUE_STEPS[LINE_2]} steps)")

    # Update rate, measured where the handler sends: poll() pages are at least one interval apart,
    # forced pages (the first one, a saved clip with its audio) come on top.
    times = [t for t, _ in EMITTED]
    regular = [EMITTED[i][0] for i in sorted(REGULAR)]
    gaps = [b - a for a, b in zip(regular, regular[1:])]
    check(len(times) == n, f"the handler sent {len(times)} pages, the client received {n}")
    check(min(gaps) >= progress_ui.UPDATE_INTERVAL - 0.01, f"pages from poll() are at least {progress_ui.UPDATE_INTERVAL} s apart (smallest gap {min(gaps):.2f} s)")
    window = max(sum(1 for t2 in times if t <= t2 < t + 1.0) for t in times)
    check(window <= 3, f"at most {window} pages in any second (2 regular plus a forced one)")
    check(len(times) - len(regular) == 3, f"{len(times) - len(regular)} forced pages: the first, and one per saved clip")
    check(n <= 2 * wall_seconds + 3, f"{n} updates in {wall_seconds:.1f} s")

    # Result files.
    files = sorted(outputs.glob("*.wav"))
    check(len(files) == 2, f"two files were written ({[f.name for f in files]})")
    for f, line in zip(files, LINES):
        check(abs(sf.info(str(f)).duration - TRUE_STEPS[line] * PATCH) < 0.01, f"{f.name} is {sf.info(str(f)).duration:.2f} s")
    audio_updates = [i for i, u in enumerate(updates) if u["audio"] != {"__type__": "update"}]
    check(len(audio_updates) == 2, f"the Result player is updated twice ({audio_updates})")


def check_pure_functions():
    ui = progress_ui
    values = [ui.step_fraction(s, 80) for s in range(0, 400)]
    check(all(a < b for a, b in zip(values[1:], values[2:])) and values[0] == 0, "step_fraction rises with every step")
    check(max(values) < 1, f"step_fraction stays below 1 (at 5 x the estimate: {values[-1]:.4f})")
    check(abs(ui.step_fraction(72, 80) - 0.9) < 1e-9 and abs(ui.step_fraction(72.0001, 80) - 0.9) < 1e-3, "step_fraction is continuous at 90% of the estimate")
    check(ui.expected_total(30, 80) == 80 and ui.expected_total(100, 80) > 100 and ui.expected_total(200, 80) >= 200, "expected_total grows past the estimate")
    check(ui.spoken_characters("(slower, warm)  Hello   there") == len("Hello there"), "a (style) prefix is not counted as spoken text")
    check(abs(ui.prior_steps_per_character(15.0) - 6.25 / 15.0) < 1e-9, "prior: 6.25 steps per second over the characters per second")
    check(ui.prior_steps_per_character(1.0) == ui.prior_steps_per_character(ui.MIN_CHARS_PER_SECOND), "prior: voice rate clamped to the lower limit")
    fractions = [ui.clip_fraction(p, s, 100) for p, s in [("voice", 0), ("prepare", 0), ("speech", 0), ("speech", 50), ("speech", 400), ("wrapup", 0), ("decode", 0), ("saving", 0)]]
    check(all(a <= b for a, b in zip(fractions, fractions[1:])) and fractions[-1] < 1, "clip_fraction follows the phases in order and stays below 1")


def check_run_states():
    """RunProgress renders every phase, a retry and a cap lower than the estimate without error, and never goes back."""
    ui = progress_ui
    snap = {"phase": "voice", "phase_seconds": 3.0, "attempt": 0, "steps": 0, "max_steps": None, "patch_seconds": 0.16}
    clock = [1000.0]
    run = ui.RunProgress(2, lambda: dict(snap), now=lambda: clock[0])
    run.start_clip(1, 100)
    pages = []
    for phase, steps, attempt, cap in [
        ("denoise", 0, 0, None), ("voice", 0, 0, None), ("prepare", 0, 0, None), ("speech", 10, 1, 40), ("speech", 40, 1, 40),
        ("wrapup", 40, 1, 40), ("speech", 5, 2, 40), ("speech", 39, 2, 40), ("decode", 39, 2, 40), ("saving", 39, 2, 40), ("idle", 0, 0, None),
    ]:
        snap.update(phase=phase, steps=steps, attempt=attempt, max_steps=cap)
        clock[0] += 1
        pages.append(ui.RunProgress.html(run))
    fills = [float(re.search(r"width: ([\d.]+)%", p).group(1)) for p in pages]
    check(all(a <= b for a, b in zip(fills, fills[1:])), f"the bar never goes back through denoise, a retry and a cap lower than the estimate ({fills})")
    lines = [re.search(r'vc-line">(.*?)<', p).group(1) for p in pages]
    check(all(lines) and lines[0] == "Clip 1 of 2: Cleaning up the voice recording (denoiser)", f"every phase has a text; the first is {lines[0]!r}")
    check(any("(try 2)" in p for p in pages) and any("Checking the take" in p for p in pages) and any("Cleaning up the voice" in p for p in pages), "denoise, retry and checking texts exist")
    check(all(page.count('role="progressbar"') == 1 for page in pages), "one progress element per page in every phase")
    html = run.fail("boom <b>")
    check('vc-error' in html and "boom &lt;b&gt;" in html, "an error page shows the message, escaped")


def run_client():
    from gradio_client import Client

    client = Client(f"http://127.0.0.1:{PORT}", verbose=False)
    started = time.time()
    job = client.submit(
        "stub", app.MODE_TRANSCRIPT, "float16", "\n".join(LINES), 2.0, 10, False, "", "", True, api_name="/speak",
    )
    updates = []
    for output in job:
        audio, page, log = output
        updates.append({"t": time.time() - started, "audio": audio, "page": page, "log": log})
    return updates, time.time() - started


def check_error_run():
    """A generate call that raises: the bar shows the stop and the error reaches the client."""
    from gradio_client import Client

    client = Client(f"http://127.0.0.1:{PORT}", verbose=False)
    job = client.submit("stub", app.MODE_TRANSCRIPT, "float16", "this line is not known to the stand-in model", 2.0, 10, False, "", "", False, api_name="/speak")
    pages, failed = [], False
    try:
        for output in job:
            pages.append(parse(output[1]))
        job.result()
    except Exception:
        failed = True
    check(failed or job.exception() is not None, "the client gets the error of a failed generate call")
    check(bool(pages) and pages[-1]["state"] == "error" and "Stopped: KeyError" in pages[-1]["line"], f"the last page before the error shows the stop ({pages[-1]['line'][:50] if pages else None})")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true", help="serve the stand-in UI on 127.0.0.1:7861 and wait")
    args = parser.parse_args()

    root, voices, outputs = make_workdir()
    demo = start_stub_ui(voices, outputs)
    if args.serve:
        print(f"stand-in UI on http://127.0.0.1:{PORT} (voice 'stub', output {outputs}); Ctrl-C stops it", flush=True)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            demo.close()
            shutil.rmtree(root, ignore_errors=True)
            return 0
    try:
        config = demo.get_config_file()
        speak = next(d for d in config["dependencies"] if d.get("api_name") == "speak")
        check(speak.get("show_progress") == "hidden", f"the Speak event has show_progress={speak.get('show_progress')!r}")
        check(len(speak["outputs"]) == 3, "the Speak event writes to 3 outputs: Result player, progress bar, saved files")
        updates, wall = run_client()
        check_updates(updates, outputs, wall)
        print(f"{len(updates)} updates in {wall:.1f} s; last page: {parse(updates[-1]['page'])['line']}")
        check_pure_functions()
        check_run_states()
        check_error_run()
    finally:
        demo.close()
        shutil.rmtree(root, ignore_errors=True)
    check(port_free(PORT), f"port {PORT} is free again")
    print("PASS" if not failures else f"FAILED: {len(failures)} check(s)")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())

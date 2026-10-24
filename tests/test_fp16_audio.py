#!/usr/bin/env python3
"""Checks that VoxCPM2 in float16 on Apple Silicon (MPS) makes speech, and measures the run.

Run:  .venv/bin/python tests/test_fp16_audio.py [--dtype float16|float32] [--seed N] [--repeat N]
          [--workdir DIR] [--metrics-json PATH] [--ref PATH [--ref-mode reference]] [--no-asr]

What it does, in one process:
  1. Makes a stand-in reference voice of about 20 s with macOS `say` (the transcript is known),
     or uses --ref (with --ref-mode reference, the recording alone is passed, without transcript).
  2. Loads VoxCPM2 in the given precision (voxcpm_patch.load_model), seeds torch, and generates
     TARGET_TEXT. Records load time, generation time, output duration, memory (MPS driver
     allocation, process RSS, process footprint, peak RSS) and swap use (`sysctl vm.swapusage`).
  3. Frees the model, then checks the output:
       duration between 2 and 10 s, RMS above 0.01, median spectral flatness below 0.1,
       and a transcription by funasr SenseVoiceSmall that contains at least 8 of the 13 target words
       (each target word occurrence is matched once against the words of the transcription).
Exit 0 when every check passes. Outputs go to --workdir, or a temporary folder removed at the end.
The first run downloads SenseVoiceSmall (about 0.9 GB) from ModelScope into ~/.cache/modelscope.
--no-asr skips the transcription check and says so; without that flag a failing ASR is a failure.
"""
import argparse
import collections
import json
import os
import re
import resource
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

import numpy as np
import soundfile as sf
import torch

import voxcpm_patch

TARGET_TEXT = "The quick brown fox jumps over the lazy dog near the river bank."
REFERENCE_TEXT = (
    "The old lighthouse stood at the end of the pier, and every evening the keeper climbed the narrow "
    "stairs to light the lamp. Fishing boats returned slowly across the grey water, and the gulls "
    "followed them into the harbour. When the last boat was tied up, the keeper went home for supper. "
    "Later, a soft rain began to fall, and the whole town fell quiet."
)
MIN_SECONDS, MAX_SECONDS = 2.0, 10.0
MIN_RMS = 0.01
MAX_FLATNESS = 0.1
MIN_WORDS = 8
ASR_MODEL = "iic/SenseVoiceSmall"
FFMPEG = "/opt/homebrew/bin/ffmpeg" if Path("/opt/homebrew/bin/ffmpeg").exists() else shutil.which("ffmpeg")
GIB = 1024 ** 3


# ---------------------------------------------------------------- measurement

def rss_bytes():
    out = subprocess.run(["ps", "-o", "rss=", "-p", str(os.getpid())], capture_output=True, text=True).stdout
    return int(out.strip() or 0) * 1024


def footprint_bytes():
    """Physical footprint of this process (macOS `footprint`): includes compressed, swapped and GPU memory."""
    out = subprocess.run(["footprint", "-p", str(os.getpid())], capture_output=True, text=True).stdout
    match = re.search(r"Footprint:\s*([\d.]+)\s*(KB|MB|GB)", out)
    if not match:
        return 0
    return int(float(match.group(1)) * {"KB": 1024, "MB": 1024 ** 2, "GB": GIB}[match.group(2)])


def mps_bytes():
    return torch.mps.driver_allocated_memory() if torch.backends.mps.is_available() else 0


def swap_text():
    return subprocess.run(["sysctl", "-n", "vm.swapusage"], capture_output=True, text=True).stdout.strip()


def swap_used_mb(text):
    match = re.search(r"used = ([\d.]+)M", text)
    return float(match.group(1)) if match else None


class Sampler(threading.Thread):
    """Samples memory once per second and keeps the maxima."""

    def __init__(self):
        super().__init__(daemon=True)
        self.stop_flag = threading.Event()
        self.peak = {"mps_driver": 0, "rss": 0, "footprint": 0}

    def run(self):
        n = 0
        while not self.stop_flag.is_set():
            self.sample(with_footprint=(n % 3 == 0))
            n += 1
            self.stop_flag.wait(1.0)

    def sample(self, with_footprint=True):
        self.peak["mps_driver"] = max(self.peak["mps_driver"], mps_bytes())
        self.peak["rss"] = max(self.peak["rss"], rss_bytes())
        if with_footprint:
            self.peak["footprint"] = max(self.peak["footprint"], footprint_bytes())

    def stop(self):
        self.stop_flag.set()
        self.join()
        self.sample()


def now_memory():
    return {"mps_driver_gib": mps_bytes() / GIB, "rss_gib": rss_bytes() / GIB, "footprint_gib": footprint_bytes() / GIB}


# ---------------------------------------------------------------- checks

def make_reference(workdir):
    """A stand-in voice: macOS `say`, mono 48 kHz 16-bit WAV, like a saved voice."""
    text = workdir / "reference.txt"
    text.write_text(REFERENCE_TEXT, encoding="utf-8")
    aiff = workdir / "reference.aiff"
    subprocess.run(["say", "-f", str(text), "-o", str(aiff)], check=True)
    wav = workdir / "reference.wav"
    subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(aiff), "-ac", "1", "-ar", "48000",
         "-c:a", "pcm_s16le", str(wav)],
        check=True,
    )
    return wav


def spectral_checks(wav, sample_rate):
    import librosa

    seconds = len(wav) / sample_rate
    rms = float(np.sqrt(np.mean(np.square(wav.astype(np.float64)))))
    flatness = float(np.median(librosa.feature.spectral_flatness(y=wav.astype(np.float32))))
    return {"seconds": seconds, "rms": rms, "median_flatness": flatness}


def words(text):
    return re.findall(r"[a-z']+", text.lower())


def matched_words(transcript, target):
    """How many target word occurrences appear in the transcript (each transcript word is used once)."""
    available = collections.Counter(words(transcript))
    hits = 0
    for word in words(target):
        if available[word] > 0:
            available[word] -= 1
            hits += 1
    return hits


def transcribe(path):
    """SenseVoiceSmall through funasr, on the CPU, English."""
    from funasr import AutoModel
    from funasr.utils.postprocess_utils import rich_transcription_postprocess

    asr = AutoModel(model=ASR_MODEL, device="cpu", disable_update=True, disable_pbar=True)
    result = asr.generate(input=str(path), language="en", use_itn=False, batch_size_s=60)
    return rich_transcription_postprocess(result[0]["text"])


def evaluate(path, use_asr):
    """Returns (metrics dict, list of (check name, ok, detail))."""
    wav, sr = sf.read(str(path), dtype="float32")
    spec = spectral_checks(wav, sr)
    checks = [
        ("duration between 2 and 10 s", MIN_SECONDS <= spec["seconds"] <= MAX_SECONDS, f"{spec['seconds']:.2f} s"),
        ("RMS above 0.01", spec["rms"] > MIN_RMS, f"{spec['rms']:.4f}"),
        ("median spectral flatness below 0.1", spec["median_flatness"] < MAX_FLATNESS, f"{spec['median_flatness']:.2e}"),
    ]
    metrics = dict(spec)
    n_target = len(words(TARGET_TEXT))
    if use_asr:
        try:
            transcript = transcribe(path)
            hits = matched_words(transcript, TARGET_TEXT)
            metrics.update(transcript=transcript, matched_words=hits, target_words=n_target)
            checks.append((f"ASR contains at least {MIN_WORDS} of {n_target} target words", hits >= MIN_WORDS,
                           f"{hits} of {n_target}; transcript: {transcript!r}"))
        except Exception as exc:  # a failing ASR is a failed check, not a skipped one
            metrics.update(transcript=None, asr_error=f"{type(exc).__name__}: {exc}")
            checks.append(("ASR runs", False, f"{type(exc).__name__}: {exc}"))
    else:
        print("WARNING: --no-asr given. Intelligibility was NOT checked by ASR; only the spectral checks ran.")
    return metrics, checks


# ---------------------------------------------------------------- main

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dtype", choices=voxcpm_patch.DTYPES, default="float16")
    parser.add_argument("--seed", type=int, default=1234)
    parser.add_argument("--repeat", type=int, default=1, help="generations in the loaded model (default 1)")
    parser.add_argument("--workdir", help="folder for the reference and outputs (default: temporary, removed at the end)")
    parser.add_argument("--metrics-json", help="write the measurements to this file")
    parser.add_argument("--ref", help="use this recording as the reference instead of a `say` clip")
    parser.add_argument("--ref-mode", choices=("transcript", "reference"), default="transcript",
                        help="transcript: recording and its words (needs the stand-in); reference: recording alone")
    parser.add_argument("--no-asr", action="store_true", help="skip the transcription check (stated in the output)")
    args = parser.parse_args()

    if not torch.backends.mps.is_available():
        print("FAIL: MPS is not available; this test is for Apple Silicon")
        return 1
    if args.ref and args.ref_mode == "transcript":
        print("FAIL: --ref has no transcript here; use --ref-mode reference")
        return 1
    if not args.ref and (not FFMPEG or not shutil.which("say")):
        print("FAIL: needs macOS `say` and ffmpeg to make the stand-in reference")
        return 1

    workdir = Path(args.workdir) if args.workdir else Path(tempfile.mkdtemp(prefix="fp16-audio-"))
    workdir.mkdir(parents=True, exist_ok=True)
    result = {"dtype": args.dtype, "seed": args.seed, "target_text": TARGET_TEXT}
    ok = False
    try:
        ref = Path(args.ref) if args.ref else make_reference(workdir)
        ref_seconds = sf.info(str(ref)).duration
        mode = "reference only" if args.ref_mode == "reference" else "reference + transcript"
        print(f"dtype {args.dtype}; reference {ref_seconds:.1f} s, mode {mode}; workdir {workdir}", flush=True)
        voice = {"reference_wav_path": str(ref)}
        if args.ref_mode == "transcript":
            voice.update(prompt_wav_path=str(ref), prompt_text=REFERENCE_TEXT)

        result["swap_before"] = swap_text()
        result["memory_before"] = now_memory()
        sampler = Sampler()
        sampler.start()

        start = time.time()
        model = voxcpm_patch.load_model(args.dtype, device="mps")
        result["load_seconds"] = time.time() - start
        result["memory_after_load"] = now_memory()
        result["dtypes"] = voxcpm_patch.describe_dtypes(model)
        print(f"model loaded in {result['load_seconds']:.1f} s; dtypes:", flush=True)
        for part, dtype in result["dtypes"].items():
            print(f"  {part}: {dtype}", flush=True)

        sample_rate = model.tts_model.sample_rate
        outputs = []
        result["generations"] = []
        for i in range(1, args.repeat + 1):
            torch.manual_seed(args.seed)
            start = time.time()
            wav = model.generate(text=TARGET_TEXT, cfg_value=2.0, inference_timesteps=10, **voice)
            seconds = time.time() - start
            path = workdir / f"out_{args.dtype}_{i}.wav"
            sf.write(str(path), wav, sample_rate)
            outputs.append(path)
            result["generations"].append({"seconds": seconds, "audio_seconds": len(wav) / sample_rate})
            print(f"generation {i}: {len(wav) / sample_rate:.2f} s of audio in {seconds:.1f} s -> {path.name}", flush=True)
        result["memory_after_generate"] = now_memory()
        sampler.stop()
        result["peak"] = {
            "mps_driver_gib": sampler.peak["mps_driver"] / GIB,
            "rss_sampled_gib": sampler.peak["rss"] / GIB,
            "rss_max_gib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / GIB,  # bytes on macOS
            "footprint_gib": sampler.peak["footprint"] / GIB,
        }
        result["swap_after"] = swap_text()

        del model
        voxcpm_patch.release()
        result["memory_after_release"] = now_memory()

        # Checks run after the model is gone, so the ASR model does not add to the measured peak.
        all_ok = True
        result["checks"] = []
        for i, path in enumerate(outputs, 1):
            metrics, checks = evaluate(path, use_asr=not args.no_asr)
            result["checks"].append({"output": path.name, **metrics, "results": [(n, bool(o), d) for n, o, d in checks]})
            print(f"checks for {path.name}:")
            for name, passed, detail in checks:
                print(f"  {'ok  ' if passed else 'FAIL'} {name}: {detail}")
                all_ok = all_ok and passed
        ok = all_ok
    finally:
        if args.metrics_json:
            Path(args.metrics_json).write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
        if not args.workdir:
            shutil.rmtree(workdir, ignore_errors=True)

    print("peak:", json.dumps(result.get("peak")), flush=True)
    print(f"swap before: {result.get('swap_before')}\nswap after:  {result.get('swap_after')}")
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

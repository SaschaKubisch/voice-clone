#!/usr/bin/env python3
"""Checks that the chunked reference encoding in voxcpm_patch.py gives the same latents as voxcpm's own.

Run:  .venv/bin/python tests/test_chunked_encode.py [--workdir DIR]

Loads only the audio VAE (not the language model) from the cached openbmb/VoxCPM2 snapshot.
Makes a 45 s speech clip with macOS `say` and ffmpeg (in --workdir, or a temporary folder that is
removed afterwards). Compares voxcpm's own VoxCPM2Model._encode_wav with the patched one, for right
and left padding, and checks the short-audio path and the cache. Exit 0 when every check passes.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

import torch

import voxcpm_patch

CLIP_SECONDS = 45
SHORT_SECONDS = 8
COSINE_MIN = 0.999
FFMPEG = "/opt/homebrew/bin/ffmpeg" if Path("/opt/homebrew/bin/ffmpeg").exists() else shutil.which("ffmpeg")

SPEECH = (
    "The harbour was quiet in the early morning, and the fishing boats rocked gently against the old stone wall. "
    "A woman in a yellow coat carried a crate of ice across the dock, humming a tune that nobody else knew. "
    "By nine o'clock the market was full, and the smell of bread, coffee and salt drifted along the street. "
    "Children ran between the stalls, chased by a dog that had decided the day belonged to him. "
    "Later, when the sun climbed over the hills, the town settled into the slow rhythm of the afternoon. "
)


def make_clip(path, seconds, workdir):
    """Speech from `say`, converted to 16 kHz mono 16-bit WAV and cut to `seconds`."""
    text = workdir / "speech.txt"
    text.write_text(SPEECH * 6, encoding="utf-8")
    aiff = workdir / "speech.aiff"
    subprocess.run(["say", "-f", str(text), "-o", str(aiff)], check=True)
    subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(aiff), "-t", str(seconds),
         "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(path)],
        check=True,
    )


def load_vae(device):
    from huggingface_hub import snapshot_download
    from voxcpm.modules.audiovae import AudioVAEConfigV2, AudioVAEV2

    snapshot = Path(snapshot_download("openbmb/VoxCPM2", local_files_only=True))
    config = json.loads((snapshot / "config.json").read_text(encoding="utf-8"))
    vae = AudioVAEV2(config=AudioVAEConfigV2(**config["audio_vae_config"]))
    state = torch.load(snapshot / "audiovae.pth", map_location="cpu", weights_only=True)
    vae.load_state_dict(state.get("state_dict", state))
    return vae.to(torch.float32).to(device).eval(), config["patch_size"]


def stub_model(vae, patch_size, device):
    """Just the attributes VoxCPM2Model._encode_wav reads, so the language model need not load."""
    from voxcpm.model.voxcpm2 import VoxCPM2Model

    stub = types.SimpleNamespace(
        audio_vae=vae, device=device, patch_size=patch_size,
        chunk_size=vae.chunk_size, _encode_sample_rate=vae.sample_rate,
    )
    stub._encode_wav = types.MethodType(VoxCPM2Model._encode_wav, stub)
    return stub


def compare(name, reference, candidate):
    ok = reference.shape == candidate.shape
    a, b = reference.flatten().double(), candidate.flatten().double()
    if ok:
        diff = (a - b).abs()
        cosine = torch.nn.functional.cosine_similarity(a, b, dim=0).item()
        ok = cosine >= COSINE_MIN
        print(f"{name}: shape {tuple(reference.shape)} vs {tuple(candidate.shape)}, cosine {cosine:.8f}, "
              f"max abs diff {diff.max().item():.3e}, mean abs diff {diff.mean().item():.3e} "
              f"-> {'ok' if ok else 'FAIL'}")
    else:
        print(f"{name}: shape {tuple(reference.shape)} vs {tuple(candidate.shape)} -> FAIL (shapes differ)")
    return ok


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workdir", help="folder for the test clips (default: a temporary folder, removed at the end)")
    args = parser.parse_args()

    if not FFMPEG or not shutil.which("say"):
        print("FAIL: needs macOS `say` and ffmpeg to make the test clip")
        return 1
    workdir = Path(args.workdir) if args.workdir else Path(tempfile.mkdtemp(prefix="chunked-encode-"))
    workdir.mkdir(parents=True, exist_ok=True)
    device = torch.device("mps" if torch.backends.mps.is_available() else "cpu")
    results = []
    try:
        long_clip, short_clip = workdir / "clip45.wav", workdir / "clip8.wav"
        make_clip(long_clip, CLIP_SECONDS, workdir)
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(long_clip),
                        "-t", str(SHORT_SECONDS), str(short_clip)], check=True)
        print(f"device {device}; clips in {workdir}")

        vae, patch_size = load_vae(device)
        stub = stub_model(vae, patch_size, device)

        # voxcpm's own encoder, before the patch
        original = {}
        with torch.inference_mode():
            for mode in ("right", "left"):
                original[mode] = stub._encode_wav(str(long_clip), padding_mode=mode).clone()
            short_original = stub._encode_wav(str(short_clip)).clone()

        voxcpm_patch.patch_vae(vae, patch_size)
        voxcpm_patch.patch_encode_cache(stub)

        with torch.inference_mode():
            for mode in ("right", "left"):
                start = time.time()
                chunked = stub._encode_wav(str(long_clip), padding_mode=mode)
                print(f"chunked encode, {mode} padding: {time.time() - start:.1f} s, {vae.last_encode_chunks} pieces")
                results.append(compare(f"{CLIP_SECONDS} s, {mode} padding", original[mode], chunked))
                results.append(vae.last_encode_chunks == 3)  # 20 s + 20 s + 5 s

            # Audio of 20 s or less takes the original code path: identical values.
            short = stub._encode_wav(str(short_clip))
            same = vae.last_encode_chunks == 0 and torch.equal(short, short_original)
            print(f"{SHORT_SECONDS} s clip takes the original path, values identical: {'ok' if same else 'FAIL'}")
            results.append(same)

            # Cache: the second call returns the stored result without encoding; a changed file is encoded again.
            calls = []
            counted = vae.encode
            vae.encode = lambda *a, **k: (calls.append(1), counted(*a, **k))[1]
            again = stub._encode_wav(str(short_clip))
            hit = again is short and not calls
            print(f"cache hit on an unchanged file, no new encode: {'ok' if hit else 'FAIL'}")
            results.append(hit)
            os.utime(short_clip, ns=(time.time_ns(), time.time_ns() + 1_000_000_000))
            stub._encode_wav(str(short_clip))
            miss = len(calls) == 1
            print(f"cache miss after the file's modification time changed, encoded again: {'ok' if miss else 'FAIL'}")
            results.append(miss)
            vae.encode = counted
    finally:
        if not args.workdir:
            shutil.rmtree(workdir, ignore_errors=True)

    passed = all(results)
    print("PASS" if passed else "FAIL")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())

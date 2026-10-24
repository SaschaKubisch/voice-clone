"""Memory-safe reference encoding for VoxCPM2 on a 16 GB Apple Silicon Mac.

Problem: voxcpm encodes the whole reference recording through the audio VAE in one pass.
Peak memory grows with the length of the recording (about 2.2 GiB at 20 s, 5.8 GiB at 120 s,
9.2 GiB at 180 s), on top of the model, and the encoding is repeated on every generate call.

Fix, applied from this project without editing the installed package:

1. Chunked encoding. A recording longer than CHUNK_SECONDS is encoded in pieces of
   CHUNK_SECONDS. Each piece is read with a few seconds of audio on both sides, so the
   convolutions at the piece borders see the same samples as in a single pass. The latent
   frames that belong to that extra audio are dropped, and the remaining frames are joined.
   The result has the same shape as the single-pass result and the same values up to float
   rounding. A recording of CHUNK_SECONDS or less takes the original code path unchanged.
2. Cache. The encoded result is kept in memory per (absolute path, file modification time,
   file size, padding mode, VAD flag), so one recording is encoded once per app run.

Grid. The VAE turns 640 input samples into one latent frame (hop). The model reads four
frames as one patch, so one model position covers 2560 samples. Piece borders and context
lengths are whole patches, so every piece starts on both grids and the strided convolutions
see the same sample phase as in a single pass.

Use:  model = voxcpm_patch.apply(VoxCPM.from_pretrained(...))
"""
import gc
import os
import threading
from collections import OrderedDict

import torch

CHUNK_SECONDS = 20.0  # audio whose latents are kept per piece
CONTEXT_LEFT_SECONDS = 3.0  # extra audio read before a piece
CONTEXT_RIGHT_SECONDS = 1.0  # extra audio read after a piece
CACHE_ENTRIES = 4  # a voice uses two entries: reference (right padding) and prompt (left padding)


def _release_memory(device):
    """Return freed buffers to the system between pieces."""
    gc.collect()
    if device.type == "mps":
        torch.mps.empty_cache()
    elif device.type == "cuda":
        torch.cuda.empty_cache()


def patch_vae(
    vae,
    patch_size,
    chunk_seconds=CHUNK_SECONDS,
    context_left_seconds=CONTEXT_LEFT_SECONDS,
    context_right_seconds=CONTEXT_RIGHT_SECONDS,
):
    """Replace vae.encode with a version that encodes long audio in pieces.

    The original is kept as vae.encode_unchunked. After each call, vae.last_encode_chunks holds
    the number of pieces (0 when the original path ran).
    """
    if getattr(vae, "encode_unchunked", None) is not None:
        return vae
    original = vae.encode
    hop = int(vae.chunk_size)  # input samples per latent frame (640)
    sample_rate = int(vae.sample_rate)  # encoder input rate (16000)
    patch_samples = hop * int(patch_size)  # input samples per model position (2560)

    def whole_patches(seconds):
        return max(1, round(seconds * sample_rate / patch_samples)) * int(patch_size)

    core_frames = whole_patches(chunk_seconds)
    left_frames = whole_patches(context_left_seconds)
    right_frames = whole_patches(context_right_seconds)
    core_samples = core_frames * hop

    def encode(audio_data, sample_rate_in):
        """Same signature and result as AudioVAE.encode: (B, T) or (B, 1, T) to (B, D, frames)."""
        if audio_data.ndim == 2:
            audio_data = audio_data.unsqueeze(1)
        if audio_data.shape[-1] <= core_samples:
            vae.last_encode_chunks = 0
            return original(audio_data, sample_rate_in)

        device = audio_data.device
        pieces = []
        with torch.inference_mode():
            audio_data = vae.preprocess(audio_data, sample_rate_in)  # right-pad to a whole frame
            n_frames = audio_data.shape[-1] // hop
            for start in range(0, n_frames, core_frames):
                end = min(start + core_frames, n_frames)
                lo = max(0, start - left_frames)
                hi = min(n_frames, end + right_frames)
                piece = audio_data[..., lo * hop : hi * hop]
                z = original(piece, sample_rate_in)
                if z.shape[-1] != hi - lo:
                    raise RuntimeError(
                        f"VAE returned {z.shape[-1]} frames for {hi - lo} expected; "
                        "the frame grid assumption does not hold."
                    )
                pieces.append(z[..., start - lo : end - lo].contiguous().cpu())
                del piece, z
                _release_memory(device)
            result = torch.cat(pieces, dim=-1)
        vae.last_encode_chunks = len(pieces)
        return result

    vae.encode_unchunked = original
    vae.last_encode_chunks = 0
    vae.encode = encode
    return vae


def _cache_key(wav_path, padding_mode, trim_silence_vad):
    try:
        path = os.path.abspath(os.fspath(wav_path))
        stat = os.stat(path)
    except (OSError, TypeError):
        return None
    return (path, stat.st_mtime_ns, stat.st_size, padding_mode, bool(trim_silence_vad))


def patch_encode_cache(tts, max_entries=CACHE_ENTRIES):
    """Wrap tts._encode_wav so a file that did not change is encoded once.

    A cache hit returns the stored tensor itself. voxcpm 2.0.3 only reads it (torch.cat),
    so sharing it is safe. Denoised copies are temporary files with new names, so they never hit.
    """
    if getattr(tts, "encode_wav_uncached", None) is not None:
        return tts
    original = tts._encode_wav
    cache = OrderedDict()
    lock = threading.Lock()

    def encode_wav(wav_path, padding_mode="right", trim_silence_vad=False):
        key = _cache_key(wav_path, padding_mode, trim_silence_vad)
        if key is not None:
            with lock:
                hit = cache.get(key)
                if hit is not None:
                    cache.move_to_end(key)
                    return hit
        feat = original(wav_path, padding_mode=padding_mode, trim_silence_vad=trim_silence_vad)
        if key is not None:
            with lock:
                cache[key] = feat
                while len(cache) > max_entries:
                    cache.popitem(last=False)
        return feat

    tts.encode_wav_uncached = original
    tts.encode_cache = cache
    tts._encode_wav = encode_wav
    return tts


def apply(model):
    """Patch a loaded voxcpm.VoxCPM (or its tts_model) in place and return it."""
    tts = getattr(model, "tts_model", model)
    patch_vae(tts.audio_vae, tts.patch_size)
    patch_encode_cache(tts)
    return model

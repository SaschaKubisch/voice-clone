import sys
import time

import numpy as np
import soundfile as sf

from voxcpm import VoxCPM

device = sys.argv[1] if len(sys.argv) > 1 else "mps"
out_path = sys.argv[2] if len(sys.argv) > 2 else "demo.wav"
text = "Hello, this is a test of VoxCPM two."

model = VoxCPM.from_pretrained(
    "openbmb/VoxCPM2",
    load_denoiser=False,
    optimize=False,
    device=device,
)
print("device used:", model.tts_model.device)

start = time.time()
wav = model.generate(text=text, cfg_value=2.0, inference_timesteps=10)
elapsed = time.time() - start

sr = model.tts_model.sample_rate
sf.write(out_path, wav, sr)

print(f"generation time: {elapsed:.2f} s")
print(f"sample rate: {sr}, samples: {len(wav)}, duration: {len(wav) / sr:.2f} s")
print(f"rms: {float(np.sqrt(np.mean(np.square(wav)))):.5f}, peak: {float(np.max(np.abs(wav))):.5f}")
print("wrote", out_path)

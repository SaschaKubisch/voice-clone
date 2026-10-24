#!/usr/bin/env python3
"""Clone a voice with VoxCPM2 and generate speech. The model is loaded once.

Modes (chosen by the arguments):
  --ref only                    reference mode: reference_wav_path=REF
  --ref plus --ref-text(-file)  reference + continuation mode (highest fidelity):
                                reference_wav_path=REF, prompt_wav_path=REF, prompt_text=TEXT
Text input: --text, or --text-file (one clip per non-empty line), or stdin loop.
"""
import argparse
import sys
import time
from pathlib import Path

# Defaults are relative to this script's folder, so the project can live anywhere.
SCRIPT_DIR = Path(__file__).resolve().parent


def build_parser():
    p = argparse.ArgumentParser(
        description="Clone a voice with VoxCPM2 and generate speech (model loaded once).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python clone.py --ref voices/me.wav --text 'Hello there.' --out hello.wav\n"
            "  python clone.py --ref voices/me.wav --ref-text-file voices/me.txt --text-file lines.txt\n"
            "  python clone.py --ref voices/me.wav          # type lines; empty line or Ctrl-D ends\n"
            "\n"
            "A text starting with a control instruction in parentheses, e.g. '(slower, warm)Hello',\n"
            "works in reference-only mode. The shipped CLI rejects it together with a prompt text."
        ),
    )
    p.add_argument("--ref", required=True, metavar="PATH", help="reference recording of the voice")
    p.add_argument("--ref-text", metavar="TEXT", help="exact words spoken in --ref (enables highest-fidelity mode)")
    p.add_argument("--ref-text-file", metavar="PATH", help="file holding the exact words spoken in --ref")
    p.add_argument("--text", metavar="TEXT", help="text to speak (one clip)")
    p.add_argument("--text-file", metavar="PATH", help="file with one text per non-empty line (one clip each)")
    p.add_argument("--out", metavar="PATH", help="output WAV for a single --text")
    p.add_argument("--out-dir", default=str(SCRIPT_DIR / "outputs"), metavar="DIR", help="output folder for multiple clips (default: outputs/ next to this script)")
    p.add_argument("--device", default="mps", help="mps, cpu, cuda, auto (default: mps)")
    p.add_argument("--cfg", type=float, default=2.0, help="CFG guidance scale, 1.0-3.0 recommended (default: 2.0)")
    p.add_argument("--steps", type=int, default=10, help="inference steps, 4-30 recommended (default: 10)")
    p.add_argument("--denoise", action="store_true", help="load the denoiser and denoise the reference audio")
    return p


def read_text_file(path, parser, label):
    f = Path(path)
    if not f.is_file():
        parser.error(f"{label} '{path}' does not exist")
    return f.read_text(encoding="utf-8")


def next_free(out_dir, start=1):
    n = start
    while (out_dir / f"output_{n:03d}.wav").exists():
        n += 1
    return n


def main():
    parser = build_parser()
    args = parser.parse_args()

    if not Path(args.ref).is_file():
        parser.error(f"--ref '{args.ref}' does not exist")
    if args.ref_text and args.ref_text_file:
        parser.error("use either --ref-text or --ref-text-file, not both")
    if args.text and args.text_file:
        parser.error("use either --text or --text-file, not both")
    if args.out and not args.text:
        parser.error("--out is only valid with --text; use --out-dir for multiple clips")
    if not (0.1 <= args.cfg <= 10.0):
        parser.error("--cfg must be between 0.1 and 10.0 (recommended 1.0-3.0)")
    if not (1 <= args.steps <= 100):
        parser.error("--steps must be between 1 and 100 (recommended 4-30)")

    ref_text = None
    if args.ref_text_file:
        ref_text = read_text_file(args.ref_text_file, parser, "--ref-text-file").strip()
    elif args.ref_text:
        ref_text = args.ref_text.strip()
    if (args.ref_text is not None or args.ref_text_file) and not ref_text:
        parser.error("the reference text is empty")

    texts = None
    if args.text_file:
        texts = [ln.strip() for ln in read_text_file(args.text_file, parser, "--text-file").splitlines() if ln.strip()]
        if not texts:
            parser.error("--text-file has no non-empty lines")

    # Heavy imports after argument validation so --help and errors are fast.
    import soundfile as sf
    from voxcpm import VoxCPM
    from voxcpm_patch import apply as patch_reference_encoding

    t0 = time.time()
    model = VoxCPM.from_pretrained(
        "openbmb/VoxCPM2",
        load_denoiser=args.denoise,
        optimize=False,
        device=args.device,
    )
    patch_reference_encoding(model)  # long references encoded in pieces, once per run
    sr = model.tts_model.sample_rate
    print(f"model loaded on {model.tts_model.device} in {time.time() - t0:.1f} s", flush=True)

    ref_path = str(Path(args.ref))
    if ref_text:
        mode = "reference + continuation (highest fidelity)"
        voice_kwargs = dict(reference_wav_path=ref_path, prompt_wav_path=ref_path, prompt_text=ref_text)
    else:
        mode = "reference only"
        voice_kwargs = dict(reference_wav_path=ref_path)
    print(f"mode: {mode}; cfg={args.cfg}, steps={args.steps}, denoise={args.denoise}", flush=True)

    def synth(text, out_path):
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        start = time.time()
        wav = model.generate(
            text=text,
            cfg_value=args.cfg,
            inference_timesteps=args.steps,
            denoise=args.denoise,
            **voice_kwargs,
        )
        elapsed = time.time() - start
        sf.write(str(out_path), wav, sr)
        print(f"generated {len(wav) / sr:.2f} s of audio in {elapsed:.2f} s -> {out_path}", flush=True)

    out_dir = Path(args.out_dir)

    if args.text:
        synth(args.text, args.out if args.out else out_dir / "output_001.wav")
    elif texts is not None:
        for i, text in enumerate(texts, 1):
            print(f"[{i}/{len(texts)}] {text}", flush=True)
            try:
                synth(text, out_dir / f"output_{i:03d}.wav")
            except Exception as exc:
                print(f"failed on line {i}: {exc}", file=sys.stderr, flush=True)
    else:
        n = next_free(out_dir)
        print("Type a sentence and press Enter. Empty line or Ctrl-D ends.", flush=True)
        while True:
            try:
                line = input("> ").strip()
            except EOFError:
                break
            if not line:
                break
            try:
                synth(line, out_dir / f"output_{n:03d}.wav")
                n += 1
            except Exception as exc:
                print(f"failed: {exc}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()

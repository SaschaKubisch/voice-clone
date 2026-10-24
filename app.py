#!/usr/bin/env python3
"""Local web interface for VoxCPM2 voice cloning and text-to-speech.

Start:  .venv/bin/python app.py     (serves http://127.0.0.1:7860 only)

Tabs: Record voice (save a voice to voices/), Speak (generate audio), Files (list and
reveal generated audio). The model loads once per process, on the first Generate.
"""
import os

os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "False")

import re
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait as wait_for_futures
from datetime import datetime
from pathlib import Path

import gradio as gr

APP_DIR = Path(__file__).resolve().parent
VOICES_DIR = APP_DIR / "voices"
DEFAULT_OUT_DIR = APP_DIR / "outputs"
PASSAGE_FILE = VOICES_DIR / "me.txt"
FFMPEG = "/opt/homebrew/bin/ffmpeg" if Path("/opt/homebrew/bin/ffmpeg").exists() else shutil.which("ffmpeg")
DENOISER_ID = "iic/speech_zipenhancer_ans_multiloss_16k_base"

MODE_TRANSCRIPT = "Recording + transcript (closest match)"
MODE_REFERENCE = "Recording only (allows style prefix)"

# Gradio only serves files from the working directory, the temp folder and allowed paths.
# Audio players therefore get a copy in the temp folder, so any output folder works.
PLAYER_DIR = Path(tempfile.mkdtemp(prefix="voice-ui-"))

MODEL = None
LOAD_SECONDS = None
MODEL_LOCK = threading.Lock()
# One worker: the model never runs two jobs at once.
WORKER = ThreadPoolExecutor(max_workers=1)


# ---------------------------------------------------------------- helpers

def safe_name(text, max_len=60):
    """Letters, digits, dot, dash and underscore only; everything else becomes '_'."""
    name = re.sub(r"[^\w.-]+", "_", (text or "").strip()).strip("._-")
    return name[:max_len]


def voice_names():
    return sorted(p.stem for p in VOICES_DIR.glob("*.wav"))


def resolve_dir(text):
    path = Path((text or "").strip() or str(DEFAULT_OUT_DIR)).expanduser()
    return path if path.is_absolute() else APP_DIR / path


def unique_path(folder, base):
    """folder/base.wav, or base_2.wav, base_3.wav ... so nothing is overwritten."""
    path = folder / f"{base}.wav"
    n = 2
    while path.exists():
        path = folder / f"{base}_{n}.wav"
        n += 1
    return path


def auto_base(text, stamp, index=None):
    words = re.sub(r"^\([^)]*\)\s*", "", text)  # drop a "(style)" prefix
    slug = safe_name(" ".join(words.split()[:6]), max_len=40).lower()
    parts = [stamp] + ([f"{index:02d}"] if index else []) + ([slug] if slug else [])
    return "_".join(parts)


def audio_seconds(path):
    import soundfile as sf

    return sf.info(str(path)).duration


def playable(path):
    dest = PLAYER_DIR / Path(path).name
    shutil.copyfile(path, dest)
    return str(dest)


def mmss(seconds):
    seconds = int(seconds)
    return f"{seconds // 60}:{seconds % 60:02d}"


# ---------------------------------------------------------------- model

def get_model():
    """Load VoxCPM2 once per process (same call as clone.py). Runs in the worker thread."""
    global MODEL, LOAD_SECONDS
    with MODEL_LOCK:
        if MODEL is None:
            from voxcpm import VoxCPM

            start = time.time()
            MODEL = VoxCPM.from_pretrained(
                "openbmb/VoxCPM2", load_denoiser=False, optimize=False, device="mps"
            )
            LOAD_SECONDS = time.time() - start
        return MODEL


def attach_denoiser():
    """Add the denoiser to the loaded model on first use, so the model is not loaded twice."""
    with MODEL_LOCK:
        if MODEL.denoiser is None:
            from voxcpm.zipenhancer import ZipEnhancer

            MODEL.denoiser = ZipEnhancer(DENOISER_ID)


def synthesize(text, voice_kwargs, cfg, steps, denoise, folder, base):
    """Generate one clip and write it to a new file. Runs in the worker thread."""
    import soundfile as sf

    start = time.time()
    with MODEL_LOCK:
        wav = MODEL.generate(
            text=text,
            cfg_value=cfg,
            inference_timesteps=int(steps),
            denoise=denoise,
            **voice_kwargs,
        )
    elapsed = time.time() - start
    sr = MODEL.tts_model.sample_rate
    path = unique_path(folder, base)
    with open(path, "xb") as f:  # "x": fail instead of overwriting
        sf.write(f, wav, sr, format="WAV")
    return path, len(wav) / sr, elapsed


# ---------------------------------------------------------------- tab 1: record voice

def save_voice(recording, name, transcript, overwrite):
    if not recording:
        raise gr.Error("Record or upload audio first.")
    name = safe_name(name)
    if not name:
        raise gr.Error("Enter a voice name (letters, digits, dash, underscore).")
    if not FFMPEG:
        raise gr.Error("ffmpeg not found.")
    VOICES_DIR.mkdir(exist_ok=True)
    wav, txt = VOICES_DIR / f"{name}.wav", VOICES_DIR / f"{name}.txt"

    if wav.exists() and not overwrite:
        message = f"A voice named '{name}' already exists. Tick 'Overwrite' or choose another name."
        gr.Warning(message)
        return message, gr.update(), gr.update(), gr.update()

    partial = VOICES_DIR / f".{name}.partial"  # convert first, so a failure keeps the old voice
    cmd = [
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(recording),
        "-vn", "-ac", "1", "-ar", "48000", "-c:a", "pcm_s16le", "-f", "wav", str(partial),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        partial.unlink(missing_ok=True)
        raise gr.Error(f"ffmpeg failed: {result.stderr.strip()[-500:]}")
    os.replace(partial, wav)

    transcript = (transcript or "").strip()
    if transcript:
        txt.write_text(transcript + "\n", encoding="utf-8")
        note = "transcript saved"
    else:
        txt.unlink(missing_ok=True)
        note = "no transcript (only 'Recording only' mode will work)"
    seconds = audio_seconds(wav)
    status = f"Saved {wav} ({seconds:.1f} s, mono 48 kHz 16-bit); {note}."
    return status, playable(wav), f"{seconds:.1f} s", gr.update(choices=voice_names(), value=name)


# ---------------------------------------------------------------- tab 2: speak

def refresh_voices(current):
    names = voice_names()
    return gr.update(choices=names, value=current if current in names else (names[0] if names else None))


def choose_folder(current):
    script = 'POSIX path of (choose folder with prompt "Where should the audio files go?")'
    try:
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        gr.Warning("The folder dialog timed out.")
        return gr.update()
    if result.returncode != 0:
        if "-128" in result.stderr or "cancel" in result.stderr.lower():
            gr.Info("No folder chosen. Keeping the current one.")
            return gr.update()
        raise gr.Error(f"Folder dialog failed: {result.stderr.strip()}")
    return result.stdout.strip().rstrip("/") or "/"


def speak(voice, mode, text, cfg, steps, denoise, out_dir, file_name, per_line, progress=gr.Progress()):
    # Check everything cheap first, so a mistake does not cost a 4 minute model load.
    if per_line:
        texts = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    else:
        texts = [" ".join((text or "").split())]
    if not texts or not texts[0]:
        raise gr.Error("Enter some text.")
    if voice not in voice_names():
        raise gr.Error(f"No voice named '{voice}'. Record one in the first tab, or press Refresh.")
    ref = VOICES_DIR / f"{voice}.wav"
    voice_kwargs = {"reference_wav_path": str(ref)}
    if mode == MODE_TRANSCRIPT:
        txt = ref.with_suffix(".txt")
        prompt = txt.read_text(encoding="utf-8").strip() if txt.is_file() else ""
        if not prompt:
            raise gr.Error(f"Voice '{voice}' has no transcript. Choose '{MODE_REFERENCE}'.")
        if any(t.startswith("(") for t in texts):
            raise gr.Error(f"A (style) prefix only works in '{MODE_REFERENCE}'.")
        voice_kwargs.update(prompt_wav_path=str(ref), prompt_text=prompt)
    folder = resolve_dir(out_dir)
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise gr.Error(f"Cannot use output folder {folder}: {exc}")
    custom = safe_name(re.sub(r"\.wav$", "", (file_name or "").strip(), flags=re.I))

    log = []  # finished lines stay visible; the last line shows the current wait
    total = len(texts)

    def wait(future, label, done_fraction):
        """Yield a status tick every second while the worker thread runs."""
        started = time.time()
        while not future.done():
            message = f"{label} ... {mmss(time.time() - started)} elapsed. Please wait."
            progress(done_fraction, desc=message)
            yield gr.update(), "\n".join(log + [message])
            wait_for_futures([future], timeout=1)
        try:
            return future.result()
        except Exception as exc:
            raise gr.Error(f"{type(exc).__name__}: {exc}")

    if MODEL is None:
        yield gr.update(), "Loading the model. This takes 3-4 minutes the first time."
        yield from wait(WORKER.submit(get_model), "Loading the model (3-4 minutes)", 0)
        log.append(f"Model loaded in {mmss(LOAD_SECONDS)}.")
    if denoise and MODEL.denoiser is None:
        yield from wait(WORKER.submit(attach_denoiser), "Loading the denoiser", 0)
        log.append("Denoiser loaded.")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for i, line in enumerate(texts, 1):
        if custom:
            base = f"{custom}_{i:02d}" if total > 1 else custom
        else:
            base = auto_base(line, stamp, i if total > 1 else None)
        label = f"Generating {i}/{total} (1.5-4.5 minutes each)"
        future = WORKER.submit(synthesize, line, voice_kwargs, cfg, steps, denoise, folder, base)
        path, seconds, elapsed = yield from wait(future, label, (i - 1) / total)
        log.append(f"Saved {path}  ({seconds:.1f} s of audio, generated in {elapsed:.1f} s)")
        yield playable(path), "\n".join(log)
    progress(1, desc="Done")
    log.append(f"Done. {total} file(s) in {folder}")
    yield gr.update(), "\n".join(log)


# ---------------------------------------------------------------- tab 3: files

def list_files(out_dir):
    folder = resolve_dir(out_dir)
    if not folder.is_dir():
        return gr.update(choices=[], value=None), f"Folder does not exist yet: {folder}"
    files = sorted(folder.glob("*.wav"), key=lambda p: p.stat().st_mtime, reverse=True)
    choices = [
        (f"{p.name}   ({datetime.fromtimestamp(p.stat().st_mtime):%Y-%m-%d %H:%M})", str(p))
        for p in files
    ]
    value = choices[0][1] if choices else None
    return gr.update(choices=choices, value=value), f"{len(files)} .wav file(s) in {folder}, newest first."


def show_file(path):
    if not path or not Path(path).is_file():
        return None, ""
    return playable(path), f"{path}  ({audio_seconds(path):.1f} s)"


def reveal_file(path):
    if not path or not path.lower().endswith(".wav") or not Path(path).is_file():
        raise gr.Error("Select an existing .wav file first.")
    subprocess.run(["open", "-R", path], check=False)
    return f"Revealed in Finder: {path}"


# ---------------------------------------------------------------- layout

def build_ui():
    passage = PASSAGE_FILE.read_text(encoding="utf-8").strip() if PASSAGE_FILE.is_file() else ""
    voices = voice_names()

    with gr.Blocks(title="VoxCPM2 voice studio", analytics_enabled=False) as demo:
        gr.Markdown("# VoxCPM2 voice studio\nRuns on this Mac only (127.0.0.1).")

        with gr.Tabs():
            with gr.Tab("Record voice"):
                gr.Markdown("Record yourself reading the passage (about 20 seconds, quiet room), or upload a file.")
                passage_box = gr.Textbox(value=passage, label="Passage to read aloud", lines=5, interactive=False)
                recording = gr.Audio(
                    sources=["microphone", "upload"], type="filepath", label="Recording or upload"
                )
                with gr.Row():
                    voice_name = gr.Textbox(value="me", label="Voice name", scale=1)
                    overwrite = gr.Checkbox(value=False, label="Overwrite if the name exists", scale=1)
                transcript = gr.Textbox(
                    value=passage, label="Transcript (the exact words spoken)", lines=5,
                    info="Edit it if you changed or skipped words. Needed for the closest-match mode.",
                )
                save_btn = gr.Button("Save voice", variant="primary")
                save_status = gr.Textbox(label="Status", interactive=False, lines=2)
                with gr.Row():
                    saved_audio = gr.Audio(label="Saved voice", interactive=False)
                    saved_duration = gr.Textbox(label="Duration", interactive=False)

            with gr.Tab("Speak"):
                gr.Markdown(
                    "The first Generate loads the model (3-4 minutes). Each clip then takes "
                    "1.5-4.5 minutes. The status line below shows the elapsed time. Keep this page open."
                )
                with gr.Row():
                    voice = gr.Dropdown(
                        choices=voices, value=("me" if "me" in voices else (voices[0] if voices else None)),
                        label="Voice", allow_custom_value=True, scale=4,
                    )
                    refresh_btn = gr.Button("Refresh voices", scale=1)
                mode = gr.Radio([MODE_TRANSCRIPT, MODE_REFERENCE], value=MODE_TRANSCRIPT, label="Mode")
                text = gr.Textbox(
                    label="Text to speak", lines=10,
                    info="In 'Recording only' mode, a prefix like (slower, warm) sets the style.",
                )
                per_line = gr.Checkbox(value=False, label="One file per line (each non-empty line becomes its own file)")
                with gr.Row():
                    cfg = gr.Slider(1.0, 3.0, value=2.0, step=0.1, label="cfg")
                    steps = gr.Slider(4, 30, value=10, step=1, label="steps")
                denoise = gr.Checkbox(
                    value=False, label="Denoise the reference recording",
                    info="Loads an extra model the first time it is used.",
                )
                with gr.Row():
                    out_dir = gr.Textbox(value=str(DEFAULT_OUT_DIR), label="Output folder", scale=4)
                    choose_btn = gr.Button("Choose folder...", scale=1)
                file_name = gr.Textbox(
                    label="File name (optional)",
                    info="Blank: timestamp plus the first words. Existing files are never overwritten.",
                )
                generate_btn = gr.Button("Generate", variant="primary", size="lg")
                result_audio = gr.Audio(label="Result", interactive=False)
                speak_status = gr.Textbox(label="Status", interactive=False, lines=4)

            with gr.Tab("Files") as files_tab:
                gr.Markdown("The .wav files in the output folder of the Speak tab.")
                files_refresh_btn = gr.Button("Refresh list")
                files_dd = gr.Dropdown(choices=[], label="Files (newest first)", allow_custom_value=True)
                file_audio = gr.Audio(label="Player", interactive=False)
                reveal_btn = gr.Button("Reveal in Finder")
                files_status = gr.Textbox(label="Status", interactive=False, lines=2)

        # events
        save_btn.click(
            save_voice, [recording, voice_name, transcript, overwrite],
            [save_status, saved_audio, saved_duration, voice], api_name="save_voice",
        )
        refresh_btn.click(refresh_voices, [voice], [voice], api_name="refresh_voices")
        choose_btn.click(choose_folder, [out_dir], [out_dir], api_name="choose_folder")
        generate_btn.click(
            speak, [voice, mode, text, cfg, steps, denoise, out_dir, file_name, per_line],
            [result_audio, speak_status], api_name="speak",
        )
        files_refresh_btn.click(list_files, [out_dir], [files_dd, files_status], api_name="list_files")
        files_tab.select(list_files, [out_dir], [files_dd, files_status], api_visibility="private")
        files_dd.change(show_file, [files_dd], [file_audio, files_status], api_name="show_file")
        reveal_btn.click(reveal_file, [files_dd], [files_status], api_name="reveal_file")

    return demo


if __name__ == "__main__":
    os.chdir(APP_DIR)
    build_ui().queue().launch(
        server_name="127.0.0.1", server_port=7860, inbrowser=True, share=False,
        show_error=True, ssr_mode=False,
    )

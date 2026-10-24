# voice-clone

A local web interface and command-line scripts for cloning a voice and generating speech with VoxCPM2, OpenBMB's open text-to-speech model (Apache-2.0, Hugging Face id `openbmb/VoxCPM2`). Everything runs on this Mac. Nothing is sent to a server.

## Setup

Needs macOS on Apple Silicon, [uv](https://docs.astral.sh/uv/) and ffmpeg (`brew install ffmpeg`).

```
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -r requirements.txt
```

`requirements.txt` pins the versions the scripts were tested with (voxcpm 2.0.3, torch 2.14.1, gradio 6.30.0). The model weights, about 5 GB, download into `~/.cache/huggingface` on first use.

## Web interface

Double-click `start.command` in Finder, or run `.venv/bin/python app.py`. The browser opens at http://127.0.0.1:7860. The server listens on this machine only.

- **Record voice**: record or upload a voice sample, give it a name, check the transcript, save it to `voices/<name>.wav` and `voices/<name>.txt`.
- **Speak**: pick a saved voice and a mode, type text, choose the output folder with the native folder picker, generate, play the result.
- **Files**: list, play and reveal in Finder the audio files of the output folder.

The model loads on the first generation and stays loaded until the app stops.

## Modes

- **Recording + transcript**: the recording is passed with its exact words. Closest match of voice, rhythm and pronunciation. The transcript must match what was said.
- **Recording only**: the recording alone. Allows a style instruction in parentheses at the start of the text, for example `(slower, warm)Hello`.

## Command line

```
bash record.sh voices/me.wav 25
.venv/bin/python clone.py --ref voices/me.wav --ref-text-file voices/me.txt --text "Hello." --out outputs/hello.wav
.venv/bin/python clone.py --ref voices/me.wav            # type one line per clip; an empty line ends
.venv/bin/python demo.py mps                             # speech without cloning, as an install check
```

`voices/me.txt` is an English passage to read aloud for the sample.

## What stays out of git

Voice recordings, generated audio and the Python environment are ignored (`.gitignore`). A recording of a voice is enough to clone it.

## Memory

The model needs about 9 GB of memory at the float32 precision it uses on Apple Silicon. On a 16 GB Mac with other applications open, macOS moves memory to swap and model load plus one short sentence can take several minutes. Close other applications before generating. Use `--device cpu` (command line) if MPS reports an error.

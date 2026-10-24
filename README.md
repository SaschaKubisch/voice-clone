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

- **Record voice**: shows `voice-script.md`. Read it in one take (see below) or upload a recording, give the voice a name, check the transcript, save it to `voices/<name>.wav` and `voices/<name>.txt`.
- **Speak**: pick a saved voice, a mode and a precision, type text, choose the output folder with the native folder picker, generate, play the result.
- **Files**: list, play and reveal in Finder the audio files of the output folder.

The model loads on the first generation and stays loaded until the app stops. Changing Precision frees the loaded model and loads it again in the new precision on the next Generate.

## Recording the voice

`voice-script.md` is the text to read. It has ten sections, one speaking style each, about 3 minutes in total (474 spoken words). The italic line under each heading is a reading direction. It is not read aloud.

- Record in a quiet room, in one take, all ten sections.
- Pause briefly between sections.
- Stop recording about one second after the last word.

The transcript box is filled with the spoken text only. The title, the intro, the headings and the reading directions are left out. The transcript must match the words you say. Edit it if you changed, skipped or added words.

On save, the recording becomes a mono 48 kHz 16-bit WAV. Silence at the start and the end is cut to at most about 0.5 s (threshold -50 dB). Pauses inside the speech are not touched. If the room noise is louder than -50 dB, the ends are not cut. The saved duration is shown.

A long reference makes every generation slower, because the model processes the reference again on each call.

## Modes

- **Recording + transcript**: the recording is passed with its exact words. Closest match of voice, rhythm and pronunciation. The transcript must match what was said.
- **Recording only**: the recording alone. Allows a style instruction in parentheses at the start of the text, for example `(slower, warm)Hello`.

## Command line

```
bash record.sh                                           # reads voice-script.md: 200 s, writes voices/me.wav, cuts silence at both ends
bash record.sh voices/other.wav 150                      # another file name and length in seconds
.venv/bin/python -c "from app import script_transcript as s; print(s('voice-script.md'))" > voices/me.txt
.venv/bin/python clone.py --ref voices/me.wav --ref-text-file voices/me.txt --text "Hello." --out outputs/hello.wav
.venv/bin/python clone.py --ref voices/me.wav            # type one line per clip; an empty line ends
.venv/bin/python clone.py --ref voices/me.wav --dtype float32 --text "Hello."   # float32 instead of the default float16
.venv/bin/python demo.py mps                             # speech without cloning, as an install check
```

`record.sh` records the microphone for the given number of seconds, or until Ctrl-C, and keeps what was recorded. It writes only the WAV.

`voices/me.txt` is the transcript file the app writes next to `voices/me.wav` when you save the voice `me`. After `record.sh`, the `python -c` line writes it from `voice-script.md`. Without a transcript file, use `clone.py --ref` alone (reference only).

## What stays out of git

Voice recordings, generated audio and the Python environment are ignored (`.gitignore`). A recording of a voice is enough to clone it.

## Memory

Precision. The model runs in float16 by default (`--dtype` in `clone.py`, Precision in the Speak tab). voxcpm 2.0.3 would use float32 on Apple Silicon, because bfloat16 gave noise there; `voxcpm_patch.py` selects float16 through the variable voxcpm reads for this (`VOXCPM_MPS_DTYPE`). The language models, the diffusion decoder, the projections and the caches run in float16. The audio VAE stays float32. Measured on this 16 GB Mac with other applications open, with a 20 s reference and a sentence of about 4 s:

- float16: loads in 27-34 s (four runs). GPU memory (`torch.mps.driver_allocated_memory`) is 5.6 GiB after loading and 6.8 GiB after generating. The first clip takes 14-16 s (four runs); a later clip with the same voice took 9 s (one run).
- float32: loads in 155 s (one run). GPU memory is 9.6 GiB after loading and 11.8 GiB after generating. The clip took 202 s, and macOS swap use rose from 6.9 to 9.9 GB during the run.

An ASR model (SenseVoiceSmall) transcribed both outputs as "the quick brown fox jumps over the lazy dog near the riverbank". Loading float16 still peaks at about 10 GiB (process footprint), because voxcpm builds the model in float32 on the CPU first and then casts it (read from its source; the peak itself was measured). `tests/test_fp16_audio.py` checks that the float16 output is speech. The precision setting only applies on MPS. Close other applications before generating. Use `--device cpu` (command line) if MPS reports an error.

A long reference, such as the 3-minute take, is encoded in 20-second pieces to fit in memory, and once per app run instead of on every generation (`voxcpm_patch.py`, applied by `app.py` and `clone.py`). `tests/test_chunked_encode.py` checks that the pieces give the same result as encoding the whole recording.

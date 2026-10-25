# voice-clone

A local web interface and command-line scripts for cloning a voice and generating speech with VoxCPM2, OpenBMB's open text-to-speech model (Apache-2.0, Hugging Face id `openbmb/VoxCPM2`). Everything runs on this Mac. Nothing is sent to a server.

## Setup

```
git clone https://github.com/SaschaKubisch/voice-clone.git
cd voice-clone
bash install.sh
```

This installs everything. Afterwards Generate needs no further download. Run it again at any time: it skips what is already done. Needs macOS on Apple Silicon.

Options:

- `--browser`: install the browser app instead of the desktop app (see Mac app).
- `--no-app`: skip the Mac app. Start the interface with `start.command` or `.venv/bin/python app.py`.
- `--no-models`: skip the model downloads. The first Generate downloads VoxCPM2 instead.
- `-h`, `--help`: print the usage.

What it installs, in this order:

1. **uv and ffmpeg**, with Homebrew, if one is missing. Without Homebrew it stops and prints the link (https://brew.sh) and the commands to run. It does not install Homebrew.
2. **Python environment**: `.venv` in this folder, Python 3.11, with the packages of `requirements.txt`.
3. **VoxCPM2**: Hugging Face id `openbmb/VoxCPM2`, about 5 GB (4.6 GiB), into `~/.cache/huggingface/hub`.
4. **Denoiser** for the option "Denoise the reference recording": ModelScope id `iic/speech_zipenhancer_ans_multiloss_16k_base`, about 18 MB, into `~/.cache/modelscope/models`.
5. **Mac app**: `/Applications/Voice Clone.app`, built by `make-app.sh --install`. The desktop app needs the Command Line Tools (`xcode-select --install`).

The models are downloaded, not loaded into memory. The cache folders above change if you set `HF_HOME` or `MODELSCOPE_CACHE`. SenseVoiceSmall is not installed. Only `tests/test_fp16_audio.py` uses it, and that test downloads it (about 0.9 GB) on its first run.

### Manual setup

Needs [uv](https://docs.astral.sh/uv/) and ffmpeg (`brew install ffmpeg`).

```
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -r requirements.txt
```

`requirements.txt` pins the versions the scripts were tested with (voxcpm 2.0.3, torch 2.14.1, gradio 6.30.0). If the models are not downloaded, the first Generate downloads the VoxCPM2 weights into `~/.cache/huggingface`.

## Web interface

Double-click `start.command` in Finder, or run `.venv/bin/python app.py`. The browser opens at http://127.0.0.1:7860. The server listens on this machine only.

- **Record voice**: shows `voice-script.md`. Read it in one take (see below) or upload a recording, give the voice a name, check the transcript, save it to `voices/<name>.wav` and `voices/<name>.txt`.
- **Speak**: pick a saved voice, a mode and a precision, type text, choose the output folder with the native folder picker, generate, play the result.
- **Files**: list, play and reveal in Finder the audio files of the output folder.

The model loads on the first generation and stays loaded until the app stops. Changing Precision frees the loaded model and loads it again in the new precision on the next Generate.

### Progress bar of the Speak tab

One bar under the Generate button (Gradio's own progress overlay is off for this event). It shows the percentage of the whole run, the elapsed time, what is happening now in plain words, and a second line with details. The page changes at most twice a second, and only when something changed.

- The model makes speech in steps. One step makes 0.16 s of audio (6.25 steps per second). `voxcpm_patch.py` counts the steps of the generation loop of voxcpm without changing it, so the audio is the same. The bar of a clip moves with the steps.
- Reading the voice, preparing the text, turning the speech into sound, saving and loading the model have no step count. The bar stands still or shows a striped bar there, and the second line says so.
- The number of steps of a clip is estimated from the text before it starts: characters times 6.25 divided by the characters per second of the chosen voice (its transcript length over its recording length, limited to 8-20; 15 without a transcript). After each clip the measured steps per character replace the estimate for that voice. When a clip runs past its estimate, the estimate grows and the bar keeps rising, but it shows 100% only after the last clip is saved.
- Several clips ("one file per line"): the bar is (finished clips + part of the current clip) divided by the number of clips.

`tests/test_progress_ui.py` checks the bar with a stand-in model on port 7861 and never loads the real model. `tests/test_progress_hook.py` checks the step counting without loading the model.

## Mac app

`make-app.sh` builds "Voice Clone.app" in two modes. Both install as `/Applications/Voice Clone.app` (bundle id `local.voiceclone`), so installing one replaces the other.

- **Desktop app** (the default): a window of its own, with its own Dock icon and menu. It is a small Swift app (`macapp/desktop/`) that shows the web interface in a WebKit view. It needs the Command Line Tools for `swiftc` (`xcode-select --install`) and macOS 12 or newer. There is no Xcode project and no new dependency.
- **Browser app**: a launcher that opens the web interface in the default browser. It is an AppleScript applet built with `osacompile`.

```
bash make-app.sh --install             # desktop app: builds macapp/build/Voice Clone.app, copies it to /Applications, registers it with Launch Services
bash make-app.sh --install --browser   # browser app, the same steps
bash make-app.sh                       # builds the desktop app only (add --browser for the browser app)
```

`--install` refuses while either variant is running. Every run of `make-app.sh` unregisters the build in `macapp/build` from Launch Services (`lsregister -u`), so Launchpad and Spotlight list one Voice Clone, the installed one. If something opens the build copy later, Launch Services can register it again; the next `make-app.sh` run removes it. Both variants are signed ad hoc (not notarized), which is enough to run on the Mac that built them, with no hardened runtime and no sandbox.

Both modes work the same way underneath (`macapp/voiceclone.sh`):

- **Start**: if http://127.0.0.1:7860 already answers, the app uses that server. Otherwise it starts `.venv/bin/python app.py` in the background and waits up to 90 s.
- **Quit** stops the server this app started: SIGTERM, then SIGKILL after 10 s. A server the app did not start, such as one from `start.command`, keeps running.
- **Force quit or crash**: the server keeps running. The next start finds the running server and uses it, but the app does not own it, so Quit does not stop it. Stop it with `pkill -f '.venv/bin/python app.py'`.
- **Log**: `~/Library/Logs/VoiceClone/app.log`. The app names this file when the server does not start.
- **Folder**: the app points to this repo folder, fixed when `make-app.sh` runs. If the folder moves, run `bash make-app.sh --install` again there. If `.venv` is missing, the app shows the commands from Setup.

### Desktop app

- **Window**: one window titled "Voice Clone", 1200 x 900 or smaller to fit the screen. It shows "Starting Voice Clone..." until the server answers, then the web interface. If the server does not answer within 90 s, an alert names the log file and the app stops the server it started. Cmd-R then tries again. The app keeps no state between runs: no window position, no cookies.
- **Quit**: Cmd-Q, or closing the window (Cmd-W), quits the app and stops the server it started.
- **Menu**: Voice Clone (About, Hide, Quit), File (Close), Edit (Undo, Redo, Cut, Copy, Paste, Select All), View (Reload with Cmd-R, Full Screen), Window.
- **Microphone**: the first time you record in the Record voice tab, macOS asks to allow the microphone for Voice Clone. Allow it. The window grants the microphone only to the page of this server. If you chose Don't Allow, switch it on in System Settings, Privacy & Security, Microphone. macOS may ask again after you install a new build, because every build has a new ad hoc signature.
- **Downloads**: the download button of the audio player saves the file into `~/Downloads` (a name that exists gets " 2", " 3") and shows it in Finder. macOS can ask to allow access to the Downloads folder.
- **Uploads**: the upload buttons open the standard file dialog.
- **Links**: a link that wants a new window, or points outside 127.0.0.1, opens in the default browser.

### Browser app

- **Open** it from Launchpad. If the server already answers, the app only opens the page in the browser. Otherwise it starts the server, waits, then opens the browser once. Clicking the app again in the Dock or Launchpad opens the page again.
- **Quit** (Cmd-Q or Dock, Quit) stops the server this app started.

### Tests

Both tests use a stand-in server and never use port 7860.

- `VOICECLONE_PORT=7862 bash macapp/test-launcher.sh` tests `macapp/voiceclone.sh`.
- `VOICECLONE_PORT=7862 bash macapp/test-desktop.sh` runs `VoiceClone --self-test` from the desktop build in `macapp/build` (build it first). It tests the server rules of the desktop app with no window: reuse of a running server, start of its own, stop of only its own, timeout, a server that exits, a missing folder or `.venv`, and that WebKit treats http://127.0.0.1 as a secure context, which the microphone needs. The window itself (menu, microphone prompt, downloads, uploads) is not covered by a test.

## Recording the voice

`voice-script.md` is the text to read. It has ten sections, one mood each, in easy words, about 1.5 minutes in total (234 spoken words). The italic line under each heading is a reading direction. It is not read aloud.

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
bash record.sh                                           # reads voice-script.md: 100 s, writes voices/me.wav, cuts silence at both ends
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

A reference longer than 20 seconds, such as the full script, is encoded in 20-second pieces to fit in memory, and once per app run instead of on every generation (`voxcpm_patch.py`, applied by `app.py` and `clone.py`). `tests/test_chunked_encode.py` checks that the pieces give the same result as encoding the whole recording.

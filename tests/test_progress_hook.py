#!/usr/bin/env python3
"""Checks voxcpm_patch.patch_progress without loading VoxCPM2.

Run:  .venv/bin/python tests/test_progress_hook.py      # exit 0 when every check passes

1. A stand-in model class lives in a module of its own and uses `tqdm` as a module global, the way
   voxcpm.model.voxcpm2 does. patch_progress must leave what the model computes unchanged, count the
   steps, mark the phases, and be safe to call twice.
2. The installed voxcpm package is read as text (not imported, no weights): the generation loop must
   still be `for i in tqdm(range(max_len))` with tqdm imported into the module, and the cached model
   config must give 0.16 s per step.
"""
import ast
import glob
import importlib.util
import json
import math
import sys
import types
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from tqdm import tqdm

import voxcpm_patch

failures = []


def check(condition, message):
    print(("ok    " if condition else "FAIL  ") + message)
    if not condition:
        failures.append(message)


# ---------------------------------------------------------------- a stand-in model

SOURCE = '''
class StandInModel:
    patch_size = 4
    _decode_chunk_size = 1920
    sample_rate = 48000

    def __init__(self):
        self.audio_vae = StandInVAE()
        self.probe = None

    def build_prompt_cache(self, **kwargs):
        return {"mode": "reference"}

    def _inference(self, max_len, stop_at):
        out = []
        for i in tqdm(range(max_len)):
            if self.probe:
                self.probe()
            out.append(i * i + 1)
            if i + 1 >= stop_at:
                break
        return out

    def generate(self, max_len, stop_at, retries=0):
        self.build_prompt_cache(reference_wav_path="x")
        for _ in range(retries + 1):
            out = self._inference(max_len, stop_at)
        return self.audio_vae.decode(out)
'''


def make_model():
    module = types.ModuleType("stand_in_voxcpm2")
    sys.modules[module.__name__] = module
    calls = []

    def counting_tqdm(iterable=None, *args, **kwargs):
        calls.append(iterable)
        return tqdm(iterable, *args, disable=True, **kwargs)  # the real tqdm, silent

    module.tqdm = counting_tqdm
    module.StandInVAE = type("StandInVAE", (), {"decode": lambda self, out: [v * 2 for v in out]})
    exec(SOURCE, module.__dict__)
    tts = module.StandInModel()
    return types.SimpleNamespace(tts_model=tts), tts, calls


class Recording(voxcpm_patch.GenerationProgress):
    def __init__(self):
        super().__init__()
        self.events = []

    def set_phase(self, phase):
        self.events.append(phase)
        super().set_phase(phase)

    def iterate(self, iterable):
        self.events.append("iterate")
        return super().iterate(iterable)


def check_wiring():
    model, tts, calls = make_model()
    before = {args: tts.generate(*args) for args in [(50, 7), (12, 100)]}

    tracker = Recording()
    voxcpm_patch.patch_progress(model, tracker)
    seen = []
    tts.probe = lambda: seen.append((tracker.snapshot()["phase"], tracker.snapshot()["steps"]))
    after = {args: tts.generate(*args) for args in [(50, 7), (12, 100)]}
    check(before == after, "the stand-in model computes the same values with and without the hook")
    check(len(calls) == 4, "the module's own tqdm is still called, once per loop")
    check(tracker.events[:5] == ["voice", "prepare", "iterate", "wrapup", "decode"], f"phases in order: {tracker.events[:5]}")

    seen.clear()
    tracker.events.clear()
    tracker.begin("voice")  # what the app does before every clip: attempts start again at 0
    tts.generate(50, 7)
    check(seen == [("speech", i) for i in range(7)], f"inside the loop the phase is speech and steps counts finished steps: {[s for _, s in seen]}")
    snap = tracker.snapshot()
    check(snap["steps"] == 7, f"a break counts the step it stops on (steps {snap['steps']})")
    check(snap["max_steps"] == 50 and snap["attempt"] == 1 and snap["phase"] == "decode", f"cap {snap['max_steps']}, attempt {snap['attempt']}, phase {snap['phase']} after generate")

    seen.clear()
    tracker.begin("voice")
    tts.generate(12, 100)
    check(tracker.snapshot()["steps"] == 12, f"a loop that runs to the end of its range counts every step ({tracker.snapshot()['steps']})")

    tracker.begin("voice")
    tts.generate(30, 5, retries=2)
    snap = tracker.snapshot()
    check(snap["attempt"] == 3 and snap["steps"] == 5, f"a repeated loop counts attempts (attempt {snap['attempt']}, steps of the last {snap['steps']})")

    events = len(tracker.events)
    voxcpm_patch.patch_progress(model, tracker)
    tracker.events.clear()
    tts.generate(20, 3)
    check(tracker.events == ["voice", "prepare", "iterate", "wrapup", "decode"] and events > 0, "patching twice does not wrap twice")
    check(abs(tracker.patch_seconds - 0.16) < 1e-12, f"seconds per step from the model's own numbers: {tracker.patch_seconds}")


# ---------------------------------------------------------------- the installed package

def check_installed_source():
    spec = importlib.util.find_spec("voxcpm")
    root = Path(next(iter(spec.submodule_search_locations)))
    tree = ast.parse((root / "model" / "voxcpm2.py").read_text(encoding="utf-8"))

    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "tqdm" and any(a.name == "tqdm" and a.asname is None for a in n.names)]
    check(len(imports) == 1, "voxcpm/model/voxcpm2.py imports tqdm into the module (`from tqdm import tqdm`)")

    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "VoxCPM2Model")
    inference = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "_inference")
    loops = [n for n in ast.walk(inference) if isinstance(n, ast.For)]
    steps = [
        n for n in loops
        if isinstance(n.iter, ast.Call) and isinstance(n.iter.func, ast.Name) and n.iter.func.id == "tqdm"
        and len(n.iter.args) == 1 and isinstance(n.iter.args[0], ast.Call) and n.iter.args[0].func.id == "range"
        and [a.id for a in n.iter.args[0].args] == ["max_len"]
    ]
    check(len(steps) == 1 and len(loops) == 1, "_inference has one loop, `for i in tqdm(range(max_len))`")
    local_names = {n.id for n in ast.walk(inference) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)} | {a.arg for a in inference.args.args}
    check("tqdm" not in local_names, "tqdm is a module global inside _inference, so replacing the module attribute takes effect")
    appends = [n for n in ast.walk(steps[0]) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "append"]
    check(len(appends) == 1, "each pass of the loop appends exactly one patch of latent frames (pred_feat_seq)")

    configs = glob.glob(str(Path.home() / ".cache/huggingface/hub/models--openbmb--VoxCPM2/snapshots/*/config.json"))
    if not configs:
        print("skip  config.json of openbmb/VoxCPM2 is not in the Hugging Face cache; the 0.16 s per step is not checked")
        return
    config = json.loads(Path(configs[0]).read_text(encoding="utf-8"))
    vae = config["audio_vae_config"]
    seconds = config["patch_size"] * math.prod(vae["decoder_rates"]) / vae["out_sample_rate"]
    check(abs(seconds - voxcpm_patch.PATCH_SECONDS) < 1e-9, f"config: {config['patch_size']} frames x {math.prod(vae['decoder_rates'])} samples / {vae['out_sample_rate']} Hz = {seconds} s per step ({1 / seconds} steps per second)")


def main():
    check_wiring()
    check_installed_source()
    print("PASS" if not failures else f"FAILED: {len(failures)} check(s)")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())

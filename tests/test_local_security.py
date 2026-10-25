#!/usr/bin/env python3
"""Checks the local-only protections of app.py with a stand-in model. The real model is never loaded.

Run:  .venv/bin/python tests/test_local_security.py      # exit 0 when every check passes

What it does:
  1. Imports app without launching it, points it at a temporary voices folder and output folder, and
     makes loading the model an error. The checks below never reach the model.
  2. Starts the UI with app.launch_settings on 127.0.0.1:7864 (never 7860).
  3. Host header: a request whose Host is not 127.0.0.1:<port> or localhost:<port> is refused with
     400 or 403, before any route runs. This is what stops a DNS-rebinding page, a page on a name
     that points at 127.0.0.1, from using the API. A normal request works.
  4. show_file (the handler of the Files tab, also an API endpoint): it plays a .wav inside the
     output folder or the voices folder, and refuses everything else (a file elsewhere, a non-wav, a
     symlink that points out, a path with .., a sibling folder with a similar name) before any copy
     is made.
  5. reveal_file: the same rule, and `open -R --` so that a path cannot be read as an option.
  6. The other handlers that take a path or a name from the page: save_voice cannot be pointed at a file
     on disk (Gradio accepts only files uploaded through the page), and speak refuses a voice name that
     is not a saved voice, such as one with ..
"""
import http.client
import json
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

import numpy as np
import soundfile as sf

import app
import voxcpm_patch

PORT = 7864
SAMPLE_RATE = 48000

failures = []


def check(condition, message):
    print(("ok    " if condition else "FAIL  ") + message)
    if not condition:
        failures.append(message)


def write_wav(path, hz=220):
    path.parent.mkdir(parents=True, exist_ok=True)
    t = np.arange(SAMPLE_RATE // 2) / SAMPLE_RATE
    sf.write(path, (0.2 * np.sin(2 * np.pi * hz * t)).astype(np.float32), SAMPLE_RATE)
    return path


def make_workdir():
    """A temporary tree: voices/, outputs/ (the output folder), and files the app must not serve."""
    root = Path(tempfile.mkdtemp(prefix="local-security-test-")).resolve()
    voices, outputs, elsewhere = root / "voices", root / "outputs", root / "elsewhere"
    write_wav(voices / "stub.wav", 180)
    write_wav(outputs / "good.wav", 220)
    write_wav(elsewhere / "private.wav", 330)
    write_wav(elsewhere / "other.wav", 550)
    write_wav(root / "outputs_old" / "old.wav", 440)  # a sibling whose name starts like the output folder
    (elsewhere / "secret.txt").write_text("not audio\n", encoding="utf-8")
    (outputs / "notes.txt").write_text("not audio\n", encoding="utf-8")
    (outputs / "link-to-private.wav").symlink_to(elsewhere / "private.wav")
    (outputs / "linkdir").symlink_to(elsewhere, target_is_directory=True)
    return root, voices, outputs, elsewhere


def port_free(port):
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def refuse_model(*args, **kwargs):
    raise AssertionError("this test must not load the model")


def start_ui(voices, outputs):
    assert PORT != 7860, "7860 belongs to the real app"
    assert port_free(PORT), f"port {PORT} is already in use"
    app.VOICES_DIR = voices
    app.DEFAULT_OUT_DIR = outputs
    voxcpm_patch.load_model = refuse_model
    demo = app.build_ui().queue()
    demo.launch(**app.launch_settings(PORT), prevent_thread_lock=True, inbrowser=False, quiet=True)
    return demo


# ---------------------------------------------------------------- Host header

def http_request(method, target, host, headers=None, body=None):
    """One request with exactly the Host header given. Returns (status, body bytes)."""
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
    try:
        conn.putrequest(method, target, skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", host)
        for name, value in (headers or {}).items():
            conn.putheader(name, value)
        if body is not None:
            conn.putheader("Content-Length", str(len(body)))
        conn.endheaders(body)
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def check_host_header(outputs, elsewhere):
    for host in (f"127.0.0.1:{PORT}", f"localhost:{PORT}"):
        status, body = http_request("GET", "/", host)
        check(status == 200 and b"gradio" in body.lower(), f"Host {host}: the page is served (status {status})")
        status, body = http_request("GET", "/gradio_api/info", host)
        check(status == 200 and b"/show_file" in body, f"Host {host}: the API description is served (status {status})")

    bad_hosts = [
        f"evil.example:{PORT}",
        "evil.example",
        f"127.0.0.1.evil.example:{PORT}",
        f"localhost.evil.example:{PORT}",
        f"evil.example:{PORT}, 127.0.0.1:{PORT}",
        "127.0.0.1:9999",
        "127.0.0.1",
        f"[::1]:{PORT}",
    ]
    for host in bad_hosts:
        status, body = http_request("GET", "/gradio_api/info", host)
        check(status in (400, 403) and b"show_file" not in body, f"Host {host}: GET /gradio_api/info is refused (status {status})")
    status, body = http_request("GET", "/", f"evil.example:{PORT}")
    check(status in (400, 403) and b"gradio" not in body.lower(), f"Host evil.example:{PORT}: GET / is refused (status {status})")
    status, body = http_request("GET", "/config", f"evil.example:{PORT}")
    check(status in (400, 403) and b"components" not in body, f"Host evil.example:{PORT}: GET /config is refused (status {status})")

    # A call to a handler, the way a rebinding page would make it: its own Origin, its own Host.
    payload = json.dumps({"data": [str(elsewhere / "private.wav"), str(outputs)]}).encode()
    status, body = http_request(
        "POST", "/gradio_api/call/show_file", f"evil.example:{PORT}",
        {"Content-Type": "application/json", "Origin": f"http://evil.example:{PORT}"}, payload,
    )
    check(status in (400, 403) and b"event_id" not in body, f"Host evil.example:{PORT}: POST /gradio_api/call/show_file is refused (status {status})")


# ---------------------------------------------------------------- show_file, reveal_file

def player_copies():
    return sorted(p.name for p in Path(app.PLAYER_DIR).iterdir()) if app.PLAYER_DIR else []


def call_show_file(client, path, out_dir):
    """(audio, status) as the Files tab gets them. An error counts as an answer: ("error", message)."""
    try:
        return client.predict(str(path), str(out_dir), api_name="/show_file")
    except Exception as exc:
        return "error", f"{type(exc).__name__}: {exc}"


def check_show_file(client, voices, outputs, elsewhere, root):
    good, voice = outputs / "good.wav", voices / "stub.wav"

    # Allowed: the output folder, the voices folder, and the other folder when it is the output folder.
    for label, path, out_dir in [
        ("a .wav in the output folder", good, outputs),
        ("a .wav in the voices folder", voice, outputs),
        ("a .wav in the folder that is the output folder now", elsewhere / "other.wav", elsewhere),
        ("a .wav in the default output folder, with the output folder box empty", good, ""),
    ]:
        audio, status = call_show_file(client, path, out_dir)
        played = audio is not None and Path(audio).is_file() and Path(audio).read_bytes() == Path(path).read_bytes()
        check(played and "0.5 s" in status, f"plays {label} ({status!r})")

    # Refused: nothing is copied, no audio comes back, the status line says why.
    refused = [
        ("a .wav outside both folders", elsewhere / "private.wav", outputs),
        ("a non-wav file outside both folders", elsewhere / "secret.txt", outputs),
        ("a non-wav file inside the output folder", outputs / "notes.txt", outputs),
        ("a symlink in the output folder that points to a .wav outside", outputs / "link-to-private.wav", outputs),
        ("a .wav reached through a symlinked folder", outputs / "linkdir" / "private.wav", outputs),
        ("a path with .. that leaves the output folder", outputs / ".." / "elsewhere" / "private.wav", outputs),
        ("a .wav in a sibling folder with a similar name", root / "outputs_old" / "old.wav", outputs),
        ("a .wav outside, when the output folder box names the voices folder", elsewhere / "private.wav", voices),
        ("a system file", Path("/etc/hosts"), outputs),
        ("a folder", elsewhere, outputs),
        ("a path that does not exist", outputs / "missing.wav", outputs),
    ]
    for label, path, out_dir in refused:
        before = player_copies()
        audio, status = call_show_file(client, path, out_dir)
        check(audio is None and player_copies() == before and bool(status),
              f"refuses {label}: no audio, no copy ({status[:70]!r})")

    check(call_show_file(client, "", outputs) == (None, ""), "an empty selection gives an empty result")
    leaked = [n for n in player_copies() if n in ("private.wav", "secret.txt", "notes.txt", "old.wav", "hosts")]
    check(not leaked, f"the player folder holds no copy of a refused file ({leaked})")

    # The same rule without the HTTP layer.
    check(app.show_file(str(elsewhere / "private.wav"), str(outputs))[0] is None, "show_file: the direct call refuses a .wav outside")
    check(app.show_file(str(good), str(outputs))[0] is not None, "show_file: the direct call plays a .wav in the output folder")


def check_reveal_file(client, outputs, elsewhere):
    calls = []
    real_run = subprocess.run
    app.subprocess.run = lambda args, **kwargs: calls.append(list(args)) or subprocess.CompletedProcess(args, 0)
    try:
        good = outputs / "good.wav"
        message = app.reveal_file(str(good), str(outputs))
        check(calls == [["open", "-R", "--", str(good.resolve())]], f"reveal_file runs `open -R --` with the checked path ({calls})")
        check(str(good) in message or good.name in message, "reveal_file says which file it revealed")

        for label, path in [
            ("a .wav outside the folders", elsewhere / "private.wav"),
            ("a symlink that points out", outputs / "link-to-private.wav"),
            ("a non-wav file", outputs / "notes.txt"),
            ("an option-like name", Path("-a Calculator.wav")),
        ]:
            calls.clear()
            try:
                app.reveal_file(str(path), str(outputs))
                raised = False
            except Exception as exc:
                raised = type(exc).__name__ == "Error"
            check(raised and not calls, f"reveal_file refuses {label} and runs nothing")

        calls.clear()
        try:
            client.predict(str(elsewhere / "private.wav"), str(outputs), api_name="/reveal_file")
            failed = False
        except Exception:
            failed = True
        check(failed and not calls, "reveal_file over the API refuses a .wav outside and runs nothing")
    finally:
        app.subprocess.run = real_run


def check_other_handlers(client, voices, outputs, elsewhere):
    """save_voice and speak take a recording, a voice name and a folder from the page."""
    payload = json.dumps({"data": [{"path": str(elsewhere / "private.wav"), "meta": {"_type": "gradio.FileData"}}, "grabbed", "", True]}).encode()
    host = f"127.0.0.1:{PORT}"
    status, body = http_request("POST", "/gradio_api/call/save_voice", host, {"Content-Type": "application/json"}, payload)
    event_id = json.loads(body).get("event_id") if status == 200 else None
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=15)
    try:
        conn.request("GET", f"/gradio_api/call/save_voice/{event_id}", headers={"Host": host})
        result = conn.getresponse().read().decode()
    finally:
        conn.close()
    check("event: error" in result and "not uploaded by a user" in result, "save_voice refuses a recording that is a path on disk, not an upload")
    check(sorted(p.name for p in voices.iterdir()) == ["stub.wav"], "save_voice made no voice from a path on disk")

    for name in ("../elsewhere/private", "/etc/hosts", "stub/../../elsewhere/private"):
        job = client.submit(name, app.MODE_REFERENCE, "float16", "Hello.", 2.0, 10, False, str(outputs), "", False, api_name="/speak")
        try:
            job.result()
            refused = False
        except Exception as exc:
            refused = "No voice named" in str(exc)
        check(refused, f"speak refuses the voice name {name!r} before the model is touched")
    check(sorted(p.name for p in outputs.glob("*.wav")) == ["good.wav", "link-to-private.wav"], "speak wrote no file for a refused voice name")


def main():
    root, voices, outputs, elsewhere = make_workdir()
    demo = start_ui(voices, outputs)
    try:
        from gradio_client import Client

        check_host_header(outputs, elsewhere)
        client = Client(f"http://127.0.0.1:{PORT}", verbose=False)
        check("file(s) in" in client.predict(str(outputs), api_name="/list_files")[1],
              "a normal client (Host 127.0.0.1) still reaches the API")
        check_show_file(client, voices, outputs, elsewhere, root)
        check_reveal_file(client, outputs, elsewhere)
        check_other_handlers(client, voices, outputs, elsewhere)
    finally:
        demo.close()
        shutil.rmtree(root, ignore_errors=True)
    check(port_free(PORT), f"port {PORT} is free again")
    print("PASS" if not failures else f"FAILED: {len(failures)} check(s)")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())

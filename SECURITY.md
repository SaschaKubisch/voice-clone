# Security

Voice Clone runs on one Mac and is meant to be reached from that Mac only.

## How the app is kept local

- The server listens on 127.0.0.1, port 7860. It does not listen on the network.
- Gradio's public share link is off (`share=False`), and Gradio's analytics are off.
- The server answers only requests whose Host header is `127.0.0.1:7860` or `localhost:7860`. Any other Host gets status 403. This stops a web page on another name that points at 127.0.0.1 (DNS rebinding) from using the interface.
- The Files tab plays and reveals only `.wav` files inside the output folder or `voices/`.
- Recordings, transcripts and generated audio are files on the Mac. The network is used to download software and models.
- There is no password. A program that runs on the Mac and can send requests to 127.0.0.1:7860 can use the interface. The Host check stops web pages, not local programs.

The [Privacy](README.md#privacy) section of the README lists what stays on the Mac and what uses the network. `tests/test_local_security.py` checks the Host rule and the path rule.

## Supported version

Only the current state of the `main` branch. There are no releases.

## Reporting a problem

Use GitHub's private vulnerability reporting: open the Security tab of https://github.com/SaschaKubisch/voice-clone and choose "Report a vulnerability".

If that is not available, open a GitHub issue that says only that you have a security problem. Do not put details in the issue. The maintainer will then arrange a private way to receive them.

Please include the commit you tested and the steps to reproduce the problem.

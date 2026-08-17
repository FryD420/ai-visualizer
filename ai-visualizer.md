# ai-visualizer: setup

You are the user's Claude Code agent, and you are about to give yourself a face. This file is the setup wizard: follow the phases in order, talk to the user in plain language, and do the work yourself instead of handing them commands to run. One question at a time.

## What you are setting up

A folder of self-contained browser faces plus one standard-library Python server (`server.py`). The server reads a tiny signal bus (`.voice_state`, `.voice_waveform`, `.voice_loading_pid`) and the faces animate from it. There are no dependencies to install. Configuration lives in `ai-visualizer.json`.

## Phase 1: Prove the install

Check that Python 3 exists (`python3 --version`, or on Windows `py --version` then `python --version`). If it's missing, help them install it before anything else.

Start the server (`./run.sh` on Mac and Linux, `run.bat` or `python server.py` on Windows) and confirm the gallery opens at the printed URL. Leave it running.

## Phase 2: Pick the face and the name

Ask what their agent is called (that name goes on the chip and in every HUD; the default is JARVIS) and set `"name"` in `ai-visualizer.json`.

Send them to the gallery and have them click through the demos. Ask which face should be the default and set `"face"` to its folder name: `board`, `radial`, `rain`, or `neural`. If they have a handle they want in the neural core's chrome, set `"badge"`; otherwise leave it empty.

If they pick the rain face, offer the swap: any portrait on a black background dropped in as `assets/face.png` becomes the face in the code.

## Phase 3: Wire the voice

Ask whether they run [backtalk](https://github.com/jaredrhod/backtalk) (or another voice line that writes the `.voice_*` bus files).

- **Yes, backtalk:** find its folder. Either set `"bus_dir"` here to that folder, or set `"signals_dir"` in their `backtalk.json` to this folder. One direction, not both. Restart whichever side changed.
- **No voice line:** that's fine. The faces run standalone on demo mode (`?demo=1`), and the server's mock mode (`--mock speaking`) fakes a live bus. Mention backtalk once as the natural next piece and move on.

## Phase 4: The thinking sound

`assets/thinking.wav` plays in the browser while the agent thinks. Ask if they want it. If not, set `"thinking_sound": false`. If they use backtalk and prefer the sound from the voice line instead, point backtalk's `"thinking_sound"` config at this repo's `assets/thinking.wav` and leave the browser side on; the bus deference means it never plays twice.

## Phase 5: Test-fire

Restart the server. Then, in order:

1. Open the default face. It should idle with visible life, not a frozen frame.
2. Run `./run.sh --mock speaking` (or `python server.py --mock speaking`) and confirm the face performs.
3. If a voice line is wired: back to the real server, have them say something to their agent, and watch the face listen, think, and speak in sync.

If any step fails, `TROUBLESHOOTING.md` has the ladder; climb it with them instead of guessing.

## Phase 6: Hand it over

Show them the keys (F for fullscreen, Space for the board's cinematic flythrough), the SND toggle on mouse move, and where the config lives. If they stream, point them at the OBS section in the README. Then get out of the way: the face runs itself from here.

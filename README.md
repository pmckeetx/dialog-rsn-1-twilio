# Answer phone calls with Twilio Voice and Dialog-RSN-1

Call a Twilio number and Rosa, the phone assistant for a fictional Denver plumber, picks up. Twilio streams
the call to a small FastAPI server, which bridges it to PolyAI's Dialog-RSN-1 API. Dialog-RSN-1 hears the
caller, decides when they've finished and writes the reply, and the API speaks it.

Tutorial: *Connect a phone number to a Dialog-RSN-1 voice agent with Twilio and Python* (link coming when it's published).

| File | What it does |
|---|---|
| `main.py` | The whole server: TwiML webhook, media stream bridge, audio conversion, barge-in |
| `fake_call.py` | Plays the part of Twilio, so you can test a call without a phone (macOS) |
| `tests/` | Offline tests against a fake Dialog-RSN-1 server: no keys, no network |

## Run it

You need Python 3.13+ and [uv](https://docs.astral.sh/uv/), a Dialog-RSN-1 key from
[studio.poly.ai](https://studio.poly.ai), an upgraded Twilio account with a voice-capable number (trial
accounts strip the `<Stream>` verb), and [ngrok](https://ngrok.com) with its authtoken set.

```bash
cp .env.example .env    # add your POLY_API_KEY
uv sync
uv run main.py          # listens on :5050
ngrok http 5050         # in a second terminal
```

In the Twilio Console, open your number's voice configuration and set the incoming call webhook to
`https://<your-ngrok-host>/incoming-call` with **HTTP POST**. Save, then call the number.

## Test without a phone

With `main.py` running (macOS only: the caller's lines come from `say`):

```bash
uv run fake_call.py
```

It speaks three lines as the caller with macOS `say`, talks over Rosa's second answer, and saves the call to
`call.wav` (caller left, Rosa right).

## Run the tests

```bash
uv run pytest
```

The tests need no keys. A fake Dialog-RSN-1 server checks that the bridge forwards the caller's audio untouched,
converts replies to 8 kHz mu-law and clears Twilio's playback on a barge-in.

## How the audio works

- **In:** Twilio sends 8 kHz mu-law. The session sets `audio.input.format` to `{"type": "audio/pcmu"}`, so the
  bridge forwards each payload untouched.
- **Out:** the API streams the spoken reply as 24 kHz PCM16. The bridge resamples to 8 kHz with soxr, which filters out
  everything above the phone band first, and encodes mu-law with `audioop` (the `audioop-lts` package on
  Python 3.13+).
- **Barge-in:** `input_audio_buffer.speech_started` arrives when the caller starts a turn. If Rosa was
  speaking, the server is cancelling her reply. The bridge sends Twilio a `clear` to drop the audio it has
  queued.

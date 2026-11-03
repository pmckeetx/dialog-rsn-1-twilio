# Answer phone calls with Twilio Voice and Dialog-RSN-1

Call a Twilio number and Rosa, the phone assistant for a fictional Denver plumber, picks up. Twilio streams
the call to a small FastAPI server, which bridges it to PolyAI's Dialog-RSN-1. Dialog-RSN-1 hears the
caller, decides when they've finished, writes the reply and speaks it.

Tutorial: *Build an AI Voice Assistant with Twilio Voice, PolyAI's Dialog-RSN-1 API, and Python*.

| File | What it does |
|---|---|
| `main.py` | The whole server: TwiML webhook, media stream bridge, audio conversion, barge-in |
| `fake_call.py` | Plays the part of Twilio, so you can test a call without a phone (macOS) |

## Run it

You need Python 3.13+ and [uv](https://docs.astral.sh/uv/), a Dialog-RSN-1 key from
[studio.poly.ai](https://studio.poly.ai), a Twilio account with a voice-capable number, and
[ngrok](https://ngrok.com).

```bash
cp .env.example .env    # add your POLY_API_KEY
uv sync
uv run main.py          # listens on :5050
ngrok http 5050         # in a second terminal
```

In the Twilio Console, open your number, and under **Voice Configuration** set **A call comes in** to
**Webhook**, `https://<your-ngrok-host>/incoming-call`, **HTTP POST**. Save, then call the number.

## Test without a phone

With `main.py` running:

```bash
uv run fake_call.py
```

It speaks three lines as the caller with macOS `say`, talks over Rosa's second answer, and saves the call to
`call.wav` (caller left, Rosa right).

## How the audio works

- **In:** Twilio sends 8 kHz mu-law. The session sets `audio.input.format` to `{"type": "audio/pcmu"}`, so the
  bridge forwards each payload untouched.
- **Out:** Dialog-RSN-1 speaks 24 kHz PCM16. The bridge resamples to 8 kHz with soxr, which filters out
  everything above the phone band first, and encodes mu-law with `audioop` (the `audioop-lts` package on
  Python 3.13+).
- **Barge-in:** `input_audio_buffer.speech_started` arrives when the caller starts a turn. If Rosa was
  speaking, the server is cancelling her reply. The bridge sends Twilio a `clear` to drop the audio it has
  queued.

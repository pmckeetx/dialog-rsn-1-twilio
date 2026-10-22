"""A phone assistant: Twilio Voice answers the call, Dialog-RSN-1 listens and talks back.

    POST /incoming-call   Twilio's webhook. Answers with TwiML that streams the call to us.
    WS   /media-stream    Twilio Media Streams: caller audio in, agent audio out.

Twilio sends 8 kHz mu-law. Dialog-RSN-1 takes that as it is, and replies in 24 kHz PCM16, which
this file turns back into 8 kHz mu-law for the phone line.
"""

import asyncio
import base64
import json
import os

import audioop
import numpy as np
import soxr
import uvicorn
import websockets
from dotenv import load_dotenv
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import Response
from starlette.websockets import WebSocketDisconnect

load_dotenv()

POLY_API_KEY = os.environ.get("POLY_API_KEY")
DIALOG_REALTIME_URL = os.environ.get("DIALOG_REALTIME_URL", "wss://api.studio.poly.ai/v1/realtime")
PORT = int(os.environ.get("PORT", 5050))

if not POLY_API_KEY:
    raise SystemExit("POLY_API_KEY is not set. Copy .env.example to .env and add your key from studio.poly.ai.")

# A Cartesia voice id. This one is "Katie" (US English). Any Cartesia voice id works here.
VOICE = "f786b574-daa5-4673-aa0c-cbe3e8534c02"

INSTRUCTIONS = """You are Rosa, who answers the phone for Copperline Plumbing in Denver, Colorado.

Everything you say is spoken on a phone call. Keep each reply to one or two short sentences.
Never use lists, markdown, emoji or URLs. Say prices and times the way a person would.

When the call starts, say "Thanks for calling Copperline Plumbing, this is Rosa. How can I help?"

What you know:
- Office hours are 7 AM to 6 PM, Monday to Saturday. Closed Sunday.
- Emergency callouts run 24 hours a day, every day, for burst pipes, major leaks and no hot water.
- A standard visit costs 95 dollars for the first hour, then 60 dollars per half hour.
- An emergency callout outside office hours costs 180 dollars for the first hour.
- The service area is Denver, Aurora, Lakewood and Englewood.
- For anything you can't answer, offer to have a plumber call them back, and ask for their name
  and number. Read the number back to confirm it.

If the caller describes water coming through a ceiling or a burst pipe, tell them to turn off the
main water valve first, then offer an emergency callout."""


app = FastAPI()


class PhoneAudio:
    """Converts one reply at a time from 24 kHz PCM16 to 8 kHz mu-law, the only format Twilio plays."""

    def __init__(self):
        self.response_id = None
        self.resampler = None

    def start(self, response_id):
        # A fresh resampler per reply, so no filter state leaks from a cancelled one.
        self.response_id = response_id
        self.resampler = soxr.ResampleStream(24000, 8000, 1, dtype="int16")

    def convert(self, delta):
        pcm_24k = np.frombuffer(base64.b64decode(delta), dtype=np.int16)
        pcm_8k = self.resampler.resample_chunk(pcm_24k)
        return base64.b64encode(audioop.lin2ulaw(pcm_8k.tobytes(), 2)).decode()


@app.post("/incoming-call")
async def incoming_call(request: Request):
    """Answer the call by connecting it to our media stream."""
    host = request.url.hostname
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="wss://{host}/media-stream" />
  </Connect>
</Response>"""
    return Response(content=twiml, media_type="application/xml")


@app.websocket("/media-stream")
async def media_stream(twilio: WebSocket):
    """Bridge one phone call to one Dialog-RSN-1 session, both ways, until either side hangs up."""
    await twilio.accept()

    # Twilio sends "connected", then "start", which names the stream every reply must address.
    stream_sid = None
    while stream_sid is None:
        event = json.loads(await twilio.receive_text())
        if event["event"] == "start":
            stream_sid = event["start"]["streamSid"]
            print(f"[call] started {event['start']['callSid']}")

    async with websockets.connect(
        DIALOG_REALTIME_URL,
        additional_headers={"Authorization": f"Bearer {POLY_API_KEY}"},
    ) as poly:
        await poly.send(json.dumps({
            "type": "session.update",
            "session": {
                "type": "realtime",
                "instructions": INSTRUCTIONS,
                "audio": {
                    # The phone line's own format. No conversion on the way in.
                    "input": {"format": {"type": "audio/pcmu"}},
                    "output": {"voice": VOICE},
                },
            },
        }))
        # Ask for a reply straight away, so Rosa answers the phone.
        await poly.send(json.dumps({"type": "response.create"}))

        async def caller_to_agent():
            """Forward the caller's audio to Dialog-RSN-1, untouched."""
            try:
                while True:
                    event = json.loads(await twilio.receive_text())
                    if event["event"] == "media":
                        await poly.send(json.dumps({
                            "type": "input_audio_buffer.append",
                            "audio": event["media"]["payload"],
                        }))
                    elif event["event"] == "stop":
                        print("[call] caller hung up")
                        return
            except WebSocketDisconnect:
                return

        async def agent_to_caller():
            """Turn Dialog-RSN-1's replies into phone audio, and stop playback on a barge-in."""
            to_phone = PhoneAudio()
            interrupted = set()  # responses the caller talked over
            async for message in poly:
                event = json.loads(message)
                match event["type"]:
                    case "response.output_audio.delta":
                        if event["response_id"] in interrupted:
                            continue
                        await twilio.send_json({
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {"payload": to_phone.convert(event["delta"])},
                        })

                    # Dialog-RSN-1 only sends this once it's sure the caller is talking, not on an
                    # echo or a cough. If Rosa was speaking, it has already cancelled her reply.
                    # Twilio still holds the audio we sent ahead, so tell it to drop that too.
                    case "input_audio_buffer.speech_started":
                        if to_phone.response_id:
                            interrupted.add(to_phone.response_id)
                        await twilio.send_json({"event": "clear", "streamSid": stream_sid})

                    case "response.created":
                        to_phone.start(event["response"]["id"])
                    case "response.done":
                        to_phone.start(None)

                    case "conversation.item.input_audio_transcription.completed":
                        print(f"[caller] {event['transcript']}")
                    case "response.output_audio_transcript.done":
                        print(f"[rosa]   {event['transcript']}")
                    case "error":
                        print(f"[error]  {event['error']['code']}: {event['error']['message']}")

        # When either side ends, end the other.
        tasks = [asyncio.create_task(caller_to_agent()), asyncio.create_task(agent_to_caller())]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in tasks:
            task.cancel()
    await twilio.close()
    print("[call] ended")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)

"""Offline tests: no keys, no network. A fake Dialog-RSN-1 server stands in for PolyAI.

    uv run pytest
"""

import base64
import json
import os
import socket
import threading

import numpy as np
import pytest
from websockets.sync.server import serve


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


FAKE_PORT = free_port()
os.environ["POLY_API_KEY"] = "test-key"
os.environ["DIALOG_REALTIME_URL"] = f"ws://127.0.0.1:{FAKE_PORT}/v1/realtime"

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402


def pcm24k(samples, freq=440):
    """Base64 PCM16 at 24 kHz, the format Dialog-RSN-1 streams replies in."""
    t = np.arange(samples) / 24000
    tone = (8000 * np.sin(2 * np.pi * freq * t)).astype(np.int16)
    return base64.b64encode(tone.tobytes()).decode()


class FakeDialog:
    """Plays one scripted call: a greeting, the caller barging in, then a second reply."""

    def __init__(self):
        self.received = []
        self.headers = None

    def handler(self, ws):
        self.headers = dict(ws.request.headers)
        self.received.append(json.loads(ws.recv()))  # session.update
        self.received.append(json.loads(ws.recv()))  # response.create
        ws.send(json.dumps({"type": "response.created", "response": {"id": "resp_1"}}))
        ws.send(json.dumps({"type": "response.output_audio.delta", "response_id": "resp_1", "delta": pcm24k(4800)}))
        self.received.append(json.loads(ws.recv()))  # the caller's first frame
        # The caller talks over the greeting. Anything still streaming for resp_1 must not reach the phone.
        ws.send(json.dumps({"type": "input_audio_buffer.speech_started"}))
        ws.send(json.dumps({"type": "response.output_audio.delta", "response_id": "resp_1", "delta": pcm24k(24000)}))
        ws.send(json.dumps({"type": "response.done", "response": {"id": "resp_1"}}))
        ws.send(json.dumps({"type": "response.created", "response": {"id": "resp_2"}}))
        ws.send(json.dumps({"type": "response.output_audio.delta", "response_id": "resp_2", "delta": pcm24k(480)}))
        try:
            for _ in ws:
                pass
        except Exception:
            pass


@pytest.fixture
def fake_dialog():
    fake = FakeDialog()
    server = serve(fake.handler, "127.0.0.1", FAKE_PORT)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield fake
    server.shutdown()


def test_incoming_call_streams_to_the_same_host():
    client = TestClient(main.app, base_url="https://abc123.ngrok-free.app")
    response = client.post("/incoming-call")
    assert response.headers["content-type"].startswith("application/xml")
    assert '<Stream url="wss://abc123.ngrok-free.app/media-stream" />' in response.text


def test_phone_audio_is_a_third_of_the_samples_in_mu_law():
    phone = main.PhoneAudio()
    phone.start("resp_1")
    out = b"".join(base64.b64decode(phone.convert(pcm24k(480))) for _ in range(50))
    # 50 chunks of 20 ms at 24 kHz is 1 second: about 8000 mu-law bytes, less the resampler's delay.
    assert 7500 <= len(out) <= 8000


def test_bridge_forwards_audio_and_clears_on_barge_in(fake_dialog):
    client = TestClient(main.app)
    with client.websocket_connect("/media-stream") as twilio:
        twilio.send_json({"event": "connected", "protocol": "Call", "version": "1.0.0"})
        twilio.send_json({"event": "start", "start": {"streamSid": "MZ1", "callSid": "CA1"}})

        greeting = twilio.receive_json()
        assert greeting["event"] == "media"
        assert greeting["streamSid"] == "MZ1"

        caller_frame = base64.b64encode(b"\xff" * 160).decode()
        twilio.send_json({"event": "media", "media": {"payload": caller_frame}})

        assert twilio.receive_json() == {"event": "clear", "streamSid": "MZ1"}
        # The next audio is resp_2's 20 ms chunk, not the second of resp_1 that the caller talked over.
        after = twilio.receive_json()
        assert after["event"] == "media"
        assert len(base64.b64decode(after["media"]["payload"])) <= 160

        twilio.send_json({"event": "stop"})

    update, create, appended = fake_dialog.received
    assert fake_dialog.headers["authorization"] == "Bearer test-key"
    assert update["session"]["audio"]["input"]["format"] == {"type": "audio/pcmu"}
    assert create == {"type": "response.create"}
    # The caller's mu-law goes to Dialog-RSN-1 untouched.
    assert appended == {"type": "input_audio_buffer.append", "audio": caller_frame}

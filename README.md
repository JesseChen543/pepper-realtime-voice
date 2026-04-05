# Pepper Robot Realtime Voice

OpenAI Realtime API integration for **SoftBank Pepper Robot** (NAOqi 2.5). Streams audio from Pepper's microphone to a cloud backend and plays back AI-generated speech in real time.

## Features

- **Real-time audio streaming** — microphone audio sent over WebSocket
- **AI-powered voice responses** — integrates with OpenAI Realtime API (via your realtime backend)
- **Text-to-speech control** — volume, speed, and pitch adjustable at runtime
- **Body talk animations** — Pepper gestures naturally while speaking
- **WebSocket control server** — frontend can send TTS commands and voice settings
- **Reconnect logic** — automatic reconnection on connection loss

## Quick Start

### Prerequisites

- Pepper Robot with NAOqi 2.5+
- Python 2.7 + `websocket-client` installed on Pepper
- A backend implementing the OpenAI Realtime API

### 1. Configure credentials

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
# Edit .env — never commit this file
```

### 2. Run on Pepper (via SSH)

```bash
# Set credentials as environment variables
export REALTIME_WS_URL="ws://your-backend.example.com/ws/realtime"
export REALTIME_CLIENT_IDENTITY="your-client-identity"
export REALTIME_CLIENT_SECRET="your-secret"

python sound_module.py --ip 127.0.0.1 --port 9559
```

## Architecture

```
Pepper Microphone (48 kHz PCM)
    │
    ▼
sound_module.py  ──── base64 audio chunks ────▶  Backend WebSocket
(ALAudioDevice subscriber)                        (OpenAI Realtime API)
    ▲                                                    │
    └────────────────── AI audio response ───────────────┘
              (ALAudioDevice playback + ALTextToSpeech)
```

## Environment Variables

| Variable | Description |
|----------|-------------|
| `REALTIME_WS_URL` | WebSocket URL of your realtime backend |
| `REALTIME_CLIENT_IDENTITY` | Client identity header value |
| `REALTIME_CLIENT_SECRET` | Secret key header value |
| `PEPPER_SOUND_VERBOSE` | Set to `1` for debug logging |
| `PEPPER_SOUND_SEND_SESSION_UPDATE` | Set to `1` to send OpenAI session config on connect |

## WebSocket TTS Commands (port 9001)

The module also starts a local WebSocket server that accepts commands from the frontend:

| Command | Payload | Description |
|---------|---------|-------------|
| `set_volume` | `{"volume": 0.8}` | Set TTS + output volume (0–1) |
| `set_speed` | `{"speed": 100}` | Set speech speed (50–200%) |
| `set_pitch` | `{"pitch": 1.1}` | Set pitch shift (0.5–4.0) |
| `get_volume` | `{}` | Get current volume |

## Project Structure

```
pepper-voice/
├── sound_module.py    # Main audio streaming + NAOqi integration
├── socket_client.py   # WebSocket client with reconnect logic
├── tts_handler.py     # TTS volume/speed/pitch command handlers
├── Bar.py             # Terminal progress bar UI
├── .env.example       # Environment variable template
└── requirements.txt
```

## Related Projects

Part of the Wonderbyte Pepper Project ecosystem:

- [Dance routines](https://github.com/JesseChen543/naoqi-robot-dance)
- [Movement control](https://github.com/JesseChen543/pepper-motion-control)
- [LED control](https://github.com/JesseChen543/pepper-led-control)
- [Camera + gallery](https://github.com/JesseChen543/pepper-robot-camera)
- [Full dashboard](https://github.com/JesseChen543/pepper-robot-dashboard)
- [YouTube player](https://github.com/JesseChen543/pepper-youtube-player)

## Keywords

`pepper robot` `softbank pepper` `naoqi` `naoqi python` `pepper voice` `robot voice` `text to speech` `openai realtime api` `realtime voice` `ai voice` `robot tts` `speech streaming` `humanoid robot` `social robot` `python robotics` `ALTextToSpeech` `websocket`

## License

MIT

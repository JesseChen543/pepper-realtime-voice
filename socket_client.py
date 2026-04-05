import audioop
import base64
import json
import time
import threading
import os
import logging
from Queue import Queue, Empty

try:
    import websocket
except ImportError:
    websocket = None


def _env_flag(name):
    return os.environ.get(name, "").lower() in ("1", "true", "yes", "on")


VERBOSE_LOGS = _env_flag("PEPPER_SOUND_VERBOSE")

_BASE_LOG = logging.getLogger("pepper.sound")
if not _BASE_LOG.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(message)s"))
    _BASE_LOG.addHandler(_handler)
_BASE_LOG.setLevel(logging.DEBUG if VERBOSE_LOGS else logging.INFO)
_BASE_LOG.propagate = False


def log_line(category, message, level=logging.INFO):
    _BASE_LOG.log(level, "[%s] %s", category, message)


def log_debug(category, message):
    if VERBOSE_LOGS:
        log_line(category, message, logging.DEBUG)


def describe_exception(exc):
    return "%s: %s" % (exc.__class__.__name__, exc)


class SocketClient(object):
    IN_RATE = 48000
    OUT_RATE = 24000
    MAX_RECONNECT_ATTEMPTS = 10  # Increased from 5 to 10 for better reliability

    def __init__(self, ws_app, ws_url=None, ws_headers=None):
        self.ws_app = ws_app
        self.ws_url = ws_url
        self.ws_headers = list(ws_headers or [])
        self.send_q = Queue()
        self._uplink_active = False
        self._last_send_failure = None
        self._reconnect_lock = threading.Lock()
        t = threading.Thread(target=self._sender_loop)
        t.daemon = True
        t.start()

    def _sender_loop(self):
        while True:
            try:
                item = self.send_q.get(timeout=1)
            except Empty:
                if self._uplink_active:
                    self._uplink_active = False
                    log_line("audio", "uplink -> idle")
                continue
            if isinstance(item, tuple) and len(item) == 2 and item[0] in ("audio", "json"):
                kind, data = item
            else:
                kind, data = "audio", item
            try:
                if kind == "audio":
                    event_name, raw = data
                    if not self._uplink_active:
                        self._uplink_active = True
                        log_line("audio", "uplink -> streaming")
                    raw_bytes = str(raw) if isinstance(raw, bytearray) else raw
                    try:
                        pcm24k, _ = audioop.ratecv(raw_bytes, 2, 1,
                                                   self.IN_RATE, self.OUT_RATE, None)
                    except Exception as exc:
                        log_line("audio", "resample failed (%s) - frame dropped" % describe_exception(exc), logging.WARNING)
                        continue

                    b64 = base64.b64encode(pcm24k).decode('ascii')
                    payload = {"type": event_name, "audio": b64}
                    message = json.dumps(payload)

                    try:
                        self.ws_app.send(message)
                        self._last_send_failure = None
                    except Exception as exc:
                        reason = describe_exception(exc)
                        if self._last_send_failure != reason:
                            log_line("ws", "audio send failed (%s) - will reconnect" % reason, logging.WARNING)
                        self._last_send_failure = reason
                        self._uplink_active = False
                        # Auto-reconnect on send failure (like legacy)
                        self._handle_send_failure()
                        continue  # Skip this frame, retry after reconnect
                elif kind == "json":
                    payload = data
                    try:
                        self.ws_app.send(json.dumps(payload))
                        self._last_send_failure = None
                    except Exception as exc:
                        reason = describe_exception(exc)
                        log_line("ws", "json send failed (%s) - reconnecting" % reason, logging.WARNING)
                        self._last_send_failure = reason
                        self._handle_send_failure()
                else:
                    log_debug("ws", "unknown send item kind %s" % kind)
            finally:
                try:
                    self.send_q.task_done()
                except Exception:
                    pass

    def send_audio(self, event_name, raw):
        self.send_q.put(("audio", (event_name, raw)))

    def send_json(self, payload):
        self.send_q.put(("json", payload))

    def check_connection_health(self):
        """Check if WebSocket connection is alive and ready"""
        try:
            if self.ws_app is None:
                log_debug("ws", "health check: no connection")
                return False
            # Check if connection is connected (not closed)
            if hasattr(self.ws_app, 'connected'):
                is_healthy = self.ws_app.connected
            elif hasattr(self.ws_app, 'sock') and self.ws_app.sock:
                is_healthy = True
            else:
                is_healthy = False
            log_debug("ws", "health check: %s" % ("OK" if is_healthy else "FAILED"))
            return is_healthy
        except Exception as exc:
            log_debug("ws", "health check exception: %s" % describe_exception(exc))
            return False

    def wait_until_idle(self, timeout=2.0):
        end = time.time() + timeout if timeout is not None else None
        while True:
            if getattr(self.send_q, "unfinished_tasks", 0) == 0 and self.send_q.empty():
                return True
            if end is not None and time.time() >= end:
                return False
            time.sleep(0.01)

    def _handle_send_failure(self):
        try:
            if self.ws_app is not None:
                self.ws_app.close()
        except Exception as exc:
            log_debug("ws", "close on send failure: %s" % describe_exception(exc))
        time.sleep(3)
        self.reconnect_to_cloud()

    def reconnect_to_cloud(self):
        if not self.ws_url:
            log_line("ws", "reconnect skipped - no ws_url configured", logging.WARNING)
            return
        if websocket is None:
            log_line("ws", "reconnect skipped - websocket module missing", logging.ERROR)
            return

        with self._reconnect_lock:
            log_line("ws", "reconnect -> closing existing socket")
            try:
                if self.ws_app is not None:
                    self.ws_app.close()
            except Exception as exc:
                log_debug("ws", "close before reconnect: %s" % describe_exception(exc))

            last_exc = None
            for attempt in range(1, self.MAX_RECONNECT_ATTEMPTS + 1):
                try:
                    ws_new = websocket.WebSocket()
                    ws_new.connect(self.ws_url, header=self.ws_headers)
                    self.ws_app = ws_new
                    log_line("ws", "reconnect -> success on attempt %d/%d" % (attempt, self.MAX_RECONNECT_ATTEMPTS))
                    self._last_send_failure = None
                    return
                except Exception as exc:
                    last_exc = exc
                    log_line(
                        "ws",
                        "reconnect attempt %d/%d failed (%s)" % (
                            attempt,
                            self.MAX_RECONNECT_ATTEMPTS,
                            describe_exception(exc),
                        ),
                        logging.WARNING,
                    )
                    time.sleep(3)

            if last_exc is not None:
                log_line(
                    "ws",
                    "reconnect failed after %d attempts (%s)" % (
                        self.MAX_RECONNECT_ATTEMPTS,
                        describe_exception(last_exc),
                    ),
                    logging.ERROR,
                )
            else:
                log_line(
                    "ws",
                    "reconnect failed after %d attempts" % self.MAX_RECONNECT_ATTEMPTS,
                    logging.ERROR,
                )

    def send_update_event(self, ws, manual_mode=True):
        """
        Send session configuration to OpenAI Realtime API.

        Args:
            ws: WebSocket connection
            manual_mode: If True, disable auto VAD (tap-to-talk). If False, enable auto VAD (realtime).
        """
        # Configure turn detection based on mode
        if manual_mode:
            # Manual mode: User controls when to send audio (tap-to-talk)
            turn_detection = None
            mode_name = "manual (tap-to-talk)"
        else:
            # Realtime mode: Server VAD automatically detects end of speech
            # Higher threshold and longer silence to reduce self-triggering
            turn_detection = {
                "type": "server_vad",
                "threshold": 0.8,  # High threshold to ignore Pepper's voice
                "silence_duration_ms": 1000,  # 1 second silence required
                "prefix_padding_ms": 300,  # Add padding before speech detection
                "create_response": True
            }
            mode_name = "realtime (auto VAD)"

        event = {
            "type": "session.update",
            "session": {
                "modalities": ["text", "audio"],
                "instructions": "You are a helpful assistant speaking through a robot named Pepper. Respond naturally and conversationally. IMPORTANT: If you hear your own voice or robotic speech, ignore it completely - only respond to human speech.",
                "voice": "alloy",
                "input_audio_format": "pcm16",
                "output_audio_format": "pcm16",
                "input_audio_transcription": {
                    "model": "whisper-1",
                    "language": "en"
                },
                "turn_detection": turn_detection,
                "temperature": 0.8,
                "max_response_output_tokens": "inf"
            }
        }
        try:
            ws.send(json.dumps(event))
            log_line("cloud/ws", "session.update sent (%s, language: en)" % mode_name)
        except Exception as exc:
            log_line("cloud/ws", "session.update failed (%s)" % describe_exception(exc), logging.WARNING)

#!/usr/bin/env python2
# -*- coding: utf-8 -*-
"""Runtime glue that bridges Pepper's NAOqi audio stack with the cloud backend."""

import qi
import argparse
import sys
import time
import threading
import websocket
from socket_client import SocketClient
import json
import base64
import subprocess
import os
import logging
import random

# Add repository root to path for imports
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(CURRENT_DIR, "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# Import control server components
from pepper_control.control_server import PepperControlServer, PepperCommandRouter
from pepper_control.handler_registry import build_control_handlers

# Import LED handlers
from pepper_led.led_handler import create_led_handlers


def _env_flag(name):
    """Return True when an environment flag should be treated as enabled."""
    return os.environ.get(name, "").lower() in ("1", "true", "yes", "on")


VERBOSE_LOGS = _env_flag("PEPPER_SOUND_VERBOSE")
SEND_SESSION_UPDATE = _env_flag("PEPPER_SOUND_SEND_SESSION_UPDATE")

LOG = logging.getLogger("pepper.sound")
if not LOG.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    LOG.addHandler(handler)
LOG.setLevel(logging.DEBUG if VERBOSE_LOGS else logging.INFO)
LOG.propagate = False

BODYTALK_ANIMATIONS = [
    "animations/Stand/BodyTalk/Speaking/BodyTalk_%d" % idx for idx in range(1, 17)
]

def log_line(category, message, level=logging.INFO):
    """Emit a structured log line scoped by logical subsystem category."""
    LOG.log(level, "[%s] %s", category, message)


def log_debug(category, message):
    """Emit a debug log line when verbose logging is enabled."""
    if VERBOSE_LOGS:
        log_line(category, message, logging.DEBUG)


def describe_exception(exc):
    """Represent an exception using its class name and message for logging."""
    return "%s: %s" % (exc.__class__.__name__, exc)

WS_URL = os.environ.get("REALTIME_WS_URL", "ws://your-backend.example.com/ws/realtime")
WS_HEADERS = [
    "client-identity:" + os.environ.get("REALTIME_CLIENT_IDENTITY", "your-client-identity"),
    "client-secret-key:" + os.environ.get("REALTIME_CLIENT_SECRET", ""),
]

SAMPLE_RATE = 48000
MIC_CHANNELS = 1
MODULE_NAME = "SoundProcessingModule"
TTS_TEXT = "Hello, I am Pepper."

class SoundProcessingModule(object):
    """Manage Pepper's microphone streaming and playback lifecycle."""

    COMMIT_MIN_MS = 120.0

    def __init__(self, app, socket_client, log_output=True, control_server=None):
        """Initialise the NAOqi audio client and supporting communication hooks."""
        # Note: app should already be started by caller
        self.session = app.session
        self.audio_service = app.session.service("ALAudioDevice")
        self.client = socket_client
        self.log_output = log_output
        self.control_server = control_server  # For broadcasting state changes to UI
        self._lock = threading.Lock()
        self.audio_playing = False
        self.audio_enabled = False
        self._streaming_active = False
        self._captured_ms = 0.0
        self._frames_forwarded = 0
        self._audio_state_callback = None
        self._playback_callback = None
        self._response_count = 0  # Track number of responses received
        self._speech_detected = False  # Track if speech was detected in current session
        self._cancelling = False  # Track if we're in cancellation mode (drop all audio)
        self._realtime_mode = False  # Track if we're in realtime continuous conversation mode
        self._last_playback_end = None  # Track when playback ended for cooldown period

    def set_playing(self, value):
        """Record active playback state and notify any registered observer."""
        with self._lock:
            changed = (self.audio_playing != value)
            self.audio_playing = value
            callback = self._playback_callback if changed else None
        if changed:
            log_line(
                "audio",
                "cloud playback -> %s" % ("active" if value else "idle")
            )
            # Broadcast playback state to UI clients
            self._broadcast_playback_state(value)

            # In realtime mode: clear audio buffer when playback stops
            # This prevents any buffered echo from being sent to OpenAI
            if not value and self._realtime_mode:
                try:
                    self.client.send_json({"type": "input_audio_buffer.clear"})
                    log_line("audio", "cleared audio buffer (playback stopped - preventing echo)")
                except Exception as exc:
                    log_debug("audio", "failed to clear buffer (%s)" % describe_exception(exc))
        else:
            callback = None
        if callback:
            try:
                callback(value)
            except Exception as exc:
                log_line("audio", "playback callback failed (%s)" % describe_exception(exc), logging.WARNING)

    def is_playing(self):
        """Return True when the cloud playback worker is currently streaming."""
        with self._lock:
            return self.audio_playing

    def set_audio_enabled(self, value):
        """Enable or disable microphone streaming and sync observers/clients."""
        callback = None
        changed = False
        with self._lock:
            if self.audio_enabled != value:
                changed = True
                self.audio_enabled = value
                if not value:
                    self._streaming_active = False
                else:
                    self._captured_ms = 0.0
                    self._frames_forwarded = 0
                callback = self._audio_state_callback
        if changed:
            log_line(
                "audio",
                "microphone -> %s" % ("enabled" if value else "disabled")
            )
            # Broadcast state change to all connected UI clients
            self._broadcast_audio_state(value)
        if callback and changed:
            try:
                callback(value)
            except Exception as exc:
                log_line("audio", "audio state callback failed (%s)" % describe_exception(exc), logging.WARNING)

    def _broadcast_audio_state(self, enabled):
        """Broadcast audio state change to all connected UI clients."""
        if not self.control_server:
            return
        try:
            import json
            payload = {
                "type": "audio_state",
                "audio_enabled": enabled,
                "status": "recording" if enabled else "stopped"
            }
            # Send to all connected clients
            if hasattr(self.control_server, '_server'):
                server = self.control_server._server
                if hasattr(server, 'send_message_to_all'):
                    server.send_message_to_all(json.dumps(payload))
                    log_debug("audio", "broadcasted state to all clients: enabled=%s" % enabled)
        except Exception as exc:
            log_debug("audio", "broadcast failed (%s)" % describe_exception(exc))

    def _broadcast_playback_state(self, playing):
        """Broadcast playback state (Pepper speaking) to all connected UI clients."""
        if not self.control_server:
            return
        try:
            import json
            payload = {
                "type": "playback_state",
                "playing": playing,
                "status": "speaking" if playing else "idle"
            }
            # Send to all connected clients
            if hasattr(self.control_server, '_server'):
                server = self.control_server._server
                if hasattr(server, 'send_message_to_all'):
                    server.send_message_to_all(json.dumps(payload))
                    log_debug("audio", "broadcasted playback state: playing=%s" % playing)
        except Exception as exc:
            log_debug("audio", "playback broadcast failed (%s)" % describe_exception(exc))

    def is_audio_enabled(self):
        """Return True when Pepper's microphone capture pipeline is active."""
        with self._lock:
            return self.audio_enabled

    def startProcessing(self):
        """Subscribe to the NAOqi audio stream and block while relaying frames."""
        # Retry subscription with backoff in case service isn't fully registered yet
        max_attempts = 5
        for attempt in range(1, max_attempts + 1):
            try:
                self.audio_service.setClientPreferences(
                    MODULE_NAME, SAMPLE_RATE, MIC_CHANNELS, 0
                )
                self.audio_service.subscribe(MODULE_NAME)
                log_line("audio", "subscription successful")
                break
            except RuntimeError as exc:
                if attempt < max_attempts:
                    log_line(
                        "audio",
                        "subscription attempt %d/%d failed (%s) - retrying" % (
                            attempt, max_attempts, describe_exception(exc)
                        ),
                        logging.WARNING
                    )
                    time.sleep(2)
                else:
                    log_line(
                        "audio",
                        "subscription failed after %d attempts (%s)" % (
                            max_attempts, describe_exception(exc)
                        ),
                        logging.ERROR
                    )
                    raise
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        self.audio_service.unsubscribe(MODULE_NAME)

    def processRemote(self, nbCh, nbSamps, timeStamp, inputBuffer):
        """Forward microphone frames to the cloud when streaming is allowed."""
        # Only forward audio if enabled
        if not self.is_audio_enabled():
            if self._streaming_active:
                self._streaming_active = False
                log_line("audio", "capture streaming -> paused")
            return
        if not self._streaming_active:
            self._streaming_active = True
            log_line("audio", "capture streaming -> active")

        # In realtime mode: apply echo cancellation
        # Drop ALL mic input while Pepper is speaking OR just finished (1500ms cooldown)
        if self._realtime_mode:
            if self.is_playing():
                # Pepper is currently speaking - drop all audio
                self._last_playback_end = None  # Reset cooldown timer
                log_debug("audio", "dropping frame (Pepper speaking - echo cancellation)")
                return
            else:
                # Pepper finished speaking - add LONG cooldown period to prevent echo
                if self._last_playback_end is None:
                    self._last_playback_end = time.time()
                    log_line("audio", "Pepper stopped - starting 1.5s cooldown (echo prevention)")

                cooldown_elapsed = time.time() - self._last_playback_end
                if cooldown_elapsed < 1.5:  # 1.5 second cooldown after playback ends
                    log_debug("audio", "dropping frame (cooldown: %.3fs/1.5s)" % cooldown_elapsed)
                    return
                elif cooldown_elapsed < 1.6:  # Just passed cooldown
                    log_line("audio", "cooldown complete - mic active, ready for user speech")

        try:
            self.client.send_audio("input_audio_buffer.append", inputBuffer)
            self._record_frame(nbSamps)
        except Exception as exc:
            log_line("audio", "failed to push mic frame (%s)" % describe_exception(exc), logging.WARNING)

    def _record_frame(self, nb_samps):
        """Increment internal counters to track capture duration and frame count."""
        try:
            duration_ms = (float(nb_samps) / float(SAMPLE_RATE)) * 1000.0
        except Exception:
            duration_ms = 0.0
        with self._lock:
            self._frames_forwarded += 1
            self._captured_ms += duration_ms

    def get_capture_metrics(self):
        """Return cumulative capture duration (ms) and frame count since reset."""
        with self._lock:
            return self._captured_ms, self._frames_forwarded

    def reset_capture_metrics(self):
        """Clear capture statistics so the next interaction starts fresh."""
        with self._lock:
            self._captured_ms = 0.0
            self._frames_forwarded = 0

    def set_audio_state_callback(self, callback):
        """Register a hook to be called whenever the microphone state changes."""
        with self._lock:
            self._audio_state_callback = callback

    def set_playback_callback(self, callback):
        """Register a hook to be called whenever playback starts or stops."""
        with self._lock:
            self._playback_callback = callback

def _summarize_cloud_error(error_info):
    """Flatten a cloud error payload into a tuple containing log-friendly parts."""
    code = (error_info or {}).get("code") or "unknown"
    message = (error_info or {}).get("message") or "no message provided"
    note = ""
    lowered = message.lower()
    if code == "invalid_parameter" and "session update event" in lowered:
        note = "cloud ignored duplicate session.update; using existing transcription settings"
    return code, message, note


def receive_thread(ws_conn_ref, player_ref, module, pcm_buffer, session):
    """Listen for websocket events, forward audio, and handle reconnection logic."""
    log_line("cloud/ws", "listener started")

    repeat_state = {"name": None, "count": 0}

    def _interaction_elapsed():
        start = getattr(module, "_interaction_start_time", None)
        if start is None:
            return None
        try:
            return time.time() - float(start)
        except (TypeError, ValueError):
            return None

    def _flush_repeats():
        name = repeat_state["name"]
        count = repeat_state["count"]
        if name and count > 1:
            log_line("cloud/ws", "event %s total x%d" % (name, count))
        repeat_state["name"] = None
        repeat_state["count"] = 0

    def _log_event(event_name, message=None, level=logging.INFO):
        msg = message or ("event %s" % event_name)
        if repeat_state["name"] != event_name:
            _flush_repeats()
            repeat_state["name"] = event_name
            repeat_state["count"] = 1
            log_line("cloud/ws", msg, level)
        else:
            repeat_state["count"] += 1
            if repeat_state["count"] in (10, 25, 50, 75, 100):
                log_line("cloud/ws", "event %s x%d" % (event_name, repeat_state["count"]), level)

    while True:
        ws_conn = ws_conn_ref['conn']
        try:
            raw = ws_conn.recv()
        except Exception as e:
            _flush_repeats()
            log_line("cloud/ws", "connection lost (%s) - attempting reconnection" % describe_exception(e), logging.WARNING)

            # Trigger reconnection via socket_client
            if hasattr(module, 'client') and module.client:
                module.client.reconnect_to_cloud()
                # Update the reference with the new connection
                ws_conn_ref['conn'] = module.client.ws_app
                log_line("cloud/ws", "reconnection successful, resuming listener")
                continue
            else:
                log_line("cloud/ws", "cannot reconnect - no client available", logging.ERROR)
                break

        # Print raw message for debugging
        log_debug("cloud/ws", "raw packet: %s" % raw[:200])

        try:
            msg = json.loads(raw)
        except ValueError as e:
            _flush_repeats()
            log_line("cloud/ws", "json parse error (%s)" % describe_exception(e), logging.WARNING)
            log_line("cloud/ws", "non-JSON response: %s" % raw[:500], logging.WARNING)
            continue

        t = msg.get("type", "")
        if not t:
            _flush_repeats()
            log_line("cloud/ws", "message missing type", logging.DEBUG)
            log_debug("cloud/ws", "payload: %s" % json.dumps(msg))
            continue
        if t == "transcription_session.created":
            _log_event("transcription_session.created", "session.created -> OK")
        elif t == "transcription_session.updated":
            _log_event("transcription_session.updated", "session.updated -> OK")
        elif t in ("response.audio.delta", "response.output_audio.delta"):
            pass  # Audio chunks handled below after all event processing
        elif t == "input_audio_buffer.speech_started":
            # Speech detected but audio hasn't started yet - don't start bodytalk yet
            module._speech_detected = True  # Mark that speech was detected
            elapsed = _interaction_elapsed()
            if elapsed is not None:
                _log_event("input_audio_buffer.speech_started", "speech detected (waiting for audio) [+%.3fs from start]" % elapsed)
            else:
                _log_event("input_audio_buffer.speech_started", "speech detected (waiting for audio)")
        elif t == "response.completed" or t == "response.done":
            _flush_repeats()
            total_elapsed = _interaction_elapsed()
            if total_elapsed is not None:
                log_line("cloud/ws", "=== RESPONSE COMPLETED - audio stream ended [TOTAL: %.3fs] ===" % total_elapsed)
            else:
                log_line("cloud/ws", "=== RESPONSE COMPLETED - audio stream ended ===")
            pcm_buffer[:] = []
            module._response_count += 1  # Increment response counter
            log_line("cloud/ws", "response completed (total: %d)" % module._response_count)
            if module.is_playing():
                module.set_playing(False)
            module._interaction_start_time = None
            # Bodytalk will auto-stop after 5 animations
            # Don't clear buffer here - let server-side VAD handle it
            # The buffer auto-clears when new speech is detected
        elif t == "conversation.item.input_audio_transcription.completed":
            # Single mode: transcription completed, backend will auto-respond
            elapsed = _interaction_elapsed()
            if elapsed is not None:
                _log_event(t, "transcription complete (waiting for auto-response) [+%.3fs from start]" % elapsed)
            else:
                _log_event(t, "transcription complete (waiting for auto-response)")
        elif t == "conversation.item.created":
            # Realtime API: conversation item created
            _log_event(t)
        elif t.startswith("conversation.item"):
            # Handle all other conversation.item.* events
            _log_event(t)
        elif t == "error":
            code, message, note = _summarize_cloud_error(msg.get("error"))
            summary = "%s: %s" % (code, message)
            if note:
                summary = "%s (%s)" % (summary, note)
            _flush_repeats()
            log_line("cloud/ws", "error %s - continuing" % summary, logging.WARNING)
            log_debug("cloud/ws", "error payload: %s" % json.dumps(msg, indent=2))
        else:
            _log_event(t)
            log_debug("cloud/ws", "event payload: %s" % json.dumps(msg))

        # handle audio frames
        if t in ("response.audio.delta", "response.output_audio.delta"):
            pcm = base64.b64decode(msg.get("delta", ""))
            # Start bodytalk on FIRST audio chunk (when Pepper actually starts speaking)
            if not module.is_playing():
                elapsed = _interaction_elapsed()
                if elapsed is not None:
                    log_line("audio", "first audio chunk received - Pepper starting to speak [+%.3fs from start]" % elapsed)
                else:
                    log_line("audio", "first audio chunk received - Pepper starting to speak")
                module.set_playing(True)

                # Disable microphone when Pepper starts responding
                # In manual mode: always disable to prevent echo
                # In realtime mode: keep mic enabled for continuous conversation (server VAD handles it)
                if module.is_audio_enabled() and not module._realtime_mode:
                    log_line("audio", "disabling mic (Pepper is about to speak) - manual mode")
                    module.set_audio_enabled(False)

            # Only write audio if:
            # 1. Not in cancellation mode (dropping all old audio)
            # 2. Playback is still active (not cancelled)
            # 3. aplay process is actually running
            if not module._cancelling and module.is_playing() and player_ref['proc'] and player_ref['proc'].poll() is None:
                try:
                    player_ref['proc'].stdin.write(pcm)
                    log_debug("audio", "wrote %d bytes to aplay (PID: %d)" % (len(pcm), player_ref['proc'].pid))
                except IOError as exc:
                    # Broken pipe is expected when aplay is killed during cancellation
                    # This is normal and not an error - just means audio was released
                    if exc.errno == 32:  # EPIPE (Broken pipe)
                        log_debug("audio", "broken pipe (aplay was killed) - dropping remaining chunks")
                        module.set_playing(False)  # Stop trying to write more chunks
                    else:
                        log_line("audio", "playback write failed (%s) - dropping chunk" % describe_exception(exc), logging.WARNING)
                    continue
                except Exception as exc:
                    log_line("audio", "unexpected write error (%s)" % describe_exception(exc), logging.ERROR)
                    continue
                # Microphone is already disabled when first audio chunk arrives (see above)
                # No need to disable again here
            else:
                # Playback was cancelled or aplay process is dead, drop this audio chunk
                if module._cancelling:
                    log_debug("audio", "dropping audio chunk (in cancellation mode)")
                else:
                    log_debug("audio", "dropping audio chunk (playback cancelled or aplay dead)")

    _flush_repeats()
    log_line("cloud/ws", "listener stopped")
    try:
        player_ref['proc'].stdin.close()
        player_ref['proc'].wait()
    except Exception as exc:
        log_debug("audio", "player cleanup issue: %s" % describe_exception(exc))

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--qi-url", type=str, default="tcp://127.0.0.1:9559")
    args = parser.parse_args()

    app = qi.Application(["sound_streamer", "--qi-url=" + args.qi_url])
    app.start()
    session = app.session

    # connect to remote audio service with ping/pong keepalive
    ws_conn = websocket.WebSocket()
    ws_conn.connect(WS_URL, header=WS_HEADERS)
    # Set socket timeout to enable periodic ping/pong checks
    ws_conn.settimeout(60)  # 60 second ping interval

    # audio playback process
    player = subprocess.Popen(
        [
            "aplay",
            "-B",
            os.environ.get("PEPPER_SOUND_APLAY_BUFFER", "200000"),  # 200 ms buffer to avoid underruns
            "-f",
            "S16_LE",
            "-r",
            "24000",
            "-c",
            "1",
        ],
        stdin=subprocess.PIPE,
        bufsize=0,
    )

    client = SocketClient(ws_conn, ws_url=WS_URL, ws_headers=WS_HEADERS)
    module = SoundProcessingModule(app, client)  # control_server will be set later
    tts_service = session.service("ALTextToSpeech")

    # Create player_ref early so handlers can access it
    player_ref = {'proc': player}  # Use dict to allow updating player process
    try:
        memory_service = session.service("ALMemory")
    except Exception as exc:
        memory_service = None
        log_line("movement", "ALMemory unavailable (%s)" % describe_exception(exc), logging.WARNING)
    animation_player = None
    try:
        animation_player = session.service("ALAnimationPlayer")
    except Exception as exc:
        log_line("movement", "ALAnimationPlayer unavailable (%s)" % describe_exception(exc), logging.WARNING)
    audio_service = module.audio_service

    animation_guard = threading.Lock()
    animation_state = {
        "active": False,
        "thread": None,
        "last_animation": None,
        "running": False,
        "count": 0,  # Track number of animations played
        "max_count": 5,  # Stop after 5 animations
    }
    animation_flags = {
        "cloud_playback": False,
        "tts": False,
    }

    def _pick_next_bodytalk(previous):
        """Pick a non-repeating bodytalk animation name for conversational motion."""
        choices = [name for name in BODYTALK_ANIMATIONS if name != previous]
        if not choices:
            choices = BODYTALK_ANIMATIONS[:]
        return random.choice(choices)

    def _bodytalk_worker():
        """Run a loop that drives bodytalk animations while audio is active."""
        while True:
            with animation_guard:
                if not animation_state["active"]:
                    animation_state["thread"] = None
                    animation_state["running"] = False
                    animation_state["count"] = 0  # Reset count
                    log_debug("movement", "bodytalk loop idle")
                    return
                # Check if we've reached max animations
                if animation_state["count"] >= animation_state["max_count"]:
                    log_line("movement", "bodytalk reached max count (%d) - stopping" % animation_state["max_count"])
                    animation_state["active"] = False
                    animation_state["thread"] = None
                    animation_state["running"] = False
                    animation_state["count"] = 0
                    # DON'T call module.set_playing(False) here - let it be controlled by audio events only
                    return
                animation_state["running"] = True
                animation_state["count"] += 1
                current_count = animation_state["count"]
                previous = animation_state.get("last_animation")
            animation_name = _pick_next_bodytalk(previous)
            with animation_guard:
                animation_state["last_animation"] = animation_name
            log_line("movement", "bodytalk -> %s (%d/%d)" % (animation_name, current_count, animation_state["max_count"]))
            future = None
            try:
                post_iface = getattr(animation_player, "post", None)
                if post_iface is not None:
                    future = post_iface.run(animation_name)
                    # Wait for completion if future supports it
                    if hasattr(future, "isRunning"):
                        while future.isRunning():
                            with animation_guard:
                                if not animation_state["active"]:
                                    break
                            time.sleep(0.1)
                    elif hasattr(future, "wait"):
                        future.wait()
                else:
                    animation_player.run(animation_name)
            except Exception as exc_inner:
                log_line("movement", "failed to trigger animation (%s)" % describe_exception(exc_inner), logging.WARNING)
            finally:
                with animation_guard:
                    animation_state["running"] = False
            log_line("movement", "bodytalk complete")

            wait_deadline = time.time() + 1.5
            while time.time() < wait_deadline:
                with animation_guard:
                    if not animation_state["active"]:
                        animation_state["thread"] = None
                        animation_state["running"] = False
                        return
                time.sleep(0.1)

    def _update_bodytalk_flag(flag, value):
        """Track bodytalk triggers and ensure the animation loop matches demand."""
        if animation_player is None:
            log_debug("movement", "bodytalk update skipped - no animation player")
            return
        value = bool(value)
        thread_to_start = None
        thread_to_join = None
        last_animation = None
        with animation_guard:
            animation_flags[flag] = value
            should_run = any(animation_flags.values())
            thread = animation_state.get("thread")
            log_line("movement", "bodytalk flag '%s'=%s, flags=%s, should_run=%s" % (flag, value, animation_flags, should_run))
            if should_run:
                if not animation_state["active"]:
                    animation_state["active"] = True
                    log_line("movement", "bodytalk activated")
                if thread and thread.is_alive():
                    log_debug("movement", "bodytalk loop already active")
                if not thread or not thread.is_alive():
                    thread = threading.Thread(target=_bodytalk_worker, name="BodyTalkLoop")
                    thread.daemon = True
                    animation_state["thread"] = thread
                    thread_to_start = thread
                    log_line("movement", "bodytalk thread created")
            else:
                animation_state["active"] = False
                log_line("movement", "bodytalk deactivated (all flags false)")
                if thread and thread.is_alive():
                    thread_to_join = thread
                    log_line("movement", "bodytalk thread marked for stop")
                last_animation = animation_state.get("last_animation")
        if thread_to_start:
            thread_to_start.start()
        if thread_to_join:
            if last_animation:
                try:
                    animation_player.stop(last_animation)
                except Exception as exc_inner:
                    log_debug("movement", "stop warning: %s" % describe_exception(exc_inner))
            thread_to_join.join(timeout=1.0)
            with animation_guard:
                if animation_state.get("thread") is thread_to_join:
                    animation_state["thread"] = None
                    animation_state["running"] = False

    def _handle_playback_change(active):
        """Update bodytalk state when cloud playback begins or ends."""
        log_line("movement", "playback state changed -> %s (updating bodytalk)" % ("active" if active else "stopping"))
        _update_bodytalk_flag("cloud_playback", active)

    def _handle_tts_status(value):
        """Normalize NAOqi TTS status events and toggle bodytalk accordingly."""
        state = None
        if isinstance(value, (list, tuple)):
            if len(value) >= 2:
                state = value[1]
            elif value:
                state = value[0]
        else:
            state = value
        if state is None:
            return
        state_str = str(state).strip().lower()
        if not state_str:
            return
        if state_str in ("started", "start", "running", "speaking", "active"):
            _update_bodytalk_flag("tts", True)
        elif state_str in ("done", "stopped", "stop", "ended", "end", "finished", "canceled", "cancelled", "aborted"):
            _update_bodytalk_flag("tts", False)

    tts_status_subscriber = None
    if memory_service is not None:
        try:
            tts_status_subscriber = memory_service.subscriber("ALTextToSpeech/Status")
            tts_status_subscriber.signal.connect(_handle_tts_status)
            log_line("movement", "listening to ALTextToSpeech/Status")
        except Exception as exc:
            log_line("movement", "failed to subscribe to ALTextToSpeech/Status (%s)" % describe_exception(exc), logging.WARNING)
            tts_status_subscriber = None

    # Create audio-specific command handlers
    def _start_audio(_payload):
        """Start a new interaction by enabling the microphone and resetting state."""
        start_time = time.time()
        log_line("audio", "=== START_AUDIO command received [t=%.3fs] ===" % start_time)
        log_debug("audio", "state before: audio_enabled=%s, streaming=%s, playing=%s" % (
            module.is_audio_enabled(),
            module._streaming_active,
            module.is_playing()
        ))

        # Wait briefly if we just cancelled - let old audio chunks clear
        if module._cancelling:
            log_line("audio", "previous cancellation active - waiting for cleanup (500ms)")
            time.sleep(0.5)  # Increased to 500ms to let OpenAI finish sending old chunks

        # Exit cancellation mode - ready for new audio
        module._cancelling = False
        log_line("audio", "cancellation mode DISABLED - ready for new audio")

        # If player_ref is None (was killed during cancel), restart aplay now
        if player_ref['proc'] is None:
            try:
                player_ref['proc'] = subprocess.Popen(
                    [
                        "aplay",
                        "-B",
                        os.environ.get("PEPPER_SOUND_APLAY_BUFFER", "200000"),
                        "-f",
                        "S16_LE",
                        "-r",
                        "24000",
                        "-c",
                        "1",
                    ],
                    stdin=subprocess.PIPE,
                    bufsize=0,
                )
                log_line("audio", "aplay restarted (PID: %d) - ready for new audio" % player_ref['proc'].pid)
            except Exception as exc:
                log_line("audio", "failed to restart aplay (%s)" % describe_exception(exc), logging.WARNING)

        # Reset response counter for new conversation
        module._response_count = 0
        # Store start time for this interaction
        module._interaction_start_time = start_time
        module._speech_detected = False  # Reset speech detection flag
        module.reset_capture_metrics()

        # Clear audio buffer for fresh conversation
        # COMMENTED OUT: This causes delay - buffer auto-clears on server side
        # try:
        #     client.send_json({"type": "input_audio_buffer.clear"})
        #     log_line("audio", "cleared input_audio_buffer")
        # except Exception as e:
        #     log_line("audio", "buffer clear failed (%s) - continuing anyway" % describe_exception(e), logging.WARNING)

        # Enable microphone (simple enable like legacy)
        module.set_audio_enabled(True)
        elapsed = time.time() - start_time
        log_line("audio", "=== microphone ENABLED [elapsed=%.3fs] ===" % elapsed)
        return {"audio_enabled": True, "status": "recording"}

    def _stop_audio(_payload):
        """Stop microphone streaming and manually trigger AI response (hold-to-talk mode)."""
        log_line("audio", "=== STOP_AUDIO command received ===")
        log_debug("audio", "state before: audio_enabled=%s, streaming=%s, playing=%s" % (
            module.is_audio_enabled(),
            module._streaming_active,
            module.is_playing()
        ))

        # Disable microphone
        module.set_audio_enabled(False)

        # TAP-TO-TALK: Always commit and create response (manual mode)
        # User has full control - send whatever audio was captured
        try:
            # Step 1: Commit the audio buffer
            client.send_json({"type": "input_audio_buffer.commit"})
            log_line("audio", "[tap-to-talk] sent: input_audio_buffer.commit")

            # Step 2: Trigger response generation
            client.send_json({"type": "response.create"})
            log_line("audio", "[tap-to-talk] sent: response.create")
        except Exception as exc:
            log_line("audio", "[tap-to-talk] failed to commit/create response (%s)" % describe_exception(exc), logging.WARNING)

        if module.is_playing():
            module.set_playing(False)
        module.reset_capture_metrics()
        module._interaction_start_time = None
        log_line("audio", "=== microphone DISABLED ===")
        return {"audio_enabled": False, "status": "stopped"}

    def _cancel_response(_payload):
        """Cancel any ongoing response from the AI and release audio immediately."""
        log_line("audio", "=== CANCEL_RESPONSE command received ===")

        # STEP 1: Enter cancellation mode - drop ALL audio chunks until new session starts
        module._cancelling = True
        log_line("audio", "cancellation mode ENABLED - dropping all audio")

        # STEP 2: Stop playback state FIRST so receive thread stops writing
        if module.is_playing():
            module.set_playing(False)
            log_line("audio", "playback state -> stopped")

        # STEP 2: Kill aplay process to immediately release audio (释放语音)
        try:
            old_player = player_ref['proc']
            if old_player and old_player.poll() is None:  # Check if process is running
                log_line("audio", "killing aplay process (PID: %d) to release audio" % old_player.pid)
                old_player.kill()  # Immediate termination
                # Python 2.7 doesn't have timeout parameter, just wait briefly
                import time
                time.sleep(0.1)  # Give process time to die
                log_line("audio", "aplay killed - audio released")

                # Set player_ref to None temporarily to drop in-flight audio chunks
                player_ref['proc'] = None
                log_line("audio", "player_ref cleared - dropping in-flight audio chunks")

        except Exception as exc:
            log_line("audio", "failed to kill aplay (%s)" % describe_exception(exc), logging.WARNING)

        # STEP 4: Cancel cloud response
        try:
            client.send_json({"type": "response.cancel"})
            log_line("audio", "sent: response.cancel")

            client.send_json({"type": "input_audio_buffer.clear"})
            log_line("audio", "sent: input_audio_buffer.clear")
        except Exception as exc:
            log_line("audio", "failed to cancel cloud response (%s)" % describe_exception(exc), logging.WARNING)

        # STEP 5: Disable microphone and reset state
        module.set_audio_enabled(False)
        module.reset_capture_metrics()
        module._interaction_start_time = None
        module._speech_detected = False

        log_line("audio", "=== response cancelled, audio released, ready for new conversation ===")
        return {"audio_enabled": False, "status": "cancelled"}

    def _reconnect_audio(_payload):
        """Force the websocket client to reconnect to the cloud service."""
        client.reconnect_to_cloud()
        return {"reconnecting": True}

    def _speak(payload):
        """Invoke NAOqi TTS with the provided text (or default prompt)."""
        text = None
        if isinstance(payload, dict):
            text = payload.get("text") or payload.get("speech") or payload.get("message")
        if not text:
            text = TTS_TEXT
        tts_service.say(text)
        return {"spoken": text}

    def _start_realtime(_payload):
        """Start realtime mode with automatic VAD (Voice Activity Detection)."""
        log_line("audio", "=== START_REALTIME command received ===")

        # Reset any ongoing state
        if module.is_playing():
            module.set_playing(False)
            log_line("audio", "stopped any ongoing playback")

        module._cancelling = False
        module._speech_detected = False
        module._playback_start_time = None
        module._last_playback_end = None

        # Enable realtime mode flag
        module._realtime_mode = True
        log_line("audio", "realtime mode flag ENABLED")

        # Switch to realtime mode (auto VAD)
        log_line("audio", "sending session update (auto VAD)")
        client.send_update_event(ws_conn, manual_mode=False)
        time.sleep(1.0)  # Increased wait time for session update to fully process

        # Clear any buffered audio
        try:
            client.send_json({"type": "input_audio_buffer.clear"})
            log_line("audio", "cleared input audio buffer")
        except Exception as exc:
            log_line("audio", "failed to clear buffer (%s)" % describe_exception(exc), logging.WARNING)

        # Start listening (mic stays on for continuous conversation)
        module.set_audio_enabled(True)
        log_line("audio", "realtime mode ACTIVE - continuous conversation enabled")
        log_line("audio", "microphone will stay enabled during Pepper's responses")
        return {"realtime_mode": True, "audio_enabled": True}

    def _stop_realtime(_payload):
        """Stop realtime mode."""
        log_line("audio", "=== STOP_REALTIME command received ===")

        # Stop any ongoing playback
        if module.is_playing():
            module.set_playing(False)
            log_line("audio", "stopped ongoing playback")

        # Disable realtime mode flag FIRST
        module._realtime_mode = False
        log_line("audio", "realtime mode flag DISABLED")

        # Stop listening
        module.set_audio_enabled(False)
        log_line("audio", "microphone disabled")

        # Clear audio buffer
        try:
            client.send_json({"type": "input_audio_buffer.clear"})
            log_line("audio", "cleared input audio buffer")
        except Exception as exc:
            log_line("audio", "failed to clear buffer (%s)" % describe_exception(exc), logging.WARNING)

        # Cancel any ongoing response
        try:
            client.send_json({"type": "response.cancel"})
            log_line("audio", "cancelled any ongoing response")
        except Exception as exc:
            log_line("audio", "failed to cancel response (%s)" % describe_exception(exc), logging.WARNING)

        # Switch back to manual mode
        log_line("audio", "sending session update (manual mode)")
        client.send_update_event(ws_conn, manual_mode=True)
        time.sleep(0.5)  # Wait for session update

        # Reset state
        module._cancelling = False
        module._speech_detected = False
        module._playback_start_time = None
        module._last_playback_end = None

        log_line("audio", "realtime mode STOPPED - back to manual control")
        return {"realtime_mode": False, "audio_enabled": False}

    module.set_playback_callback(_handle_playback_change)

    audio_handlers = {
        "start_audio": _start_audio,
        "stop_audio": _stop_audio,
        "cancel_response": _cancel_response,
        "reconnect": _reconnect_audio,
        "speak": _speak,
        "start_realtime": _start_realtime,
        "stop_realtime": _stop_realtime,
    }

    # Create LED handlers
    led_handlers = create_led_handlers(session)
    log_line("init", "LED handlers registered: %s" % ", ".join(led_handlers.keys()))

    # Merge audio and LED handlers
    combined_handlers = {}
    combined_handlers.update(audio_handlers)
    combined_handlers.update(led_handlers)

    # Create command router and control server first (without control handlers)
    router = PepperCommandRouter(session, custom_handlers={})
    control_server = PepperControlServer(
        session,
        router=router,
        host="0.0.0.0",
        port=9001,
        shutdown_token=os.environ.get("PEPPER_CONTROL_SHUTDOWN_TOKEN", "pepper-control-local"),
    )

    # Now build control handlers with control_server reference
    combined_handlers['control_server'] = control_server
    control_handlers = build_control_handlers(
        session,
        repo_root=REPO_ROOT,
        audio_service=audio_service,
        extra_handlers=combined_handlers,
    )

    # Update router with actual handlers
    for name, handler in control_handlers.items():
        router.register(name, handler)

    log_line("init", "Registered %d control handlers" % len(control_handlers))
    # Give module reference to control_server for broadcasting state changes
    module.control_server = control_server
    control_server.start(background=True)
    log_line("control", "WebSocket server listening on ws://0.0.0.0:9001")

    # Start battery level broadcasting (every 30 seconds)
    control_server.start_battery_updates()

    # start thread to receive and route remote audio with auto-reconnect
    pcm_buffer = []
    ws_conn_ref = {'conn': ws_conn}  # Use dict to allow updating connection
    # player_ref already created earlier (line 475) so handlers can access it
    t = threading.Thread(
        target=receive_thread,
        args=(ws_conn_ref, player_ref, module, pcm_buffer, session)
    )
    t.daemon = True
    t.start()

    # register the service so Pepper will call processRemote()
    log_line("init", "registering NAOqi service: %s" % MODULE_NAME)
    try:
        session.registerService(MODULE_NAME, module)
        log_line("init", "service registration completed")
    except Exception as exc:
        log_line("init", "service registration failed (%s)" % describe_exception(exc), logging.ERROR)
        raise

    # Wait briefly for cloud to send session.created
    log_line("init", "waiting for cloud to send initial messages")
    time.sleep(2)

    # Send session configuration after cloud initialization
    # Always send session update for manual mode (tap-to-talk)
    log_line("init", "sending session configuration to cloud (manual mode)")
    client.send_update_event(ws_conn, manual_mode=True)
    log_line("init", "waiting for session.updated response")
    time.sleep(3)

    # give NAOqi more time to fully register the service
    log_line("init", "waiting for NAOqi service registration")
    time.sleep(3)
    log_line("init", "starting audio processing loop")
    module.startProcessing()

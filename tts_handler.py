#!/usr/bin/env python2
# -*- coding: utf-8 -*-

"""
TTS (Text-to-Speech) parameter handlers for Pepper robot.

Provides volume, speed, and pitch control for Pepper's voice.
"""

try:
    string_types = (basestring,)
except NameError:
    string_types = (str,)


def create_tts_handlers(session, audio_service):
    """
    Create TTS parameter command handlers.

    Args:
        session: NAOqi session object
        audio_service: ALAudioDevice service for output volume control

    Returns:
        dict: Dictionary of command handlers
    """
    tts_service = session.service("ALTextToSpeech")

    def _choose_value(raw_payload, keys):
        """Extract value from payload using multiple possible keys."""
        value = raw_payload
        if isinstance(raw_payload, dict):
            for key in keys:
                if key in raw_payload:
                    candidate = raw_payload[key]
                    if candidate not in (None, ""):
                        value = candidate
                        break
        return value

    def _parse_volume(payload):
        """Parse volume from payload (0-100% or 0.0-1.0)."""
        value = _choose_value(payload, ["volume", "volume_percent", "value", "argument", "level"])
        if value is None or value == "":
            raise ValueError("missing volume value")
        if isinstance(value, string_types):
            text = value.strip()
            if not text:
                raise ValueError("empty volume value")
            percent_hint = text.endswith("%")
            if percent_hint:
                text = text[:-1].strip()
            parsed = float(text)
            if percent_hint:
                parsed /= 100.0
        else:
            parsed = float(value)
        if 1.0 < parsed <= 100.0:
            parsed /= 100.0
        parsed = max(0.0, min(1.0, parsed))
        return parsed

    def _parse_speed(payload):
        """Parse speed from payload (50-200%)."""
        value = _choose_value(payload, ["speed", "value", "argument", "percent", "rate"])
        if value is None or value == "":
            raise ValueError("missing speed value")
        if isinstance(value, string_types):
            text = value.strip()
            if not text:
                raise ValueError("empty speed value")
            percent_hint = text.endswith("%")
            if percent_hint:
                text = text[:-1].strip()
            parsed = float(text)
        else:
            parsed = float(value)
        if parsed <= 4.0:
            parsed *= 100.0
        parsed = round(parsed)
        parsed = int(max(50, min(200, parsed)))
        return parsed

    def _parse_pitch(payload):
        """Parse pitch from payload (0.5-4.0 multiplier)."""
        value = _choose_value(payload, ["pitch", "pitch_shift", "value", "argument", "ratio", "level"])
        if value is None or value == "":
            raise ValueError("missing pitch value")
        if isinstance(value, string_types):
            text = value.strip()
            if not text:
                raise ValueError("empty pitch value")
            parsed = float(text)
        else:
            parsed = float(value)
        if 4.0 < parsed <= 400.0:
            parsed /= 100.0
        parsed = max(0.5, min(4.0, parsed))
        return parsed

    def _volume_payload(volume, output_percent=None):
        """Create volume response payload."""
        clamped = max(0.0, min(1.0, volume))
        percent = int(round(clamped * 100.0))
        if output_percent is None:
            output_percent = percent
        else:
            output_percent = int(round(output_percent))
        output_percent = max(0, min(100, output_percent))
        return {
            "volume": clamped,
            "volume_percent": percent,
            "output_volume_percent": output_percent
        }

    def _speed_payload(speed):
        """Create speed response payload."""
        speed_value = int(speed)
        return {
            "speed": speed_value,
            "speed_percent": speed_value,
            "speed_ratio": speed_value / 100.0,
        }

    def _pitch_payload(pitch):
        """Create pitch response payload."""
        multiplier = float(pitch)
        return {
            "pitch": multiplier,
            "pitch_percent": int(round(multiplier * 100.0)),
            "pitch_multiplier": multiplier,
        }

    def _attach_request_id(data, request_id):
        """Attach request_id to response if provided."""
        if request_id is not None:
            data["request_id"] = request_id
        return data

    def _set_volume(payload):
        """Set TTS and output volume."""
        print("[TTS] _set_volume called with payload:", payload)
        request_id = None
        if isinstance(payload, dict):
            request_id = payload.get("request_id")
        try:
            volume = _parse_volume(payload)
            print("[TTS] Parsed volume:", volume)
        except ValueError as exc:
            print("[TTS] Volume parse error:", exc)
            return False, _attach_request_id({"error": str(exc)}, request_id)

        percent_value = int(round(volume * 100.0))
        try:
            tts_service.setVolume(volume)
            print("[TTS] TTS volume set to:", volume)
        except Exception as exc:
            print("[TTS] TTS setVolume failed:", exc)
            return False, _attach_request_id({"error": "failed to set volume: {}".format(exc)}, request_id)

        output_percent = None
        try:
            audio_service.setOutputVolume(percent_value)
            output_percent = audio_service.getOutputVolume()
            print("[TTS] Output volume set to:", output_percent)
        except Exception as exc:
            print("[TTS] Failed to set output volume: {}".format(exc))

        response = _volume_payload(volume, output_percent)
        print("[TTS] Returning response:", response)
        return True, _attach_request_id(response, request_id)

    def _get_volume(_payload):
        """Get current TTS and output volume."""
        try:
            volume = float(tts_service.getVolume())
        except Exception as exc:
            return False, {"error": "failed to get volume: {}".format(exc)}

        output_percent = None
        try:
            output_percent = audio_service.getOutputVolume()
        except Exception:
            pass
        return True, _volume_payload(volume, output_percent)

    def _set_speed(payload):
        """Set TTS speech speed."""
        request_id = None
        if isinstance(payload, dict):
            request_id = payload.get("request_id")
        try:
            speed = _parse_speed(payload)
        except ValueError as exc:
            return False, _attach_request_id({"error": str(exc)}, request_id)
        try:
            tts_service.setParameter("speed", speed)
        except Exception as exc:
            return False, _attach_request_id({"error": "failed to set speed: {}".format(exc)}, request_id)
        response = _speed_payload(speed)
        return True, _attach_request_id(response, request_id)

    def _get_speed(_payload):
        """Get current TTS speech speed."""
        try:
            speed = float(tts_service.getParameter("speed"))
        except Exception as exc:
            return False, {"error": "failed to get speed: {}".format(exc)}
        return True, _speed_payload(int(round(speed)))

    def _set_pitch(payload):
        """Set TTS pitch shift."""
        request_id = None
        if isinstance(payload, dict):
            request_id = payload.get("request_id")
        try:
            pitch = _parse_pitch(payload)
        except ValueError as exc:
            return False, _attach_request_id({"error": str(exc)}, request_id)
        try:
            tts_service.setParameter("pitchShift", pitch)
        except Exception as exc:
            return False, _attach_request_id({"error": "failed to set pitch: {}".format(exc)}, request_id)
        response = _pitch_payload(pitch)
        return True, _attach_request_id(response, request_id)

    def _get_pitch(_payload):
        """Get current TTS pitch shift."""
        try:
            pitch = float(tts_service.getParameter("pitchShift"))
        except Exception as exc:
            return False, {"error": "failed to get pitch: {}".format(exc)}
        return True, _pitch_payload(pitch)

    return {
        "set_volume": _set_volume,
        "get_volume": _get_volume,
        "volume": _set_volume,  # Alias
        "set_speed": _set_speed,
        "get_speed": _get_speed,
        "speed": _set_speed,  # Alias
        "set_pitch": _set_pitch,
        "get_pitch": _get_pitch,
        "pitch": _set_pitch,  # Alias
    }

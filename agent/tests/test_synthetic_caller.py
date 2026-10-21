import wave

import numpy as np
import pytest

from synthetic_caller import (
    SAMPLE_RATE,
    SAMPLES_PER_FRAME,
    SPEECH_RMS,
    TEXT_PROMPTS,
    answered,
    is_audible,
    load_wav,
)


def tone(amplitude: float) -> np.ndarray:
    t = np.arange(SAMPLES_PER_FRAME) / SAMPLE_RATE
    return (amplitude * np.sin(2 * np.pi * 440 * t)).astype(np.int16)


def test_silence_is_not_audible():
    assert not is_audible(np.zeros(SAMPLES_PER_FRAME, dtype=np.int16))


def test_comfort_noise_is_not_audible():
    rng = np.random.default_rng(0)
    noise = rng.normal(0, SPEECH_RMS / 5, SAMPLES_PER_FRAME).astype(np.int16)
    assert not is_audible(noise)


def test_speech_level_tone_is_audible():
    # A sine's RMS is amplitude / sqrt(2), so 3x the threshold is clearly above it.
    assert is_audible(tone(3 * SPEECH_RMS))


def test_empty_frame_is_not_audible():
    assert not is_audible(np.array([], dtype=np.int16))


def test_large_samples_do_not_overflow():
    # Squaring int16 in int16 would wrap around; full-scale must still read as loud.
    assert is_audible(np.full(SAMPLES_PER_FRAME, 32767, dtype=np.int16))


def write_wav(path, samples: np.ndarray, rate: int = SAMPLE_RATE, channels: int = 1) -> None:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(samples.tobytes())


def test_load_wav_returns_samples(tmp_path):
    path = tmp_path / "ok.wav"
    write_wav(path, tone(3 * SPEECH_RMS))
    assert len(load_wav(path)) == SAMPLES_PER_FRAME


def test_load_wav_rejects_empty_audio(tmp_path):
    # What a wedged `say` left behind: a header with zero frames.
    path = tmp_path / "empty.wav"
    write_wav(path, np.array([], dtype=np.int16))
    with pytest.raises(ValueError, match="no audio"):
        load_wav(path)


def test_load_wav_rejects_wrong_format(tmp_path):
    path = tmp_path / "16k.wav"
    write_wav(path, tone(3 * SPEECH_RMS), rate=16000)
    with pytest.raises(ValueError, match="mono"):
        load_wav(path)


def test_answered_matches_expected_words_case_insensitively():
    assert answered("It's 33 Degrees and clear in Ubud.", ("degree", "rain"))
    assert not answered("Sorry, I couldn't check just now.", ("degree", "rain"))


def test_text_prompts_each_expect_something():
    assert TEXT_PROMPTS and all(prompt and expect for prompt, expect in TEXT_PROMPTS)

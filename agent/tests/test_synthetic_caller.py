import numpy as np

from synthetic_caller import SAMPLE_RATE, SAMPLES_PER_FRAME, SPEECH_RMS, is_audible


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

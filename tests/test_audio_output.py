import unittest

from raspiaudio_radio.backend import RadioBackend


class _FakeRadio:
    def __init__(self) -> None:
        self.calls = []

    def configure_audio(self, mode: str, master: bool, sample_rate: int, sample_size: int) -> None:
        self.calls.append({"mode": mode, "master": master})


class _Config:
    i2s_master = False
    sample_rate = 48_000
    sample_size = 16


class _MasterConfig(_Config):
    i2s_master = True


def _backend(audio_out: str, config: object) -> RadioBackend:
    backend = object.__new__(RadioBackend)
    backend._audio_out_mode = audio_out
    backend.config = config
    return backend


class AudioOutputModeTests(unittest.TestCase):
    def test_both_falls_back_to_analog_while_pi_is_clock_slave(self) -> None:
        backend = _backend("both", _Config())
        radio = _FakeRadio()

        backend._apply_audio_config_locked(radio)

        self.assertEqual([call["mode"] for call in radio.calls], ["analog"])

    def test_both_keeps_i2s_when_the_chip_is_the_clock_master(self) -> None:
        # With the chip driving BCLK/LRCLK there is no starving slave to leave
        # armed, so "both" is safe and stays as the user asked for.
        backend = _backend("both", _MasterConfig())
        radio = _FakeRadio()

        backend._apply_audio_config_locked(radio)

        self.assertEqual([call["mode"] for call in radio.calls], ["both"])

    def test_explicit_modes_are_untouched(self) -> None:
        for mode in ("analog", "i2s"):
            backend = _backend(mode, _Config())
            radio = _FakeRadio()

            backend._apply_audio_config_locked(radio)

            self.assertEqual([call["mode"] for call in radio.calls], [mode])


if __name__ == "__main__":
    unittest.main()

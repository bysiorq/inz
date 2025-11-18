"""
sensors.py
===========

Moduł definiuje klasy obsługujące przetwornik ADC MCP3008 oraz czujnik
alkoholu MQ‑3.  Klasa ``MCP3008`` umożliwia odczyt napięć z kanałów
przetwornika przez magistralę SPI, a klasa ``MQ3Sensor`` implementuje
podstawowe przetwarzanie surowych pomiarów do wartości promili.
"""

import spidev
import numpy as np


class MCP3008:
    """Prosty interfejs do układu ADC MCP3008 poprzez SPI."""

    def __init__(self, bus: int = 0, device: int = 0, max_speed_hz: int = 1000000):
        self.spi = spidev.SpiDev()
        self.spi.open(bus, device)
        self.spi.max_speed_hz = max_speed_hz
        self.spi.mode = 0

    def read_channel(self, ch: int) -> int:
        """
        Odczytaj kanał 0..7 i zwróć wartość 10‑bitową (0..1023).
        """
        if ch < 0 or ch > 7:
            raise ValueError("MCP3008 channel 0..7 only")
        odpowiedz = self.spi.xfer2([1, (8 | ch) << 4, 0])
        return ((odpowiedz[1] & 3) << 8) | odpowiedz[2]

    def close(self):
        """Zamknij połączenie SPI."""
        try:
            self.spi.close()
        except Exception:
            pass


class MQ3Sensor:
    """
    Klasa obsługująca czujnik alkoholu MQ‑3 podłączony do MCP3008.

    Surowe odczyty z przetwornika są uśredniane, a różnica względem
    kalibrowanej linii bazowej przeliczana jest na wartość w promilach.
    """

    def __init__(self, adc: MCP3008, channel: int, baseline_samples: int, promille_scale: float):
        self.adc = adc
        self.channel = channel
        self.baseline_samples = baseline_samples
        self.promille_scale = promille_scale
        self.baseline = None

    def calibrate_baseline(self) -> float:
        """Pobierz serię próbek aby ustalić linię bazową czujnika."""
        probki = [self.adc.read_channel(self.channel) for _ in range(self.baseline_samples)]
        self.baseline = float(np.median(probki))
        return self.baseline

    def read_raw(self) -> int:
        """Odczytaj pojedynczą próbkę surową z kanału ADC."""
        return self.adc.read_channel(self.channel)

    def promille_from_samples(self, samples):
        """
        Przelicz średni odczyt z listy ``samples`` na wartość w promilach.
        Jeśli lista jest pusta, odczytywana jest pojedyncza próbka.
        """
        import numpy as _np
        wartosc = float(_np.mean(samples)) if samples else float(self.read_raw())
        if self.baseline is None:
            self.baseline = wartosc
        delta = max(0.0, wartosc - self.baseline)
        return delta / max(1e-6, self.promille_scale)
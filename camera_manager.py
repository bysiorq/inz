"""
camera_manager.py
===================

Moduł ten udostępnia prostą klasę do obsługi kamery Picamera2 w
raspberry.  Aby uniknąć problemów z kolejnością kanałów (RGB/BGR)
wszystkie ramki zwracane przez metodę ``get_frame_bgr()`` są
traktowane jako BGR.  Ewentualny obrót obrazu (np. na potrzeby
orientacji portretowej) wykonywany jest za pomocą funkcji
``numpy.rot90``.

Uwaga: klasy i metody zachowują nazwę angielską (CameraManager), aby
nie łamać istniejących importów w głównej aplikacji.
"""

import numpy as np
from picamera2 import Picamera2


class CameraManager:
    """Prosty wrapper na Picamera2 zwracający obrócone klatki jako BGR."""

    def __init__(self, szer: int, wys: int, rotate_dir: str):
        """
        Inicjalizuj kamerę.

        :param szer: szerokość obrazu z sensora (W)
        :param wys: wysokość obrazu z sensora (H)
        :param rotate_dir: kierunek obrotu: "cw", "ccw", "180" lub "none"
        """
        # upewnij się że cv2 jest dostępne — import lokalny ucisza linter
        import cv2  # noqa: F401
        self.kierunek_obrotu = rotate_dir
        self.kamera = Picamera2()
        cfg = self.kamera.create_preview_configuration(
            main={"size": (szer, wys), "format": "RGB888"}
        )
        self.kamera.configure(cfg)
        self.kamera.start()

    def get_frame_bgr(self):
        """
        Pobierz klatkę z kamery w formacie BGR.

        Funkcja pobiera obraz jako RGB888, następnie dokonuje ewentualnego
        obrotu i zwraca wynik, traktując tablicę jako BGR bez dalszych
        konwersji.  Obrót wykonywany jest według parametru podanego
        konstruktorowi.
        """
        klatka = self.kamera.capture_array("main")  # numpy (H,W,3)
        # obrót w prawo (clockwise) to 3 * 90° ccw
        if self.kierunek_obrotu == "cw":
            klatka = np.rot90(klatka, 3)
        elif self.kierunek_obrotu == "ccw":
            klatka = np.rot90(klatka, 1)
        elif self.kierunek_obrotu == "180":
            klatka = np.rot90(klatka, 2)
        # gdy rotate_dir == "none" nie obracamy
        return klatka

    def stop(self):
        """Zatrzymaj kamerę (bez rzucania wyjątku przy błędzie)."""
        try:
            self.kamera.stop()
        except Exception:
            pass
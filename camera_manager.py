# camera_manager.py
import numpy as np
from picamera2 import Picamera2


class CameraManager:
    """Prosty wrapper na Picamera2, zwraca klatki jako BGR po obrocie."""

    def __init__(self, szer, wys, rotate_dir):
        import cv2  # tylko po to, żeby mieć pewność że cv2 jest
        _ = cv2     # ucisz linter

        self.kierunek_obrotu = rotate_dir
        self.kamera = Picamera2()
        cfg = self.kamera.create_preview_configuration(
            main={"size": (szer, wys), "format": "RGB888"}
        )
        self.kamera.configure(cfg)
        self.kamera.start()

    def get_frame_bgr(self):
        klatka = self.kamera.capture_array("main")  # numpy (H,W,3)

        if self.kierunek_obrotu == "cw":      # 90° w prawo
            klatka = np.rot90(klatka, 3)
        elif self.kierunek_obrotu == "ccw":   # 90° w lewo
            klatka = np.rot90(klatka, 1)
        elif self.kierunek_obrotu == "180":
            klatka = np.rot90(klatka, 2)

        # od tej chwili traktujemy bufor jako BGR
        return klatka

    def stop(self):
        try:
            self.kamera.stop()
        except Exception:
            pass

#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Główna aplikacja Alkotester z rozszerzonymi funkcjami.

Ten moduł stanowi serce programu uruchamianego na Raspberry Pi.  W
dodatku do oryginalnej funkcjonalności wprowadzono obsługę czujnika
odległości GP2Y0A21, mikrofonu analogowego na MCP3008 oraz diod LED
sygnalizujących wynik pomiaru.  Użytkownik ma również możliwość
wykorzystania specjalnego trybu "Gość", który pozwala na przejście
bez rozpoznawania twarzy.
"""

import os
import sys
import cv2
import time
import signal
import threading
import json
import requests
import numpy as np
from datetime import datetime

from PyQt5 import QtCore, QtGui, QtWidgets
import RPi.GPIO as GPIO

from config import CONFIG
from utils_fs import ensure_dirs, now_str, log_csv
from sensors import MCP3008, MQ3Sensor
from facedb import FaceDB
from camera_manager import CameraManager
from keypad import KeypadDialog

try:
    from pymongo import MongoClient  # type: ignore
except Exception:
    MongoClient = None

_MONGO_CLIENT = None
_MONGO_DISABLED = False


def _face_quality(szare_roi):
    """
    Oceń jakość próbki twarzy.

    Zwraca krotkę (ok, ostrość, jasność) gdzie:
    - ok: True jeśli próbka spełnia minimalne wymagania,
    - ostrość: wariancja operatora Laplace'a,
    - jasność: średnia wartość pikseli.
    """
    ostrosc = cv2.Laplacian(szare_roi, cv2.CV_64F).var()
    jasnosc = float(np.mean(szare_roi))
    ok = (
        ostrosc >= CONFIG["quality_min_sharpness"]
        and CONFIG["quality_min_brightness"] <= jasnosc <= CONFIG["quality_max_brightness"]
    )
    return ok, ostrosc, jasnosc


class MainWindow(QtWidgets.QMainWindow):
    """Główne okno aplikacji oraz implementacja FSM."""

    def __init__(self):
        super().__init__()
        ensure_dirs()

        # Konfiguracja GPIO
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        GPIO.setup(CONFIG["gate_gpio"], GPIO.OUT, initial=GPIO.LOW)
        # Konfiguracja diod LED sygnalizujących wynik
        GPIO.setup(CONFIG["led_pass_gpio"], GPIO.OUT, initial=GPIO.LOW)
        GPIO.setup(CONFIG["led_deny_gpio"], GPIO.OUT, initial=GPIO.LOW)

        # Czujnik MQ-3 i konwerter MCP3008
        self.adc = MCP3008(CONFIG["spi_bus"], CONFIG["spi_device"])
        self.mq3 = MQ3Sensor(
            self.adc,
            CONFIG["mq3_channel"],
            CONFIG["baseline_samples"],
            CONFIG["promille_scale"],
        )

        # Baza twarzy
        self.facedb = FaceDB(
            CONFIG["faces_dir"],
            CONFIG["index_dir"],
            CONFIG["employees_json"]
        )
        boot = CONFIG["bootstrap_employee"]
        self.facedb.ensure_employee_exists(boot["id"], boot["name"], boot["pin"])

        # Synchronizacja employees.json z serwera master (Render)
        try:
            self.sync_employees_from_server()
        except Exception as e:
            print(f"[SYNC] Błąd sync przy starcie: {e}")

        self.setWindowTitle("Alkotester – Raspberry Pi")

        if CONFIG["hide_cursor"]:
            self.setCursor(QtCore.Qt.BlankCursor)

        # Tworzenie interfejsu
        centralny = QtWidgets.QWidget()
        self.setCentralWidget(centralny)
        uklad_zew = QtWidgets.QVBoxLayout(centralny)
        uklad_zew.setContentsMargins(0, 0, 0, 0)
        uklad_zew.setSpacing(0)

        # Podgląd kamery
        self.view = QtWidgets.QLabel()
        self.view.setAlignment(QtCore.Qt.AlignCenter)
        self.view.setStyleSheet("background:black;")
        self.view.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,
            QtWidgets.QSizePolicy.Expanding
        )
        uklad_zew.addWidget(self.view, 1)

        # Pasek dolny (overlay)
        self.overlay = QtWidgets.QFrame()
        self.overlay.setFixedHeight(CONFIG["overlay_height_px"])
        self.overlay.setStyleSheet("background: rgba(0,0,0,110); color:white;")

        uklad_overlay = QtWidgets.QVBoxLayout(self.overlay)
        uklad_overlay.setContentsMargins(16, 12, 16, 12)
        uklad_overlay.setSpacing(8)

        # Górny pasek z zegarem i przyciskiem Gość
        top_row = QtWidgets.QHBoxLayout()
        top_row.setContentsMargins(0, 0, 0, 0)
        top_row.setSpacing(8)
        self.lbl_top = QtWidgets.QLabel("")
        self.lbl_top.setStyleSheet("color:white; font-size:28px; font-weight:600;")
        top_row.addWidget(self.lbl_top)
        top_row.addStretch(1)
        self.btn_guest = QtWidgets.QPushButton("Gość")
        self.btn_guest.setStyleSheet(
            "font-size:20px; padding:6px 12px; border-radius:12px; "
            "background:#6a1b9a; color:white;"
        )
        self.btn_guest.clicked.connect(self.on_btn_guest)
        top_row.addWidget(self.btn_guest)
        uklad_overlay.addLayout(top_row)

        # Centralny napis
        self.lbl_center = QtWidgets.QLabel("")
        self.lbl_center.setAlignment(QtCore.Qt.AlignCenter)
        self.lbl_center.setStyleSheet("color:white; font-size:36px; font-weight:700;")
        uklad_overlay.addWidget(self.lbl_center, 1)

        # Rząd przycisków (podczas decyzji / retry)
        rzad_przyciski = QtWidgets.QHBoxLayout()
        rzad_przyciski.setSpacing(12)
        self.btn_primary = QtWidgets.QPushButton("Ponów pomiar")
        self.btn_primary.setStyleSheet(
            "font-size:24px; padding:12px 18px; border-radius:16px; background:#2e7d32; color:white;"
        )
        self.btn_secondary = QtWidgets.QPushButton("Wprowadź PIN")
        self.btn_secondary.setStyleSheet(
            "font-size:24px; padding:12px 18px; border-radius:16px; background:#1565c0; color:white;"
        )
        rzad_przyciski.addWidget(self.btn_primary)
        rzad_przyciski.addWidget(self.btn_secondary)
        uklad_overlay.addLayout(rzad_przyciski)

        # Pasek postępu dla pomiaru nadmuchowego
        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setStyleSheet(
            "QProgressBar {background-color: #444444; border-radius: 10px; color:white; font-size:24px;} "
            "QProgressBar::chunk { background-color: #00c853; }"
        )
        self.progress_bar.setFixedHeight(30)
        self.progress_bar.hide()
        uklad_overlay.addWidget(self.progress_bar)

        uklad_zew.addWidget(self.overlay, 0)

        # Stan maszyny stanowej
        self.state = "INIT"

        # Dane aktualnego pracownika
        self.current_emp_id = None
        self.current_emp_name = None

        # Flaga dopuszczenia po PIN
        self.fallback_pin_flag = False

        # Ostatnio rozpoznana twarz
        self.last_face_bbox = None
        self.last_confidence = 0.0
        self.last_promille = 0.0

        self.frame_last_bgr = None

        self.detect_fail_count = 0
        self.detect_retry_count = 0

        self._stable_emp_id = None
        self._stable_count = 0


        self.calibrate_good_face = False
        self.calibrate_seen_face = False

        self.measure_samples = []

        self.post_training_action = None

        # Dodatkowe zmienne dla pomiaru nadmuchowego
        self.distance_channel = CONFIG.get("distance_channel", 1)
        self.mic_channel = CONFIG.get("mic_channel", 2)
        self.distance_min_cm = CONFIG.get("distance_min_cm", 15.0)
        self.distance_max_cm = CONFIG.get("distance_max_cm", 20.0)
        self.mic_threshold = CONFIG.get("mic_threshold", 500)

        self.blow_elapsed = 0.0
        self.is_guest = False


        # Inicjalizacja kamery
        self.cam = CameraManager(
            CONFIG["camera_main_size"][0],
            CONFIG["camera_main_size"][1],
            CONFIG["rotate_dir"],
        )

        # Timery
        self.cam_timer = QtCore.QTimer(self)
        self.cam_timer.timeout.connect(self.on_camera_tick)
        self.cam_timer.start(int(1000 / max(1, CONFIG["camera_fps"])))

        self.face_timer = QtCore.QTimer(self)
        self.face_timer.timeout.connect(self.on_face_tick)

        self.ui_timer = QtCore.QTimer(self)
        self.ui_timer.timeout.connect(self.on_ui_tick)

        self.identified_timer = QtCore.QTimer(self)
        self.identified_timer.timeout.connect(self.on_identified_tick)

        self.measure_timer = QtCore.QTimer(self)
        self.measure_timer.timeout.connect(self.on_measure_tick)

        # Timer okresowego sync-a (np. co 10 minut)
        self.sync_timer = QtCore.QTimer(self)
        self.sync_timer.timeout.connect(self.on_sync_tick)
        self.sync_timer.start(1 * 60 * 1000)  # 10 minut

        # Obsługa przycisków
        self.btn_primary.clicked.connect(self.on_btn_primary)
        self.btn_secondary.clicked.connect(self.on_btn_secondary)

        # Komunikat startowy – kalibracja MQ-3
        self.set_message(
            "Proszę czekać…",
            "Kalibracja czujnika MQ-3 w toku",
            color="white",
        )
        self.show_buttons(primary_text=None, secondary_text=None)

        # Rozpocznij kalibrację MQ-3
        self._calibrate_mq3_start()

    # --- SYNC employees.json z masterem na Renderze ---

    def _crop_and_scale_fill(self, img, target_w, target_h):
        """
        Przytnij obraz tak, żeby zachować proporcje, a potem przeskaluj
        do (target_w, target_h), wypełniając cały label (bez czarnych pasów).

        img – numpy array w RGB (h, w, 3)
        Zwraca: przeskalowany obraz RGB lub None gdy coś jest nie tak.
        """
        if img is None or target_w <= 0 or target_h <= 0:
            return None

        h, w = img.shape[:2]
        if h == 0 or w == 0:
            return None

        src_ar = w / float(h)
        dst_ar = target_w / float(target_h)

        # Jeśli źródło szersze niż docelowy prostokąt – przytnij boki.
        if src_ar > dst_ar:
            new_w = int(h * dst_ar)
            if new_w <= 0:
                return None
            x1 = max(0, (w - new_w) // 2)
            x2 = x1 + new_w
            crop = img[:, x1:x2]
        else:
            # Źródło „wyższe” – przytnij górę/dół.
            new_h = int(w / dst_ar)
            if new_h <= 0:
                return None
            y1 = max(0, (h - new_h) // 2)
            y2 = y1 + new_h
            crop = img[y1:y2, :]

        if crop.size == 0:
            return None

        # Skaluje do dokładnego rozmiaru labela
        resized = cv2.resize(crop, (int(target_w), int(target_h)), interpolation=cv2.INTER_AREA)
        return resized


    def sync_employees_from_server(self):
        """
        Pobierz aktualną listę pracowników z serwera (Render master)
        i zapisz jako lokalne employees.json, potem przeładuj FaceDB.
        """
        base_url = CONFIG.get("server_base_url")
        token = CONFIG.get("sync_token")
        employees_path = CONFIG.get("employees_json", "data/employees.json")

        # Upewnij się, że mamy absolutną ścieżkę (katalog projektu)
        if not os.path.isabs(employees_path):
            base_dir = os.path.dirname(os.path.abspath(__file__))
            employees_path = os.path.join(base_dir, employees_path)

        if not base_url:
            print("[SYNC] Brak server_base_url w CONFIG – pomijam sync.")
            return

        url = f"{base_url.rstrip('/')}/api/employees_public"
        params = {}
        if token:
            params["token"] = token

        try:
            print(f"[SYNC] Pobieram pracowników z {url} ...")
            resp = requests.get(url, params=params, timeout=3)
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict):
                print("[SYNC] Odpowiedź nie jest dict-em – pomijam.")
                return

            employees = data.get("employees", [])
            data_to_save = {"employees": employees}

            os.makedirs(os.path.dirname(employees_path), exist_ok=True)
            with open(employees_path, "w", encoding="utf-8") as f:
                json.dump(data_to_save, f, ensure_ascii=False, indent=2)

            print(f"[SYNC] Zapisano {len(employees)} pracowników do {employees_path}")

            try:
                self.facedb._load_employees()
                print("[SYNC] FaceDB przeładowany.")
            except Exception as e:
                print(f"[SYNC] Błąd przeładowania FaceDB: {e}")

        except Exception as e:
            print(f"[SYNC] Błąd pobierania z serwera: {e}")

    def _online_learn_face(self, emp_id: str):
        """
        Doincrementalne uczenie twarzy – dociągamy pojedynczą klatkę
        i dokładamy próbkę do bazy, jeśli jest wystarczająco dobra.
        """
        try:
            if self.last_face_bbox is None:
                return
            if self.frame_last_bgr is None:
                return

            (fx, fy, fw, fh) = self.last_face_bbox
            fx = int(max(0, fx))
            fy = int(max(0, fy))
            fw = int(max(0, fw))
            fh = int(max(0, fh))

            h_img, w_img, _ = self.frame_last_bgr.shape
            x2 = min(fx + fw, w_img)
            y2 = min(fy + fh, h_img)
            if x2 <= fx or y2 <= fy:
                return

            # Wytnij twarz, przeskaluj do 240x240
            twarz_bgr = self.frame_last_bgr[fy:y2, fx:x2].copy()
            twarz_bgr = cv2.resize(
                twarz_bgr,
                (240, 240),
                interpolation=cv2.INTER_LINEAR,
            )

            # Sprawdź jakość (ostrość / jasność)
            twarz_szara = cv2.cvtColor(twarz_bgr, cv2.COLOR_BGR2GRAY)
            ok_q, ostrosc, jasnosc = _face_quality(twarz_szara)
            if not ok_q:
                return

            # Dodaj próbkę do bazy twarzy
            self.facedb.add_online_face_sample(emp_id, twarz_bgr)
        except Exception:
            # Lepiej nic nie zepsuć niż wywalić całą aplikację
            pass


    def on_sync_tick(self):
        """Okresowy sync z masterem."""
        try:
            self.sync_employees_from_server()
        except Exception as e:
            print(f"[SYNC] Błąd okresowego sync-a: {e}")

    # ----- Pomocnicze: timery, napisy -----

    def _stop_timer(self, timer_obj: QtCore.QTimer):
        """Zatrzymaj timer jeżeli jest aktywny."""
        try:
            if timer_obj.isActive():
                timer_obj.stop()
        except Exception:
            pass

    def set_message(self, tekst_gora, tekst_srodek=None, color="white"):
        """Ustaw tekst na górnym i środkowym pasku wraz z kolorem."""
        if color == "green":
            kolor_css = "#00ff00"
        elif color == "red":
            kolor_css = "#ff4444"
        else:
            kolor_css = "white"

        self.lbl_top.setText(tekst_gora)
        self.lbl_top.setStyleSheet(
            f"color:{kolor_css}; font-size:28px; font-weight:600;"
        )

        self.lbl_center.setText(tekst_srodek or "")
        self.lbl_center.setStyleSheet(
            f"color:{kolor_css}; font-size:36px; font-weight:700;"
        )

    def show_buttons(self, primary_text=None, secondary_text=None):
        """Pokaż lub ukryj przyciski z tekstami."""
        if primary_text is None:
            self.btn_primary.hide()
        else:
            self.btn_primary.setText(primary_text)
            self.btn_primary.show()

        if secondary_text is None:
            self.btn_secondary.hide()
        else:
            self.btn_secondary.setText(secondary_text)
            self.btn_secondary.show()

    # ----- Stany FSM -----
    def enter_idle(self):
        """Przejście do stanu IDLE."""
        self.state = "IDLE"
        self.current_emp_id = None
        self.current_emp_name = None
        self.fallback_pin_flag = False
        self.is_guest = False


        self.last_face_bbox = None
        self.last_confidence = 0.0
        self.detect_fail_count = 0
        self.detect_retry_count = 0
        self._stable_emp_id = None
        self._stable_count = 0

        self.calibrate_good_face = False
        self.calibrate_seen_face = False

        self.measure_samples = []
        self.blow_elapsed = 0.0

        self.progress_bar.hide()

        self.ui_timer.start(250)
        self.face_timer.start(CONFIG["face_detect_interval_ms"])
        self._stop_timer(self.identified_timer)
        self._stop_timer(self.measure_timer)

        self.set_message(now_str(), "Podejdź bliżej", color="white")
        self.show_buttons(primary_text=None, secondary_text="Wprowadź PIN")

    def enter_detect(self):
        """Przejście do stanu DETECT."""
        self.state = "DETECT"
        self.detect_fail_count = 0
        self._stable_emp_id = None
        self._stable_count = 0

        self.ui_timer.start(250)
        self.face_timer.start(CONFIG["face_detect_interval_ms"])
        self._stop_timer(self.identified_timer)
        self._stop_timer(self.measure_timer)

        self.set_message(now_str(), "Szukam twarzy…", color="white")
        self.show_buttons(primary_text=None, secondary_text="Wprowadź PIN")

    def enter_pin_entry(self):
        """Przejście do stanu wprowadzania PIN."""
        self.state = "PIN_ENTRY"
        self._stop_timer(self.face_timer)
        self._stop_timer(self.ui_timer)
        self._stop_timer(self.identified_timer)
        self._stop_timer(self.measure_timer)

        # ---- NOWOŚĆ: wymuś sync z Renderem przed wpisaniem PIN-u ----
        try:
            print("[SYNC] Ręczny sync przed wprowadzeniem PIN-u...")
            self.sync_employees_from_server()
        except Exception as e:
            print(f"[SYNC] Błąd sync przy wprowadzaniu PIN: {e}")

        dlg = KeypadDialog(self, title="Wprowadź PIN")
        if dlg.exec() == QtWidgets.QDialog.Accepted:
            pin = dlg.value()
            # Wczytaj aktualną bazę pracowników na wypadek modyfikacji przez panel admina
            try:
                self.facedb._load_employees()
            except Exception:
                pass
            emp = self.facedb.emp_by_pin.get(pin)
            if not emp:
                self.set_message("Zły PIN – brak danych", "", color="red")
                self.show_buttons(primary_text=None, secondary_text=None)
                QtCore.QTimer.singleShot(2000, self.enter_idle)
                return

            self.current_emp_id = emp.get("id") or emp.get("name")
            self.current_emp_name = emp.get("name")
            self.fallback_pin_flag = False

            self.collect_new_shots_for_current_emp()
        else:
            self.enter_idle()


    def enter_detect_retry(self):
        """Stan ponownej próby rozpoznania po treningu."""
        self.state = "DETECT_RETRY"
        self.detect_retry_count = 0

        self.face_timer.start(CONFIG["face_detect_interval_ms"])
        self._stop_timer(self.ui_timer)
        self._stop_timer(self.identified_timer)
        self._stop_timer(self.measure_timer)

        self.set_message("Sprawdzam twarz…", self.current_emp_name or "", color="white")
        self.show_buttons(primary_text=None, secondary_text=None)

    def enter_identified_wait(self):
        """
        Przejście do stanu oczekiwania przed pomiarem.

        W tej wersji:
        - nie ma odliczania w sekundach,
        - mierzymy na bieżąco odległość z czujnika,
        - gdy pracownik znajdzie się w dopuszczalnym przedziale,
          przechodzimy automatycznie do stanu MEASURE.
        """
        self.state = "IDENTIFIED_WAIT"

        # Reset flag kalibracji twarzy – będą aktualizowane w on_face_tick
        self.calibrate_good_face = False
        self.calibrate_seen_face = False

        # Detekcja twarzy nadal działa, żeby pilnować, że ktoś stoi przed kamerą
        self.face_timer.start(CONFIG["face_detect_interval_ms"])

        # Zatrzymaj inne timery poza identified_timer
        self._stop_timer(self.ui_timer)
        self._stop_timer(self.measure_timer)
        self._stop_timer(self.identified_timer)

        # Timer do sprawdzania odległości co 200 ms
        self.identified_timer.start(200)

        imie = self.current_emp_name or ""
        dist_cm = self.read_distance_cm()
        if dist_cm == float("inf"):
            dist_txt = "brak odczytu"
        elif dist_cm > 80:
            dist_txt = ">80 cm"
        else:
            dist_txt = f"{dist_cm:0.0f} cm"

        tekst_gora = "Podejdź bliżej"
        tekst_srodek = f"Cześć {imie}\nOdległość: {dist_txt}"
        self.set_message(tekst_gora, tekst_srodek, color="white")
        self.show_buttons(primary_text=None, secondary_text=None)



    def enter_measure(self):
        """Stan pomiaru stężenia alkoholu wraz z kontrolą dystansu i dmuchania."""
        self.state = "MEASURE"
        # Przygotuj listę próbek i licznik nadmuchu
        self.measure_samples = []
        self.blow_elapsed = 0.0

        # Wyłącz podgląd twarzy podczas pomiaru
        self.last_face_bbox = None
        self.last_confidence = 0.0

        self._stop_timer(self.face_timer)
        self._stop_timer(self.ui_timer)
        self._stop_timer(self.identified_timer)
        # Resetuj i pokaż pasek postępu
        self.progress_bar.setValue(0)
        self.progress_bar.show()

        # Uruchom timer pomiaru w odstępach ~100 ms
        self.measure_timer.start(100)

        self.set_message(
            "Przeprowadzam pomiar…",
            f"{CONFIG['measure_seconds']:.1f} s",
            color="white",
        )
        self.show_buttons(primary_text=None, secondary_text=None)

    @QtCore.pyqtSlot()
    def _measure_done(self):
        """Slot wywoływany z wątku pomiarowego po policzeniu promili."""
        promille = getattr(self, "_pending_promille", 0.0)
        self.enter_decide(promille)

    def enter_decide(self, promille):
        """Podejmij decyzję na podstawie wyniku pomiaru."""
        # Ukryj pasek postępu po zakończeniu pomiaru
        self.progress_bar.hide()

        self.last_promille = float(promille)

        try:
            thr_pass = float(CONFIG.get("threshold_pass", 0.0))
        except Exception:
            thr_pass = 0.0
        try:
            thr_deny = float(CONFIG.get("threshold_deny", 0.5))
        except Exception:
            thr_deny = 0.5

        if thr_pass > thr_deny:
            print(
                f"[WARN] threshold_pass ({thr_pass}) > threshold_deny ({thr_deny}) – zamieniam kolejność"
            )
            thr_pass, thr_deny = thr_deny, thr_pass

        print(
            f"[DECIDE] promille={self.last_promille:.3f}, "
            f"pass <= {thr_pass:.3f}, deny >= {thr_deny:.3f}"
        )

        tekst_pomiar = f"Pomiar: {self.last_promille:.3f} [‰]"

        self._stop_timer(self.face_timer)
        self._stop_timer(self.ui_timer)
        self._stop_timer(self.identified_timer)
        self._stop_timer(self.measure_timer)

        # Domyślnie zakładamy, że warunek pass/deny opiera się na progu
        result_pass = False
        result_retry = False
        if self.last_promille <= thr_pass:
            result_pass = True
        elif self.last_promille < thr_deny:
            result_retry = True
        else:
            result_pass = False

        # Zaktualizuj interfejs i steruj bramką/LED w zależności od wyniku
        if result_pass and not result_retry:
            self.state = "DECIDE_PASS"
            self.set_message(tekst_pomiar, "Przejście otwarte", color="green")
            self.show_buttons(primary_text=None, secondary_text=None)
            self.trigger_gate_and_log(True, self.last_promille)
            self.trigger_led(True)
            QtCore.QTimer.singleShot(2500, self.enter_idle)
            return

        if result_retry:
            self.state = "RETRY"
            self.set_message(
                tekst_pomiar,
                "Ponów pomiar",
                color="red",
            )
            self.show_buttons(primary_text="Ponów pomiar", secondary_text="Odmowa")
            return

        # Odmowa
        self.state = "DECIDE_DENY"
        self.set_message(tekst_pomiar, "Odmowa", color="red")
        self.show_buttons(primary_text=None, secondary_text=None)
        self.trigger_gate_and_log(False, self.last_promille)
        self.trigger_led(False)
        QtCore.QTimer.singleShot(3000, self.enter_idle)

    # ----- Ticki odliczania -----
    def on_identified_tick(self):
        """
        Okresowo sprawdzaj odległość w stanie IDENTIFIED_WAIT
        i aktualizuj komunikat. Gdy odległość jest w dobrym zakresie
        (i kamera widzi twarz), przejdź do pomiaru.
        """
        if self.state != "IDENTIFIED_WAIT":
            self._stop_timer(self.identified_timer)
            return

        dist_cm = self.read_distance_cm()
        imie = self.current_emp_name or ""
        if dist_cm == float("inf"):
            dist_txt = "brak odczytu"
        elif dist_cm > 80:
            dist_txt = ">80 cm"
        else:
            dist_txt = f"{dist_cm:0.0f} cm"

        # Jesteśmy w dobrym zakresie odległości
        if self.distance_min_cm <= dist_cm <= self.distance_max_cm:
            # Kamera widziała sensowną twarz (ustawiane w on_face_tick)
            if self.calibrate_good_face or self.fallback_pin_flag:
                self._stop_timer(self.identified_timer)
                tekst_gora = "Nie ruszaj się – start pomiaru"
                tekst_srodek = f"Cześć {imie}\nOdległość: {dist_txt}"
                self.set_message(tekst_gora, tekst_srodek, color="white")
                self.enter_measure()
                return
            else:
                # Jesteś blisko, ale twarz jeszcze nie jest OK
                self.set_message(
                    "Stań przodem do kamery",
                    f"Cześć {imie}\nOdległość: {dist_txt}",
                    color="white",
                )
                return

        # Poza zakresem – prosimy o podejście bliżej
        self.set_message(
            "Podejdź bliżej",
            f"Cześć {imie}\nOdległość: {dist_txt}",
            color="white",
        )

    def on_measure_tick(self):
        if self.state != "MEASURE":
            self._stop_timer(self.measure_timer)
            return

        # Odstęp czasowy między tickami
        try:
            dt = self.measure_timer.interval() / 1000.0
        except Exception:
            dt = 0.1

        # Odczyty z czujników
        dist_cm = self.read_distance_cm()
        amp, avg = self.read_mic_amplitude(samples=CONFIG.get("mic_amp_samples", 32))

        blow_detected = (
            self.distance_min_cm <= dist_cm <= self.distance_max_cm
            and amp >= self.mic_threshold
        )

        if blow_detected:
            self.blow_elapsed += dt
            # Pobierz próbkę z MQ-3 tylko podczas dmuchania
            try:
                self.measure_samples.append(self.mq3.read_raw())
            except Exception:
                pass

        remaining = CONFIG["measure_seconds"] - self.blow_elapsed
        if remaining < 0:
            remaining = 0.0

        # Aktualizacja paska postępu
        progress = max(0.0, min(self.blow_elapsed / CONFIG["measure_seconds"], 1.0))
        self.progress_bar.setValue(int(progress * 100))
        self.progress_bar.show()

        # Aktualizacja komunikatu zależnie od warunków
        if blow_detected:
            self.set_message("Przeprowadzam pomiar…", f"{remaining:0.1f} s", color="white")
        else:
            if not (self.distance_min_cm <= dist_cm <= self.distance_max_cm):
                self.set_message("Podejdź bliżej", f"{remaining:0.1f} s", color="white")
            else:
                self.set_message("Dmuchaj", f"{remaining:0.1f} s", color="white")

        # Zakończenie pomiaru po osiągnięciu wymaganego czasu nadmuchu
        if self.blow_elapsed >= CONFIG["measure_seconds"]:
            self._stop_timer(self.measure_timer)
            samples = list(self.measure_samples)

            def worker():
                try:
                    promille = float(self.mq3.promille_from_samples(samples))
                except Exception as e:
                    print(f"[MEASURE] błąd liczenia promili: {e}")
                    promille = 0.0
                self._pending_promille = promille
                QtCore.QMetaObject.invokeMethod(
                    self,
                    "_measure_done",
                    QtCore.Qt.QueuedConnection
                )

            threading.Thread(target=worker, daemon=True).start()


    # ----- Przycisk primary -----
    def on_btn_primary(self):
        if self.state == "RETRY":
            self.enter_measure()

    # ----- Przycisk secondary -----
    def on_btn_secondary(self):
        if self.state == "RETRY":
            self.set_message("Odmowa", "", color="red")
            self.trigger_gate_and_log(False, self.last_promille)
            self.trigger_led(False)
            self.show_buttons(primary_text=None, secondary_text=None)
            QtCore.QTimer.singleShot(2000, self.enter_idle)
            return

        if self.state in ("IDLE", "DETECT"):
            self.enter_pin_entry()

    # ----- Zbieranie próbek twarzy po PIN -----
    def collect_new_shots_for_current_emp(self):
        emp_id = self.current_emp_id
        if not emp_id:
            self.enter_idle()
            return

        ile_potrzeba = CONFIG["train_required_shots"]
        timeout_s = CONFIG["train_timeout_sec"]
        deadline = time.time() + timeout_s

        self.set_message(
            "Przytrzymaj twarz w obwódce",
            f"Zbieram próbki 0/{ile_potrzeba}",
            color="white",
        )
        self.show_buttons(primary_text=None, secondary_text=None)

        zapisane = 0
        lista_obrazow = []

        def tick():
            nonlocal zapisane, lista_obrazow, deadline

            if time.time() > deadline:
                self.last_face_bbox = None
                self.set_message(
                    "Nie udało się zebrać próbek",
                    "Spróbuj ponownie",
                    color="red",
                )
                QtCore.QTimer.singleShot(2000, self.enter_idle)
                return

            if self.frame_last_bgr is None:
                QtCore.QTimer.singleShot(80, tick)
                return

            klatka = self.frame_last_bgr
            szary = cv2.cvtColor(klatka, cv2.COLOR_BGR2GRAY)

            twarze = self.facedb._detect_faces(klatka)
            if not twarze:
                self.last_face_bbox = None
                self.set_message(
                    "Przytrzymaj twarz w obwódce",
                    f"Zbieram próbki {zapisane}/{ile_potrzeba}",
                    color="white",
                )
                QtCore.QTimer.singleShot(80, tick)
                return

            (x, y, w, h) = max(twarze, key=lambda r: r[2] * r[3])
            self.last_face_bbox = (x, y, w, h)
            self.last_confidence = 100.0

            h_img, w_img = szary.shape[:2]
            x1 = max(0, int(x))
            y1 = max(0, int(y))
            x2 = min(int(x + w), w_img)
            y2 = min(int(y + h), h_img)

            if x2 <= x1 or y2 <= y1:
                self.set_message(
                    "Przytrzymaj twarz w obwódce",
                    f"Zbieram próbki {zapisane}/{ile_potrzeba}",
                    color="white",
                )
                QtCore.QTimer.singleShot(80, tick)
                return

            if max(x2 - x1, y2 - y1) < CONFIG["face_min_size"]:
                self.set_message(
                    "Podejdź bliżej",
                    f"Zbieram próbki {zapisane}/{ile_potrzeba}",
                    color="white",
                )
                QtCore.QTimer.singleShot(80, tick)
                return

            roi_gray = szary[y1:y2, x1:x2]
            if roi_gray.size == 0:
                QtCore.QTimer.singleShot(80, tick)
                return

            roi_gray_resized = cv2.resize(
                roi_gray,
                (240, 240),
                interpolation=cv2.INTER_LINEAR,
            )

            ok, ostrosc, jasnosc = _face_quality(roi_gray_resized)
            if not ok:
                self.set_message(
                    "Stań prosto, popraw światło",
                    f"ostrość {ostrosc:0.0f}, jasność {jasnosc:0.0f}  [{zapisane}/{ile_potrzeba}]",
                    color="white",
                )
                QtCore.QTimer.singleShot(80, tick)
                return

            twarz_bgr = klatka[y1:y2, x1:x2].copy()
            twarz_bgr = cv2.resize(twarz_bgr, (240, 240), interpolation=cv2.INTER_LINEAR)
            lista_obrazow.append(twarz_bgr)
            zapisane += 1

            self.set_message(
                "Próbka zapisana",
                f"Zbieram próbki {zapisane}/{ile_potrzeba}",
                color="green",
            )

            if zapisane >= ile_potrzeba:
                self.facedb.add_three_shots(emp_id, lista_obrazow)
                self.training_start(post_action="DETECT_RETRY")
                return

            QtCore.QTimer.singleShot(120, tick)

        QtCore.QTimer.singleShot(80, tick)

    def training_start(self, post_action):
        """Uruchom reindeksację twarzy w osobnym wątku."""
        self.post_training_action = post_action

        self.set_message("Proszę czekać…", "Trening AI", color="white")
        self.show_buttons(primary_text=None, secondary_text=None)

        def worker():
            self.facedb.train_reindex()
            QtCore.QMetaObject.invokeMethod(
                self,
                "_training_done",
                QtCore.Qt.QueuedConnection
            )

        threading.Thread(target=worker, daemon=True).start()

    @QtCore.pyqtSlot()
    def _training_done(self):
        akcja = self.post_training_action
        self.post_training_action = None

        if akcja == "DETECT_RETRY":
            self.enter_detect_retry()
        else:
            self.enter_detect()

    def _log_to_mongo_async(self, ts, emp_id, emp_name, emp_pin, promille, pass_ok: bool):
        global _MONGO_CLIENT, _MONGO_DISABLED

        if MongoClient is None or _MONGO_DISABLED:
            return

        def worker():
            global _MONGO_CLIENT, _MONGO_DISABLED
            try:
                if _MONGO_CLIENT is None:
                    _MONGO_CLIENT = MongoClient(
                        CONFIG.get("mongo_uri"),
                        serverSelectionTimeoutMS=5000,  # 5 s na znalezienie primary
                        connectTimeoutMS=5000,
                        socketTimeoutMS=5000,
                    )

                db = _MONGO_CLIENT[CONFIG.get("mongodb_db_name", "alkotester")]
                col = db["entries"]
                doc = {
                    "datetime": ts,
                    "employee_id": emp_id,
                    "employee_name": emp_name,
                    "employee_pin": emp_pin,
                    "promille": float(promille),
                    "result": "pass" if pass_ok else "deny",
                    "fallback_pin": bool(self.fallback_pin_flag),
                }
                col.insert_one(doc)
            except Exception as e:
                print(f"[Mongo] wyłączam logowanie do Mongo po błędzie: {e}")
                _MONGO_DISABLED = True

        threading.Thread(target=worker, daemon=True).start()

    # ----- bramka + logi -----
    def trigger_gate_and_log(self, pass_ok: bool, promille: float):
        # Tryb Gość: nie logujemy nic do plików ani Mongo,
        # ale możemy nadal sterować bramką na podstawie wyniku.
        if self.is_guest:
            if pass_ok:
                GPIO.output(CONFIG["gate_gpio"], GPIO.HIGH)

                def pulse():
                    time.sleep(CONFIG["gate_pulse_sec"])
                    GPIO.output(CONFIG["gate_gpio"], GPIO.LOW)

                threading.Thread(target=pulse, daemon=True).start()
            # brak logów dla gościa
            return

        emp_name = self.current_emp_name or "<nieznany>"
        emp_id = self.current_emp_id or "<none>"
        ts = datetime.now().isoformat()


        # Obsługa bramki
        if pass_ok:
            GPIO.output(CONFIG["gate_gpio"], GPIO.HIGH)

            def pulse():
                time.sleep(CONFIG["gate_pulse_sec"])
                GPIO.output(CONFIG["gate_gpio"], GPIO.LOW)

            threading.Thread(target=pulse, daemon=True).start()

            log_csv(
                os.path.join(CONFIG["logs_dir"], "events.csv"),
                ["datetime", "event", "employee_name", "employee_id"],
                [ts, "gate_open", emp_name, emp_id]
            )
        else:
            log_csv(
                os.path.join(CONFIG["logs_dir"], "events.csv"),
                ["datetime", "event", "employee_name", "employee_id"],
                [ts, "deny_access", emp_name, emp_id]
            )

        # Zapisz pomiar do pliku – zawsze lokalny CSV
        log_csv(
            os.path.join(CONFIG["logs_dir"], "measurements.csv"),
            ["datetime", "employee_name", "employee_id", "promille", "fallback_pin"],
            [ts, emp_name, emp_id, f"{promille:.3f}", int(self.fallback_pin_flag)]
        )

        # PIN pracownika do logu w Mongo
        emp_pin = None
        try:
            entry = self.facedb.emp_by_id.get(self.current_emp_id or "")
            if entry:
                emp_pin = entry.get("pin")
        except Exception:
            emp_pin = None

        # Asynchroniczne logowanie do Mongo (nie blokuje RPi)
        self._log_to_mongo_async(ts, emp_id, emp_name, emp_pin, promille, pass_ok)

    # ----- sterowanie diodami LED -----
    def trigger_led(self, pass_ok: bool):
        """Zaświeć odpowiednią diodę LED przez zadany czas."""
        try:
            pin = CONFIG["led_pass_gpio"] if pass_ok else CONFIG["led_deny_gpio"]
            pulse_sec = float(CONFIG.get("led_pulse_sec", 2.0))
            GPIO.output(pin, GPIO.HIGH)

            def worker():
                try:
                    time.sleep(pulse_sec)
                finally:
                    GPIO.output(pin, GPIO.LOW)

            threading.Thread(target=worker, daemon=True).start()
        except Exception as e:
            print(f"[LED] Błąd sterowania diodą: {e}")

    # ----- pomocnicze odczyty z czujników -----
    def read_distance_cm(self) -> float:
        """Przekształć surowy odczyt z czujnika GP2Y0A21 na odległość w cm."""
        try:
            raw = self.adc.read_channel(self.distance_channel)
            # Zakładamy, że napięcie odniesienia wynosi 3.3 V
            voltage = (raw / 1023.0) * 3.3
            # Wzór przybliżony z dokumentacji czujnika GP2Y0A21 (dla zakresu 10–80 cm)
            if voltage - 0.42 <= 0:
                return float("inf")
            distance = 27.86 / (voltage - 0.42)
            # Ogranicz zakres do 0–80 cm
            if distance < 0 or distance > 80:
                return float("inf")
            return distance
        except Exception:
            return float("inf")

    def read_mic_amplitude(self, samples: int = 32):
        """
        Zwróć (amplituda, średnia) z kilku szybkich próbek z kanału mikrofonu.

        amplituda = max - min – prosta miara „głośności” sygnału.
        """
        try:
            n = max(1, int(samples))
            vals = [self.adc.read_channel(self.mic_channel) for _ in range(n)]
            vmin = min(vals)
            vmax = max(vals)
            avg = int(sum(vals) / len(vals))
            amp = vmax - vmin
            return amp, avg
        except Exception as e:
            print(f"[MIC] błąd odczytu: {e}")
            return 0, 0


    # ----- obsługa przycisku Gość -----
    def on_btn_guest(self):
        """
        Tryb Gość – pełny pomiar (odległość + dmuchanie),
        ale bez logowania wyników do CSV/Mongo.
        """
        # Ustaw dane gościa
        self.is_guest = True
        self.current_emp_id = "<guest>"
        self.current_emp_name = "Gość"

        # Gość nie wymaga rozpoznanej twarzy – traktujemy jak fallback
        # (on_identified_tick pozwoli wejść w pomiar bez kalibracji twarzy)
        self.fallback_pin_flag = True

        # Wejdź w standardowy stan IDENTIFIED_WAIT
        # (kamera i timery zostają, niczego ręcznie nie zatrzymujemy)
        self.enter_identified_wait()



    # ----- CAMERA TICK -----
    def on_camera_tick(self):
        frame_bgr = self.cam.get_frame_bgr()
        if frame_bgr is None:
            return

        self.frame_last_bgr = frame_bgr

        disp_bgr = frame_bgr.copy()

        if self.last_face_bbox is not None:
            (x, y, w, h) = self.last_face_bbox
            x1, y1, x2, y2 = int(x), int(y), int(x + w), int(y + h)

            if self.last_confidence >= CONFIG["recognition_conf_ok"]:
                kolor = (0, 255, 0)
            elif self.last_confidence <= CONFIG["recognition_conf_low"]:
                kolor = (0, 255, 255)
            else:
                kolor = (255, 255, 0)

            cv2.rectangle(disp_bgr, (x1, y1), (x2, y2), kolor, 2)
            napis = f"{self.last_confidence:.0f}%"
            cv2.putText(
                disp_bgr, napis,
                (x2 - 10, y2 - 10),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.7, kolor, 2, cv2.LINE_AA,
            )

        disp_rgb = cv2.cvtColor(disp_bgr, cv2.COLOR_BGR2RGB)

        target_w = self.view.width()
        target_h = self.view.height()
        fitted = self._crop_and_scale_fill(disp_rgb, target_w, target_h)
        if fitted is None:
            return

        h, w, _ = fitted.shape
        qimg = QtGui.QImage(fitted.data, w, h, 3 * w, QtGui.QImage.Format_RGB888)
        self.view.setPixmap(QtGui.QPixmap.fromImage(qimg))

    # ----- FACE TICK -----
    def on_face_tick(self):
        if self.frame_last_bgr is None:
            return

        emp_id, emp_name, conf, bbox = self.facedb.recognize_face(self.frame_last_bgr)

        self.last_face_bbox = bbox
        self.last_confidence = conf or 0.0

        if self.state == "IDLE":
            if bbox is not None:
                self.enter_detect()
            return

        if self.state == "DETECT":
            if bbox is None:
                self.detect_fail_count = 0
                self._stable_emp_id = None
                self._stable_count = 0
                self.set_message(now_str(), "Szukam twarzy…", color="white")
                return

            target_emp = emp_id if emp_id else None
            if target_emp is not None:
                if self._stable_emp_id == target_emp:
                    self._stable_count += 1
                else:
                    self._stable_emp_id = target_emp
                    self._stable_count = 1
            else:
                self._stable_emp_id = None
                self._stable_count = 0

            if (
                emp_name and
                conf >= CONFIG["recognition_conf_ok"] and
                self._stable_emp_id == emp_id and
                self._stable_count >= CONFIG["recognition_stable_ticks"]
            ):
                self.current_emp_id = emp_id
                self.current_emp_name = emp_name
                self.fallback_pin_flag = False

                self._online_learn_face(emp_id)

                self.enter_identified_wait()
                return

            self.detect_fail_count += 1
            if self.detect_fail_count >= CONFIG["detect_fail_limit"]:
                self.enter_pin_entry()
                return

            if conf <= CONFIG["recognition_conf_low"]:
                self.set_message(now_str(), "Nie rozpoznaję…", color="white")
            else:
                self.set_message(now_str(), f"pewność: {conf:.0f}%", color="white")
            return

        if self.state == "DETECT_RETRY":
            self.detect_retry_count += 1
            if (
                emp_id == self.current_emp_id and
                conf >= CONFIG["recognition_conf_ok"]
            ):
                self.fallback_pin_flag = False
                self._online_learn_face(emp_id)
                self.enter_identified_wait()
                return

            if self.detect_retry_count >= CONFIG["detect_retry_limit"]:
                self.fallback_pin_flag = True
                self.enter_identified_wait()
                return

            txt_conf = f"{conf:.0f}%" if conf is not None else ""
            self.set_message(
                "Sprawdzam twarz…",
                f"{self.current_emp_name or ''} {txt_conf}",
                color="white",
            )
            return

        if self.state == "IDENTIFIED_WAIT":
            if self.last_face_bbox is not None:
                self.calibrate_seen_face = True
                (_, _, w, h) = self.last_face_bbox
                if max(w, h) >= CONFIG["face_min_size"]:
                    self.calibrate_good_face = True
            return


        return

    # ----- UI tick (zegarek) -----
    def on_ui_tick(self):
        if self.state in ("IDLE", "DETECT"):
            self.lbl_top.setText(now_str())
            self.lbl_top.setStyleSheet(
                "color:white; font-size:28px; font-weight:600;"
            )

    # ----- baseline MQ-3 -----
    def _calibrate_mq3_start(self):
        def worker():
            self.mq3.calibrate_baseline()
            QtCore.QMetaObject.invokeMethod(
                self,
                "_baseline_done",
                QtCore.Qt.QueuedConnection
            )

        threading.Thread(target=worker, daemon=True).start()

    @QtCore.pyqtSlot()
    def _baseline_done(self):
        self.enter_idle()

    # ----- zamykanie -----
    def closeEvent(self, e: QtGui.QCloseEvent):
        for t in [
            getattr(self, "measure_timer", None),
            getattr(self, "calibrate_timer", None),
            getattr(self, "identified_timer", None),
            getattr(self, "face_timer", None),
            getattr(self, "ui_timer", None),
            getattr(self, "cam_timer", None),
            getattr(self, "sync_timer", None),
        ]:
            try:
                if t and t.isActive():
                    t.stop()
            except Exception:
                pass

        try:
            self.cam.stop()
        except Exception:
            pass

        try:
            self.adc.close()
        except Exception:
            pass

        try:
            GPIO.cleanup()
        except Exception:
            pass

        for w in QtWidgets.QApplication.topLevelWidgets():
            if w is not self:
                try:
                    w.close()
                except Exception:
                    pass

        return super().closeEvent(e)


def setup_qt_env():
    """Ustaw zmienne środowiskowe wymagane przez Qt na Raspberry Pi."""
    os.environ.setdefault("DISPLAY", ":0")
    os.environ.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    os.environ.setdefault("QT_QPA_PLATFORM", "xcb")
    os.environ.setdefault("QT_OPENGL", "software")
    os.environ.setdefault("LIBGL_ALWAYS_SOFTWARE", "1")
    os.environ.setdefault("QT_XCB_GL_INTEGRATION", "none")


def main():
    setup_qt_env()

    app = QtWidgets.QApplication(sys.argv)
    win = MainWindow()

    if CONFIG["fullscreen"]:
        win.showFullScreen()
    else:
        win.resize(CONFIG["screen_width"], CONFIG["screen_height"])
        win.show()

    signal.signal(signal.SIGINT, signal.SIG_DFL)

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
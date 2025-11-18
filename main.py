#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import cv2
import time
import signal
import threading
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


def _face_quality(szare_roi):
    """
    Zwraca (ok, ostrość, jasność).
    """
    ostrosc = cv2.Laplacian(szare_roi, cv2.CV_64F).var()
    jasnosc = float(np.mean(szare_roi))
    ok = (
        ostrosc >= CONFIG["quality_min_sharpness"]
        and CONFIG["quality_min_brightness"] <= jasnosc <= CONFIG["quality_max_brightness"]
    )
    return ok, ostrosc, jasnosc


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        ensure_dirs()

        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        GPIO.setup(CONFIG["gate_gpio"], GPIO.OUT, initial=GPIO.LOW)

        self.adc = MCP3008(CONFIG["spi_bus"], CONFIG["spi_device"])
        self.mq3 = MQ3Sensor(
            self.adc,
            CONFIG["mq3_channel"],
            CONFIG["baseline_samples"],
            CONFIG["promille_scale"],
        )

        self.facedb = FaceDB(
            CONFIG["faces_dir"],
            CONFIG["index_dir"],
            CONFIG["employees_json"]
        )
        boot = CONFIG["bootstrap_employee"]
        self.facedb.ensure_employee_exists(boot["id"], boot["name"], boot["pin"])

        self.setWindowTitle("Alkotester – Raspberry Pi")

        if CONFIG["hide_cursor"]:
            self.setCursor(QtCore.Qt.BlankCursor)

        centralny = QtWidgets.QWidget()
        self.setCentralWidget(centralny)
        uklad_zew = QtWidgets.QVBoxLayout(centralny)
        uklad_zew.setContentsMargins(0, 0, 0, 0)
        uklad_zew.setSpacing(0)

        self.view = QtWidgets.QLabel()
        self.view.setAlignment(QtCore.Qt.AlignCenter)
        self.view.setStyleSheet("background:black;")
        self.view.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,
            QtWidgets.QSizePolicy.Expanding
        )
        uklad_zew.addWidget(self.view, 1)

        self.overlay = QtWidgets.QFrame()
        self.overlay.setFixedHeight(CONFIG["overlay_height_px"])
        self.overlay.setStyleSheet("background: rgba(0,0,0,110); color:white;")

        uklad_overlay = QtWidgets.QVBoxLayout(self.overlay)
        uklad_overlay.setContentsMargins(16, 12, 16, 12)
        uklad_overlay.setSpacing(8)

        self.lbl_top = QtWidgets.QLabel("")
        self.lbl_top.setStyleSheet("color:white; font-size:28px; font-weight:600;")
        uklad_overlay.addWidget(self.lbl_top)

        self.lbl_center = QtWidgets.QLabel("")
        self.lbl_center.setAlignment(QtCore.Qt.AlignCenter)
        self.lbl_center.setStyleSheet("color:white; font-size:36px; font-weight:700;")
        uklad_overlay.addWidget(self.lbl_center, 1)

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

        uklad_zew.addWidget(self.overlay, 0)

        self.state = "INIT"

        self.current_emp_id = None
        self.current_emp_name = None

        self.fallback_pin_flag = False

        self.last_face_bbox = None
        self.last_confidence = 0.0
        self.last_promille = 0.0

        self.frame_last_bgr = None

        self.detect_fail_count = 0
        self.detect_retry_count = 0

        self._stable_emp_id = None
        self._stable_count = 0

        self.identified_seconds_left = 0
        self.calibrate_seconds_left = 0

        self.calibrate_good_face = False
        self.calibrate_seen_face = False

        self.measure_deadline = 0.0
        self.measure_samples = []

        self.post_training_action = None

        self.cam = CameraManager(
            CONFIG["camera_main_size"][0],
            CONFIG["camera_main_size"][1],
            CONFIG["rotate_dir"],
        )

        self.cam_timer = QtCore.QTimer(self)
        self.cam_timer.timeout.connect(self.on_camera_tick)
        self.cam_timer.start(int(1000 / max(1, CONFIG["camera_fps"])))

        self.face_timer = QtCore.QTimer(self)
        self.face_timer.timeout.connect(self.on_face_tick)

        self.ui_timer = QtCore.QTimer(self)
        self.ui_timer.timeout.connect(self.on_ui_tick)

        self.identified_timer = QtCore.QTimer(self)
        self.identified_timer.timeout.connect(self.on_identified_tick)

        self.calibrate_timer = QtCore.QTimer(self)
        self.calibrate_timer.timeout.connect(self.on_calibrate_tick)

        self.measure_timer = QtCore.QTimer(self)
        self.measure_timer.timeout.connect(self.on_measure_tick)

        self.btn_primary.clicked.connect(self.on_btn_primary)
        self.btn_secondary.clicked.connect(self.on_btn_secondary)

        self.set_message(
            "Proszę czekać…",
            "Kalibracja czujnika MQ-3 w toku",
            color="white",
        )
        self.show_buttons(primary_text=None, secondary_text=None)

        self._calibrate_mq3_start()

    def _stop_timer(self, timer_obj: QtCore.QTimer):
        try:
            if timer_obj.isActive():
                timer_obj.stop()
        except Exception:
            pass

    def set_message(self, tekst_gora, tekst_srodek=None, color="white"):
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

    # --- stany FSM ---

    def enter_idle(self):
        self.state = "IDLE"
        self.current_emp_id = None
        self.current_emp_name = None
        self.fallback_pin_flag = False

        self.last_face_bbox = None
        self.last_confidence = 0.0
        self.detect_fail_count = 0
        self.detect_retry_count = 0
        self._stable_emp_id = None
        self._stable_count = 0

        self.identified_seconds_left = 0
        self.calibrate_seconds_left = 0
        self.calibrate_good_face = False
        self.calibrate_seen_face = False

        self.measure_deadline = 0.0
        self.measure_samples = []

        self.ui_timer.start(250)
        self.face_timer.start(CONFIG["face_detect_interval_ms"])
        self._stop_timer(self.identified_timer)
        self._stop_timer(self.calibrate_timer)
        self._stop_timer(self.measure_timer)

        self.set_message(now_str(), "Podejdź bliżej", color="white")
        self.show_buttons(primary_text=None, secondary_text="Wprowadź PIN")

    def enter_detect(self):
        self.state = "DETECT"
        self.detect_fail_count = 0
        self._stable_emp_id = None
        self._stable_count = 0

        self.ui_timer.start(250)
        self.face_timer.start(CONFIG["face_detect_interval_ms"])
        self._stop_timer(self.identified_timer)
        self._stop_timer(self.calibrate_timer)
        self._stop_timer(self.measure_timer)

        self.set_message(now_str(), "Szukam twarzy…", color="white")
        self.show_buttons(primary_text=None, secondary_text="Wprowadź PIN")

    def enter_pin_entry(self):
        self.state = "PIN_ENTRY"
        self._stop_timer(self.face_timer)
        self._stop_timer(self.ui_timer)
        self._stop_timer(self.identified_timer)
        self._stop_timer(self.calibrate_timer)
        self._stop_timer(self.measure_timer)

        dlg = KeypadDialog(self, title="Wprowadź PIN")
        if dlg.exec() == QtWidgets.QDialog.Accepted:
            pin = dlg.value()
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
        self.state = "DETECT_RETRY"
        self.detect_retry_count = 0

        self.face_timer.start(CONFIG["face_detect_interval_ms"])
        self._stop_timer(self.ui_timer)
        self._stop_timer(self.identified_timer)
        self._stop_timer(self.calibrate_timer)
        self._stop_timer(self.measure_timer)

        self.set_message("Sprawdzam twarz…", self.current_emp_name or "", color="white")
        self.show_buttons(primary_text=None, secondary_text=None)

    def enter_identified_wait(self):
        self.state = "IDENTIFIED_WAIT"
        self.identified_seconds_left = 5

        self.last_face_bbox = None
        self.last_confidence = 0.0

        self._stop_timer(self.face_timer)
        self._stop_timer(self.calibrate_timer)
        self._stop_timer(self.measure_timer)
        self._stop_timer(self.ui_timer)

        self.identified_timer.start(1000)

        tekst_gora = f"Cześć {self.current_emp_name or ''}"
        tekst_srodek = f"Za {self.identified_seconds_left} s zaczynamy pomiar"
        self.set_message(tekst_gora, tekst_srodek, color="white")
        self.show_buttons(primary_text=None, secondary_text=None)

    def enter_calibrate(self):
        self.state = "CALIBRATE"
        self.calibrate_seconds_left = 3
        self.calibrate_good_face = False
        self.calibrate_seen_face = False

        self.face_timer.start(CONFIG["face_detect_interval_ms"])
        self._stop_timer(self.ui_timer)
        self._stop_timer(self.identified_timer)
        self._stop_timer(self.measure_timer)

        self.calibrate_timer.start(1000)

        self.set_message(
            "Ustaw się prosto, podejdź bliżej",
            f"Start za {self.calibrate_seconds_left} s",
            color="white",
        )
        self.show_buttons(primary_text=None, secondary_text=None)

    def enter_measure(self):
        self.state = "MEASURE"
        self.measure_samples = []
        self.measure_deadline = time.time() + CONFIG["measure_seconds"]

        self.last_face_bbox = None
        self.last_confidence = 0.0

        self._stop_timer(self.face_timer)
        self._stop_timer(self.ui_timer)
        self._stop_timer(self.identified_timer)
        self._stop_timer(self.calibrate_timer)

        self.measure_timer.start(100)

        self.set_message(
            "Przeprowadzam pomiar…",
            f"{CONFIG['measure_seconds']:.1f} s",
            color="white",
        )
        self.show_buttons(primary_text=None, secondary_text=None)

    def enter_decide(self, promille):
        self.last_promille = promille
        tekst_pomiar = f"Pomiar: {promille:.3f} [‰]"

        self._stop_timer(self.face_timer)
        self._stop_timer(self.ui_timer)
        self._stop_timer(self.identified_timer)
        self._stop_timer(self.calibrate_timer)
        self._stop_timer(self.measure_timer)

        if promille <= CONFIG["threshold_pass"]:
            self.state = "DECIDE_PASS"
            self.set_message(tekst_pomiar, "Przejście otwarte", color="green")
            self.show_buttons(primary_text=None, secondary_text=None)
            self.trigger_gate_and_log(True, promille)
            QtCore.QTimer.singleShot(2500, self.enter_idle)
            return

        if promille < CONFIG["threshold_deny"]:
            self.state = "RETRY"
            self.set_message(
                tekst_pomiar,
                "Ponów pomiar",
                color="red",
            )
            self.show_buttons(primary_text="Ponów pomiar", secondary_text="Odmowa")
            return

        self.state = "DECIDE_DENY"
        self.set_message(tekst_pomiar, "Odmowa", color="red")
        self.show_buttons(primary_text=None, secondary_text=None)
        self.trigger_gate_and_log(False, promille)
        QtCore.QTimer.singleShot(3000, self.enter_idle)

    # --- ticki odliczania ---

    def on_identified_tick(self):
        if self.state != "IDENTIFIED_WAIT":
            self._stop_timer(self.identified_timer)
            return

        self.identified_seconds_left -= 1
        if self.identified_seconds_left > 0:
            tekst_gora = f"Cześć {self.current_emp_name or ''}"
            tekst_srodek = f"Za {self.identified_seconds_left} s zaczynamy pomiar"
            self.set_message(tekst_gora, tekst_srodek, color="white")
        else:
            self._stop_timer(self.identified_timer)
            self.enter_calibrate()

    def on_calibrate_tick(self):
        if self.state != "CALIBRATE":
            self._stop_timer(self.calibrate_timer)
            return

        self.calibrate_seconds_left -= 1
        if self.calibrate_seconds_left > 0:
            self.set_message(
                "Ustaw się prosto, podejdź bliżej",
                f"Start za {self.calibrate_seconds_left} s",
                color="white",
            )
            return

        self._stop_timer(self.calibrate_timer)

        try:
            self.on_face_tick()
        except Exception:
            pass

        if self.fallback_pin_flag:
            self.enter_measure()
            return

        if self.calibrate_good_face:
            self.enter_measure()
            return

        if self.calibrate_seen_face:
            self.fallback_pin_flag = True
            self.enter_measure()
            return

        self.enter_detect()

    def on_measure_tick(self):
        if self.state != "MEASURE":
            self._stop_timer(self.measure_timer)
            return

        ile_pozostalo = self.measure_deadline - time.time()
        self.measure_samples.append(self.mq3.read_raw())

        if ile_pozostalo > 0:
            self.set_message(
                "Przeprowadzam pomiar…",
                f"{ile_pozostalo:0.1f} s",
                color="white",
            )
        else:
            self._stop_timer(self.measure_timer)
            promille = self.mq3.promille_from_samples(self.measure_samples)
            self.enter_decide(promille)

    # --- przyciski overlay ---

    def on_btn_primary(self):
        if self.state == "RETRY":
            self.enter_measure()

    def on_btn_secondary(self):
        if self.state == "RETRY":
            self.set_message("Odmowa", "", color="red")
            self.trigger_gate_and_log(False, self.last_promille)
            self.show_buttons(primary_text=None, secondary_text=None)
            QtCore.QTimer.singleShot(2000, self.enter_idle)
            return

        if self.state in ("IDLE", "DETECT"):
            self.enter_pin_entry()

    # --- zbieranie próbek twarzy po PIN ---

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
                self.set_message("Nie udało się zebrać próbek", "Spróbuj ponownie", color="red")
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
                self.set_message("Przytrzymaj twarz w obwódce", f"Zbieram próbki {zapisane}/{ile_potrzeba}", color="white")
                QtCore.QTimer.singleShot(80, tick)
                return

            (x, y, w, h) = max(twarze, key=lambda r: r[2] * r[3])
            self.last_face_bbox = (x, y, w, h)
            self.last_confidence = 100.0

            if max(w, h) < CONFIG["face_min_size"]:
                self.set_message("Podejdź bliżej", f"Zbieram próbki {zapisane}/{ile_potrzeba}", color="white")
                QtCore.QTimer.singleShot(80, tick)
                return

            roi_gray = szary[y:y + h, x:x + w]
            roi_gray_resized = cv2.resize(roi_gray, (240, 240), interpolation=cv2.INTER_LINEAR)
            ok, ostrosc, jasnosc = _face_quality(roi_gray_resized)
            if not ok:
                self.set_message(
                    "Stań prosto, popraw światło",
                    f"ostrość {ostrosc:0.0f}, jasność {jasnosc:0.0f}  [{zapisane}/{ile_potrzeba}]",
                    color="white"
                )
                QtCore.QTimer.singleShot(80, tick)
                return

            twarz_bgr = klatka[y:y + h, x:x + w].copy()
            twarz_bgr = cv2.resize(twarz_bgr, (240, 240), interpolation=cv2.INTER_LINEAR)
            lista_obrazow.append(twarz_bgr)
            zapisane += 1

            self.set_message("Próbka zapisana", f"Zbieram próbki {zapisane}/{ile_potrzeba}", color="green")

            if zapisane >= ile_potrzeba:
                self.facedb.add_three_shots(emp_id, lista_obrazow)
                self.training_start(post_action="DETECT_RETRY")
                return

            QtCore.QTimer.singleShot(120, tick)

        QtCore.QTimer.singleShot(80, tick)

    def training_start(self, post_action):
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

    # --- bramka + logi ---

    def trigger_gate_and_log(self, pass_ok: bool, promille: float):
        emp_name = self.current_emp_name or "<nieznany>"
        emp_id = self.current_emp_id or "<none>"
        ts = datetime.now().isoformat()

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

        log_csv(
            os.path.join(CONFIG["logs_dir"], "measurements.csv"),
            ["datetime", "employee_name", "employee_id", "promille", "fallback_pin"],
            [ts, emp_name, emp_id, f"{promille:.3f}", int(self.fallback_pin_flag)]
        )

    # --- preview helpers ---

    def _crop_and_scale_fill(self, src_rgb, target_w, target_h):
        if target_w <= 0 or target_h <= 0:
            return None

        sh, sw, _ = src_rgb.shape

        skala_w = target_w / float(sw)
        skala_h = target_h / float(sh)
        skala = min(skala_w, skala_h, 1.0)

        nowe_w = int(sw * skala)
        nowe_h = int(sh * skala)

        if skala != 1.0:
            przeskalowany = cv2.resize(src_rgb, (nowe_w, nowe_h), interpolation=cv2.INTER_AREA)
        else:
            przeskalowany = src_rgb

        wynik = np.zeros((target_h, target_w, 3), dtype=src_rgb.dtype)
        x0 = (target_w - nowe_w) // 2
        y0 = (target_h - nowe_h) // 2
        wynik[y0:y0 + nowe_h, x0:x0 + nowe_w] = przeskalowany

        return wynik

    def _online_learn_face(self, emp_id: str):
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

            twarz_bgr = self.frame_last_bgr[fy:y2, fx:x2].copy()
            twarz_bgr = cv2.resize(twarz_bgr, (240, 240), interpolation=cv2.INTER_LINEAR)

            twarz_szara = cv2.cvtColor(twarz_bgr, cv2.COLOR_BGR2GRAY)
            ok_q, ostrosc, jasnosc = _face_quality(twarz_szara)
            if not ok_q:
                return

            self.facedb.add_online_face_sample(emp_id, twarz_bgr)

        except Exception:
            pass

    # --- CAMERA TICK ---

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

    # --- FACE TICK ---

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

        if self.state == "CALIBRATE":
            if self.last_face_bbox is not None:
                self.calibrate_seen_face = True
                (_, _, w, h) = self.last_face_bbox
                if max(w, h) >= CONFIG["face_min_size"]:
                    self.calibrate_good_face = True
            return

        return

    # --- UI tick (zegarek) ---

    def on_ui_tick(self):
        if self.state in ("IDLE", "DETECT"):
            self.lbl_top.setText(now_str())
            self.lbl_top.setStyleSheet(
                "color:white; font-size:28px; font-weight:600;"
            )

    # --- baseline MQ-3 ---

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

    # --- zamykanie ---

    def closeEvent(self, e: QtGui.QCloseEvent):
        for t in [
            getattr(self, "measure_timer", None),
            getattr(self, "calibrate_timer", None),
            getattr(self, "identified_timer", None),
            getattr(self, "face_timer", None),
            getattr(self, "ui_timer", None),
            getattr(self, "cam_timer", None),
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

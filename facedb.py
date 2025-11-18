# facedb.py
import os
import glob
import json
import cv2
import numpy as np
from datetime import datetime

from config import CONFIG

# Tylko YuNet (z katalogu models/)
_SCIEZKA_YUNET = CONFIG.get("yunet_model_path", "models/face_detection_yunet_2023mar.onnx")


class FaceDB:
    """
    Baza pracowników + zrzuty twarzy + indeks ORB + YuNet/Haar do detekcji.
    """

    def __init__(self, faces_dir, index_dir, employees_json):
        self.faces_dir = faces_dir
        self.index_dir = index_dir
        self.employees_json = employees_json

        self._load_employees()

        self._init_detectors()
        self.cascade = cv2.CascadeClassifier(self._find_haar())

        self.orb = cv2.ORB_create(nfeatures=1000)

        self.index = {}
        self._load_index()

    # ---------- Detektory twarzy ----------
    def _init_detectors(self):
        self._det_yunet = None

        try:
            if hasattr(cv2, "FaceDetectorYN_create") and os.path.exists(_SCIEZKA_YUNET):
                prog_score = float(CONFIG.get("yunet_score_thresh", 0.85))
                prog_nms = float(CONFIG.get("yunet_nms_thresh", 0.3))
                limit_top = int(CONFIG.get("yunet_top_k", 5000))

                # rozmiar wejścia ustawiamy później dynamicznie
                self._det_yunet = cv2.FaceDetectorYN_create(
                    _SCIEZKA_YUNET, "", (320, 320), prog_score, prog_nms, limit_top
                )
        except Exception:
            self._det_yunet = None

    def _detect_faces(self, obraz_bgr):
        """
        Zwraca listę ramek [(x,y,w,h), ...] w pikselach.
        Najpierw YuNet, potem Haar.
        """
        wys, szer = obraz_bgr.shape[:2]

        # --- YuNet ---
        if self._det_yunet is not None:
            try:
                self._det_yunet.setInputSize((szer, wys))
                _, twarze = self._det_yunet.detect(obraz_bgr)
                ramki = []
                if twarze is not None and len(twarze) > 0:
                    for det in twarze:
                        x, y, ww, hh = det[:4]
                        ramki.append((int(x), int(y), int(ww), int(hh)))
                if ramki:
                    return ramki
            except Exception:
                pass

        # --- Haar (fallback) ---
        try:
            szary = cv2.cvtColor(obraz_bgr, cv2.COLOR_BGR2GRAY)
            twarze = self.cascade.detectMultiScale(szary, 1.2, 5)
            return [(int(x), int(y), int(ww), int(hh)) for (x, y, ww, hh) in twarze]
        except Exception:
            return []

    # ---------- Init helpers ----------
    def _find_haar(self):
        """Znajdź haarcascade_frontalface_default.xml w typowych ścieżkach."""
        katalogi = []

        if hasattr(cv2, "data") and hasattr(cv2.data, "haarcascades"):
            katalogi.append(cv2.data.haarcascades)

        katalogi += [
            "/usr/share/opencv4/haarcascades/",
            "/usr/share/opencv/haarcascades/",
            "/usr/local/share/opencv4/haarcascades/",
            "./",
        ]

        nazwa = "haarcascade_frontalface_default.xml"
        for baza in katalogi:
            sciezka = os.path.join(baza, nazwa)
            if os.path.exists(sciezka):
                return sciezka

        return nazwa

    def _load_employees(self):
        with open(self.employees_json, "r", encoding="utf-8") as f:
            self.employees = json.load(f)

        self.emp_by_pin = {
            prac["pin"]: prac
            for prac in self.employees.get("employees", [])
            if "pin" in prac
        }
        self.emp_by_id = {
            (prac.get("id") or prac.get("name")): prac
            for prac in self.employees.get("employees", [])
        }

    def save_employees(self):
        with open(self.employees_json, "w", encoding="utf-8") as f:
            json.dump(self.employees, f, ensure_ascii=False, indent=2)
        self._load_employees()

    def ensure_employee_exists(self, emp_id: str, name: str, pin: str):
        jest = any((e.get("id") == emp_id) for e in self.employees["employees"])
        if not jest:
            self.employees["employees"].append({"id": emp_id, "name": name, "pin": pin})
            self.save_employees()
        os.makedirs(os.path.join(self.faces_dir, emp_id), exist_ok=True)

    # ---------- Zrzuty twarzy ----------
    def add_three_shots(self, emp_id: str, imgs_bgr_list):
        folder_prac = os.path.join(self.faces_dir, emp_id)
        os.makedirs(folder_prac, exist_ok=True)
        for obraz in imgs_bgr_list:
            nazwa = datetime.now().strftime("%Y%m%d_%H%M%S_%f") + ".jpg"
            sciezka_wyj = os.path.join(folder_prac, nazwa)
            cv2.imwrite(sciezka_wyj, obraz, [int(cv2.IMWRITE_JPEG_QUALITY), 90])

    def _enforce_faces_limit(self, emp_id: str, max_len: int):
        folder_prac = os.path.join(self.faces_dir, emp_id)
        pliki = sorted(glob.glob(os.path.join(folder_prac, "*.jpg")))
        nadmiar = len(pliki) - max_len
        if nadmiar > 0:
            for sciezka in pliki[0:nadmiar]:
                try:
                    os.remove(sciezka)
                except Exception:
                    pass

    def add_online_face_sample(self, emp_id: str, face_bgr_240):
        szary = cv2.cvtColor(face_bgr_240, cv2.COLOR_BGR2GRAY)
        _, deskryptory = self.orb.detectAndCompute(szary, None)
        if deskryptory is None or len(deskryptory) == 0:
            return False

        if emp_id not in self.index:
            self.index[emp_id] = []
        self.index[emp_id].append(deskryptory)

        max_len = CONFIG.get("online_max_samples_per_emp", 20)
        if len(self.index[emp_id]) > max_len:
            self.index[emp_id] = self.index[emp_id][-max_len:]

        folder_prac = os.path.join(self.faces_dir, emp_id)
        os.makedirs(folder_prac, exist_ok=True)
        nazwa = datetime.now().strftime("%Y%m%d_%H%M%S_%f") + ".jpg"
        sciezka = os.path.join(folder_prac, nazwa)
        cv2.imwrite(sciezka, face_bgr_240, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
        self._enforce_faces_limit(emp_id, max_len)

        self._save_index_for(emp_id, self.index[emp_id])
        return True

    # ---------- Index ORB ----------
    def _load_index(self):
        self.index = {}
        for prac in self.employees.get("employees", []):
            emp_id = prac.get("id") or prac.get("name")
            sciezka_npz = os.path.join(self.index_dir, f"{emp_id}.npz")
            if os.path.exists(sciezka_npz):
                try:
                    npz = np.load(sciezka_npz, allow_pickle=True)
                    self.index[emp_id] = list(npz.get("descriptors", []))
                except Exception:
                    self.index[emp_id] = []
            else:
                self.index[emp_id] = []

    def _save_index_for(self, emp_id: str, descriptors_list):
        os.makedirs(self.index_dir, exist_ok=True)
        np.savez_compressed(
            os.path.join(self.index_dir, f"{emp_id}.npz"),
            descriptors=np.array(descriptors_list, dtype=object)
        )

    def train_reindex(self, progress_callback=None):
        pracownicy = self.employees.get("employees", [])
        ile = len(pracownicy)
        for idx, prac in enumerate(pracownicy):
            emp_id = prac.get("id") or prac.get("name")
            folder_prac = os.path.join(self.faces_dir, emp_id)

            lista_desc = []
            for sciezka_obr in sorted(glob.glob(os.path.join(folder_prac, "*.jpg"))):
                obraz = cv2.imread(sciezka_obr)
                if obraz is None:
                    continue
                szary = cv2.cvtColor(obraz, cv2.COLOR_BGR2GRAY)
                twarze = self.cascade.detectMultiScale(szary, 1.2, 5)
                if len(twarze) > 0:
                    (x, y, w, h) = max(twarze, key=lambda r: r[2] * r[3])
                    roi = szary[y:y + h, x:x + w]
                else:
                    roi = szary
                roi = cv2.resize(roi, (240, 240), interpolation=cv2.INTER_LINEAR)
                _, desc = self.orb.detectAndCompute(roi, None)
                if desc is not None and len(desc) > 0:
                    lista_desc.append(desc)

            self.index[emp_id] = lista_desc
            self._save_index_for(emp_id, lista_desc)
            if progress_callback:
                progress_callback(idx + 1, ile)

    # ---------- Rozpoznawanie ----------
    def recognize_face(self, img_bgr):
        """
        Zwraca:
            (emp_id or None,
             display_name or None,
             confidence%,
             bbox=(x,y,w,h) or None)
        """
        szary = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)

        twarze = self._detect_faces(img_bgr)
        if not twarze:
            return None, None, 0.0, None

        (x, y, w, h) = max(twarze, key=lambda r: r[2] * r[3])

        H, W = szary.shape[:2]
        x = int(max(0, x))
        y = int(max(0, y))
        w = int(max(0, w))
        h = int(max(0, h))
        x2 = min(x + w, W)
        y2 = min(y + h, H)

        if x2 <= x or y2 <= y:
            return None, None, 0.0, (x, y, max(0, x2 - x), max(0, y2 - y))

        roi_szary = szary[y:y2, x:x2]
        if roi_szary.size == 0:
            return None, None, 0.0, (x, y, max(0, x2 - x), max(0, y2 - y))

        try:
            roi_szary = cv2.resize(roi_szary, (240, 240), interpolation=cv2.INTER_LINEAR)
        except cv2.error:
            return None, None, 0.0, (x, y, max(0, x2 - x), max(0, y2 - y))

        _, desc = self.orb.detectAndCompute(roi_szary, None)
        if desc is None or len(desc) == 0:
            return None, None, 0.0, (x, y, max(0, x2 - x), max(0, y2 - y))

        prog_ratio = CONFIG["recognition_ratio_thresh"]
        prog_min_match = CONFIG["recognition_min_match"]
        prog_margin = CONFIG["recognition_min_margin"]

        knn = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=False)

        najlepszy_emp = None
        najlepszy_wynik = 0
        drugi_wynik = 0

        for emp_id, lista_desc in self.index.items():
            wynik_emp = 0
            for dset in lista_desc:
                if dset is None or len(dset) == 0:
                    continue
                dopasowania = knn.knnMatch(desc, dset, k=2)
                for para in dopasowania:
                    if len(para) < 2:
                        continue
                    m1, m2 = para[0], para[1]
                    if m1.distance < prog_ratio * m2.distance:
                        wynik_emp += 1

            if wynik_emp > najlepszy_wynik:
                drugi_wynik, najlepszy_wynik, najlepszy_emp = najlepszy_wynik, wynik_emp, emp_id
            elif wynik_emp > drugi_wynik:
                drugi_wynik = wynik_emp

        if najlepszy_wynik < prog_min_match:
            return None, None, 0.0, (x, y, max(0, x2 - x), max(0, y2 - y))

        if (najlepszy_wynik - drugi_wynik) < prog_margin:
            return None, None, 0.0, (x, y, max(0, x2 - x), max(0, y2 - y))

        suma = max(1, najlepszy_wynik + drugi_wynik)
        pewnosc = min(100.0, 100.0 * (najlepszy_wynik / suma))

        pokaz_nazwa = None
        if najlepszy_emp:
            wpis = self.emp_by_id.get(najlepszy_emp)
            pokaz_nazwa = (wpis.get("name", najlepszy_emp) if wpis else najlepszy_emp)

        bw = max(0, x2 - x)
        bh = max(0, y2 - y)
        return najlepszy_emp, pokaz_nazwa, pewnosc, (x, y, bw, bh)

"""
Konfiguracja aplikacji Alkotester z rozszerzonymi funkcjami.

Ta konfiguracja bazuje na oryginalnych ustawieniach projektu i rozszerza je
o obsługę dodatkowych czujników (dystansu i mikrofonu) oraz sterowanie
dodatkowymi diodami LED sygnalizującymi wynik pomiaru.  Wartości te można
dostosować do konkretnej instalacji sprzętowej.
"""

# Oryginalna konfiguracja została zachowana w całości.  Nowe opcje zostały
# dodane na końcu słownika CONFIG.

CONFIG = {
    # --- ekran / UI ---
    "screen_width": 720,
    "screen_height": 1280,
    "overlay_height_px": 220,
    "fullscreen": True,
    "hide_cursor": True,

    # --- kamera ---
    "camera_main_size": (1280, 720),  # (W,H) z sensora
    "camera_fps": 10,
    "rotate_dir": "cw",

    "yunet_model_path": "models/face_detection_yunet_2023mar.onnx",
    "yunet_score_thresh": 0.85,
    "yunet_nms_thresh": 0.3,
    "yunet_top_k": 5000,

    # --- rozpoznawanie twarzy ---
    "face_detect_interval_ms": 1000,
    "face_min_size": 120,
    "recognition_conf_ok": 55.0,
    "recognition_conf_low": 20.0,

    "detect_fail_limit": 5,
    "detect_retry_limit": 3,

    # --- jakość próbek treningowych po PIN ---
    "train_required_shots": 10,
    "train_timeout_sec": 15,
    "quality_min_sharpness": 60.0,
    "quality_min_brightness": 40.0,
    "quality_max_brightness": 210.0,

    # --- anty-false-positive ---
    "recognition_min_match": 65,
    "recognition_ratio_thresh": 0.75,
    "recognition_min_margin": 10,
    "recognition_stable_ticks": 2,

    # --- on-line uczenie twarzy ---
    "online_max_samples_per_emp": 40,

    # --- MQ-3 / MCP3008 ---
    "spi_bus": 0,
    "spi_device": 0,
    "mq3_channel": 0,
    "baseline_samples": 150,
    "promille_scale": 220.0,
    "measure_seconds": 3.0,

    # --- progi decyzji [‰] ---
    "threshold_pass": 0.2,
    "threshold_deny": 0.5,

    # --- przekaźnik bramki ---
    "gate_gpio": 18,
    "gate_pulse_sec": 5.0,

    # --- pliki danych na RPi ---
    "data_dir": "data",
    "faces_dir": "data/faces",
    "index_dir": "data/index",
    "employees_json": "data/employees.json",
    "logs_dir": "logs",

    # --- pracownik testowy ---
    "bootstrap_employee": {
        "id": "1",
        "name": "Kamil Karolak",
        "pin": "0000",
    },

    # --- logowanie do panelu admina (tylko dla /login na serwerze) ---
    "admin_username": "admin",
    "admin_password": "admin123",

    # --- MongoDB Atlas: logi pomiarów ---
    # UZUPEŁNIJ PRAWIDŁOWE HASŁO ZMIENIAJĄC <TWOJE_NOWE_HASLO>
    "mongo_uri": "mongodb+srv://kamilox123000_db_user:Mos9YrkkJYx5WW2a@alkotesterdb.bfua4dg.mongodb.net/alkotester?retryWrites=true&w=majority&appName=AlkotesterDB",
    "mongodb_db_name": "alkotester",

    # --- adres panelu na Render (master) ---
    # użyjesz tego później, jeśli zrobimy synchronizację employees.json po HTTP
    "server_base_url": "https://inz-di1v.onrender.com",
    "sync_token": "admin123",

    # Port dla lokalnego serwera admina (jeśli kiedyś włączysz na RPi)
    "admin_port": 5000,

    # === Dodatkowe czujniki i elementy sygnalizacyjne ===
    # Kanał MCP3008, do którego podłączony jest czujnik odległości GP2Y0A21.
    # Numeracja kanałów zaczyna się od 0. W instalacji referencyjnej MQ-3 jest na kanale 0,
    # więc odległości używamy kanału 1.
    "distance_channel": 1,
    # Kanał MCP3008 dla mikrofonu wykrywającego nadmuch powietrza (dmuchanie).
    "mic_channel": 2,
    # Minimalna i maksymalna odległość [cm], w której rozpoczyna się pomiar.
    "distance_min_cm": 8.0,
    "distance_max_cm": 20.0,
    # Minimalna wartość surowego odczytu z mikrofonu, którą traktujemy jako dmuchanie.
    # Wartość ta zależy od konkretnego mikrofonu – w razie potrzeby dostosuj.
    "mic_threshold": 150,
    # Numer GPIO dla zielonej diody LED (sygnalizacja pozytywnego wyniku).
    "led_pass_gpio": 24,
    # Numer GPIO dla czerwonej diody LED (sygnalizacja odmowy).
    "led_deny_gpio": 23,
    # Czas trzymania diody LED w stanie wysokim [s].
    "led_pulse_sec": 3.0,
}
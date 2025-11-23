CONFIG = {
    "screen_width": 720,
    "screen_height": 1280,
    "overlay_height_px": 240,
    "fullscreen": True,
    "hide_cursor": True,

    "camera_main_size": (1280, 720),  # (W,H) z sensora
    "camera_fps": 10,
    "rotate_dir": "cw",

    "yunet_model_path": "models/face_detection_yunet_2023mar.onnx",
    "yunet_score_thresh": 0.85,
    "yunet_nms_thresh": 0.3,
    "yunet_top_k": 5000,

    "face_detect_interval_ms": 1000,
    "face_min_size": 120,
    "recognition_conf_ok": 55.0,
    "recognition_conf_low": 20.0,

    "detect_fail_limit": 5,
    "detect_retry_limit": 3,

    "train_required_shots": 10,
    "train_timeout_sec": 15,
    "quality_min_sharpness": 60.0,
    "quality_min_brightness": 40.0,
    "quality_max_brightness": 210.0,

    "recognition_min_match": 65,
    "recognition_ratio_thresh": 0.75,
    "recognition_min_margin": 10,
    "recognition_stable_ticks": 2,

    "online_max_samples_per_emp": 40,

    # MCP3008
    "spi_bus": 0,
    "spi_device": 0,
    "mq3_channel": 0,
    "baseline_samples": 150,
    "promille_scale": 220.0,
    "measure_seconds": 2.0,

    "threshold_pass": 0.2,
    "threshold_deny": 0.5,

    "gate_gpio": 18,
    "gate_pulse_sec": 5.0,

    "data_dir": "data",
    "faces_dir": "data/faces",
    "index_dir": "data/index",
    "employees_json": "data/employees.json",
    "logs_dir": "logs",

    "bootstrap_employee": {
        "id": "1",
        "name": "Kamil Karolak",
        "pin": "0000",
    },

    "admin_username": "admin",
    "admin_password": "admin123",

    # --- MongoDB Atlas: logi pomiarów ---
    "mongo_uri": "",
    "mongodb_db_name": "alkotester",
    # "mongo_uri": "mongodb://localhost:27017",
    # "mongodb_db_name": "alkotester",

    "server_base_url": "https://inz-di1v.onrender.com",
    "sync_token": "admin123",

    "admin_port": 5000,

    "distance_channel": 1,
    "mic_channel": 2,
    "distance_min_cm": 8.0,
    "distance_max_cm": 20.0,
    "mic_threshold": 150,
    "led_pass_gpio": 24,
    "led_deny_gpio": 23,
    "led_pulse_sec": 3.0,
}
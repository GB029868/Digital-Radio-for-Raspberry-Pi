#!/usr/bin/env python3
"""Sweep the FM band and print the raw SI4689 RSQ reading for every frequency.

Use this when the FM scan returns a suspicious station count. It applies no
scoring, no clustering and no persistence, so it shows exactly what the chip
reports for each of the 206 frequencies the scan visits, plus the cluster
groups the scan logic would collapse them into.

Stop the service first or SPI access will collide:

    sudo systemctl stop raspiaudio-radio.service
    sudo python3 tools/fm_band_dump.py
    sudo systemctl start raspiaudio-radio.service
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from legacy.dab_radio_i2c_safe2 import Si468xDabRadio  # noqa: E402
from raspiaudio_radio.backend import _analog_score  # noqa: E402

PATCH = ROOT / "firmwares" / "rom00_patch.016.bin"
FMHD_FW = ROOT / "firmwares" / "fmhd_radio_5_3_3.bin"

FM_MIN_KHZ = 87_500
FM_MAX_KHZ = 108_000
FM_STEP_KHZ = 100

# Mirrors RadioConfig / configure_fmhd_frontend defaults.
FM_RSSI_MIN = 18
FM_SNR_MIN = 6
CLUSTER_WINDOW_KHZ = 200

XTAL_FREQ = 19_200_000
CTUN = 0x07
ANTCAP = 0
RST_PIN = 25
INT_PIN = 23
SPI_SPEED_HZ = 30_000_000


def probe(radio: Si468xDabRadio, freq_khz: int) -> Dict[str, Any]:
    """Replicates _wait_fm_signal_locked(require_hd=False) + _is_fm_analog_ready."""
    radio.fm_tune(freq_khz, antcap=ANTCAP, tune_mode=0)
    time.sleep(0.2)
    best: Optional[Dict[str, Any]] = None
    for _ in range(3):
        signal = radio.fm_rsq_status(attune=True)
        score = _analog_score(signal)
        if best is None or score > best["score"]:
            best = dict(signal)
            best["score"] = score
        time.sleep(0.05)
    signal = best or {}
    signal["pass"] = bool(
        signal.get("valid")
        or (int(signal.get("rssi", 0)) >= FM_RSSI_MIN and int(signal.get("snr", 0)) >= FM_SNR_MIN)
    )
    return signal


def local_maxima(scores: List[int]) -> int:
    peaks = 0
    for index, score in enumerate(scores):
        left = scores[index - 1] if index > 0 else -1
        right = scores[index + 1] if index + 1 < len(scores) else -1
        if score >= left and score > right:
            peaks += 1
    return peaks


def main() -> int:
    if not PATCH.exists() or not FMHD_FW.exists():
        print(f"Missing firmware under {PATCH.parent}", file=sys.stderr)
        return 1

    print("Booting SI4689 host-load with fmhd firmware...", flush=True)
    radio = Si468xDabRadio(
        rst_pin=RST_PIN,
        int_pin=INT_PIN,
        use_spi=True,
        spi_bus=0,
        spi_dev=0,
        spi_speed_hz=SPI_SPEED_HZ,
    )
    radio.reset()
    radio.power_up(xtal_freq=XTAL_FREQ, ctun=CTUN, retries=2)
    radio.load_patch_and_firmware(PATCH, FMHD_FW)
    radio.configure_fmhd_frontend()

    rows: List[Dict[str, Any]] = []
    freq = FM_MIN_KHZ
    while freq <= FM_MAX_KHZ:
        signal = probe(radio, freq)
        signal["freq_khz"] = freq
        rows.append(signal)
        if len(rows) % 25 == 0:
            print(f"  ...{len(rows)} frequencies measured", flush=True)
        freq += FM_STEP_KHZ

    try:
        radio.close()
    except Exception:
        pass

    print()
    print(f"{'freq':>9} {'rssi':>5} {'snr':>5} {'valid':>6} {'hd':>4} {'score':>6}  gate")
    print("-" * 50)
    for row in rows:
        print(
            f"{row['freq_khz'] / 1000:9.1f} "
            f"{int(row.get('rssi', 0)):5d} "
            f"{int(row.get('snr', 0)):5d} "
            f"{'yes' if row.get('valid') else '-':>6} "
            f"{'yes' if row.get('hd_detected') else '-':>4} "
            f"{int(row.get('score', 0)):6d}  "
            f"{'PASS' if row['pass'] else 'fail'}"
        )

    # Group contiguous passes exactly the way _scan_fm_clusters_locked does.
    runs: List[Dict[str, Any]] = []
    current: List[Dict[str, Any]] = []
    for row in rows:
        if row["pass"]:
            current.append(row)
        elif current:
            runs.append(_describe_run(current))
            current = []
    if current:
        runs.append(_describe_run(current))

    print()
    print("Contiguous PASS runs (each one collapses to a single station):")
    print(f"{'start':>7} {'end':>7} {'n':>4} {'peak':>7} {'peaks':>6}  note")
    print("-" * 60)
    for run in runs:
        note = []
        if run["span"] > CLUSTER_WINDOW_KHZ:
            note.append("span>200kHz, distance rule never fires")
        if run["peaks"] > 1:
            note.append(f"{run['peaks']} local maxima merge into 1 station")
        if run["contains_937"]:
            note.append("contains 93.7")
        print(
            f"{run['start'] / 1000:7.1f} {run['end'] / 1000:7.1f} "
            f"{run['count']:4d} {run['best'] / 1000:7.1f} {run['peaks']:6d}  "
            f"{'; '.join(note)}"
        )

    passed = sum(1 for row in rows if row["pass"])
    print()
    print(f"Total frequencies measured : {len(rows)}")
    print(f"Frequencies passing gate   : {passed}")
    print(f"Clusters produced (=stations): {len(runs)}")
    has_937 = any(row["freq_khz"] == 93_700 for row in rows if row["pass"])
    print(f"93.7 MHz in list           : {'yes' if has_937 else 'NO'}")

    merged = sum(1 for run in runs if run["peaks"] > 1)
    if merged:
        print()
        print(
            f"{merged} run(s) contain more than one signal peak but only their "
            "single highest-scoring frequency survives."
        )
    return 0


def _describe_run(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    scores = [int(row.get("score", 0)) for row in rows]
    best = max(rows, key=lambda row: int(row.get("score", 0)))
    return {
        "start": rows[0]["freq_khz"],
        "end": rows[-1]["freq_khz"],
        "count": len(rows),
        "span": rows[-1]["freq_khz"] - rows[0]["freq_khz"],
        "best": best["freq_khz"],
        "peaks": local_maxima(scores),
        "contains_937": any(row["freq_khz"] == 93_700 for row in rows),
    }


if __name__ == "__main__":
    raise SystemExit(main())

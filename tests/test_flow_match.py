"""Local parts of flow_match: case loading, input building, the profile update."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from flow_match import config as cfg
from flow_match import procedure as proc

CASE = "nt193793_polaris"


def test_case_loads_with_defaults():
    case = cfg.load_case(CASE)
    assert case.machine == "polaris"
    assert case.flow["param"] == "eq_flow_file"
    assert case.species["f3d_var"] == "i2u_para"
    assert case.job("polaris")["xgc_exe"].endswith("xgca")
    with pytest.raises(cfg.CaseError):
        case.job("nowhere")


def test_build_input_overrides_flow_file_and_mstep():
    case = cfg.load_case(CASE)
    text, diff, _notes = proc._build_input(case, "omg_test_it07.prf", mstep=20)
    assert "eq_flow_file='omg_test_it07.prf' 'omg_test_it07.prf' 'omg_test_it07.prf'" in text
    assert "sml_mstep=20" in text
    assert "sml_input_file_dir='../XGC_inputs/'" in text
    assert "omg_test_it07" in diff
    assert "n570pe_NT_xgc1" not in text.split("!")[0]


def _write_prf(path: Path, psi, val):
    proc.save_prf(path, psi, val)


def test_adjust_moves_input_toward_target_in_vtor(tmp_path, monkeypatch):
    case = cfg.load_case(CASE)
    monkeypatch.setattr(cfg.Case, "iterations_dir", property(lambda self: tmp_path / "iterations"))
    monkeypatch.setattr(cfg.Case, "log_path", property(lambda self: tmp_path / "log.yaml"))
    case.data["campaign"] = {"enabled": False}  # keep the unit test off the archive

    psi_in = np.linspace(0.0, 1.1, 111)
    omega_in = 4000.0 * np.ones_like(psi_in)
    current = tmp_path / "current.prf"
    proc.save_prf(current, psi_in, omega_in)

    # Geometry: R from 1.7 to 2.3 m, B_phi/B = 0.95. The run returned a toroidal
    # velocity 2 km/s below what the input omega implies (v = omega R (Bphi/B)^2).
    psi_sim = np.linspace(0.0, 1.12, 200)
    r_sim = 1.7 + 0.6 * psi_sim
    bratio = 0.95 * np.ones_like(psi_sim)
    v_input = 4000.0 * r_sim * bratio**2
    v_sim = v_input - 2000.0
    label = "it01"
    (tmp_path / "iterations" / label).mkdir(parents=True)
    with (tmp_path / "iterations" / label / f"midplane_{label}.json").open("w") as fh:
        json.dump({"midplane": {"psi_n": psi_sim.tolist(), "r": r_sim.tolist(), "bphi_over_b": bratio.tolist(), "vtor": v_sim.tolist(), "u_para": (v_sim / bratio).tolist()}}, fh)

    result = proc.adjust(case, label, current_prf=current)
    psi_new, omega_new = proc.load_prf(result["next_prf"])
    psi_t, v_t = proc.target_vtor(case)
    v_t_p = np.interp(psi_in, psi_t, v_t)
    r_p = np.interp(psi_in, psi_sim, r_sim)
    geom = r_p * 0.95**2
    # Expected: omega_new = omega + (v_target - v_sim) / (R (Bphi/B)^2)
    expected = (v_t_p - np.interp(psi_in, psi_sim, v_sim)) / geom
    inner = (psi_in > 0.1) & (psi_in < 0.9)
    assert np.allclose((omega_new - omega_in)[inner], expected[inner], rtol=0.15, atol=150.0)
    assert result["metrics"]["compare"] == "vtor"
    assert result["metrics"]["rms_delta_vtor"] > 0
    entries = cfg.read_log(case)
    assert entries[-1]["stage"] == "adjust"


def test_next_label_counts_up(tmp_path, monkeypatch):
    case = cfg.load_case(CASE)
    monkeypatch.setattr(cfg.Case, "iterations_dir", property(lambda self: tmp_path / "iterations"))
    monkeypatch.setattr(cfg.Case, "log_path", property(lambda self: tmp_path / "log.yaml"))
    assert cfg.next_label(case) == "it01"
    (tmp_path / "iterations" / "it03").mkdir(parents=True)
    assert cfg.next_label(case) == "it04"

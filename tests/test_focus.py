"""Focus-aid indicators (nightscribe-focus.py), tested with synthetic grids."""

import ast

import pytest

from conftest import FOCUS, load_focus


def test_profiles():
    ns = load_focus()
    grid = [[1.0, 2.0, 3.0], [1.0, 2.0, 3.0]]   # nx=2, ny=3
    assert ns["row_profile"](grid) == [1.0, 2.0, 3.0]
    assert ns["col_profile"](grid) == [2.0, 2.0]


def test_contrast():
    ns = load_focus()
    assert ns["contrast"]([5.0, 5.0, 5.0]) == 0.0
    assert ns["contrast"]([1.0, 3.0]) > 0.0


def test_gradient_energy():
    ns = load_focus()
    assert ns["gradient_energy"]([5.0, 5.0, 5.0]) == 0.0
    assert ns["gradient_energy"]([0.0, 10.0, 0.0, 10.0]) > 0.0


def test_count_peaks():
    ns = load_focus()
    assert ns["count_peaks"]([0.0, 1.0, 0.0, 1.0, 0.0]) == 2
    assert ns["count_peaks"]([0.0, 0.0, 0.0, 0.0]) == 0


def _grid(nx, ny, fn):
    """Grid[i][j] with i along X (slit) and j along Y (dispersion)."""
    return [[fn(i, j) for j in range(ny)] for i in range(nx)]


def test_detect_edge_vertical_is_flat():
    ns = load_focus()
    # Step along X at column 3, constant across rows -> vertical and straight.
    grid = _grid(5, 6, lambda i, j: 0.0 if i < 3 else 10.0)
    sharp, flat, orient = ns["detect_edge"](grid)
    assert orient == "vertical"
    assert flat == 0.0
    assert sharp > 0.0


def test_detect_edge_vertical_is_tilted():
    ns = load_focus()
    # The step position moves with the row -> vertical edge, not straight.
    grid = _grid(8, 5, lambda i, j: 0.0 if i < 2 + j else 10.0)
    sharp, flat, orient = ns["detect_edge"](grid)
    assert orient == "vertical"
    assert flat > 0.0


def test_detect_edge_horizontal():
    ns = load_focus()
    # Step along Y, constant across columns -> horizontal edge.
    grid = _grid(4, 5, lambda i, j: 0.0 if j < 3 else 10.0)
    sharp, flat, orient = ns["detect_edge"](grid)
    assert orient == "horizontal"


def test_compute_metrics_keys():
    ns = load_focus()
    grid = [[float(i + j) for j in range(6)] for i in range(8)]
    m = ns["compute_metrics"](grid)
    for key in ("line_contrast", "line_gradient", "detail_contrast",
                "detail_gradient", "detail_count", "edge_sharp", "edge_flat",
                "edge_orientation"):
        assert key in m


def test_frame_grid_with_stub():
    import sharpcap_stub
    ns = load_focus()
    roi = sharpcap_stub.ROI(width=200, height=100)
    frame = sharpcap_stub.Frame(mean=7.0, stddev=1.0)
    grid = ns["frame_grid"](frame, roi, 4, 2)
    assert len(grid) == 4 and len(grid[0]) == 2
    assert grid[0][0] == 7.0


def test_watcher_updates_state():
    import sharpcap_stub
    cam = sharpcap_stub.Camera(roi=sharpcap_stub.ROI(width=200, height=100))
    ns = load_focus(camera=cam)
    watcher = ns["FocusWatcher"](cam)
    watcher.start()
    # Feed one frame through the event; the watcher computes and holds it.
    cam.FrameCaptured.fire(sharpcap_stub.Frame(mean=5.0, stddev=1.0))
    watcher.stop()
    assert ns["state"]["latest"] is not None
    assert "line_contrast" in ns["state"]["latest"]


def test_focus_script_parses_as_python_3_4():
    src = open(FOCUS).read()
    ast.parse(src, filename=FOCUS, feature_version=(3, 4))
    assert not any(isinstance(n, ast.JoinedStr) for n in ast.walk(ast.parse(src)))

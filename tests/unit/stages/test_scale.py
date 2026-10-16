"""Scale stage and scale ladder, on synthetic scenes with a rendered marker of known size."""

from datetime import UTC, datetime

import cv2
import numpy as np
import pytest

from claimagent.core.context import RunContext
from claimagent.core.runner import Runner
from claimagent.core.settings import MarkerConfig
from claimagent.core.sweep import frame_name
from claimagent.domain.records import FrameInfo, Manifest
from claimagent.scale.ladder import choose
from claimagent.scale.marker import aruco_dictionary
from claimagent.scale.sources import CardScale, ScaleEstimate
from claimagent.stages.keyframes import KeyframesStage
from claimagent.stages.scale import ScaleStage
from claimagent.vision.geometry import local_scale, map_points, plane_distance
from claimagent.vision.imaging import write_jpeg

CFG = MarkerConfig()                 # 15 cm marker, DICT_4X4_50, id 0
PX_PER_CM = 10                       # resolution of the flat "wall" texture the scenes are made from
MARKER_AT = (50, 17.5)               # marker's top-left corner on the wall, in cm
STAGES = {"keyframes": KeyframesStage, "scale": ScaleStage}


def wall(with_marker: bool = True, seed: int = 0) -> np.ndarray:
    """An 80 x 50 cm textured wall at 10 px/cm, optionally with the marker on a white page."""
    rng = np.random.default_rng(seed)
    img = np.full((50 * PX_PER_CM, 80 * PX_PER_CM, 3), 90, np.uint8)
    for _ in range(500):
        x, y = rng.integers(0, img.shape[1] - 30), rng.integers(0, img.shape[0] - 30)
        cv2.rectangle(img, (int(x), int(y)), (int(x + rng.integers(8, 30)), int(y + rng.integers(8, 30))),
                      tuple(int(c) for c in rng.integers(0, 255, 3)), -1)
    if with_marker:
        side = round(CFG.size_cm * PX_PER_CM)
        x0, y0 = (round(v * PX_PER_CM) for v in MARKER_AT)
        margin = 2 * PX_PER_CM
        img[y0 - margin:y0 + side + margin, x0 - margin:x0 + side + margin] = 255  # white quiet zone
        marker = cv2.aruco.generateImageMarker(aruco_dictionary(CFG.dictionary), CFG.marker_id, side)
        img[y0:y0 + side, x0:x0 + side] = marker[..., None]
    return img


def tilted_camera(yaw_deg: float, size=(1280, 960), f=900.0, distance_cm=110.0) -> np.ndarray:
    """Homography mapping wall cm onto image pixels for a pinhole camera turned `yaw_deg` about the vertical."""
    a = np.radians(yaw_deg)
    rotation = np.array([[np.cos(a), 0, np.sin(a)], [0, 1, 0], [-np.sin(a), 0, np.cos(a)]])
    centre = np.array([40.0, 25.0])                          # aim at the wall's centre
    k = np.array([[f, 0, size[0] / 2], [0, f, size[1] / 2], [0, 0, 1]])
    t = np.array([0, 0, distance_cm]) - rotation @ np.array([centre[0], centre[1], 0])
    return k @ np.column_stack([rotation[:, 0], rotation[:, 1], t])


def render(cm_to_image: np.ndarray, texture: np.ndarray, size=(1280, 960)) -> np.ndarray:
    texture_to_image = cm_to_image @ np.diag([1 / PX_PER_CM, 1 / PX_PER_CM, 1])
    return cv2.warpPerspective(texture, texture_to_image, size, borderValue=(90, 90, 90))


def write_photos(ctx: RunContext, images: list[np.ndarray]) -> list[str]:
    """Replace the fixture sweep's frames with photos (keyframes keeps every photo)."""
    for path in ctx.sweep.frames_dir.glob("*.jpg"):
        path.unlink()
    infos = []
    for i, img in enumerate(images):
        name = frame_name(i, i * 0.25)
        write_jpeg(ctx.sweep.frame_path(name), img, 97)
        infos.append(FrameInfo(name=name, index=i, t=i * 0.25, source=f"{i}.heic", width=img.shape[1], height=img.shape[0]))
    ctx.sweep.write_manifest(Manifest(sweep_id="test", created_at=datetime.now(UTC), sources=["x"], fps=4, frames=infos))
    return [f.name for f in infos]


# ---- CardScale ----

def test_frontal_marker_gives_cm_per_px_within_1_percent():
    estimate = CardScale(CFG).estimate(wall())
    assert estimate is not None and estimate.method == "card"
    assert estimate.cm_per_px == pytest.approx(1 / PX_PER_CM, rel=0.01)
    assert estimate.confidence > 0.9


def test_marker_turned_30_degrees_measures_the_plane_within_3_percent():
    cm_to_image = tilted_camera(30)
    image = render(cm_to_image, wall())
    estimate = CardScale(CFG).estimate(image)
    assert estimate is not None
    h = np.asarray(estimate.image_to_plane)

    # A 60 cm line across the wall, mostly outside the marker, measured through the card's mapping.
    p, q = map_points(cm_to_image, np.array([[10.0, 45.0], [70.0, 45.0]]))
    assert plane_distance(h, tuple(p), tuple(q)) == pytest.approx(60.0, rel=0.03)

    # The reported cm/px matches the true scale at the marker's position.
    true_corners = map_points(cm_to_image, np.array(MARKER_AT) + np.array([[0, 0], [15, 0], [15, 15], [0, 15]]))
    assert estimate.cm_per_px == pytest.approx(local_scale(np.linalg.inv(cm_to_image), true_corners), rel=0.03)


def test_no_card_gives_no_estimate():
    assert CardScale(CFG).estimate(wall(with_marker=False)) is None


def test_marker_with_another_id_is_ignored():
    assert CardScale(MarkerConfig(marker_id=7)).estimate(wall()) is None


# ---- Stage ----

def test_stage_propagates_scale_to_a_zoomed_frame_without_the_card(ctx: RunContext):
    full = wall()
    zoom = 1.25
    # Second view: the left half of the wall, closer to the camera, so the card is out of view.
    left = cv2.resize(full[50:450, 0:400], None, fx=zoom, fy=zoom, interpolation=cv2.INTER_LINEAR)
    card_frame, other = write_photos(ctx, [full, left])

    out = Runner(ctx, STAGES)
    out.run_stage("keyframes")
    frames = {f["frame"]: f for f in out.run_stage("scale").data["frames"]}

    assert frames[card_frame]["method"] == "card" and frames[card_frame]["propagated_from"] is None
    carried = frames[other]
    assert carried["method"] == "card" and carried["propagated_from"] == card_frame
    assert carried["cm_per_px"] == pytest.approx(1 / PX_PER_CM / zoom, rel=0.05)
    assert carried["confidence"] < frames[card_frame]["confidence"]
    assert (ctx.sweep.debug_dir / f"scale_{card_frame}").exists()


def test_stage_without_card_reports_none_and_warns(ctx: RunContext):
    write_photos(ctx, [wall(with_marker=False), wall(with_marker=False, seed=1)])
    runner = Runner(ctx, STAGES)
    runner.run_stage("keyframes")
    out = runner.run_stage("scale")
    assert [f["method"] for f in out.data["frames"]] == ["none", "none"]
    assert all(f["cm_per_px"] is None for f in out.data["frames"])
    assert out.data["card_frames"] == 0
    assert any("no scale card" in w for w in out.warnings)


def test_unrelated_frame_is_not_given_a_scale(ctx: RunContext):
    _, unrelated = write_photos(ctx, [wall(), wall(with_marker=False, seed=5)])
    runner = Runner(ctx, STAGES)
    runner.run_stage("keyframes")
    out = runner.run_stage("scale")
    frames = {f["frame"]: f for f in out.data["frames"]}
    assert frames[unrelated]["method"] == "none"
    assert any("could not be linked" in w for w in out.warnings)


# ---- Ladder ----

def est(method, cm_per_px, confidence=0.8) -> ScaleEstimate:
    return ScaleEstimate(method=method, cm_per_px=cm_per_px, confidence=confidence)


def test_ladder_prefers_higher_rung_over_higher_confidence():
    choice = choose([est("depth", 0.1, 0.99), est("card", 0.1, 0.6)])
    assert choice.chosen.method == "card"


def test_ladder_agreement_raises_and_conflict_lowers_confidence():
    agreed = choose([est("card", 0.100), est("catalogue_height", 0.105)])
    assert agreed.chosen.confidence > 0.8 and agreed.agreed_with == ["catalogue_height"]

    conflict = choose([est("catalogue_height", 0.10), est("known_object", 0.13)])
    assert conflict.chosen.method == "catalogue_height"
    assert conflict.chosen.confidence < 0.8
    assert conflict.conflicts == ["known_object scale differs from catalogue_height by +30%"]


def test_ladder_with_nothing_chooses_nothing():
    assert choose([]).chosen is None

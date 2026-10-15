import cv2
import numpy as np
import pytest

from claimagent.core.settings import MarkerConfig
from claimagent.domain.records import BBox
from claimagent.scale.marker import DPI, aruco_dictionary, render_marker_page
from claimagent.vision.imaging import contact_sheet, crop, draw_boxes, encode_jpeg, resize_max, rotate90


def test_crop_with_padding_is_clipped():
    img = np.zeros((100, 200, 3), np.uint8)
    out = crop(img, BBox(x0=10, y0=10, x1=50, y1=90), pad=0.5)
    assert out.shape[:2] == (100, 70)  # y clipped to the image, x padded by 20 each side


def test_crop_outside_image_fails():
    with pytest.raises(ValueError):
        crop(np.zeros((10, 10, 3), np.uint8), BBox(x0=20, y0=20, x1=30, y1=30))


def test_rotate_and_resize():
    img = np.zeros((10, 30, 3), np.uint8)
    assert rotate90(img, 1).shape[:2] == (30, 10)
    assert resize_max(img, 15).shape[:2] == (5, 15)
    assert resize_max(img, 100) is img


def test_draw_boxes_does_not_modify_input():
    img = np.zeros((100, 100, 3), np.uint8)
    out = draw_boxes(img, [BBox(x0=10, y0=10, x1=60, y1=60)])
    assert img.sum() == 0 and out.sum() > 0


def test_contact_sheet_grid():
    sheet = contact_sheet([np.zeros((50, 100, 3), np.uint8)] * 5, thumb_width=40, columns=4)
    assert sheet.shape == (40, 160, 3)  # 2 rows of 20 px, 4 columns of 40 px


def test_encode_jpeg_is_decodable():
    data = encode_jpeg(np.full((20, 20, 3), 128, np.uint8), max_side=10)
    assert cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR).shape[:2] == (10, 10)


def test_printed_marker_is_detectable_at_its_stated_size():
    cfg = MarkerConfig(size_cm=15.0)
    page = np.asarray(render_marker_page(cfg))
    detector = cv2.aruco.ArucoDetector(aruco_dictionary(cfg.dictionary))
    corners, ids, _ = detector.detectMarkers(page)
    assert ids is not None and ids.flatten().tolist() == [cfg.marker_id]
    side_px = np.linalg.norm(corners[0][0][0] - corners[0][0][1])
    assert side_px / DPI * 2.54 == pytest.approx(15.0, rel=0.01)

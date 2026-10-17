"""Stage 4: Inventory. Count every book once, and give it a unit, shelf and position.

Detect boxes the spines in each keyframe separately, so a book seen in four keyframes appears
four times. This stage turns those per-frame boxes into one list of physical books:

  1. Reference frame: the keyframe with the most spines that are not cut off by the frame edge.
     All positions are expressed in its pixel coordinates (the "plane" of the shelving front).
  2. Link: every other keyframe is mapped onto the reference frame by a feature homography,
     directly or through an already linked keyframe. Keyframes that cannot be linked are left out
     of the count (counting them could count the same books twice) and reported for review.
  3. Clean each frame: two boxes in the same frame that overlap heavily (IoU >= 0.5) are one book
     boxed twice; the tighter box is kept.
  4. Merge across frames: starting from the reference frame's spines, each frame's mapped boxes
     are matched one-to-one to known books when they overlap enough (IoU >= 0.5, or mostly inside
     a known book, which catches partial boxes at frame edges). Boxes are compared only inside the
     area both frames can see, so an object cut off by one frame's edge still matches its full box
     in the other. Unmatched boxes start new books.
  5. Confirm: a book counts when the reference frame saw it, when it lies outside the reference
     frame's view (so only other frames could see it), or when at least two frames agree on it.
     A box seen once, inside the reference view, where the reference saw nothing, is more likely a
     detector mistake than a book: it goes to `unconfirmed` for review instead of into the count.
  6. Locate: units come from separate (not nested) shelving boxes in the reference frame (A, B, ...
     left to right). Books whose vertical extents overlap share a shelf (1 = top). Within a shelf,
     books are numbered left to right; flat books stacked on each other are numbered top first.
  7. Pick each book's best view (`frame_ref`) for reading its spine later: not cut off by the
     frame edge, agreeing with the merged box, and sharpest at a common size.

Non-book items are merged the same way (same category, looser overlap) but need no confirmation.
Within one frame, an item box nested inside a larger box of the same category is treated as the
same item (models often box the whole cupboard and also part of it); counting both would price it twice.
Output: books, items, unconfirmed boxes and unlinked frames; evidence/shelf_<unit><shelf>.jpg.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from string import ascii_uppercase

import cv2
import numpy as np
from pydantic import BaseModel

from claimagent.core.context import RunContext
from claimagent.core.stage import Stage, register
from claimagent.domain.records import BBox
from claimagent.stages.detect import DetectedObject, DetectOutput, FrameDetections
from claimagent.stages.keyframes import KeyframesOutput
from claimagent.vision.geometry import (containment, estimate_homography, intersect, iou, map_box, outside_share,
                                        overlap_fraction)
from claimagent.vision.imaging import crop, draw_boxes, read_image, write_jpeg

DUPLICATE_IOU = 0.5        # two spine boxes in one frame overlapping this much are the same book
MERGE_IOU = 0.5            # a mapped box joins a known book at this overlap ...
MERGE_CONTAINMENT = 0.7    # ... or when this share of the smaller box lies inside the other
ITEM_MERGE_IOU = 0.3       # items' boxes vary more between views than spines' boxes
NESTED_ITEM = 0.8          # an item box this much inside a larger same-category box in one frame is the same item
CONFIRM_VIEWS = 2          # frames needed to confirm a book the reference frame did not see
OUTSIDE_REFERENCE = 0.5    # share of a box outside the reference frame for it to count as "not visible there"
EDGE_MARGIN = 0.015        # a box this close to the frame border (share of the side) is cut off
MIN_LINK_OVERLAP = 0.1     # two keyframes must share at least this much to be linked
LINK_TRIES = 4             # linked keyframes tried per unlinked keyframe, nearest in time first
ROW_OVERLAP = 0.5          # books overlapping vertically by this share of the shorter one share a shelf
STACK_OVERLAP = 0.5        # flat books overlapping horizontally by this share of the narrower one form a stack
VIEW_AGREEMENT = 0.6       # a view whose box agrees this well with the merged box is a clean view of the book
VIEW_SIDE = 512            # views are compared for sharpness at this size
SHELF_PAD = 0.1            # annotated shelf images extend this share beyond the books


# ---- What this stage writes ----

class View(BaseModel):
    frame: str
    bbox: BBox                  # pixels of that frame
    truncated: bool             # cut off by the frame edge


class InventoryBook(BaseModel):
    book_id: str                # "<unit><shelf>-<position>", e.g. "A2-3"
    unit: str
    shelf: int                  # 1 = top
    position: int               # 1 = leftmost; a flat stack is numbered top first
    orientation: str            # "upright" or "flat"
    frame_ref: str              # best view, used to read the spine
    bbox: BBox                  # the book's box in frame_ref
    plane_box: BBox             # the book's box in reference-frame pixels
    views: list[View]
    annotated_ref: str | None   # evidence image that shows this book's number
    review: list[str]           # reasons a person should check this book


class InventoryItem(BaseModel):
    item_id: str                # "I1", "I2", ...
    category: str
    label: str
    material: str | None
    brand: str | None
    unit: str | None            # for shelving: the unit letter it holds
    frame_ref: str
    bbox: BBox
    plane_box: BBox
    views: list[View]


class Unconfirmed(BaseModel):
    frame: str
    bbox: BBox
    plane_box: BBox
    reason: str


class Shelf(BaseModel):
    unit: str
    shelf: int
    count: int
    annotated_ref: str | None


class InventoryOutput(BaseModel):
    reference_frame: str | None
    books: list[InventoryBook]
    items: list[InventoryItem]
    shelves: list[Shelf]
    unconfirmed: list[Unconfirmed]           # boxes not counted as books; for review
    unlinked_frames: list[str]               # keyframes left out of the count; for review


# ---- Merging (pure: works on boxes only, so it is tested without images) ----

@dataclass
class Observation:
    """One detected object in one frame, with its box mapped into the reference frame."""

    frame: str
    obj: DetectedObject
    plane_box: BBox
    truncated: bool
    footprint: BBox | None = None   # the observing frame's own outline, mapped into the reference frame


@dataclass
class Cluster:
    """One physical object, seen in one or more frames."""

    views: list[Observation] = field(default_factory=list)

    @property
    def first(self) -> Observation:
        return self.views[0]

    @property
    def plane_box(self) -> BBox:
        # The first view is the reference frame's when the reference saw it (it is merged first).
        return self.first.plane_box

    @property
    def frames(self) -> set[str]:
        return {v.frame for v in self.views}


def is_truncated(box: BBox, width: int, height: int, margin: float = EDGE_MARGIN) -> bool:
    mx, my = margin * width, margin * height
    return box.x0 <= mx or box.y0 <= my or box.x1 >= width - mx or box.y1 >= height - my


def drop_duplicates(spines: list[DetectedObject]) -> list[DetectedObject]:
    """Remove spines boxed twice in the same frame, keeping the tighter (smaller) box of each pair."""
    kept: list[DetectedObject] = []
    for spine in sorted(spines, key=lambda s: s.bbox.area):
        if all(iou(spine.bbox, k.bbox) < DUPLICATE_IOU for k in kept):
            kept.append(spine)
    return sorted(kept, key=lambda s: s.id)


def drop_nested_items(items: list[DetectedObject]) -> list[DetectedObject]:
    """Remove item boxes lying inside a larger box of the same category in the same frame."""
    kept: list[DetectedObject] = []
    for item in sorted(items, key=lambda o: -o.bbox.area):
        if all(k.category != item.category or containment(item.bbox, k.bbox) < NESTED_ITEM for k in kept):
            kept.append(item)
    return sorted(kept, key=lambda o: o.id)


def merge(frames: list[list[Observation]], compatible: Callable[[Observation, Observation], bool],
          min_iou: float) -> list[Cluster]:
    """Group observations of the same object across frames. `frames` lists each frame's observations,
    the reference frame first. Matching is one-to-one per frame, best overlap first."""
    clusters: list[Cluster] = []
    for observations in frames:
        pairs = []
        for i, obs in enumerate(observations):
            for j, cluster in enumerate(clusters):
                if obs.frame in cluster.frames or not compatible(obs, cluster.first):
                    continue
                shared = _shared_parts(obs, cluster.first)
                if shared is None:
                    continue
                overlap, inside = iou(*shared), containment(*shared)
                if overlap >= min_iou or inside >= MERGE_CONTAINMENT:
                    pairs.append((overlap, inside, i, j))
        used_obs, used_clusters = set(), set()
        for _, _, i, j in sorted(pairs, reverse=True):
            if i not in used_obs and j not in used_clusters:
                clusters[j].views.append(observations[i])
                used_obs.add(i)
                used_clusters.add(j)
        clusters.extend(Cluster([obs]) for i, obs in enumerate(observations) if i not in used_obs)
    return clusters


def _shared_parts(a: Observation, b: Observation) -> tuple[BBox, BBox] | None:
    """Both boxes clipped to the area both frames saw, or None if either box lies outside it."""
    if a.footprint is None or b.footprint is None:
        return a.plane_box, b.plane_box
    common = intersect(a.footprint, b.footprint)
    if common is None:
        return None
    clipped_a, clipped_b = intersect(a.plane_box, common), intersect(b.plane_box, common)
    return None if clipped_a is None or clipped_b is None else (clipped_a, clipped_b)


def same_orientation(a: Observation, b: Observation) -> bool:
    return a.obj.orientation == b.obj.orientation


def same_category(a: Observation, b: Observation) -> bool:
    return a.obj.category == b.obj.category


def unconfirmed_reason(cluster: Cluster, reference: str, ref_width: int, ref_height: int) -> str | None:
    """Why a spine cluster should not be counted as a book, or None if it should."""
    if reference in cluster.frames or len(cluster.frames) >= CONFIRM_VIEWS:
        return None
    if outside_share(cluster.plane_box, ref_width, ref_height) >= OUTSIDE_REFERENCE:
        return None  # the reference frame could not see it; another frame is the only witness
    return "seen in one frame only, where the reference frame shows no spine"


# ---- Locating (pure) ----

def assign_units(boxes: list[BBox], shelving: list[BBox]) -> tuple[list[str], list[BBox]]:
    """Unit letter for each book box, and the shelving box of each unit (left to right).

    Separate shelving units are shelving boxes that hold books and neither overlap nor contain each
    other (a model often boxes the whole cupboard and also a part of it). With fewer than two,
    everything is unit A.
    """
    units: list[BBox] = []
    for box in sorted(shelving, key=lambda b: -b.area):
        holds_books = any(_contains_centre(box, b) for b in boxes)
        if holds_books and all(iou(box, u) < 0.1 and containment(box, u) < 0.5 for u in units):
            units.append(box)
    if len(units) < 2:
        return ["A"] * len(boxes), units[:1]
    units.sort(key=lambda u: u.x0)

    def nearest(b: BBox) -> int:
        inside = [i for i, u in enumerate(units) if _contains_centre(u, b)]
        if inside:
            return inside[0]
        cx, cy = _centre(b)
        return min(range(len(units)), key=lambda i: np.hypot(cx - _centre(units[i])[0], cy - _centre(units[i])[1]))

    return [ascii_uppercase[nearest(b)] for b in boxes], units


def shelf_positions(boxes: list[BBox], flat: list[bool]) -> list[tuple[int, int]]:
    """(shelf, position) for each book of one unit; shelf 1 is the top, position 1 the leftmost."""
    n = len(boxes)
    parent = list(range(n))  # union-find: books overlapping vertically end up in the same group

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        for j in range(i + 1, n):
            overlap = min(boxes[i].y1, boxes[j].y1) - max(boxes[i].y0, boxes[j].y0)
            if overlap >= ROW_OVERLAP * min(boxes[i].height, boxes[j].height):
                parent[root(i)] = root(j)

    rows: dict[int, list[int]] = {}
    for i in range(n):
        rows.setdefault(root(i), []).append(i)
    ordered_rows = sorted(rows.values(), key=lambda r: np.mean([_centre(boxes[i])[1] for i in r]))

    result: dict[int, tuple[int, int]] = {}
    for shelf, row in enumerate(ordered_rows, start=1):
        # Columns left to right; flat books on top of each other share a column (a stack).
        columns: list[list[int]] = []
        for i in sorted(row, key=lambda i: _centre(boxes[i])[0]):
            last = columns[-1][-1] if columns else None
            if last is not None and flat[i] and flat[last]:
                overlap = min(boxes[i].x1, boxes[last].x1) - max(boxes[i].x0, boxes[last].x0)
                if overlap >= STACK_OVERLAP * min(boxes[i].width, boxes[last].width):
                    columns[-1].append(i)
                    continue
            columns.append([i])
        order = [i for column in columns for i in sorted(column, key=lambda i: boxes[i].y0)]
        for position, i in enumerate(order, start=1):
            result[i] = (shelf, position)
    return [result[i] for i in range(n)]


def _centre(box: BBox) -> tuple[float, float]:
    return (box.x0 + box.x1) / 2, (box.y0 + box.y1) / 2


def _contains_centre(outer: BBox, inner: BBox) -> bool:
    cx, cy = _centre(inner)
    return outer.x0 <= cx <= outer.x1 and outer.y0 <= cy <= outer.y1


# ---- Stage ----

@register
class InventoryStage(Stage):
    name = "inventory"
    order = 4
    needs = ("keyframes", "detect")
    output = InventoryOutput

    def run(self, ctx: RunContext, inputs: Mapping[str, BaseModel]) -> InventoryOutput:
        keyframes: KeyframesOutput = inputs["keyframes"]  # type: ignore[assignment]
        detect: DetectOutput = inputs["detect"]  # type: ignore[assignment]
        times = {k.frame: k.t for k in keyframes.keyframes}
        sharp = {k.frame: k.sharpness for k in keyframes.keyframes}
        frames = {f.frame: f for f in detect.frames if f.detector is not None}
        if not any(_spines(f) for f in frames.values()):
            ctx.warn("inventory: no spines were detected in any keyframe")
            return InventoryOutput(reference_frame=None, books=[], items=[], shelves=[], unconfirmed=[],
                                   unlinked_frames=[])

        reference = max(frames, key=lambda name: (_complete_spines(frames[name]), len(_spines(frames[name])),
                                                  sharp.get(name, 0.0)))
        images = {name: read_image(ctx.sweep.frame_path(name)) for name in frames}
        to_reference = link_frames(images, reference, times)
        unlinked = [name for name in frames if name not in to_reference]
        if unlinked:
            ctx.warn(f"inventory: {len(unlinked)} keyframe(s) could not be linked to the reference frame "
                     f"{reference} and were not counted: {', '.join(unlinked)}")

        # Observations per frame, the reference frame first (link order puts it first).
        spine_obs, item_obs = [], []
        for name, homography in to_reference.items():
            det = frames[name]
            spine_obs.append([_observe(name, o, homography, det) for o in drop_duplicates(_spines(det))])
            items = drop_nested_items([o for o in det.objects if o.kind == "item" and o.valid])
            item_obs.append([_observe(name, o, homography, det) for o in items])

        ref_det = frames[reference]
        clusters, unconfirmed = [], []
        for cluster in merge(spine_obs, same_orientation, MERGE_IOU):
            reason = unconfirmed_reason(cluster, reference, ref_det.width, ref_det.height)
            if reason is None:
                clusters.append(cluster)
            else:
                unconfirmed.append(Unconfirmed(frame=cluster.first.frame, bbox=cluster.first.obj.bbox,
                                               plane_box=cluster.plane_box, reason=reason))
        if unconfirmed:
            ctx.warn(f"inventory: {len(unconfirmed)} spine box(es) seen in a single frame were not counted; see `unconfirmed`")

        item_clusters = merge(item_obs, same_category, ITEM_MERGE_IOU)
        # Units come from the reference frame's own shelving boxes: boxes mapped in from other frames
        # are often cut off by those frames' edges and would split one cupboard into several.
        shelving = [v.plane_box for c in item_clusters for v in c.views
                    if c.first.obj.category == "shelving" and v.frame == reference]
        units, unit_boxes = assign_units([c.plane_box for c in clusters], shelving)

        books: list[InventoryBook] = []
        for unit in sorted(set(units)):
            members = [c for c, u in zip(clusters, units, strict=True) if u == unit]
            places = shelf_positions([c.plane_box for c in members], [c.first.obj.orientation == "flat" for c in members])
            for cluster, (shelf, position) in zip(members, places, strict=True):  # one place per book
                books.append(_book(cluster, unit, shelf, position, images, reference))
        books.sort(key=lambda b: (b.unit, b.shelf, b.position))

        shelves = self._write_shelves(ctx, books, images[reference], reference)
        items = [_item(n, c, images, unit_boxes) for n, c in enumerate(item_clusters, start=1)]
        return InventoryOutput(reference_frame=reference, books=books, items=items, shelves=shelves,
                               unconfirmed=unconfirmed, unlinked_frames=unlinked)

    @staticmethod
    def _write_shelves(ctx: RunContext, books: list[InventoryBook], ref_image: np.ndarray, reference: str) -> list[Shelf]:
        """evidence/shelf_<unit><shelf>.jpg: the reference frame cropped to one shelf, books numbered."""
        shelves = []
        height, width = ref_image.shape[:2]
        for old in ctx.sweep.evidence_dir.glob("shelf_*.jpg"):  # shelves from an earlier run may no longer exist
            old.unlink()
        for key in sorted({(b.unit, b.shelf) for b in books}):
            members = [b for b in books if (b.unit, b.shelf) == key]
            ref_path = None
            region = BBox(x0=min(b.plane_box.x0 for b in members), y0=min(b.plane_box.y0 for b in members),
                          x1=max(b.plane_box.x1 for b in members), y1=max(b.plane_box.y1 for b in members))
            if outside_share(region, width, height) < 1:
                drawn = draw_boxes(ref_image, [b.plane_box for b in members], [str(b.position) for b in members])
                name = f"shelf_{key[0]}{key[1]}.jpg"
                write_jpeg(ctx.sweep.evidence_dir / name, crop(drawn, region, pad=SHELF_PAD))
                ref_path = f"evidence/{name}"
            else:
                ctx.warn(f"inventory: shelf {key[0]}{key[1]} lies outside the reference frame {reference}; no annotated image")
            for b in members:
                b.annotated_ref = ref_path
            shelves.append(Shelf(unit=key[0], shelf=key[1], count=len(members), annotated_ref=ref_path))
        return shelves


def link_frames(images: dict[str, np.ndarray], reference: str, times: dict[str, float]) -> dict[str, np.ndarray]:
    """Homography from each frame to the reference frame, for every frame that can be linked.

    A frame is linked directly, or through an already linked frame (nearest in time first), so a
    long pan can be followed step by step. The result is ordered: reference first, then link order.
    """
    linked: dict[str, np.ndarray] = {reference: np.eye(3)}
    pending = [f for f in images if f != reference]
    failed: set[tuple[str, str]] = set()
    progress = True
    while pending and progress:
        progress = False
        for frame in list(pending):
            candidates = sorted(linked, key=lambda g: abs(times.get(g, 0.0) - times.get(frame, 0.0)))
            for other in [g for g in candidates if (frame, g) not in failed][:LINK_TRIES]:
                pair = estimate_homography(images[frame], images[other])
                if pair is None or overlap_fraction(pair, images[frame].shape, images[other].shape) < MIN_LINK_OVERLAP:
                    failed.add((frame, other))
                    continue
                linked[frame] = linked[other] @ pair
                pending.remove(frame)
                progress = True
                break
    return linked


def _spines(det: FrameDetections) -> list[DetectedObject]:
    return [o for o in det.objects if o.kind == "spine" and o.valid]


def _complete_spines(det: FrameDetections) -> int:
    return sum(not is_truncated(o.bbox, det.width, det.height) for o in _spines(det))


def _observe(frame: str, obj: DetectedObject, homography: np.ndarray, det: FrameDetections) -> Observation:
    outline = BBox(x0=0, y0=0, x1=det.width, y1=det.height)
    return Observation(frame=frame, obj=obj, plane_box=map_box(homography, obj.bbox),
                       truncated=is_truncated(obj.bbox, det.width, det.height), footprint=map_box(homography, outline))


def view_sharpness(image: np.ndarray, box: BBox) -> float:
    """Sharpness of a box's content, measured at a common size so near and far views compare fairly."""
    patch = crop(image, box)
    scale = VIEW_SIDE / max(patch.shape[:2])
    patch = cv2.resize(patch, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC)
    return float(cv2.Laplacian(cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var())


def _best_view(cluster: Cluster, images: dict[str, np.ndarray]) -> Observation:
    """Prefer views not cut off by the frame edge, that agree with the merged box, then the sharpest."""
    return max(cluster.views, key=lambda v: (not v.truncated, iou(v.plane_box, cluster.plane_box) >= VIEW_AGREEMENT,
                                             view_sharpness(images[v.frame], v.obj.bbox)))


def _views(cluster: Cluster) -> list[View]:
    return [View(frame=v.frame, bbox=v.obj.bbox, truncated=v.truncated) for v in cluster.views]


def _book(cluster: Cluster, unit: str, shelf: int, position: int, images: dict[str, np.ndarray], reference: str) -> InventoryBook:
    best = _best_view(cluster, images)
    review = []
    if all(v.truncated for v in cluster.views):
        review.append("only seen cut off at a frame edge")
    if all(v.obj.uncertain for v in cluster.views):
        review.append("detector was unsure this is a book")
    if reference not in cluster.frames:
        review.append("not visible in the reference frame")
    return InventoryBook(book_id=f"{unit}{shelf}-{position}", unit=unit, shelf=shelf, position=position,
                         orientation=cluster.first.obj.orientation or "upright", frame_ref=best.frame,
                         bbox=best.obj.bbox, plane_box=cluster.plane_box, views=_views(cluster),
                         annotated_ref=None, review=review)


def _item(n: int, cluster: Cluster, images: dict[str, np.ndarray], unit_boxes: list[BBox]) -> InventoryItem:
    best = _best_view(cluster, images)
    obj = cluster.first.obj
    unit = None
    if obj.category == "shelving" and unit_boxes:
        # The shelving item that defines a unit carries its letter; with one unit, any shelving is unit A.
        unit = "A" if len(unit_boxes) == 1 else next(
            (ascii_uppercase[i] for i, u in enumerate(unit_boxes) if iou(u, cluster.plane_box) >= 0.5), None)
    return InventoryItem(item_id=f"I{n}", category=obj.category or "other", label=obj.label, material=obj.material,
                         brand=obj.brand, unit=unit, frame_ref=best.frame, bbox=best.obj.bbox,
                         plane_box=cluster.plane_box, views=_views(cluster))

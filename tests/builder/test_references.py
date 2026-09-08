"""R-26 intake inputs, R-27 originals preserved and versioned, regions in original space, R-16 probe image."""
from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from builder.ids import sha256_file
from builder.project import Project
from builder.references import References, make_probe_image


@pytest.fixture
def project(workdir):
    prj = Project.create(workdir / "wf", name="Refs", asset_name="Lamp")
    yield prj
    prj.close()


@pytest.fixture
def concept(workdir):
    p = workdir / "concept ü.png"
    img = Image.new("RGB", (640, 480), (200, 200, 200))
    for x in range(100, 300):
        for y in range(150, 400):
            img.putpixel((x, y), (20, 40, 200))
    img.save(p)
    return p


def test_add_copies_original_unmodified_and_records_dimensions(project, concept):
    before = sha256_file(concept)
    refs = References(project)
    ref = refs.add(concept, labels=["front", "three-quarter"], kind="target", notes="lower-left figure")
    assert ref.data["sha256"] == before and sha256_file(concept) == before
    assert ref.data["width"] == 640 and ref.data["height"] == 480 and ref.data["version"] == 1
    assert ref.data["labels"] == ["front", "three-quarter"] and ref.data["kind"] == "target"
    copy = Path(ref.data["file"])
    assert copy.parent == project.path("refs") and sha256_file(copy) == before
    assert ref.data["original_path"] == str(concept)


def test_replace_creates_new_evidence_version(project, concept, workdir):
    refs = References(project)
    v1 = refs.add(concept, labels=["front"])
    p2 = workdir / "concept v2.png"
    Image.new("RGB", (800, 600), (10, 10, 10)).save(p2)
    v2 = refs.add(p2, labels=["front"], replaces_id=v1.id)
    assert v2.data["version"] == 2 and v2.data["replaces_id"] == v1.id
    assert [r.id for r in refs.current()] == [v2.id]
    assert project.store.get("reference", v1.id) is not None  # history kept


def test_kind_labels_and_composite_target_region_rule(project, concept):
    refs = References(project)
    with pytest.raises(ValueError):
        refs.add(concept, labels=["front"], kind="whatever")
    sheet = refs.add(concept, labels=["front"], kind="target", composite=True)
    ready, reasons = refs.intake_ready()
    assert not ready and any("target region" in r for r in reasons)
    refs.add_region(sheet.id, "lower-left humanoid", [100, 150, 200, 250], purpose="target_region")
    ready, reasons = refs.intake_ready()
    assert ready and reasons == []


def test_region_validation_is_in_original_pixel_space(project, concept):
    refs = References(project)
    ref = refs.add(concept, labels=["front"])
    with pytest.raises(ValueError):
        refs.add_region(ref.id, "off", [600, 400, 100, 100])
    with pytest.raises(ValueError):
        refs.add_region(ref.id, "neg", [-1, 0, 10, 10])
    with pytest.raises(ValueError):
        refs.add_region(ref.id, "bad purpose", [0, 0, 10, 10], purpose="thing")
    reg = refs.add_region(ref.id, "cap", [100, 150, 200, 250], purpose="detail_crop")
    assert reg.data["bbox"] == [100, 150, 200, 250] and reg.data["reference_id"] == ref.id
    assert reg.data["space"] == "original_pixels"


def test_crop_at_native_resolution_never_touches_original(project, concept, workdir):
    refs = References(project)
    ref = refs.add(concept, labels=["front"])
    before = sha256_file(Path(ref.data["file"]))
    out = workdir / "crop ü.png"
    info = refs.crop(ref.id, [100, 150, 200, 250], out)
    assert info["width"] == 200 and info["height"] == 250 and info["bbox"] == [100, 150, 200, 250]
    assert info["source_sha256"] == before and Path(info["path"]).is_file()
    with Image.open(out) as im:
        assert im.size == (200, 250) and im.getpixel((5, 5)) == (20, 40, 200)
    assert sha256_file(Path(ref.data["file"])) == before


def test_generated_studies_are_hypotheses(project, concept):
    refs = References(project)
    hyp = refs.add(concept, labels=["side"], kind="hypothesis")
    assert hyp.data["kind"] == "hypothesis" and hyp.data["evidence_of_original"] is False
    target = refs.add(concept, labels=["front"], kind="target")
    assert target.data["evidence_of_original"] is True


def test_probe_image_has_known_content(workdir):
    path = workdir / "probe ü.png"
    expected = make_probe_image(path, shape="triangle", color="red", number=7)
    assert expected == {"shape": "triangle", "color": "red", "number": 7}
    with Image.open(path) as im:
        w, h = im.size
        assert im.getpixel((w // 2, h // 2))[:3] == (220, 30, 30)
        assert im.getpixel((3, 3))[:3] == (255, 255, 255)

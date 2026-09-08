"""The manual image seat (addendum D12, R-96, R-109, R-110): prompts out as PROMPT.md with the attachment list, no
generation on its own, three preflight tiers with live 'not applicable', a folder watcher for `--watch`, and the
seat contract kept free of any key handling so an API seat can be added later with keys from the environment only."""
from __future__ import annotations

import re
import threading
import time
from pathlib import Path

import pytest
from PIL import Image

from builder.ids import sha256_file
from builder.providers.imagegen.base import ImageSeat, watch_folder
from builder.providers.imagegen.manual import ManualImageSeat
from builder.records import NOT_APPLICABLE


@pytest.fixture
def png(workdir):
    p = workdir / "anchor ü.png"
    Image.new("RGB", (64, 48), (10, 200, 30)).save(p)
    return p


def test_seat_contract_and_declarations():
    seat = ManualImageSeat(vendor="gemini", model="Nano Banana (as shown)")
    assert isinstance(seat, ImageSeat) and seat.name == "manual" and seat.label == "I"
    d = seat.declared()
    assert d["seat"] == "manual" and d["vendor"] == "gemini" and d["model"] == "Nano Banana (as shown)"
    assert "declared" in d["provenance"]           # never "verified"
    assert seat.cost_quantity() is NOT_APPLICABLE  # R-105: not applicable, never zero
    assert seat.generate({"request_id": "gen_x"}) is None   # the owner generates; nothing automated
    assert ManualImageSeat().declared()["model"].startswith("as declared")


def test_publish_request_writes_prompt_md_with_attachments_and_import_line(workdir, png):
    seat = ManualImageSeat(vendor="chatgpt")
    rdir = workdir / "concept" / "requests" / "gen_TEST1"
    prompt = "A compact hexapod maintenance drone, matte olive panels, brass fittings.\nFront three-quarter, neutral grey."
    info = seat.publish_request(rdir, request_id="gen_TEST1", target={"kind": "view", "view": "side"}, prompt=prompt,
                                attachments=[{"reference_id": "ref_A", "path": str(png), "sha256": sha256_file(png),
                                              "role": "anchor"}],
                                expected_count=1, import_command="python -m builder concept import <wf> gen_TEST1 <file>...")
    text = Path(info["prompt_file"]).read_text(encoding="utf-8")
    assert info["prompt_file"].endswith("PROMPT.md") and Path(info["prompt_file"]).parent == rdir
    assert "gen_TEST1" in text and prompt in text and "side" in text
    attached = Path(info["attachments"][0]["copy"])
    assert attached.is_file() and attached.parent == rdir / "attach" and sha256_file(attached) == sha256_file(png)
    assert str(attached) in text and sha256_file(png)[:12] in text
    assert "concept import" in text and "gen_TEST1" in text
    assert re.search(r"declar", text)              # vendor/model are declarations, said so in the file
    assert "\r\n" not in Path(info["prompt_file"]).read_bytes().decode("utf-8")


def test_preflight_tiers_for_the_manual_seat(workdir):
    seat = ManualImageSeat(vendor="chatgpt")
    report = seat.preflight(import_dir=workdir / "imports ü", flow_confirmed=False)
    assert report["declared"]["seat"] == "manual" and report["declared"]["vendor"] == "chatgpt"
    assert report["tiers"]["declared"] == "manual"
    assert report["tiers"]["local"] == "not_confirmed" and "flow" in report["local"]["evidence"]
    assert report["local"]["import_dir_writable"] is True and (workdir / "imports ü").is_dir()
    assert report["tiers"]["live"] == "not_applicable"
    assert "declared by the owner" in report["live"]["evidence"] and "both LLM seats" in report["live"]["evidence"]
    ok = seat.preflight(import_dir=workdir / "imports ü", flow_confirmed=True)
    assert ok["tiers"]["local"] == "ok" and ok["ok"] is True
    blocked_path = workdir / "a file"
    blocked_path.write_text("x", encoding="utf-8")
    bad = seat.preflight(import_dir=blocked_path, flow_confirmed=True)
    assert bad["tiers"]["local"] == "missing" and bad["ok"] is False and bad["local"]["import_dir_writable"] is False
    # nothing under the seat's work was left behind by the write probe
    assert not list((workdir / "imports ü").iterdir())


def test_watch_folder_imports_new_files_once_and_stops(workdir):
    folder = workdir / "drop ü"
    folder.mkdir()
    (folder / "old.png").write_bytes(b"old")
    seen: list[Path] = []
    stop = threading.Event()

    def on_file(p: Path) -> None:
        seen.append(p)
        if len(seen) == 2:
            stop.set()

    def dropper():
        time.sleep(0.2)
        (folder / "new one.png").write_bytes(b"n1")
        time.sleep(0.2)
        (folder / "new two.PNG").write_bytes(b"n2")
        (folder / "notes.txt").write_bytes(b"ignored")

    threading.Thread(target=dropper, daemon=True).start()
    result = watch_folder(folder, on_file, poll_s=0.05, stop=stop, timeout_s=5.0, ignore_existing=True)
    assert [p.name for p in seen] == ["new one.png", "new two.PNG"]
    assert result["imported"] == 2 and result["stopped_by"] == "stop"
    timed = watch_folder(folder, on_file, poll_s=0.05, stop=threading.Event(), timeout_s=0.15, ignore_existing=True)
    assert timed["stopped_by"] == "timeout" and len(seen) == 2


def test_seat_modules_handle_no_keys_and_document_the_environment_only_rule():
    import builder.providers.imagegen.base as base
    import builder.providers.imagegen.manual as manual

    for mod in (base, manual):
        src = Path(mod.__file__).read_text(encoding="utf-8")
        assert "os.environ" not in src and "getenv" not in src and "api_key" not in src.lower().replace("api key", "api_key")
    assert ImageSeat.SECRETS_POLICY == "environment_only_never_in_argv_records_logs_or_packets"

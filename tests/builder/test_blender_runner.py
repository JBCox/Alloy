"""Section 5 Blender runner contract: argv, --python-exit-code, result protocol, deadlines, Unicode paths (layer B)."""
from __future__ import annotations

import textwrap
import time

import pytest

from builder.blender.runner import SCRIPTS_DIR, BlenderRunner

pytestmark = pytest.mark.blender


@pytest.fixture
def runner(blender_exe, workdir):
    return BlenderRunner(blender_exe, logs_dir=workdir / "logs", deadlines={"validate": 60, "apply": 120, "render": 180,
                                                                            "measure": 60, "fixture": 180})


def _script(workdir, name, body):
    p = workdir / f"{name} ü.py"
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


def test_argv_is_a_list_with_exit_code_flag_and_no_shell(runner, workdir):
    argv = runner.argv(SCRIPTS_DIR / "smoke.py", workdir / "args ü.json", blend=workdir / "a b.blend")
    assert argv[0].lower().endswith("blender.exe")
    assert "-b" in argv and "--factory-startup" in argv and "-noaudio" in argv
    i = argv.index("--python-exit-code")
    assert argv[i + 1] == "3"
    assert argv.index("--python") > argv.index(str(workdir / "a b.blend"))
    assert argv[-2] == "--" and argv[-1] == str(workdir / "args ü.json")


def test_smoke_reports_versions_and_engines(runner):
    info = runner.smoke()
    assert info["blender_version"].startswith("5.")
    assert info["python_version"].startswith("3.13")
    assert "BLENDER_WORKBENCH" in info["engines_ok"] and "BLENDER_EEVEE" in info["engines_ok"]
    assert "BLENDER_WORKBENCH" in info["engines_listed"]


def test_exception_in_script_is_failed_with_exit_code_3(runner, workdir):
    script = _script(workdir, "boom", """
        import sys, json, os
        argv = sys.argv[sys.argv.index('--') + 1:]
        raise RuntimeError('boom ünï')
    """)
    res = runner.run_script(script, {"x": 1}, op_id="op_boom", deadline_s=60)
    assert res.outcome == "failed" and res.exit_code == 3
    assert "RuntimeError" in res.error and "boom" in res.error
    assert res.result is None


def test_exit_zero_without_result_is_uncertain_never_success(runner, workdir):
    script = _script(workdir, "silent", "print('did nothing')\n")
    res = runner.run_script(script, {}, op_id="op_silent", deadline_s=60)
    assert res.outcome == "uncertain" and res.exit_code == 0
    assert "result.json" in res.error


def test_args_round_trip_with_unicode_and_metacharacters(runner, workdir):
    script = _script(workdir, "echo", """
        import sys, json
        argv = sys.argv[sys.argv.index('--') + 1:]
        with open(argv[0], 'r', encoding='utf-8') as f:
            args = json.load(f)
        with open(args['_result_path'], 'w', encoding='utf-8') as f:
            json.dump({'ok': True, 'op_id': args['_op_id'], 'stage': 'echo', 'data': {'echo': args['payload']},
                       'errors': [], 'warnings': []}, f, ensure_ascii=False)
    """)
    payload = {"path": r"C:\tmp dir\ü nï 名.json", "meta": 'a&b|c^d%e "q"'}
    res = runner.run_script(script, {"payload": payload}, op_id="op_echo", deadline_s=60)
    assert res.outcome == "ok", res.error
    assert res.result["data"]["echo"] == payload


def test_deadline_kills_long_script_and_confirms(runner, workdir):
    script = _script(workdir, "sleep", "import time\ntime.sleep(120)\n")
    t0 = time.time()
    res = runner.run_script(script, {}, op_id="op_sleep", deadline_s=2)
    assert res.outcome == "timeout"
    assert res.kill_confirmed is True
    assert time.time() - t0 < 40


def test_script_reported_failure_is_failed_with_message(runner, workdir):
    script = _script(workdir, "softfail", """
        import sys, json
        argv = sys.argv[sys.argv.index('--') + 1:]
        args = json.load(open(argv[0], encoding='utf-8'))
        json.dump({'ok': False, 'op_id': args['_op_id'], 'stage': 'validate', 'data': {},
                   'errors': ['missing part p_x'], 'warnings': []}, open(args['_result_path'], 'w', encoding='utf-8'))
    """)
    res = runner.run_script(script, {}, op_id="op_soft", deadline_s=60)
    assert res.outcome == "failed" and res.exit_code == 0
    assert "missing part p_x" in res.error

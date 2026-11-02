"""平台侧脚本（SKILL.md 上传）的离线单测。

这些测试不连 Nexent、不需要 JWT、不发任何网络请求：平台访问客户端一律用假实现替换，
只验证"幂等判定 / 证据口径 / multipart 编码 / 退出码"这些会在真机上产生后果的逻辑。
真机验证见 `deploy/nexent/README.md` 的复现步骤与 `samples/nexent/live/skills.json`。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

DEPLOY_DIR = Path(__file__).resolve().parents[1] / "deploy" / "nexent"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, DEPLOY_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


platform_client = _load("platform_client")
upload_skills = _load("upload_skills")


class FakeClient:
    """记录调用并按路径回放预设响应；不发网络请求。"""

    def __init__(self, responses: dict[str, tuple[int, object]] | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.responses = responses or {}

    def call(self, path: str, **kwargs):
        self.calls.append((path, kwargs))
        return self.responses.get(path, (200, {"skills": []}))

    def call_multipart(self, path: str, **kwargs):
        self.calls.append((path, kwargs))
        return self.responses.get(path, (201, {"skill_id": 7, "name": "demo-skills", "source": "custom"}))


def _write_skill(tmp_path: Path, name: str = "demo-skills") -> Path:
    root = tmp_path / name
    root.mkdir(parents=True)
    path = root / "SKILL.md"
    path.write_text("---\nname: demo-skills\ndescription: demo\n---\n\n正文\n", encoding="utf-8")
    return path


def _parsed(path: Path, *, content: str = "正文", description: str = "demo", tags: list[str] | None = None):
    return {
        "name": "demo-skills",
        "description": description,
        "content": content,
        "tags": tags or [],
        "file": str(path),
        "file_sha256": upload_skills.sha256_hex(path.read_bytes()),
        "content_sha256": upload_skills.sha256_hex(content),
    }


def test_iter_skill_files_only_matches_skill_md_and_sorts(tmp_path: Path) -> None:
    (tmp_path / "b-skill").mkdir()
    (tmp_path / "b-skill" / "SKILL.md").write_text("---\nname: b\n---\n", encoding="utf-8")
    (tmp_path / "a-skill").mkdir()
    (tmp_path / "a-skill" / "SKILL.md").write_text("---\nname: a\n---\n", encoding="utf-8")
    (tmp_path / "a-skill" / "notes.md").write_text("忽略", encoding="utf-8")
    (tmp_path / "flat.md").write_text("忽略", encoding="utf-8")

    files = upload_skills.iter_skill_files(tmp_path)

    assert [path.parent.name for path in files] == ["a-skill", "b-skill"]


def test_iter_skill_files_missing_directory_is_empty(tmp_path: Path) -> None:
    assert upload_skills.iter_skill_files(tmp_path / "nope") == []


def test_sha256_hex_is_stable_across_str_and_bytes() -> None:
    assert upload_skills.sha256_hex("正文") == upload_skills.sha256_hex("正文".encode("utf-8"))
    assert upload_skills.sha256_hex("正文") != upload_skills.sha256_hex("正文 ")


def test_upload_one_creates_when_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _write_skill(tmp_path)
    monkeypatch.setattr(upload_skills, "parse_skill_file", lambda p: _parsed(p))
    client = FakeClient()

    record = upload_skills.upload_one(client, path, {})

    assert record["action"] == "created"
    assert record["http_status"] == 201
    assert record["platform_skill_id"] == 7
    assert client.calls[0][0] == "/api/skills/upload"
    assert client.calls[0][1]["fields"] == {"skill_name": "demo-skills", "source": "custom"}


def test_upload_one_is_idempotent_when_platform_matches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _write_skill(tmp_path)
    parsed = _parsed(path)
    monkeypatch.setattr(upload_skills, "parse_skill_file", lambda p: parsed)
    existing = {"demo-skills": {"skill_id": 7, "description": "demo", "content": "正文", "tags": []}}
    client = FakeClient()

    record = upload_skills.upload_one(client, path, existing)

    assert record["action"] == "unchanged"
    assert record["http_status"] is None
    assert client.calls == []


def test_upload_one_updates_when_content_drifts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _write_skill(tmp_path)
    parsed = _parsed(path, content="新的正文")
    monkeypatch.setattr(upload_skills, "parse_skill_file", lambda p: parsed)
    existing = {"demo-skills": {"skill_id": 7, "description": "demo", "content": "旧正文", "tags": []}}
    client = FakeClient(responses={"/api/skills/demo-skills/upload": (200, {"skill_id": 7, "source": "custom"})})

    record = upload_skills.upload_one(client, path, existing)

    assert record["action"] == "updated"
    assert record["http_status"] == 200
    assert client.calls[0][0] == "/api/skills/demo-skills/upload"
    assert client.calls[0][1]["method"] == "PUT"


def test_upload_one_marks_failure_and_keeps_body(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _write_skill(tmp_path)
    monkeypatch.setattr(upload_skills, "parse_skill_file", lambda p: _parsed(p))
    client = FakeClient(responses={"/api/skills/upload": (500, "Internal server error")})

    record = upload_skills.upload_one(client, path, {})

    assert record["action"].startswith("failed")
    assert record["http_status"] == 500
    assert "Internal server error" in record["error"]


def test_build_evidence_requires_byte_identical_stored_content(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _write_skill(tmp_path)
    parsed = _parsed(path)
    monkeypatch.setattr(upload_skills, "parse_skill_file", lambda p: parsed)
    client = FakeClient()

    record = upload_skills.upload_one(client, path, {})
    ok = upload_skills.build_evidence([record], {"demo-skills": {"content": "正文\n"}})
    drift = upload_skills.build_evidence([record], {"demo-skills": {"content": "被改过的正文"}})

    assert ok["all_verified"] is True
    assert ok["platform_verification"][0]["stored_content_sha256"] == parsed["content_sha256"]
    assert drift["all_verified"] is False
    assert drift["platform_verification"][0]["content_matches"] is False


def test_build_evidence_without_platform_skill_is_not_verified(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _write_skill(tmp_path)
    monkeypatch.setattr(upload_skills, "parse_skill_file", lambda p: _parsed(p))
    record = upload_skills.upload_one(FakeClient(), path, {})

    evidence = upload_skills.build_evidence([record], {})

    assert evidence["all_verified"] is False
    assert evidence["platform_skill_names"] == []


def test_call_multipart_encodes_file_part_and_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_send(self, path, *, data, content_type, method, timeout):
        captured.update({"path": path, "data": data, "content_type": content_type, "method": method})
        return 201, {"skill_id": 1}

    monkeypatch.setattr(platform_client.PlatformClient, "_send", fake_send)
    client = object.__new__(platform_client.PlatformClient)
    client.base_url = "http://localhost:5010"
    client.token = "test-token"

    status, _ = client.call_multipart(
        "/api/skills/upload",
        fields={"skill_name": "demo-skills"},
        file_field="file",
        filename="SKILL.md",
        content="---\nname: demo-skills\n---\n".encode("utf-8"),
    )

    body = captured["data"]
    assert status == 201
    assert captured["path"] == "/api/skills/upload"
    assert captured["method"] == "POST"
    boundary = str(captured["content_type"]).split("boundary=")[1]
    assert captured["content_type"].startswith("multipart/form-data; boundary=")
    assert f'name="skill_name"'.encode() in body
    assert b'filename="SKILL.md"' in body
    assert b"name: demo-skills" in body
    assert body.rstrip().endswith(f"--{boundary}--".encode())


def test_main_status_exit_code_and_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_skill(tmp_path)
    client = FakeClient(responses={"/api/skills": (200, {"skills": [
        {"name": "demo-skills", "skill_id": 7, "source": "custom", "tags": [], "description": "demo"},
    ]})})
    monkeypatch.setattr(upload_skills, "PlatformClient", lambda: client)
    out = tmp_path / "evidence.json"

    code = upload_skills.main(["upload_skills.py", "status", "--skills-dir", str(tmp_path), "--json", str(out)])

    assert code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["platform_skill_names"] == ["demo-skills"]


def test_main_returns_2_when_no_skill_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(upload_skills, "PlatformClient", FakeClient)

    code = upload_skills.main(["upload_skills.py", "upload", "--skills-dir", str(tmp_path / "empty")])

    assert code == 2


def test_main_returns_1_when_platform_verification_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _write_skill(tmp_path)
    parsed = _parsed(path)
    monkeypatch.setattr(upload_skills, "parse_skill_file", lambda p: parsed)
    # 上传成功，但平台复核时列表里没有该技能 → 视为未验证通过
    client = FakeClient(responses={
        "/api/skills": (200, {"skills": []}),
        "/api/skills/upload": (201, {"skill_id": 7}),
    })
    monkeypatch.setattr(upload_skills, "PlatformClient", lambda: client)

    code = upload_skills.main(["upload_skills.py", "upload", "--skills-dir", str(tmp_path)])

    assert code == 1

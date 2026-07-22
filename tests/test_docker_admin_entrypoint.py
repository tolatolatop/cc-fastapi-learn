import subprocess
from pathlib import Path


ENTRYPOINT = Path(__file__).parents[1] / "docker-admin-entrypoint.sh"


def _fake_python(tmp_path: Path) -> Path:
    executable = tmp_path / "python"
    executable.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$CC_FASTAPI_BASE_URL\"\n"
        "printf '%s\\n' \"${CC_FASTAPI_TOKEN:-}\"\n"
        "printf '%s\\n' \"$*\"\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


def test_docker_admin_entrypoint_connects_to_local_api_and_reuses_server_token(
    tmp_path: Path,
):
    _fake_python(tmp_path)

    result = subprocess.run(
        ["/bin/sh", str(ENTRYPOINT), "status"],
        env={"PATH": str(tmp_path), "API_TOKEN": "server-token"},
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.splitlines() == [
        "http://127.0.0.1:8000",
        "server-token",
        "-m cc_fastapi.cli status",
    ]


def test_docker_admin_entrypoint_preserves_explicit_client_configuration(
    tmp_path: Path,
):
    _fake_python(tmp_path)

    result = subprocess.run(
        ["/bin/sh", str(ENTRYPOINT), "pr", "recent"],
        env={
            "PATH": str(tmp_path),
            "API_TOKEN": "server-token",
            "CC_FASTAPI_BASE_URL": "https://api.example.test",
            "CC_FASTAPI_TOKEN": "client-token",
        },
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.splitlines() == [
        "https://api.example.test",
        "client-token",
        "-m cc_fastapi.cli pr recent",
    ]

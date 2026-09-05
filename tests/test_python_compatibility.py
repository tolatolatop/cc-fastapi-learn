from pathlib import Path
import tomllib


PROJECT_ROOT = Path(__file__).parents[1]


def test_project_metadata_supports_python_312():
    metadata = tomllib.loads(
        (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )

    assert metadata["project"]["requires-python"] == ">=3.12,<4.0"


def test_production_image_uses_python_312_and_installs_project_package():
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert dockerfile.startswith("FROM python:3.12-slim\n")
    assert "python -m pip install --no-cache-dir ." in dockerfile

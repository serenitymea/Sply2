from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TMP_ROOT = PROJECT_ROOT / "tmp"
OUTPUT_ROOT = PROJECT_ROOT / "output"


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def ensure_tmp_root() -> Path:
    return ensure_dir(TMP_ROOT)


def ensure_output_root() -> Path:
    return ensure_dir(OUTPUT_ROOT)

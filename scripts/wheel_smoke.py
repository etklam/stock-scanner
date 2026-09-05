"""Install the built wheel with locked dependencies and run outside the repository."""

import os
import subprocess
import tempfile
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    wheel = next((root / "dist").glob("*.whl"))
    with tempfile.TemporaryDirectory(prefix="qscan wheel 測試 ") as temporary:
        directory = Path(temporary)
        environment = directory / "wheel env"
        subprocess.run(["uv", "venv", "--python", "3.12", str(environment)], check=True)
        binary = environment / ("Scripts" if os.name == "nt" else "bin")
        python = binary / ("python.exe" if os.name == "nt" else "python")
        requirements = directory / "dependencies.txt"
        subprocess.run(
            [
                "uv",
                "export",
                "--locked",
                "--no-dev",
                "--no-emit-project",
                "--output-file",
                str(requirements),
            ],
            cwd=root,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        subprocess.run(
            ["uv", "pip", "install", "--python", str(python), "-r", str(requirements), str(wheel)],
            check=True,
        )
        subprocess.run(
            [str(python), "-m", "qscan.demo", "--data-dir", str(directory / "資料 data")],
            cwd=directory,
            check=True,
        )


if __name__ == "__main__":
    main()

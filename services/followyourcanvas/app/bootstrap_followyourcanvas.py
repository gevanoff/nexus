import runpy
import sys


def _initialize_cuda() -> None:
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.get_device_capability(0)
    except Exception:
        pass


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: bootstrap_followyourcanvas.py SCRIPT [ARGS...]")

    script = sys.argv[1]
    sys.argv = [script, *sys.argv[2:]]
    _initialize_cuda()
    runpy.run_path(script, run_name="__main__")


if __name__ == "__main__":
    main()

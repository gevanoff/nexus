import runpy
import sys


def _patch_distributed_cleanup() -> None:
    try:
        import torch.distributed as dist

        original = dist.destroy_process_group

        def destroy_process_group(*args, **kwargs):
            if not dist.is_available() or not dist.is_initialized():
                return None
            return original(*args, **kwargs)

        dist.destroy_process_group = destroy_process_group
    except Exception:
        pass


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
    _patch_distributed_cleanup()
    _initialize_cuda()
    runpy.run_path(script, run_name="__main__")


if __name__ == "__main__":
    main()

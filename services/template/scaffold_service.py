from __future__ import annotations

import argparse
import re
import shutil
from pathlib import Path


ROUTE_KINDS = ("chat", "embeddings", "images", "tts", "ocr", "video", "music", "json")
TEXT_EXTENSIONS = {".py", ".md", ".txt", ".yml", ".yaml", ".env", ".json"}
TEXT_NAMES = {"Dockerfile", "requirements.txt"}
FILE_RENAMES = {
    "docker-compose.service.yml": "docker-compose.{service_name}.yml",
}


def _service_title(name: str) -> str:
    return " ".join(part.capitalize() for part in name.replace("_", "-").split("-") if part)


def _service_prefix(name: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")


def _validate_service_name(name: str) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,62}", name):
        raise SystemExit("Service name must match [a-z0-9][a-z0-9-]{1,62}")
    return name


def _render_text(text: str, replacements: dict[str, str]) -> str:
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text


def _copy_skeleton(
    *,
    source_root: Path,
    destination_root: Path,
    replacements: dict[str, str],
    dry_run: bool,
) -> list[Path]:
    created: list[Path] = []
    for source_path in source_root.rglob("*"):
        relative = source_path.relative_to(source_root)
        rename_pattern = FILE_RENAMES.get(relative.name)
        if rename_pattern:
            relative = relative.with_name(rename_pattern.format(service_name=replacements["__SERVICE_NAME__"]))
        destination_path = destination_root / relative

        if source_path.is_dir():
            if not dry_run:
                destination_path.mkdir(parents=True, exist_ok=True)
            continue

        created.append(destination_path)
        if dry_run:
            continue

        destination_path.parent.mkdir(parents=True, exist_ok=True)
        if source_path.suffix in TEXT_EXTENSIONS or source_path.name in TEXT_NAMES:
            rendered = _render_text(source_path.read_text(encoding="utf-8"), replacements)
            destination_path.write_text(rendered, encoding="utf-8")
        else:
            shutil.copy2(source_path, destination_path)
    return created


def main() -> int:
    parser = argparse.ArgumentParser(description="Scaffold a Nexus model service from the template.")
    parser.add_argument("--name", required=True, help="Service directory and backend name, e.g. sdxl-refiner")
    parser.add_argument("--route-kind", required=True, choices=ROUTE_KINDS, help="Primary API route exposed by the service")
    parser.add_argument("--port", required=True, type=int, help="Port exposed by the service")
    parser.add_argument("--model-id", default="", help="Default model id returned from /v1/models")
    parser.add_argument("--description", default="", help="Service description used in README and metadata")
    parser.add_argument("--output-root", default="", help="Directory that will contain the generated service directory")
    parser.add_argument("--force", action="store_true", help="Overwrite an existing destination directory")
    parser.add_argument("--dry-run", action="store_true", help="Print the generated files without writing them")
    args = parser.parse_args()

    service_name = _validate_service_name(args.name.strip())
    if args.port <= 0 or args.port > 65535:
        raise SystemExit("Port must be between 1 and 65535")

    template_root = Path(__file__).resolve().parent
    skeleton_root = template_root / "skeleton"
    if not skeleton_root.is_dir():
        raise SystemExit(f"Template skeleton not found: {skeleton_root}")

    output_root = Path(args.output_root).resolve() if args.output_root else template_root.parent
    destination = output_root / service_name

    if destination.exists():
        if not args.force:
            raise SystemExit(f"Destination already exists: {destination}")
        if not args.dry_run:
            shutil.rmtree(destination)

    replacements = {
        "__SERVICE_NAME__": service_name,
        "__SERVICE_TITLE__": _service_title(service_name),
        "__SERVICE_PREFIX__": _service_prefix(service_name),
        "__ROUTE_KIND__": args.route_kind,
        "__PORT__": str(args.port),
        "__MODEL_ID__": args.model_id.strip() or service_name,
        "__SERVICE_DESCRIPTION__": args.description.strip() or f"Nexus {args.route_kind} shim for {service_name}.",
    }

    created = _copy_skeleton(
        source_root=skeleton_root,
        destination_root=destination,
        replacements=replacements,
        dry_run=args.dry_run,
    )

    print(f"{'Would create' if args.dry_run else 'Created'} service template at: {destination}")
    for path in created:
        print(f" - {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

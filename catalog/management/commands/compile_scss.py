from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

import sass


class Command(BaseCommand):
    help = "Compile catalog/static/css/main.scss into catalog/static/css/main.css"

    def add_arguments(self, parser):
        parser.add_argument(
            "--style",
            choices=["nested", "expanded", "compact", "compressed"],
            default="expanded",
            help="Output style for the generated CSS.",
        )

    def handle(self, *args, **options):
        style = options["style"]
        base_dir = Path(settings.BASE_DIR)
        input_path = base_dir / "catalog" / "static" / "css" / "main.scss"
        output_path = base_dir / "catalog" / "static" / "css" / "main.css"

        if not input_path.exists():
            raise CommandError(f"SCSS source not found: {input_path}")

        try:
            css = sass.compile(
                filename=str(input_path),
                include_paths=[str(input_path.parent)],
                output_style=style,
            )
        except sass.CompileError as exc:
            raise CommandError(f"SCSS compilation failed: {exc}") from exc

        output_path.write_text(css, encoding="utf-8")
        self.stdout.write(
            self.style.SUCCESS(f"Compiled {input_path.relative_to(base_dir)} -> {output_path.relative_to(base_dir)}")
        )

"""
Process queued synthesis-route discretization jobs.

Batch uploads enqueue a ``SynthesisParseJob`` per imported record whose free-text
"Synthesis route" should be split into typed steps by the LLM. The in-process
daemon (enabled with ``SYNTHESIS_LLM_WORKER=1``) drains this queue automatically;
this command is the manual/cron entry point and drives the same worker.

Examples::

    python manage.py discretize_synthesis
    python manage.py discretize_synthesis --limit 100
    python manage.py discretize_synthesis --retry-failed
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from catalog.documents import SynthesisParseJob
from catalog.synthesis_worker import process_pending_jobs


class Command(BaseCommand):
    help = "Discretize queued batch synthesis routes via the LLM worker."

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=25,
            help="Maximum number of jobs to process this run (default: 25).",
        )
        parser.add_argument(
            "--retry-failed",
            action="store_true",
            help="Requeue failed/skipped jobs to pending before processing.",
        )

    def handle(self, *args, **options):
        if options["retry_failed"]:
            requeued = SynthesisParseJob.objects(
                status__in=["failed", "skipped"]
            ).update(set__status="pending", set__attempts=0)
            self.stdout.write(f"Requeued {requeued} failed/skipped job(s).")

        pending = SynthesisParseJob.objects(status="pending").count()
        if not pending:
            self.stdout.write("No pending synthesis jobs.")
            return

        counts = process_pending_jobs(limit=int(options["limit"] or 25))
        self.stdout.write(
            self.style.SUCCESS(
                "Processed: "
                f"done={counts.get('done', 0)}, "
                f"skipped={counts.get('skipped', 0)}, "
                f"failed={counts.get('failed', 0)}, "
                f"requeued={counts.get('pending', 0)}."
            )
        )

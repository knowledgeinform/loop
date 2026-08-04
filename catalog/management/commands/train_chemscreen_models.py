"""Queue or immediately run the versioned EFA/DEED training pipeline."""
from __future__ import annotations

from django.core.management.base import BaseCommand

from catalog.model_training import (
    enqueue_model_retraining,
    process_pending_training_jobs,
    train_and_maybe_promote,
)


class Command(BaseCommand):
    help = (
        "Train the LOOP ChemScreen-style EFA/DEED models, record validation "
        "metrics, and promote the candidate only when it improves."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--enqueue",
            action="store_true",
            help="Queue a coalesced background job instead of training now.",
        )
        parser.add_argument(
            "--process-pending",
            action="store_true",
            help="Process one already queued job immediately.",
        )

    def handle(self, *args, **options):
        if options["enqueue"]:
            job = enqueue_model_retraining(
                reason="Manual management command",
                force=True,
            )
            self.stdout.write(
                self.style.SUCCESS(f"Queued ChemScreen retraining job {job.id}.")
            )
            return
        if options["process_pending"]:
            counts = process_pending_training_jobs(limit=1)
            self.stdout.write(self.style.SUCCESS(f"Processed jobs: {counts}"))
            return

        result = train_and_maybe_promote(reason="Manual management command")
        style = self.style.SUCCESS if result.get("status") == "done" else self.style.WARNING
        self.stdout.write(style(str(result)))
